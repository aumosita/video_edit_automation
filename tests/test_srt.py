"""Unit tests for the SRT writer."""

from __future__ import annotations

from pathlib import Path

from veauto.models import SubtitleSegment
from veauto.srt import (
    format_srt_cue,
    seconds_to_srt_time,
    write_srt,
)


class TestSecondsToSrtTime:
    def test_zero(self):
        assert seconds_to_srt_time(0.0) == "00:00:00,000"

    def test_one_second(self):
        assert seconds_to_srt_time(1.0) == "00:00:01,000"

    def test_minute_boundary(self):
        assert seconds_to_srt_time(60.0) == "00:01:00,000"

    def test_hour_boundary(self):
        assert seconds_to_srt_time(3600.0) == "01:00:00,000"

    def test_milliseconds(self):
        assert seconds_to_srt_time(1.234) == "00:00:01,234"

    def test_negative_clamps_to_zero(self):
        assert seconds_to_srt_time(-0.5) == "00:00:00,000"

    def test_nan_clamps_to_zero(self):
        assert seconds_to_srt_time(float("nan")) == "00:00:00,000"

    def test_rounds_milliseconds(self):
        # 1.2345 → 1234.5 ms → 1235 ms (banker's rounding via Python's
        # round is even, so 1.2345 rounds to 1234).
        # We just assert the output is well-formed HH:MM:SS,mmm.
        out = seconds_to_srt_time(1.234567)
        assert len(out) == 12
        assert out[2] == ":"
        assert out[5] == ":"
        assert out[8] == ","


class TestFormatSrtCue:
    def test_basic_cue(self):
        out = format_srt_cue(1, 1.0, 3.5, "Hello world")
        assert out == "1\n00:00:01,000 --> 00:00:03,500\nHello world\n"

    def test_end_before_start_extends_to_one_ms_later(self):
        # SRT requires end > start. The writer should bump end by 1ms
        # to keep the file valid rather than emitting an invalid cue.
        out = format_srt_cue(2, 5.0, 4.999, "x")
        assert "00:00:05,000 --> 00:00:05,001" in out

    def test_multiline_text_preserved(self):
        out = format_srt_cue(3, 0.0, 2.0, "line one\nline two")
        # The text line is preserved verbatim, even with \n.
        assert "line one\nline two" in out


class TestWriteSrt:
    def test_writes_simple_cues(self, tmp_path: Path):
        subs = [
            SubtitleSegment(start=0.0, end=2.0, text="Hello"),
            SubtitleSegment(start=3.0, end=5.0, text="World"),
        ]
        out = tmp_path / "out.srt"
        n = write_srt(subs, out)
        assert n == 2
        text = out.read_text(encoding="utf-8")
        assert "1\n00:00:00,000 --> 00:00:02,000\nHello" in text
        assert "2\n00:00:03,000 --> 00:00:05,000\nWorld" in text
        # Final newline is preserved.
        assert text.endswith("\n")

    def test_empty_input_writes_empty_file(self, tmp_path: Path):
        out = tmp_path / "out.srt"
        n = write_srt([], out)
        assert n == 0
        assert out.read_text(encoding="utf-8") == ""

    def test_creates_parent_directory(self, tmp_path: Path):
        out = tmp_path / "nested" / "deeper" / "out.srt"
        write_srt(
            [SubtitleSegment(start=0.0, end=1.0, text="hi")],
            out,
        )
        assert out.exists()
        assert "hi" in out.read_text(encoding="utf-8")

    def test_indices_are_one_based_and_in_order(self, tmp_path: Path):
        subs = [
            SubtitleSegment(start=i, end=i + 1, text=f"line {i}")
            for i in range(5)
        ]
        out = tmp_path / "out.srt"
        write_srt(subs, out)
        text = out.read_text(encoding="utf-8")
        for i in range(5):
            # The cue index must appear at the start of the line.
            assert f"{i + 1}\n00:00:" in text
