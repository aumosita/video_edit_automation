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
import shutil
import subprocess as _subprocess
import threading
import time
import traceback as _tb
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..report import build_report_data
from .schemas import JobOptions, JobRecord, StageState

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class JobCancelled(Exception):
    """Raised inside the worker when the job was cancelled."""


# Per-stage weights used by ``_build_stages`` for the live progress UI.
# These are the *raw* weights for the default (both stages enabled)
# case; when the user disables one of the stages the remaining
# weights are renormalised so they still sum to 1.0.
_DEFAULT_STAGE_WEIGHTS: dict[str, float] = {
    "probing": 0.05,
    "detecting_silence": 0.15,
    "building_cuts": 0.05,
    "extracting_audio": 0.05,
    "transcribing": 0.50,
    "grouping_subtitles": 0.05,
    "rendering_fcpxml": 0.05,
    "writing": 0.10,
}
_DEFAULT_STAGE_LABELS: dict[str, str] = {
    "probing": "Probing media",
    "detecting_silence": "Detecting silence",
    "building_cuts": "Building cuts",
    "extracting_audio": "Extracting audio",
    "transcribing": "Transcribing",
    "grouping_subtitles": "Grouping subtitles",
    "rendering_fcpxml": "Rendering FCPXML",
    "writing": "Writing outputs",
}


def _build_stages(opts: JobOptions) -> list[StageState]:
    """Build the per-stage progress table for one job.

    Stages that don't apply to the requested options are filtered out
    (transcribe-related stages when ``no_subtitles`` or
    ``subtitle_target="none"``; silence / cuts when ``no_silence``).
    The remaining stages' weights are renormalised so the overall
    bar still tops out at 100% when every stage is done.

    The function reads both the legacy ``no_subtitles`` bool and the
    new ``subtitle_target`` 4-way control — whichever is in effect,
    the transcribe chain (``extracting_audio`` / ``transcribing`` /
    ``grouping_subtitles``) is dropped iff the resolved subtitle
    config has no STT. ``subtitle_target`` takes precedence when
    both are set (mirrors :meth:`JobOptions.to_pipeline_config`).
    """
    if opts.subtitle_target is not None:
        stt_enabled = opts.subtitle_target in ("both", "srt", "fcpxml")
    else:
        stt_enabled = not opts.no_subtitles
    enabled: list[str] = []
    enabled.append("probing")
    if not opts.no_silence:
        enabled.append("detecting_silence")
        enabled.append("building_cuts")
    if stt_enabled:
        enabled.append("extracting_audio")
        enabled.append("transcribing")
        enabled.append("grouping_subtitles")
    enabled.append("rendering_fcpxml")
    enabled.append("writing")
    raw = [_DEFAULT_STAGE_WEIGHTS[n] for n in enabled]
    total = sum(raw) or 1.0
    return [
        StageState(
            name=n,
            label=_DEFAULT_STAGE_LABELS[n],
            weight=w / total,
            status="pending",
            progress=0.0,
        )
        for n, w in zip(enabled, raw)
    ]


