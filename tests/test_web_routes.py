"""HTTP integration tests for the veauto web API.

These tests exercise the FastAPI app via :class:`fastapi.testclient.TestClient`.
They do **not** run the real veauto pipeline — instead they patch
``JobManager._run_job`` so jobs finish immediately.
"""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from veauto.models import (
    CutSegment,
    MediaInfo,
    SubtitleSegment,
)
from veauto.pipeline import PipelineResult
from veauto.web import jobs as jobs_mod
from veauto.web.app import create_app


@pytest.fixture
def app_root(tmp_path: Path) -> Path:
    root = tmp_path / "veauto-web"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def client(app_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose pipeline is faked (no ffmpeg / faster-whisper)."""
    app = create_app(output_root=app_root, max_workers=2)

    def fake_run(self, job):  # type: ignore[ANN001]
        from veauto.web.jobs import JobCancelled
        from veauto.web.utils import _output_basename
        result = PipelineResult(
            media=MediaInfo(
                path=job.input_path,
                duration=10.0,
                width=1920,
                height=1080,
                frame_rate=30.0,
                has_audio=False,
            ),
            cuts=[
                CutSegment(source_in=0.0, source_out=10.0, duration=10.0),
            ],
        )
        result.removed = []
        result.words = []
        result.subtitles = [
            SubtitleSegment(start=0.0, end=2.0, text="hello world"),
        ]
        result.fcpxml = "<?xml version='1.0'?><fcpxml/>"
        mgr = self
        try:
            mgr._check_cancel(job)  # type: ignore[attr-defined]
        except JobCancelled:
            return
        mgr._set_status(job, "running", stage="silence", progress=0.2)  # type: ignore[attr-defined]
        mgr._set_status(job, "running", stage="transcribing", progress=0.6)  # type: ignore[attr-defined]
        # Mirror the real _run_job: derive artefact names from the
        # source filename so the test exercises the same code path.
        base = _output_basename(job.record.input_name,
                               fallback_id=job.record.id)
        fcpxml_name = f"{base}.fcpxml"
        srt_name = f"{base}.srt"
        report_md_name = f"{base}.report.md"
        report_json_name = f"{base}.report.json"
        (job.output_dir / fcpxml_name).write_text(
            result.fcpxml, encoding="utf-8"
        )
        (job.output_dir / report_md_name).write_text(
            "# report\n", encoding="utf-8"
        )
        (job.output_dir / report_json_name).write_text(
            '{"ok": true}', encoding="utf-8"
        )
        (job.output_dir / srt_name).write_text(
            "1\n00:00:00,000 --> 00:00:01,000\ntest\n", encoding="utf-8"
        )
        # Mutate the record's stats in place (the same way the real
        # _run_job does), then set the terminal status.
        with mgr._lock:  # type: ignore[attr-defined]
            job.record.input_duration = result.media.duration
            job.record.num_silences = len(result.removed)
            job.record.num_cuts = len(result.cuts)
            job.record.num_subtitles = len(result.subtitles)
            job.record.kept_duration = result.kept_duration
            job.record.removed_duration = result.removed_duration
            job.record.fcpxml_name = fcpxml_name
            job.record.report_md_name = report_md_name
            job.record.report_json_name = report_json_name
            job.record.srt_name = srt_name
        mgr._set_status(  # type: ignore[attr-defined]
            job,
            "completed",
            stage="done",
            progress=1.0,
            message="Done (fake).",
        )

    monkeypatch.setattr(jobs_mod.JobManager, "_run_job", fake_run)
    return TestClient(app)


@pytest.fixture
def failing_client(app_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose pipeline is faked to crash so we can exercise
    the failure-diagnostics flow (error.log, error_traceback, etc.).
    """
    app = create_app(output_root=app_root, max_workers=1)

    def fake_fail(self, job):  # type: ignore[ANN001]
        mgr = self
        mgr._set_status(  # type: ignore[attr-defined]
            job, "running", stage="transcribing", progress=0.6,
            message="Faking a crash in the transcribe stage…",
        )
        # Simulate the same error-handling path the real worker uses.
        mgr._write_error_log(  # type: ignore[attr-defined]
            job.record.id, job.output_dir,
            stage="transcribing",
            error_kind="exception",
            summary="RuntimeError: simulated failure",
            traceback_text="Traceback (most recent call last):\n  File 'fake.py', line 1\nRuntimeError: simulated failure\n",
        )
        with mgr._lock:  # type: ignore[attr-defined]
            job.record.error_log_name = "error.log"
        mgr._set_status(  # type: ignore[attr-defined]
            job, "failed", stage="failed", progress=0.0,
            message="RuntimeError: simulated failure",
            error="RuntimeError: simulated failure",
            error_kind="exception",
            error_stage="transcribing",
            error_traceback="Traceback (most recent call last):\nRuntimeError: simulated failure\n",
        )

    monkeypatch.setattr(jobs_mod.JobManager, "_run_job", fake_fail)
    return TestClient(app)


class TestHealthAndConfig:
    def test_health(self, client: TestClient):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["service"] == "veauto-web"
        assert "version" in body

    def test_models_endpoint(self, client: TestClient):
        r = client.get("/api/config/models")
        assert r.status_code == 200
        data = r.json()
        assert "tiny" in data["models"]
        assert "large-v3" in data["models"]

    def test_defaults_endpoint(self, client: TestClient):
        r = client.get("/api/config/defaults")
        assert r.status_code == 200
        data = r.json()
        assert data["model"] == "medium"
        assert data["noise_db"] == -30.0


class TestJobSubmission:
    def test_upload_creates_job(self, client: TestClient, app_root: Path):
        r = client.post(
            "/api/jobs",
            params={"options": '{"model": "tiny"}'},
            files={"file": ("talk.mp4", io.BytesIO(b"\x00" * 256), "video/mp4")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert "id" in body
        # The worker thread may already have flipped the status to "running"
        # before the response is sent. Both are acceptable.
        assert body["status"] in {"queued", "running"}
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = client.get(f"/api/jobs/{body['id']}")
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        assert r.json()["status"] == "completed"
        # All four artefacts must be exposed via download URLs once
        # the job has finished. The base name is derived from the
        # uploaded filename (``talk.mp4`` → ``talk``).
        assert r.json()["fcpxml_url"] == (
            f"/api/jobs/{body['id']}/download/talk.fcpxml"
        )
        assert r.json()["report_md_url"] == (
            f"/api/jobs/{body['id']}/download/talk.report.md"
        )
        assert r.json()["report_json_url"] == (
            f"/api/jobs/{body['id']}/download/talk.report.json"
        )
        assert r.json()["srt_url"] == (
            f"/api/jobs/{body['id']}/download/talk.srt"
        )

    def test_download_srt_returns_text(
        self, client: TestClient, app_root: Path
    ):
        """The SRT artefact is downloadable through the generic
        /download/{name} endpoint and comes back as text with the
        correct MIME type.
        """
        r = client.post(
            "/api/jobs",
            params={"options": '{"model": "tiny"}'},
            files={"file": ("talk.mp4", io.BytesIO(b"\x00" * 256), "video/mp4")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = client.get(f"/api/jobs/{body['id']}")
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        r = client.get(f"/api/jobs/{body['id']}/download/talk.srt")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-subrip")
        assert r.text.startswith("1\n00:00:00,000 --> 00:00:01,000\n")

    def test_artefact_names_track_source_filename(
        self, client: TestClient, app_root: Path
    ):
        """The four artefacts (.fcpxml, .srt, .report.md,
        .report.json) must be named after the uploaded source file
        so downloading them in the browser shows a clear
        relationship with the original.

        ``My Talk (v2).mov`` → ``My_Talk_v2.fcpxml`` etc.
        """
        r = client.post(
            "/api/jobs",
            params={"options": '{"model": "tiny"}'},
            files={
                "file": (
                    "My Talk (v2).mov",
                    io.BytesIO(b"\x00" * 256),
                    "video/quicktime",
                ),
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = client.get(f"/api/jobs/{body['id']}")
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        jid = body["id"]
        assert r.json()["status"] == "completed"
        assert r.json()["fcpxml_url"].endswith("/My_Talk_v2.fcpxml")
        assert r.json()["srt_url"].endswith("/My_Talk_v2.srt")
        assert r.json()["report_md_url"].endswith("/My_Talk_v2.report.md")
        assert r.json()["report_json_url"].endswith("/My_Talk_v2.report.json")
        # The downloaded file content must also use the new name.
        r = client.get(f"/api/jobs/{jid}/download/My_Talk_v2.fcpxml")
        assert r.status_code == 200


class TestErrorLogDownload:
    """Failed jobs must expose ``error.log`` through the same download
    endpoint the other artefacts use, with text/plain content type.
    """

    def test_failed_job_has_error_log_url(
        self, failing_client: TestClient, app_root: Path
    ):
        r = failing_client.post(
            "/api/jobs",
            params={"options": '{"model": "tiny"}'},
            files={"file": ("boom.mp4", io.BytesIO(b"\x00" * 256), "video/mp4")},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = failing_client.get(f"/api/jobs/{body['id']}")
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
        job = r.json()
        assert job["status"] == "failed"
        assert job["error_kind"] == "exception"
        assert job["error_stage"] == "transcribing"
        assert job["error_traceback"]  # non-empty
        # error_log_url wired so the UI can hand the user a download link.
        assert job["error_log_url"] is not None
        assert job["error_log_url"].endswith("/error.log")

    def test_download_error_log_returns_text(
        self, failing_client: TestClient, app_root: Path
    ):
        r = failing_client.post(
            "/api/jobs",
            params={"options": '{"model": "tiny"}'},
            files={"file": ("boom.mp4", io.BytesIO(b"\x00" * 256), "video/mp4")},
        )
        assert r.status_code == 201
        jid = r.json()["id"]
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r = failing_client.get(f"/api/jobs/{jid}")
            if r.json()["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)

        r = failing_client.get(f"/api/jobs/{jid}/download/error.log")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        body = r.text
        assert "simulated failure" in body
        assert "RuntimeError" in body
        # The log header should make the stage obvious without having
        # to open the UI.
        assert "# stage: transcribing" in body

    def test_empty_file_rejected(self, client: TestClient):
        r = client.post(
            "/api/jobs",
            params={"options": "{}"},
            files={"file": ("empty.mp4", io.BytesIO(b""), "video/mp4")},
        )
        assert r.status_code == 400
        assert "Empty" in r.json()["detail"]

    def test_invalid_options_rejected(self, client: TestClient):
        r = client.post(
            "/api/jobs",
            params={"options": "not-json"},
            files={"file": ("x.mp4", io.BytesIO(b"abc"), "video/mp4")},
        )
        assert r.status_code == 422

    def test_invalid_model_rejected(self, client: TestClient):
        r = client.post(
            "/api/jobs",
            params={"options": '{"model": "gigantic-v99"}'},
            files={"file": ("x.mp4", io.BytesIO(b"abc"), "video/mp4")},
        )
        assert r.status_code == 422

    def test_list_jobs(self, client: TestClient):
        client.post(
            "/api/jobs",
            params={"options": "{}"},
            files={"file": ("a.mp4", io.BytesIO(b"x"), "video/mp4")},
        )
        r = client.get("/api/jobs")
        assert r.status_code == 200
        assert len(r.json()) >= 1

    def test_get_unknown_job_404(self, client: TestClient):
        r = client.get("/api/jobs/ghost")
        assert r.status_code == 404

    def test_delete_unknown_job_is_idempotent_204(self, client: TestClient):
        """DELETE must be idempotent: an unknown id returns 204, not
        404, so the UI can blindly call it without race conditions.
        """
        r = client.delete("/api/jobs/ghost")
        assert r.status_code == 204
