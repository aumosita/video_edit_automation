"""Tests for the Pydantic schemas used by the veauto web API."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veauto.web.schemas import (
    JobCreateResponse,
    JobOptions,
    JobRecord,
    WSMessage,
)


class TestJobOptions:
    def test_defaults(self):
        opts = JobOptions()
        assert opts.noise_db == -30.0
        assert opts.min_silence == 1.5
        assert opts.margin == 0.3
        assert opts.min_keep_seconds == 0.15
        assert opts.model == "medium"
        assert opts.style_max_chars == 42
        assert opts.style_max_lines == 2

    def test_model_is_unknown_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(model="gigantic-v9")

    def test_position_is_unknown_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(style_position="sideways")

    def test_device_is_unknown_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(device="tpu")

    def test_compute_type_is_unknown_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(compute_type="int4")

    def test_json_round_trip(self):
        opts = JobOptions(model="tiny", language="ko", style_max_chars=30)
        again = JobOptions.model_validate_json(opts.model_dump_json())
        assert again == opts


class TestJobRecordDownloadUrls:
    """``JobRecord.with_download_urls`` must populate the ``*_url``
    fields based on the ``*_name`` fields and the job id.
    """

    def _make(self, **kw):
        from veauto.web.schemas import JobRecord
        defaults = {
            "id": "abc123",
            "status": "completed",
            "input_name": "x.mp4",
            "input_size": 1,
        }
        defaults.update(kw)
        return JobRecord(**defaults)

    def test_urls_none_when_names_none(self):
        rec = self._make()
        rec2 = rec.with_download_urls()
        assert rec2.fcpxml_url is None
        assert rec2.report_md_url is None
        assert rec2.report_json_url is None
        assert rec2.srt_url is None

    def test_urls_populated_from_names(self):
        rec = self._make(
            fcpxml_name="output.fcpxml",
            report_md_name="report.md",
            report_json_name="report.json",
            srt_name="output.srt",
        )
        rec2 = rec.with_download_urls()
        assert rec2.fcpxml_url == "/api/jobs/abc123/download/output.fcpxml"
        assert rec2.report_md_url == "/api/jobs/abc123/download/report.md"
        assert rec2.report_json_url == "/api/jobs/abc123/download/report.json"
        assert rec2.srt_url == "/api/jobs/abc123/download/output.srt"

    def test_does_not_mutate_original(self):
        rec = self._make(fcpxml_name="output.fcpxml")
        rec2 = rec.with_download_urls()
        assert rec.fcpxml_url is None
        assert rec2.fcpxml_url is not None

    def test_partial_names(self):
        rec = self._make(fcpxml_name="out.fcpxml")  # no md / json
        rec2 = rec.with_download_urls()
        assert rec2.fcpxml_url is not None
        assert rec2.report_md_url is None
        assert rec2.report_json_url is None

    def test_extreme_values(self):
        # Pydantic does not clamp by default; ensure values are preserved
        opts = JobOptions(noise_db=-90.0, beam_size=1, style_font_size=8)
        assert opts.noise_db == -90.0
        assert opts.beam_size == 1
        assert opts.style_font_size == 8

    def test_to_pipeline_config(self):
        opts = JobOptions(
            model="tiny",
            language="ko",
            style_max_chars=30,
            style_position="top",
            no_silence=False,
            no_subtitles=True,
        )
        cfg = opts.to_pipeline_config()
        assert cfg.subtitle.model == "tiny"
        assert cfg.subtitle.language == "ko"
        assert cfg.subtitle.style.max_chars_per_line == 30
        assert cfg.subtitle.style.position == "top"
        assert cfg.silence.enabled is True
        assert cfg.subtitle.enabled is False
        # `no_subtitles=True` maps to target="none" by default
        assert cfg.subtitle.target == "none"

    @pytest.mark.parametrize(
        "kwargs,expected_target,expected_stt,expected_in_fcpxml",
        [
            # default: no flag, no override => "both"
            ({}, "both", True, True),
            # srt only: STT runs but FCPXML has no captions
            ({"subtitle_target": "srt"}, "srt", True, False),
            # both: explicit
            ({"subtitle_target": "both"}, "both", True, True),
            # none: skip everything
            ({"subtitle_target": "none"}, "none", False, False),
            # fcpxml: reserved. Without STT results there's nothing to
            # bake, so the resolved `in_fcpxml` is False at runtime.
            ({"subtitle_target": "fcpxml"}, "fcpxml", False, False),
            # legacy no_subtitles still works on its own
            ({"no_subtitles": True}, "none", False, False),
            # subtitle_target wins over no_subtitles
            ({"subtitle_target": "srt", "no_subtitles": True}, "srt", True, False),
        ],
    )
    def test_subtitle_target_resolution(
        self, kwargs, expected_target, expected_stt, expected_in_fcpxml
    ):
        cfg = JobOptions(**kwargs).to_pipeline_config()
        assert cfg.subtitle.target == expected_target
        assert cfg.subtitle.stt_enabled is expected_stt
        assert cfg.subtitle.in_fcpxml is expected_in_fcpxml
        # `enabled` should match stt_enabled for all current targets.
        assert cfg.subtitle.enabled is expected_stt

    def test_subtitle_target_invalid_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(subtitle_target="bogus")

    def test_no_silence_disables_stage(self):
        cfg = JobOptions(no_silence=True).to_pipeline_config()
        assert cfg.silence.enabled is False

    def test_noise_db_offset_passes_through(self):
        cfg = JobOptions(auto_noise_db=True, noise_db_offset=-5.0).to_pipeline_config()
        assert cfg.silence.auto_noise_db is True
        assert cfg.silence.noise_db_offset == -5.0

    def test_noise_db_offset_bounds(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            JobOptions(noise_db_offset=31.0)
        with pytest.raises(ValidationError):
            JobOptions(noise_db_offset=-31.0)

    def test_below_min_silence_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(min_silence=-1.0)

    def test_beam_size_above_max_raises(self):
        with pytest.raises(ValidationError):
            JobOptions(beam_size=100)


class TestJobCreateResponse:
    def test_basic(self):
        resp = JobCreateResponse(id="abc123", status="queued")
        assert resp.id == "abc123"
        assert resp.status == "queued"


class TestJobRecord:
    def test_minimal(self):
        rec = JobRecord(
            id="j1",
            status="queued",
            input_name="talk.mp4",
            input_size=1024,
        )
        assert rec.id == "j1"
        assert rec.status == "queued"
        assert rec.input_name == "talk.mp4"
        assert rec.input_size == 1024
        assert rec.input_mime is None

    def test_invalid_status_raises(self):
        with pytest.raises(ValidationError):
            JobRecord(
                id="j1",
                status="aborted",  # type: ignore[arg-type]
                input_name="x",
                input_size=1,
            )

    def test_json_serialisable(self):
        rec = JobRecord(
            id="j1",
            status="completed",
            input_name="x.mp4",
            input_size=10,
            progress=1.0,
            stage="done",
        )
        data = rec.model_dump(mode="json")
        assert data["id"] == "j1"
        assert data["progress"] == 1.0


class TestWSMessage:
    def test_state_type(self):
        m = WSMessage(type="state", payload={"id": "j1"})
        assert m.type == "state"
        assert m.payload == {"id": "j1"}

    def test_progress_type(self):
        m = WSMessage(type="progress", payload={"stage": "transcribing", "p": 0.5})
        assert m.type == "progress"
        assert m.payload == {"stage": "transcribing", "p": 0.5}