def _overall_progress(stages: list[StageState]) -> float:
    """Sum each stage's weight × (1.0 if done, 0.0 if pending /
    skipped, ``progress`` if active) into a 0..1 overall value.

    Used by ``_set_status`` as the new ``JobRecord.progress`` so the
    legacy single-bar clients keep working alongside the new
    per-stage UI.
    """
    if not stages:
        return 0.0
    total = 0.0
    for s in stages:
        if s.status == "done":
            total += s.weight * 1.0
        elif s.status == "active":
            total += s.weight * s.progress
    # Clamp into the open interval (0, 1) to match the pre-existing
    # ``min(0.95, max(0.05, ...))`` behaviour that the progress bar
    # used to apply at the call site.
    return min(0.99, max(0.0, total))


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
    # Child processes (ffmpeg) spawned while this job runs, so a cancel
    # can terminate them instead of waiting for a long call to return.
    procs: list = field(default_factory=list)
    # Set by the worker when it returns, used by the watchdog thread.
    done: threading.Event = field(default_factory=threading.Event)
    # Wall-clock start of execution, for the hard timeout watchdog.
    run_started: float = 0.0


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
        job_timeout: float | None = 6 * 60 * 60,
        watchdog_interval: float = 2.0,
    ) -> None:
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        # On-disk index of every job record so the job table survives a
        # server restart / upgrade. Artifacts (fcpxml / report / srt)
        # already live under ``output_root/<job_id>/``; this file only
        # stores the metadata needed to list and re-download them.
        self._index_path = self.output_root / "jobs.json"
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
        # Hard ceiling (seconds) on a single job's execution. None = no
        # limit. Prevents a wedged ffmpeg/whisper call from pinning a
        # worker slot (and several CPU cores) forever.
        self._job_timeout = job_timeout
        self._watchdog_interval = watchdog_interval
        # Thread-local so concurrently running jobs register their own
        # subprocesses (the Popen patch below is process-wide).
        self._local = threading.local()
        self._load_index()

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
        self._persist()
        # Watchdog enforces cancellation and the hard timeout even when
        # the worker is blocked inside a long ffmpeg/whisper call.
        threading.Thread(
            target=self._watchdog, args=(job,), daemon=True,
            name=f"veauto-watchdog-{job_id}",
        ).start()
        job.future = self._executor.submit(self._run_job, job)
        logger.info("Job %s queued (%s, %d bytes)", job_id, input_name, input_size)
        # Return a deep copy so the caller's snapshot is decoupled from
        # any mutations the worker applies in the background.
        return record.model_copy(deep=True)

    def cancel(self, job_id: str) -> bool:
        """Request cancellation of a job, keeping its record.

        Cancellation is cooperative (the worker checks ``cancel_event``
        between stages) *plus* forcible: any child process (ffmpeg)
        currently spawned by this job is terminated immediately, so a
        cancel doesn't have to wait for a long call to return.
        """
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        self._kill_procs(job)
        logger.info("Job %s cancel requested", job_id)
        return True

    # ------------------------------------------------------------------
    # Child-process tracking / forcible stop
    # ------------------------------------------------------------------

    def _kill_procs(self, job: _Job) -> None:
        """Terminate (then kill) every child process this job spawned."""
        with self._lock:
            procs = list(job.procs)
        for proc in procs:
            if proc.poll() is not None:
                continue
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + 3.0
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _track_subprocess(self, job: _Job):
        """Context manager recording every ``subprocess.Popen`` this
        thread (i.e. this job) creates into ``job.procs``.

        The pipeline calls ffmpeg through ``subprocess.run``, which
        internally uses ``Popen``; patching that global gives us the
        handles needed to interrupt a long-running encode.
        """
        manager = self
        orig_popen = _subprocess.Popen

        class _TrackedPopen(orig_popen):  # type: ignore[misc, valid-type]
            def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
                super().__init__(*args, **kwargs)
                with manager._lock:
                    job.procs.append(self)

        manager._local.job = job

        class _Ctx:
            def __enter__(self):
                _subprocess.Popen = _TrackedPopen
                return self

            def __exit__(self, *exc):
                _subprocess.Popen = orig_popen
                manager._local.job = None
                return False

        return _Ctx()

    def _watchdog(self, job: _Job) -> None:
        """Enforce cancellation and the hard timeout for one job.

        A worker blocked inside ffmpeg or faster-whisper never reaches
        the cooperative cancel checkpoints, so the watchdog polls the
        cancel event and kills the offending child processes. It exits
        once the worker returns (``job.done``).
        """
        job.run_started = time.monotonic()
        timed_out = False
        while not job.done.wait(self._watchdog_interval):
            if job.cancel_event.is_set():
                self._kill_procs(job)
                continue
            if self._job_timeout is not None:
                elapsed = time.monotonic() - job.run_started
                if elapsed > self._job_timeout:
                    if not timed_out:
                        timed_out = True
                        logger.error(
                            "Job %s exceeded %.0fs timeout — forcing stop",
                            job.record.id, self._job_timeout,
                        )
                        job.cancel_event.set()
                        self._kill_procs(job)
                    # Give the worker a moment to unwind, then report.
                    if job.done.wait(30):
                        break
                    self._set_status(
                        job,
                        "failed",
                        stage="timeout",
                        error=f"Timed out after {self._job_timeout / 3600:.1f}h",
                        error_kind="timeout",
                        error_stage="timeout",
                    )
                    break
        # One final sweep in case a process was spawned right at the end.
        if job.cancel_event.is_set():
            self._kill_procs(job)

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
        # Stop any ffmpeg child right away; otherwise the worker thread
        # keeps burning CPU on a job nobody can see any more.
        self._kill_procs(job)
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
        self._persist()
        logger.info("Job %s deleted", job_id)
        return True

    def clear(self) -> int:
        """Delete every job (records + artifacts). Returns the count."""
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            job.cancel_event.set()
            self._kill_procs(job)
            shutil.rmtree(job.output_dir, ignore_errors=True)
        self._persist()
        self._broadcast_global({"type": "jobs.cleared"})
        logger.info("Cleared %d job(s)", len(jobs))
        return len(jobs)

    # ------------------------------------------------------------------
    # Persistence (job index on disk)
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write the job index to ``output_root/jobs.json``.

        Failures are logged and swallowed: persistence is a convenience,
        and a read-only or full disk must never break a running job.
        """
        with self._lock:
            records = [j.record for j in self._jobs.values()]
            payload = [
                r.model_dump(mode="json") for r in records
            ]
        try:
            # Unique temp name: several threads (worker status updates,
            # watchdog, submit) persist concurrently and a shared name
            # races — one replace() would delete the other's temp file.
            tmp = self._index_path.with_name(
                f"{self._index_path.name}.{uuid.uuid4().hex}.tmp"
            )
            tmp.write_text(
                json.dumps({"version": 1, "jobs": payload}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._index_path)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist job index")

    def _load_index(self) -> None:
        """Restore the job table from ``output_root/jobs.json``.

        Jobs that were ``queued`` or ``running`` when the server last
        stopped can no longer make progress, so they are shown as
        ``cancelled`` ("interrupted by restart") instead of hanging
        forever in the UI. A missing or corrupt index just means an
        empty job table.
        """
        if not self._index_path.exists():
            return
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            entries = raw.get("jobs", []) if isinstance(raw, dict) else raw
        except Exception:  # noqa: BLE001
            logger.exception("Could not read job index; starting empty")
            return

        restored = 0
        for entry in entries:
            try:
                record = JobRecord.model_validate(entry)
            except Exception:  # noqa: BLE001
                logger.warning("Skipping unparsable job entry in index")
                continue
            if record.status in ("queued", "running"):
                record.status = "cancelled"
                record.error_kind = "cancelled"  # type: ignore[assignment]
                record.error = "Interrupted by server restart"
                record.message = "Interrupted by server restart"
                record.stage = "interrupted"
            job_dir = self.output_root / record.id
            input_path = (
                job_dir / record.input_name
                if (job_dir / record.input_name).exists()
                else job_dir
            )
            self._jobs[record.id] = _Job(
                record=record,
                input_path=input_path,
                output_dir=job_dir,
            )
            restored += 1
        if restored:
            logger.info("Restored %d job(s) from index", restored)

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
    # Per-stage progress helpers
    # ------------------------------------------------------------------

    def _update_stage(
        self,
        stages: list[StageState],
        name: str,
        *,
        active: bool,
        fraction: float,
        skipped: bool = False,
    ) -> None:
        """Mutate ``stages`` to reflect a new state for ``name``.

        When ``active=True``, any previously active stage is closed
        out (``status="done"``, ``progress=1.0``) so the stacked bar
        doesn't show two active segments at once. The new active
        stage's within-stage ``progress`` is set to ``fraction``,
        clamped to ``[0, 1]``.

        ``skipped=True`` is used for stages that never ran (e.g. no
        subtitles to group). It takes precedence over ``active``.
        """
        fraction = max(0.0, min(1.0, float(fraction)))
        if active and not skipped:
            # Close out any previously active stage so the bar never
            # shows two active segments at once. This must run
            # *before* the name lookup below in case the new active
            # stage happens to be the same one (no-op re-emit).
            for s in stages:
                if s.status == "active" and s.name != name:
                    s.status = "done"
                    s.progress = 1.0
        for s in stages:
            if s.name == name:
                if skipped:
                    s.status = "skipped"
                    s.progress = 0.0
                elif active:
                    s.status = "active"
                    s.progress = fraction
                return
        # Stage name not in the table — likely a hook reporting a
        # stage that was filtered out by options. Silently ignore so
        # a downstream ``_progress`` call doesn't crash the worker.

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

        # Record every ffmpeg child spawned below so a cancel can kill
        # it instead of waiting for the call to return.
        tracker = self._track_subprocess(job)
        tracker.__enter__()
        try:
            self._check_cancel(job)
            cfg = record.options.to_pipeline_config()
            # Initialise the per-stage progress table based on the job's
            # options. ``no_subtitles`` skips the audio / transcribe /
            # grouping stages; ``no_silence`` skips the silence /
            # cuts stages. The remaining stages keep their normal
            # weights and their sum is renormalised to 1.0.
            stages = _build_stages(record.options)
            with self._lock:
                record.stages = stages
            # First stage is "probing" — mark it active from the start
            # so the stacked bar shows motion immediately.
            self._update_stage(stages, "probing", active=True, fraction=0.0)
            overall = _overall_progress(stages)
            self._set_status(
                job, "running", stage="probing", progress=overall,
                message="Probing media…",
            )
            _enter_stage("probing")

            def _progress(stage: str, fraction: float, message: str = "") -> None:
                if job.cancel_event.is_set():
                    raise JobCancelled()
                _heartbeat()
                # Mark the previously active stage as done (if any) and
                # make ``stage`` the new active one. ``fraction`` is the
                # *within-stage* 0..1 value coming from the
                # ``_run_pipeline_with_progress`` hooks.
                self._update_stage(
                    stages, stage, active=True, fraction=fraction,
                )
                overall = _overall_progress(stages)
                self._set_status(
                    job,
                    "running",
                    stage=stage,
                    progress=overall,
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

            # Mark the post-pipeline "writing" stage active and update
            # the stacked bar / overall progress.
            self._update_stage(stages, "writing", active=True, fraction=0.0)
            self._set_status(
                job, "running", stage="writing",
                progress=_overall_progress(stages),
                message="Writing report…",
            )
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

            # If no subtitles were generated the grouping stage has
            # nothing to do — mark it skipped so the stacked bar
            # doesn't leave an empty segment that the user mistakes
            # for a not-yet-started stage.
            if not result.subtitles:
                self._update_stage(
                    stages, "grouping_subtitles", active=False, fraction=0.0,
                    skipped=True,
                )
            # Final flush: every remaining active stage completes at
            # 1.0 so the bar reads as a clean 100%.
            for s in stages:
                if s.status == "active":
                    s.status = "done"
                    s.progress = 1.0
            with self._lock:
                record.stages = list(stages)
            self._set_status(
                job, "completed", stage="done", progress=1.0,
                message=f"Done in {result.kept_duration:.1f}s kept "
                        f"({result.removed_duration:.1f}s removed).",
            )
            logger.info("Job %s completed", record.id)
        except JobCancelled:
            self._set_status(
                job, "cancelled",
                stage="cancelled", progress=0.0,
                message="Cancelled by user.",
                error="Cancelled by user.",
                error_kind="cancelled",
                error_stage=_stage_name[0] or None,
            )
            logger.info("Job %s cancelled", record.id)
        except Exception as exc:  # noqa: BLE001
            tb_text = _tb.format_exc()
            summary = f"{type(exc).__name__}: {exc}"
            # Persist a diagnostic log next to any output artefacts so
            # the user can download it from the web UI. We keep the
            # last-known stage and a wall-clock timestamp so the file
            # alone is enough to reconstruct *when* the worker died.
            log_path = self._write_error_log(
                record.id, job.output_dir,
                stage=_stage_name[0] or "",
                error_kind="exception",
                summary=summary,
                traceback_text=tb_text,
            )
            with self._lock:
                if log_path is not None:
                    record.error_log_name = log_path.name
            self._set_status(
                job, "failed",
                stage="failed", progress=0.0,
                message=summary,
                error=summary,
                error_kind="exception",
                error_stage=_stage_name[0] or None,
                error_traceback=tb_text,
            )
            logger.exception("Job %s failed", record.id)
        finally:
            # Release the watchdog and drop process handles.
            self._kill_procs(job)
            with self._lock:
                job.procs.clear()
            job.done.set()
            tracker.__exit__(None, None, None)
            if self._on_job_done is not None:
                try:
                    self._on_job_done(record.model_copy(deep=True))
                except Exception:  # noqa: BLE001
                    logger.exception("on_job_done callback failed")

    @staticmethod
    def _render_markdown(data: dict) -> str:
        from ._report_md import render_markdown_report
        return render_markdown_report(data)

    @staticmethod
    def _write_error_log(
        job_id: str,
        output_dir: Path,
        *,
        stage: str,
        error_kind: str,
        summary: str,
        traceback_text: str,
    ) -> Path | None:
        """Persist a diagnostic log to ``<output_dir>/error.log``.

        Returns the file path, or ``None`` if writing failed. The
        caller wires the filename into :class:`JobRecord` so the web
        UI can expose it as a download.
        """
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            log_path = output_dir / "error.log"
            payload = (
                f"# veauto job {job_id} failed\n"
                f"# timestamp: {datetime.now(tz=UTC).isoformat()}\n"
                f"# stage: {stage or '(unknown)'}\n"
                f"# kind: {error_kind}\n"
                f"# error: {summary}\n"
                f"\n--- traceback ---\n"
                f"{traceback_text}\n"
            )
            log_path.write_text(payload, encoding="utf-8")
            return log_path
        except OSError as exc:  # pragma: no cover - filesystem errors
            logger.warning(
                "Could not write error.log for job %s: %s", job_id, exc
            )
            return None

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

        def _cuts(total, silences, *, margin, min_keep_seconds=0.0):
            # ``build_cut_segments`` gained an optional ``min_keep_seconds``
            # kwarg in the silence-stage overhaul; the progress hook here
            # must forward it, otherwise ``pipeline.run_pipeline`` would
            # raise TypeError when the web worker tries to compile cut
            # segments. The default of 0 keeps the legacy behaviour for
            # any caller that doesn't pass the new flag.
            _check()
            _enter("build_cut_segments")
            progress("building_cuts", 0.20, "Building cut segments…")
            res = orig_cuts(total, silences, margin=margin,
                            min_keep_seconds=min_keep_seconds)
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
        error_kind: str | None = None,
        error_stage: str | None = None,
        error_traceback: str | None = None,
    ) -> None:
        rec = job.record
        now = datetime.now(tz=UTC)
        with self._lock:
            # A job whose record was cancelled/deleted must not resurrect
            # itself in the UI: the worker may still be unwinding from a
            # long ffmpeg call and would otherwise emit a ``job.update``
            # for a row the client has already removed (the SPA upserts
            # unknown ids, so the row would pop back into the table).
            alive = self._jobs.get(rec.id) is job
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
            if error_kind is not None:
                rec.error_kind = error_kind  # type: ignore[assignment]
            if error_stage is not None:
                rec.error_stage = error_stage
            if error_traceback is not None:
                rec.error_traceback = error_traceback
            snapshot = rec.model_copy(deep=True)
            subs = list(job.subscribers)
            loop = job.loop
        # Per-job subscribers (legacy ``/ws/jobs/{id}`` endpoint) get a
        # ``state`` message; the global subscribers (the SPA's main
        # ``/api/ws`` connection) get a ``job.update`` message with the
        # same payload. Both carry the full record so the client can
        # replace its row in place.
        if not alive:
            # Record state locally (useful for logs / on_job_done) but
            # send nothing to clients and don't rewrite the index.
            return
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
        # Keep the on-disk index in sync so a restart restores this state.
        self._persist()
