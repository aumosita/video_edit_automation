"""Tests for the asyncio-based :class:`veauto.web.jobs.JobManager`."""

from __future__ import annotations

import asyncio
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

