"""Unit tests for the report module (P4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veauto.models import MediaInfo
from veauto.pipeline import PipelineResult
from veauto.report import (
    build_report_data,
    render_json_report,
    render_markdown_report,
    render_report,
    write_report,
)


def _make_result(
    *,
    duration: float = 100.0,
    silences=(),
    cuts=(),
    words=(),
    subtitles=(),
    has_audio: bool = True,
) -> PipelineResult:
    media = MediaInfo(
        path=Path("/tmp/example.mp4"),
        duration=duration,
        width=1920,
        height=1080,
        frame_rate=30.0,
        has_audio=has_audio,
    )
    return PipelineResult(
        media=media,
        cuts=list(cuts),
        removed=list(silences),
        words=list(words),
        subtitles=list(subtitles),
        audio_path=None,
        fcpxml="<fcpxml/>",
    )


# ---------------------------------------------------------------------------
# build_report_data
# ---------------------------------------------------------------------------


class TestBuildReportData:
    def test_empty_result(self):
        result = _make_result()
        data = build_report_data(result)
        assert data["input"]["duration_s"] == 100.0
        assert data["silence_removal"]["num_silences"] == 0
        assert data["silence_removal"]["removed_duration_s"] == 0.0
        assert data["subtitles"]["num_subtitles"] == 0
        assert data["subtitles"]["avg_chars_per_subtitle"] == 0.0

    def test_removed_ratio_basic(self):
        # 30s removed out of 100s = 0.3
        result = _make_result(duration=100.0)
        data = build_report_data(result)
        assert data["silence_removal"]["removed_ratio"] == 0.0
        # Now exercise with non-zero removed
        from veauto.models import RemovedSilence
        result2 = _make_result(
            duration=100.0,
            silences=[RemovedSilence(source_in=10, source_out=40, duration=30.0)],
        )
        data2 = build_report_data(result2)
        assert data2["silence_removal"]["removed_ratio"] == 0.3

    def test_round_values_to_3dp(self):
        result = _make_result(duration=12.3456789)
        data = build_report_data(result)
        assert data["input"]["duration_s"] == 12.346

    def test_data_is_json_serialisable(self):
        result = _make_result()
        data = build_report_data(result)
        # Should not raise
        json.dumps(data, ensure_ascii=False)


# ---------------------------------------------------------------------------
# JSON / Markdown rendering
# ---------------------------------------------------------------------------


class TestRenderJsonReport:
    def test_basic_json(self):
        result = _make_result()
        text = render_json_report(result)
        parsed = json.loads(text)
        assert parsed["input"]["path"].endswith("example.mp4")
        assert "silence_removal" in parsed
        assert "subtitles" in parsed

    def test_indent_parameter(self):
        result = _make_result()
        assert "\n  " in render_json_report(result, indent=2)
        assert "\n" in render_json_report(result, indent=0)


class TestRenderMarkdownReport:
    def test_contains_header_sections(self):
        result = _make_result()
        md = render_markdown_report(result)
        assert "# veauto report" in md
        assert "## Input" in md
        assert "## Silence removal" in md
        assert "## Subtitles" in md

    def test_with_silences_lists_first_20(self):
        from veauto.models import RemovedSilence

        silences = [
            RemovedSilence(source_in=i, source_out=i + 1.0, duration=1.0)
            for i in range(25)
        ]
        result = _make_result(
            silences=silences,
            cuts=[],
        )
        md = render_markdown_report(result)
        assert "### Silence intervals (first 20)" in md
        assert "5 more" in md

    def test_with_subtitles_includes_table(self):
        from veauto.models import SubtitleSegment

        subs = [
            SubtitleSegment(start=i, end=i + 1.0, duration=1.0, text=f"line {i}")
            for i in range(35)
        ]
        result = _make_result(subtitles=subs)
        md = render_markdown_report(result)
        assert "### Subtitle preview (first 30 lines)" in md
        assert "5 more" in md

    def test_escapes_pipe_in_subtitle_text(self):
        from veauto.models import SubtitleSegment

        result = _make_result(
            subtitles=[SubtitleSegment(start=0, end=1, duration=1, text="a | b")],
        )
        md = render_markdown_report(result)
        # The pipe inside the cell should be escaped to keep table valid
        assert "a \\| b" in md

    def test_render_report_dispatch(self):
        result = _make_result()
        assert render_report(result, "json").startswith("{")
        assert render_report(result, "md").startswith("#")
        assert render_report(result, "markdown").startswith("#")
        with pytest.raises(ValueError, match="Unknown report format"):
            render_report(result, "html")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


class TestWriteReport:
    def test_writes_file_and_returns_body(self, tmp_path: Path):
        result = _make_result()
        target = tmp_path / "sub" / "report.md"
        body = write_report(result, target, "md")
        assert target.exists()
        assert target.read_text(encoding="utf-8") == body
        assert body.startswith("# veauto report")

    def test_writes_json(self, tmp_path: Path):
        result = _make_result()
        target = tmp_path / "r.json"
        body = write_report(result, target, "json")
        assert target.exists()
        # Re-parse to ensure valid JSON
        json.loads(body)
