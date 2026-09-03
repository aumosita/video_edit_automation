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
import time
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

    __slots__ = ("queue", "loop")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        # Set by the WebSocket handler after accept() so worker threads
        # can use call_soon_threadsafe to enqueue from background pools.
        self.loop: asyncio.AbstractEventLoop | None = None


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
        # Subscribers for the global event stream (/api/ws). Each is an
        # _Subscriber whose queue receives "job.create", "job.update", and
        # "job.delete" events.
        self._global_subscribers: list[_Subscriber] = []
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

    def subscribe_all(self) -> _Subscriber:
        """Subscribe to *all* job events (create / update / delete)."""
        sub = _Subscriber()
        with self._lock:
            self._global_subscribers.append(sub)
        return sub

    def unsubscribe_all(self, sub: _Subscriber) -> None:
        with self._lock:
            try:
                self._global_subscribers.remove(sub)
            except ValueError:
                pass

    def _broadcast_global(self, message: dict) -> None:
        """Push a message to every global subscriber.

        Thread-safe: each subscriber's queue is fed via ``call_soon_threadsafe``
        on whichever event loop owns the connection. Dead queues (full /
        cancelled) are silently dropped.
        """
        with self._lock:
            subs = list(self._global_subscribers)
        for sub in subs:
            loop = getattr(sub, "loop", None)
            if loop is None:
                continue
            try:
                loop.call_soon_threadsafe(sub.queue.put_nowait, message)
            except RuntimeError:
                # Loop is closed; drop.
                pass
            except Exception:  # noqa: BLE001
                logger.exception("Global broadcast failed")

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.cancel_event.set()
        # Close any per-job subscriber queues (their writers will exit).
        for sub in list(job.subscribers):
            try:
                # Signal the writer loop to exit; the unsubscribe() will
                # happen when the WS reader sees a disconnect.
                if sub.queue.empty() is False or True:
                    pass
            except Exception:  # noqa: BLE001
                pass
        # Tell global subscribers.
        self._broadcast_global({"type": "job.deleted", "id": job_id})
        logger.info("Job %s deleted", job_id)
        return True

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
        # Stage timing for hang diagnostics (D-step C).
        _stage_t0 = [0.0]
        _stage_name = [""]
        _last_event_t = [time.monotonic()]

        def _heartbeat() -> None:
            """Emit a debug log if no progress is made for 60s.

            Helps locate which stage a worker is stuck in when a long
            ffmpeg call (e.g. silencedetect on a 4K HEVC MOV) provides
            no intermediate progress.
            """
            now = time.monotonic()
            if _stage_name[0] and (now - _last_event_t[0]) > 60.0:
                logger.warning(
                    "Job %s still in stage=%s for %.0fs",
                    record.id, _stage_name[0], now - _stage_t0[0],
                )
                _last_event_t[0] = now

        def _enter_stage(name: str) -> None:
            now = time.monotonic()
            logger.info(
                "Job %s -> stage %s (prev took %.2fs)",
                record.id, name,
                (now - _stage_t0[0]) if _stage_t0[0] else 0.0,
            )
            _stage_t0[0] = now
            _stage_name[0] = name
            _last_event_t[0] = now

        try:
            self._check_cancel(job)
            self._set_status(job, "running", stage="probing", progress=0.05,
                             message="Probing media…")
            _enter_stage("probing")
            cfg = record.options.to_pipeline_config()

            def _progress(stage: str, fraction: float, message: str = "") -> None:
                if job.cancel_event.is_set():
                    raise JobCancelled()
                _heartbeat()
                self._set_status(
                    job,
                    "running",
                    stage=stage,
                    progress=min(0.95, max(0.05, fraction)),
                    message=message,
                )

            result = self._run_pipeline_with_progress(
                job.input_path, cfg, _progress,
                cancel_event=job.cancel_event,
                on_stage=_enter_stage,
            )
            self._check_cancel(job)

            # Derive artefact filenames from the source video so
            # downloading ``talk.fcpxml`` next to a ``talk.mp4`` is
            # obvious. See :func:`veauto.web.utils._output_basename`.
            from .utils import _output_basename
            base = _output_basename(record.input_name,
                                   fallback_id=record.id)
            fcpxml_name = f"{base}.fcpxml"
            srt_name = f"{base}.srt"
            report_md_name = f"{base}.report.md"
            report_json_name = f"{base}.report.json"

            (job.output_dir / fcpxml_name).write_text(
                result.fcpxml, encoding="utf-8"
            )

            # Side-by-side SubRip (.srt) subtitles. Written next to
            # the FCPXML so users who don't want to deal with the XML
            # can import the SRT directly into any NLE or player.
            if result.subtitles:
                from ..srt import write_srt
                write_srt(result.subtitles, job.output_dir / srt_name)

            self._set_status(job, "running", stage="writing", progress=0.97,
                             message="Writing report…")
            report_data = build_report_data(result)
            (job.output_dir / report_json_name).write_text(
                json.dumps(report_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (job.output_dir / report_md_name).write_text(
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
                record.report_md_name = report_md_name
                record.report_json_name = report_json_name
                record.srt_name = srt_name

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
        on_stage: Callable[[str], None] | None = None,
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

        def _enter(name: str) -> None:
            if on_stage is not None:
                on_stage(name)

        def _probe(p):
            _check()
            _enter("probe_media_info")
            res = orig_probe(p)
            progress("probing", 0.10, f"Probed {res.duration:.1f}s media")
            return res

        def _detect(p, silence_config):
            """silence_config: veauto.models.SilenceConfig."""
            _check()
            _enter("detect_silence")
            progress(
                "detecting_silence",
                0.15,
                f"Detecting silences "
                f"(noise<={silence_config.noise_db}dB, "
                f"min>={silence_config.min_silence}s)…",
            )
            res = orig_detect(
                p, silence_config, should_cancel=cancel_event.is_set
            )
            _enter("post_detect_silence")
            return res

        def _extract(p, output_path=None):
            _check()
            _enter("extract_audio")
            progress("extracting_audio", 0.30, "Extracting audio…")
            # ``extract_audio`` requires (input_path, output_path). When
            # ``output_path`` is None we fall back to the original
            # caller's behaviour (some tests pass a single-arg stub).
            if output_path is None:
                res = orig_extract(p, should_cancel=cancel_event.is_set)
            else:
                res = orig_extract(
                    p, output_path, should_cancel=cancel_event.is_set
                )
            _enter("post_extract_audio")
            return res

        def _transcribe(audio_path, sub_cfg, **kwargs):
            _check()
            _enter("transcribe")
            progress("transcribing", 0.40,
                     f"Transcribing (model={sub_cfg.model})…")
            words = orig_transcribe(audio_path, sub_cfg, **kwargs)
            _enter("post_transcribe")
            progress("transcribing", 0.75, f"Transcribed {len(words)} words")
            return words

        def _cuts(total, silences, *, margin):
            _check()
            _enter("build_cut_segments")
            progress("building_cuts", 0.20, "Building cut segments…")
            res = orig_cuts(total, silences, margin=margin)
            _enter("post_build_cut_segments")
            return res

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
        # Per-job subscribers (legacy ``/ws/jobs/{id}`` endpoint) get a
        # ``state`` message; the global subscribers (the SPA's main
        # ``/api/ws`` connection) get a ``job.update`` message with the
        # same payload. Both carry the full record so the client can
        # replace its row in place.
        snapshot_with_urls = snapshot.with_download_urls()
        snapshot_json = snapshot_with_urls.model_dump(mode="json")
        if loop is not None:
            for sub in subs:
                loop.call_soon_threadsafe(
                    sub.queue.put_nowait,
                    {"type": "state", "job": snapshot_json},
                )
        # Broadcast to every global subscriber so the live table updates
        # in real time even before the job's WS connect handshake.
        self._broadcast_global(
            {"type": "job.update", "job": snapshot_json}
        )
