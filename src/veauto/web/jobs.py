"""Asyncio-based job manager for the veauto web API.

The :class:`JobManager` accepts jobs, runs them on a thread-pool
executor (so the event loop is never blocked by CPU/IO-heavy work),
and broadcasts progress / state changes to any subscribed WebSocket
connections.

Concurrency
-----------

* Multiple jobs can run in parallel — each is dispatched to a
  :class:`ThreadPoolExecutor` with ``max_workers`` slots.
* Cancellation is cooperative: each job has a :class:`threading.Event`
  that the worker checks between pipeline stages.

WebSocket protocol
------------------

* Clients subscribe by connecting to ``/ws/jobs/{job_id}``.
* The server sends a ``state`` message immediately on connect, then
  ``progress`` messages whenever progress / stage changes, then a final
  ``state`` message when the job finishes.
* Clients may send ``{"type": "cancel"}`` to cancel the job.
* Clients may send ``{"type": "ping"}`` to keep the connection alive
  (the server replies with ``{"type": "pong"}``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..report import build_report_data
from .schemas import JobOptions, JobRecord

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class JobCancelled(Exception):
    """Raised inside the worker when the job was cancelled."""


class _Subscriber:
    """An asyncio.Queue owned by a single WebSocket connection."""

    __slots__ = ("queue",)

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()


@dataclass
class _Job:
    record: JobRecord
    input_path: Path
    output_dir: Path
    cancel_event: threading.Event = field(default_factory=threading.Event)
    subscribers: list[_Subscriber] = field(default_factory=list)
    loop: Any = None  # asyncio.AbstractEventLoop
    future: Any = None  # concurrent.futures.Future


class JobManager:
    """Dispatches jobs to a thread pool and broadcasts progress.

    Parameters
    ----------
    output_root:
        Directory under which per-job subdirectories are created.
    max_workers:
        Maximum number of jobs that may run in parallel.
    on_job_done:
        Optional callback invoked after a job terminates.
    """

    def __init__(
        self,
        output_root: Path,
        *,
        max_workers: int = 2,
        on_job_done: Callable[[JobRecord], None] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="veauto-job"
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._on_job_done = on_job_done

    def submit(
        self,
        *,
        input_path: Path,
        input_name: str,
        input_size: int,
        options: JobOptions,
        loop,
    ) -> JobRecord:
        """Queue a new job and return its record."""
        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(
            id=job_id,
            status="queued",
            created_at=datetime.now(tz=UTC),
            input_name=input_name,
            input_size=input_size,
            options=options,
        )
        job_dir = self.output_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = _Job(
            record=record,
            input_path=Path(input_path),
            output_dir=job_dir,
            loop=loop,
        )
        with self._lock:
            self._jobs[job_id] = job
        job.future = self._executor.submit(self._run_job, job)
        logger.info("Job %s queued (%s, %d bytes)", job_id, input_name, input_size)
        # Return a deep copy so the caller's snapshot is decoupled from
        # any mutations the worker applies in the background.
        return record.model_copy(deep=True)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        logger.info("Job %s cancel requested", job_id)
        return True

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return job.record.model_copy(deep=True)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.record.created_at, reverse=True)
        return [j.record.model_copy(deep=True) for j in jobs]

    def subscribe(self, job_id: str) -> _Subscriber | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        sub = _Subscriber()
        job.subscribers.append(sub)
        return sub

    def unsubscribe(self, job_id: str, sub: _Subscriber) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return
        try:
            job.subscribers.remove(sub)
        except ValueError:
            pass

    def job_dir(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return job.output_dir

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            j.cancel_event.set()
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _check_cancel(self, job: _Job) -> None:
        if job.cancel_event.is_set():
            raise JobCancelled()

    def _run_job(self, job: _Job) -> None:
        """Worker body. Runs in a thread-pool thread."""
        record = job.record
        try:
            self._check_cancel(job)
            self._set_status(job, "running", stage="probing", progress=0.05,
                             message="Probing media…")
            cfg = record.options.to_pipeline_config()

            def _progress(stage: str, fraction: float, message: str = "") -> None:
                if job.cancel_event.is_set():
                    raise JobCancelled()
                self._set_status(
                    job,
                    "running",
                    stage=stage,
                    progress=min(0.95, max(0.05, fraction)),
                    message=message,
                )

            result = self._run_pipeline_with_progress(
                job.input_path, cfg, _progress, cancel_event=job.cancel_event
            )
            self._check_cancel(job)

            fcpxml_name = "output.fcpxml"
            (job.output_dir / fcpxml_name).write_text(result.fcpxml, encoding="utf-8")
            self._set_status(job, "running", stage="writing", progress=0.97,
                             message="Writing report…")
            report_data = build_report_data(result)
            (job.output_dir / "report.json").write_text(
                json.dumps(report_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (job.output_dir / "report.md").write_text(
                self._render_markdown(report_data), encoding="utf-8"
            )

            with self._lock:
                record.input_duration = result.media.duration
                record.num_silences = len(result.removed)
                record.num_cuts = len(result.cuts)
                record.num_subtitles = len(result.subtitles)
                record.kept_duration = result.kept_duration
                record.removed_duration = result.removed_duration
                record.fcpxml_name = fcpxml_name
                record.report_md_name = "report.md"
                record.report_json_name = "report.json"

            self._set_status(
                job, "completed", stage="done", progress=1.0,
                message=f"Done in {result.kept_duration:.1f}s kept "
                        f"({result.removed_duration:.1f}s removed).",
            )
            logger.info("Job %s completed", record.id)
        except JobCancelled:
            self._set_status(job, "cancelled", stage="cancelled", progress=0.0,
                             message="Cancelled by user.")
            logger.info("Job %s cancelled", record.id)
        except Exception as exc:  # noqa: BLE001
            self._set_status(
                job, "failed", stage="failed", progress=0.0,
                message="", error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception("Job %s failed", record.id)
        finally:
            if self._on_job_done is not None:
                try:
                    self._on_job_done(record.model_copy(deep=True))
                except Exception:  # noqa: BLE001
                    logger.exception("on_job_done callback failed")

    @staticmethod
    def _render_markdown(data: dict) -> str:
        from ._report_md import render_markdown_report
        return render_markdown_report(data)

    # ------------------------------------------------------------------
    # Pipeline with progress hooks
    # ------------------------------------------------------------------

    def _run_pipeline_with_progress(
        self,
        input_path: Path,
        cfg,
        progress: Callable[[str, float, str], None],
        *,
        cancel_event: threading.Event,
    ):
        """Run ``run_pipeline`` while emitting progress events.

        The current :func:`run_pipeline` doesn't accept a progress
        callback, so we instrument it by monkey-patching the helpers
        within this thread. This is intentional: it keeps the public
        library API unchanged and confines progress reporting to the
        web layer.
        """
        from .. import pipeline as pl

        # `pipeline._transcribe` is the module-level symbol run_pipeline
        # actually calls; `pipeline.transcribe` is not exported.
        orig_probe = pl.probe_media_info
        orig_detect = pl.detect_silence
        orig_extract = pl.extract_audio
        orig_transcribe = pl._transcribe
        orig_segments = pl.words_to_subtitle_segments
        orig_fcpxml = pl.build_fcpxml
        orig_cuts = pl.build_cut_segments

        def _check() -> None:
            if cancel_event.is_set():
                raise JobCancelled()

        def _probe(p):
            _check()
            res = orig_probe(p)
            progress("probing", 0.10, f"Probed {res.duration:.1f}s media")
            return res

        def _detect(p, *, noise_db, min_silence):
            _check()
            progress("detecting_silence", 0.15, "Detecting silences…")
            return orig_detect(p, noise_db=noise_db, min_silence=min_silence)

        def _extract(p):
            _check()
            progress("extracting_audio", 0.30, "Extracting audio…")
            return orig_extract(p)

        def _transcribe(audio_path, sub_cfg):
            _check()
            progress("transcribing", 0.40,
                     f"Transcribing (model={sub_cfg.model})…")
            words = orig_transcribe(audio_path, sub_cfg)
            progress("transcribing", 0.75, f"Transcribed {len(words)} words")
            return words

        def _cuts(total, silences, *, margin):
            _check()
            progress("building_cuts", 0.20, "Building cut segments…")
            return orig_cuts(total, silences, margin=margin)

        def _words_to_subs(words, **kw):
            _check()
            progress("grouping_subtitles", 0.80, "Grouping subtitle lines…")
            return orig_segments(words, **kw)

        def _build_fcpxml(media, cuts, *, subtitles=None, subtitle_style=None,
                          project_name="Auto Edit", event_name="veauto", **kwargs):
            _check()
            progress("rendering_fcpxml", 0.90, "Rendering FCPXML…")
            return orig_fcpxml(
                media, cuts,
                subtitles=subtitles,
                subtitle_style=subtitle_style,
                project_name=project_name,
                event_name=event_name,
            )

        pl.probe_media_info = _probe
        pl.detect_silence = _detect
        pl.extract_audio = _extract
        pl._transcribe = _transcribe
        pl.words_to_subtitle_segments = _words_to_subs
        pl.build_fcpxml = _build_fcpxml
        pl.build_cut_segments = _cuts  # type: ignore[attr-defined]
        try:
            return pl.run_pipeline(input_path, cfg)
        finally:
            pl.probe_media_info = orig_probe
            pl.detect_silence = orig_detect
            pl.extract_audio = orig_extract
            pl._transcribe = orig_transcribe
            pl.words_to_subtitle_segments = orig_segments
            pl.build_fcpxml = orig_fcpxml
            pl.build_cut_segments = orig_cuts  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Status updates → record + broadcast
    # ------------------------------------------------------------------

    def _set_status(
        self,
        job: _Job,
        status: JobStatus,
        *,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        rec = job.record
        now = datetime.now(tz=UTC)
        with self._lock:
            rec.status = status
            if status == "running" and rec.started_at is None:
                rec.started_at = now
            if status in ("completed", "failed", "cancelled"):
                rec.finished_at = now
            if stage is not None:
                rec.stage = stage
            if progress is not None:
                rec.progress = progress
            if message is not None:
                rec.message = message
            if error is not None:
                rec.error = error
            snapshot = rec.model_copy(deep=True)
            subs = list(job.subscribers)
            loop = job.loop
        if loop is None:
            return
        for sub in subs:
            loop.call_soon_threadsafe(
                sub.queue.put_nowait,
                {"type": "state", "record": snapshot.model_dump(mode="json")},
            )
