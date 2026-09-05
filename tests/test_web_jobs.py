"""Tests for the asyncio-based :class:`veauto.web.jobs.JobManager`."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from veauto.web.jobs import JobCancelled, JobManager
from veauto.web.schemas import JobOptions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path) -> JobManager:
    return JobManager(output_root=tmp_path, max_workers=2)


def _write_dummy_video(directory: Path, name: str = "in.mp4", size: int = 1024) -> Path:
    p = directory / name
    p.write_bytes(b"\x00" * size)
    return p


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    """Poll until ``predicate()`` is truthy, or raise :class:`AssertionError`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("Predicate did not become true within timeout")


# ---------------------------------------------------------------------------
# Submit / list / get
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    def test_submit_creates_record(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="in.mp4",
                input_size=1024,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        assert rec.id
        assert rec.input_name == "in.mp4"
        assert rec.input_size == 1024
        assert rec.options.model == "medium"
        # Status transitions are observed via get(); at submit time the
        # worker may have already finished for a tiny task, so we only
        # check that the initial state was either queued or one of the
        # terminal states.
        observed = manager.get(rec.id).status
        assert observed in ("queued", "running", "completed", "failed", "cancelled")

    def test_list_returns_submitted(self, manager: JobManager, tmp_path: Path):
        loop = asyncio.new_event_loop()
        try:
            for i in range(3):
                v = _write_dummy_video(tmp_path, name=f"v{i}.mp4")
                manager.submit(
                input_path=v,
                input_name=v.name,
                input_size=10,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        jobs = manager.list_jobs()
        assert len(jobs) == 3

    def test_get_unknown_returns_none(self, manager: JobManager):
        assert manager.get("nope") is None

    def test_index_written_on_submit(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(model="small", language="ko"),
                loop=loop,
            )
        finally:
            loop.close()
        index = tmp_path / "jobs.json"
        assert index.exists()
        data = json.loads(index.read_text(encoding="utf-8"))
        assert len(data["jobs"]) == 1

    def test_index_restored_across_restart(self, tmp_path: Path):
        # Simulate a server restart: submit, throw the manager away,
        # construct a new one over the same output root.
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            m1 = JobManager(output_root=tmp_path, max_workers=2)
            rec = m1.submit(
                input_path=v,
                input_name="persisted.mp4",
                input_size=1,
                options=JobOptions(model="medium", language="en"),
                loop=loop,
            )
        finally:
            loop.close()

        m2 = JobManager(output_root=tmp_path, max_workers=2)
        restored = m2.get(rec.id)
        assert restored is not None
        assert restored.input_name == "persisted.mp4"
        assert restored.options.model == "medium"
        assert restored.options.language == "en"

    def test_running_job_marked_interrupted_on_restart(self, tmp_path: Path):
        index = tmp_path / "jobs.json"
        index.write_text(
            json.dumps(
                {
                    "version": 1,
                    "jobs": [
                        {
                            "id": "abc123",
                            "status": "running",
                            "created_at": "2024-01-01T00:00:00+00:00",
                            "started_at": "2024-01-01T00:00:01+00:00",
                            "input_name": "in.mp4",
                            "input_size": 10,
                            "options": {},
                            "progress": 0.4,
                            "stage": "transcribe",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        m = JobManager(output_root=tmp_path, max_workers=2)
        rec = m.get("abc123")
        assert rec is not None
        assert rec.status == "cancelled"
        assert rec.error == "Interrupted by server restart"
        assert rec.stage == "interrupted"

    def test_corrupt_index_starts_empty(self, tmp_path: Path):
        (tmp_path / "jobs.json").write_text("{not json", encoding="utf-8")
        m = JobManager(output_root=tmp_path, max_workers=2)
        assert m.list_jobs() == []

    def test_clear_removes_jobs_and_artifacts(self, manager: JobManager, tmp_path: Path):
        loop = asyncio.new_event_loop()
        try:
            for i in range(3):
                v = _write_dummy_video(tmp_path, name=f"c{i}.mp4")
                manager.submit(
                    input_path=v,
                    input_name=v.name,
                    input_size=1,
                    options=JobOptions(),
                    loop=loop,
                )
        finally:
            loop.close()
        removed = manager.clear()
        assert removed == 3
        assert manager.list_jobs() == []
        # The index is emptied too, so a restart stays empty.
        data = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
        assert data["jobs"] == []

    def test_deleted_job_does_not_broadcast(self, manager: JobManager, tmp_path: Path):
        """A job that was deleted must not reappear in the UI.

        The worker can still be unwinding from a long ffmpeg call after
        the record is gone; broadcasting a ``job.update`` for it would
        make the SPA (which upserts unknown ids) resurrect the row.
        """
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="gone.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()

        collected: list[dict] = []

        class _FakeLoop:
            def call_soon_threadsafe(self, fn, *args):  # noqa: ANN002
                fn(*args)

        sub = manager.subscribe_all()
        sub.loop = _FakeLoop()  # type: ignore[assignment]

        job = manager._jobs[rec.id]
        assert manager.delete(rec.id) is True

        # Late status update from the still-running worker thread.
        manager._set_status(job, "cancelled", stage="cancelled", error="Cancelled")

        # Drain the queue. The delete itself broadcasts ``job.deleted``,
        # which is expected; what must NOT appear is a late update (or a
        # per-job ``state`` message) for the removed job.
        while not sub.queue.empty():
            collected.append(sub.queue.get_nowait())
        assert [m["type"] for m in collected] == ["job.deleted"]
        assert manager.get(rec.id) is None

    def test_cancel_keeps_record_and_marks_cancelled(
        self, manager: JobManager, tmp_path: Path
    ):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="stop.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()

        assert manager.cancel(rec.id) is True
        job = manager._jobs.get(rec.id)
        assert job is not None
        # The record stays in the table (visible as cancelled) instead of
        # silently disappearing as it did when cancel == delete.
        _wait_for(lambda: manager.get(rec.id) is not None
                  and manager.get(rec.id).status == "cancelled")

    def test_cancel_kills_registered_child_process(
        self, manager: JobManager, tmp_path: Path
    ):
        """Cancelling terminates an ffmpeg child instead of waiting."""
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="long.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()

        job = manager._jobs[rec.id]
        # Simulate a long ffmpeg call the worker is blocked in.
        with manager._track_subprocess(job):
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        assert proc.poll() is None
        manager.cancel(rec.id)
        assert proc.poll() is not None  # terminated, not waited out
        manager._kill_procs(job)

    def test_job_dir_created(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        d = manager.job_dir(rec.id)
        assert d is not None
        assert d.is_dir()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancel_marks_event(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        assert manager.cancel(rec.id) is True
        assert manager.cancel(rec.id) is True  # idempotent

    def test_cancel_unknown_returns_false(self, manager: JobManager):
        assert manager.cancel("ghost") is False

    def test_check_cancel_raises_when_set(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        job = manager._jobs[rec.id]  # type: ignore[attr-defined]
        job.cancel_event.set()
        with pytest.raises(JobCancelled):
            manager._check_cancel(job)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Failure diagnostics: error.log, error_traceback, error_kind, error_stage
# ---------------------------------------------------------------------------


class TestJobFailure:
    """A worker that crashes mid-pipeline must leave a ``error.log``
    next to the output artefacts and fill the diagnostic fields on the
    :class:`JobRecord` so the UI can surface the cause.
    """

    def test_exception_writes_error_log_and_fills_record(
        self, manager: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from veauto import pipeline as pl

        def _explode(*_a, **_kw):
            raise RuntimeError("boom from fake pipeline")

        # ``_run_pipeline_with_progress`` calls ``pl.run_pipeline``
        # directly, so we patch the pipeline module's symbol.
        monkeypatch.setattr(pl, "run_pipeline", _explode)

        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()

        _wait_for(lambda: manager.get(rec.id).status == "failed")
        job = manager._jobs[rec.id]  # type: ignore[attr-defined]
        final = job.record
        # Status + summary populated.
        assert final.status == "failed"
        assert final.error is not None and "RuntimeError" in final.error
        assert "boom from fake pipeline" in final.error
        # New diagnostic fields populated.
        assert final.error_kind == "exception"
        assert final.error_stage is not None
        assert final.error_traceback is not None
        assert "RuntimeError" in final.error_traceback
        # error.log file written and wired into the record.
        assert final.error_log_name == "error.log"
        log_path = manager.job_dir(rec.id) / "error.log"
        assert log_path.exists()
        body = log_path.read_text(encoding="utf-8")
        assert "boom from fake pipeline" in body
        assert "traceback" in body.lower()
        # Surface file to the UI as a download URL.
        assert final.with_download_urls().error_log_url is not None
        assert final.with_download_urls().error_log_url.endswith("/error.log")

    def test_cancellation_records_kind_and_stage(
        self, manager: JobManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A clean user cancel should be reported as ``cancelled`` (not
        ``failed``) and carry the last-known pipeline stage so the user
        can see *where* the cancel hit."""
        import threading

        from veauto import pipeline as pl

        # Block inside run_pipeline until the test trips the cancel event.
        proceed = threading.Event()

        def _slow(*_a, **_kw):
            proceed.wait(timeout=5.0)
            # Even if the test fails to cancel, raise the cancel error so
            # the worker still terminates.
            raise JobCancelled()

        monkeypatch.setattr(pl, "run_pipeline", _slow)

        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()

        # Give the worker a moment to enter run_pipeline, then cancel.
        time.sleep(0.05)
        assert manager.cancel(rec.id) is True
        proceed.set()

        _wait_for(
            lambda: manager.get(rec.id).status in ("failed", "cancelled"),
            timeout=3.0,
        )
        final = manager.get(rec.id)
        assert final.status == "cancelled"
        assert final.error_kind == "cancelled"
        assert final.error_stage is not None
        # No error.log is written for clean cancels — the summary line
        # on the badge is enough.
        assert final.error_log_name is None


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    def test_subscribe_returns_queue(self, manager: JobManager, tmp_path: Path):
        v = _write_dummy_video(tmp_path)
        loop = asyncio.new_event_loop()
        try:
            rec = manager.submit(
                input_path=v,
                input_name="x.mp4",
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        sub = manager.subscribe(rec.id)
        assert sub is not None
        assert isinstance(sub.queue, asyncio.Queue)
        manager.unsubscribe(rec.id, sub)

    def test_subscribe_unknown_returns_none(self, manager: JobManager):
        assert manager.subscribe("ghost") is None

    def test_unsubscribe_unknown_job_does_not_raise(self, manager: JobManager):
        manager.unsubscribe("ghost", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_marks_all_cancelled(self, manager: JobManager, tmp_path: Path):
        loop = asyncio.new_event_loop()
        try:
            for i in range(2):
                v = _write_dummy_video(tmp_path, name=f"v{i}.mp4")
                manager.submit(
                input_path=v,
                input_name=v.name,
                input_size=1,
                options=JobOptions(),
                loop=loop,
            )
        finally:
            loop.close()
        manager.shutdown(wait=True)
        for job in manager._jobs.values():  # type: ignore[attr-defined]
            assert job.cancel_event.is_set()


# ---------------------------------------------------------------------------
# Global broadcast (regression: live table updates)
# ---------------------------------------------------------------------------


class TestGlobalBroadcast:
    """The SPA's main ``/api/ws`` connection subscribes to *all* job
    events via :meth:`JobManager.subscribe_all`. Every state change
    (create / update / delete) must put a message on that subscriber's
    queue, otherwise the live table does not refresh until the user
    reloads the page.
    """

    def test_delete_broadcasts_job_deleted(self, tmp_path: Path):
        """Regression: DELETE must pop the job AND broadcast so the
        UI removes the row immediately.
        """
        from veauto.web.jobs import JobManager, _Job
        from veauto.web.schemas import JobRecord

        manager = JobManager(output_root=tmp_path / "out")
        v = tmp_path / "in.mp4"
        v.write_bytes(b"x")
        record = JobRecord(
            id="jid", status="completed", input_name="x.mp4",
            input_size=1, fcpxml_name="out.fcpxml",
        )
        j = _Job(record=record, input_path=v, output_dir=tmp_path / "jid")
        manager._jobs["jid"] = j  # type: ignore[attr-defined]

        # Subscribe a fake "global" WS connection.
        sub = manager.subscribe_all()
        # We need a real event loop so the broadcast can call
        # call_soon_threadsafe on it.
        loop = asyncio.new_event_loop()
        sub.loop = loop
        try:
            ok = manager.delete("jid")
            assert ok is True
            assert "jid" not in manager._jobs  # type: ignore[attr-defined]

            # Drain the queue synchronously: call_soon_threadsafe has
            # already scheduled the put_nowait. The loop's ready queue
            # is processed on the next iteration; we step it manually.
            loop.run_until_complete(sub.queue.get())
            # The test would block here if the message had not been
            # scheduled; the fact that we reach this line is the proof.
        finally:
            loop.close()

    def test_set_status_broadcasts_job_update(
        self, manager: JobManager, tmp_path: Path
    ):
        """Regression: ``_set_status`` (called by every stage
        transition) must broadcast ``job.update`` to global
        subscribers, otherwise the live progress bar never moves.
        """
        from veauto.web.jobs import _Job
        from veauto.web.schemas import JobRecord

        v = tmp_path / "in.mp4"
        v.write_bytes(b"x")
        record = JobRecord(
            id="upd", status="running", input_name="x.mp4", input_size=1,
        )
        j = _Job(record=record, input_path=v, output_dir=tmp_path / "upd")
        manager._jobs["upd"] = j  # type: ignore[attr-defined]

        sub = manager.subscribe_all()
        loop = asyncio.new_event_loop()
        sub.loop = loop
        try:
            manager._set_status(  # type: ignore[attr-defined]
                j, "running", stage="detecting_silence", progress=0.42,
            )
            msg = loop.run_until_complete(
                asyncio.wait_for(sub.queue.get(), timeout=1.0)
            )
            assert msg["type"] == "job.update"
            assert msg["job"]["id"] == "upd"
            assert msg["job"]["progress"] == 0.42
            assert msg["job"]["stage"] == "detecting_silence"
            # Download URLs must be attached so the UI can render
            # the action column even mid-run.
            assert msg["job"]["fcpxml_url"] is None  # no artefact yet
        finally:
            loop.close()

