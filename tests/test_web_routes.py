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
        (job.output_dir / "out.fcpxml").write_text(result.fcpxml, encoding="utf-8")
        (job.output_dir / "report.md").write_text("# report\n", encoding="utf-8")
        (job.output_dir / "report.json").write_text(
            '{"ok": true}', encoding="utf-8"
        )
        # Mutate the record's stats in place (the same way the real
        # _run_job does), then set the terminal status. The signature
        # of _set_status does not accept ``result``/``fcpxml_path``;
        # we update the record directly under the manager's lock.
        with mgr._lock:  # type: ignore[attr-defined]
            job.record.input_duration = result.media.duration
            job.record.num_silences = len(result.removed)
            job.record.num_cuts = len(result.cuts)
            job.record.num_subtitles = len(result.subtitles)
            job.record.kept_duration = result.kept_duration
            job.record.removed_duration = result.removed_duration
            job.record.fcpxml_name = "out.fcpxml"
            job.record.report_md_name = "report.md"
            job.record.report_json_name = "report.json"
        mgr._set_status(  # type: ignore[attr-defined]
            job,
            "completed",
            stage="done",
            progress=1.0,
            message="Done (fake).",
        )

    monkeypatch.setattr(jobs_mod.JobManager, "_run_job", fake_run)
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
