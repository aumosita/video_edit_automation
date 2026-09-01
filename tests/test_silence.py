"""Tests for veauto.silence (parsing/merging, no ffmpeg required)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from veauto.models import SilenceConfig, SilenceInterval
from veauto.silence import (
    detect_silence,
    merge_close_intervals,
    parse_silencedetect_output,
    probe_duration,
)


def test_parse_silencedetect_basic() -> None:
    stderr = (
        "size=N/A time=00:00:01.00 bitrate=N/A\n"
        "[silencedetect @ 0x7f8] silence_start: 1.5\n"
        "[silencedetect @ 0x7f8] silence_end: 3.25 | silence_duration: 1.75\n"
    )
    intervals = parse_silencedetect_output(stderr)
    assert len(intervals) == 1
    assert intervals[0].start == pytest.approx(1.5)
    assert intervals[0].end == pytest.approx(3.25)


def test_parse_silencedetect_multiple() -> None:
    stderr = "[s] silence_start: 0.5\n[s] silence_end: 1.0\n[s] silence_start: 5.0\n[s] silence_end: 6.5\n[s] silence_start: 10.0\n[s] silence_end: 12.0\n"
    intervals = parse_silencedetect_output(stderr)
    assert len(intervals) == 3
    assert [iv.start for iv in intervals] == [0.5, 5.0, 10.0]
    assert [iv.end for iv in intervals] == [1.0, 6.5, 12.0]


def test_parse_silencedetect_empty() -> None:
    stderr = "ffmpeg version 8.0.1\nInput #0, wav\n"
    assert parse_silencedetect_output(stderr) == []


def test_parse_silencedetect_unterminated() -> None:
    """Trailing silence without closing silence_end is dropped."""
    stderr = "[s] silence_start: 2.0\n[s] silence_end: 3.0\n[s] silence_start: 7.5\n"
    intervals = parse_silencedetect_output(stderr)
    assert len(intervals) == 1
    assert intervals[0].start == 2.0
    assert intervals[0].end == 3.0


def test_parse_silencedetect_ignores_noise_setting() -> None:
    """Lines like noise=-30dB (config echo) must not be parsed as events."""
    stderr = "Parsed silencedetect: noise=-30dB d=0.5\n[s] silence_start: 4.0\n[s] silence_end: 5.0\n"
    intervals = parse_silencedetect_output(stderr)
    assert len(intervals) == 1
    assert intervals[0].start == 4.0


def test_merge_empty() -> None:
    assert merge_close_intervals([], min_gap=0.5) == []


def test_merge_no_merge_when_min_gap_zero() -> None:
    ivs = [
        SilenceInterval(start=0.0, end=1.0),
        SilenceInterval(start=1.1, end=2.0),
        SilenceInterval(start=2.05, end=3.0),
    ]
    out = merge_close_intervals(ivs, min_gap=0.0)
    assert len(out) == 3


def test_merge_merges_close_intervals() -> None:
    ivs = [
        SilenceInterval(start=0.0, end=1.0),
        SilenceInterval(start=1.3, end=2.0),  # gap=0.3
        SilenceInterval(start=4.0, end=5.0),  # gap=2.0
        SilenceInterval(start=5.4, end=6.0),  # gap=0.4
    ]
    out = merge_close_intervals(ivs, min_gap=0.5)
    assert len(out) == 2
    assert out[0].start == 0.0
    assert out[0].end == 2.0
    assert out[1].start == 4.0
    assert out[1].end == 6.0


def test_merge_returns_new_list() -> None:
    ivs = [SilenceInterval(start=0.0, end=1.0)]
    out = merge_close_intervals(ivs)
    assert out is not ivs




FFMPEG_SKIP = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_synthetic_wav(path, duration):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    silence_dur = max(0.0, duration - 0.5)
    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=16000",
        "-filter_complex",
        "[1:a]atrim=duration=" + str(silence_dur) + ",asetpts=PTS-STARTPTS[s];[0:a][s]concat=n=2:v=0:a=1[aout]",
        "-map", "[aout]",
        "-ar", "16000", "-ac", "1", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"ffmpeg failed: {proc.stderr}")


@FFMPEG_SKIP
def test_detect_silence_on_synthetic_wav(tmp_path):
    wav = tmp_path / "test.wav"
    _make_synthetic_wav(wav, duration=2.0)
    intervals = detect_silence(
        wav, SilenceConfig(noise_db=-30.0, min_silence=0.5, enabled=True)
    )
    assert len(intervals) >= 1
    starts = [iv.start for iv in intervals]
    assert any(0.3 <= s <= 0.8 for s in starts), (
        f"Expected a silence starting near 0.5s, got {starts}"
    )


@FFMPEG_SKIP
def test_probe_duration_on_synthetic_wav(tmp_path):
    wav = tmp_path / "test.wav"
    _make_synthetic_wav(wav, duration=2.0)
    duration = probe_duration(wav)
    assert 1.9 < duration < 2.5, f"Expected ~2s, got {duration}"


@FFMPEG_SKIP
def test_detect_silence_no_silence_returns_empty(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    wav = tmp_path / "tone.wav"
    proc = subprocess.run(
        [
            ffmpeg, "-y", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1.0",
            "-ar", "16000", "-ac", "1", str(wav),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    intervals = detect_silence(
        wav, SilenceConfig(noise_db=-30.0, min_silence=0.3, enabled=True)
    )
    assert intervals == [], f"Expected no silence, got {intervals}"
