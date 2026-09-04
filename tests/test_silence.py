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


# ---------------------------------------------------------------------------
# Adaptive (auto) silence threshold
# ---------------------------------------------------------------------------


class TestEstimateSilenceThreshold:
    """``estimate_silence_threshold`` derives a threshold from the
    loudness distribution so quiet recordings don't get their soft
    speech cut as "silence" (the -30dB default bug)."""

    def test_empty_windows_falls_back_to_default(self):
        from veauto.silence import estimate_silence_threshold
        assert estimate_silence_threshold([]) == -30.0

    def test_quiet_recording_gets_lower_threshold(self):
        """Speech at ~-32dB RMS, noise floor at ~-85dB → the threshold
        must land below the speech level, well past the fixed -30."""
        from veauto.silence import estimate_silence_threshold
        windows = (
            [-32.0] * 50    # speech
            + [-85.0] * 40  # noise floor
        )
        t = estimate_silence_threshold(windows)
        assert t < -40.0, f"threshold {t} too high for quiet speech"
        assert t > -85.0 + 5, "threshold must stay above the noise floor"

    def test_loud_recording_gets_higher_threshold(self):
        from veauto.silence import estimate_silence_threshold
        windows = [-10.0] * 50 + [-70.0] * 40
        t = estimate_silence_threshold(windows)
        # speech -12 headroom = -22, clamped to max -20
        assert t == pytest.approx(-22.0)

    def test_threshold_respects_headroom_option(self):
        from veauto.silence import estimate_silence_threshold
        windows = [-32.0] * 50 + [-85.0] * 40
        aggressive = estimate_silence_threshold(windows, headroom_db=6.0)
        conservative = estimate_silence_threshold(windows, headroom_db=20.0)
        assert aggressive > conservative

    def test_clamped_to_bounds(self):
        from veauto.silence import estimate_silence_threshold
        # Everything extremely loud → clamp to max_db.
        assert estimate_silence_threshold([-1.0] * 10) == -20.0
        # Everything extremely quiet → clamp to min_db.
        assert estimate_silence_threshold([-110.0] * 10) == -70.0


def test_resolve_noise_db_passthrough_when_auto_off():
    from veauto.silence import resolve_noise_db

    class _P:
        def __fspath__(self):
            return "/dev/null"

    cfg = SilenceConfig(noise_db=-42.0, auto_noise_db=False)
    # Auto off → no ffmpeg call at all; /dev/null is never probed.
    assert resolve_noise_db(_P(), cfg) == -42.0


def test_resolve_noise_db_applies_offset(monkeypatch):
    """noise_db_offset shifts the auto-derived threshold; the result is
    re-clamped to [-70, -20] so it can never go out of range."""
    from veauto import silence as sl

    monkeypatch.setattr(
        sl, "measure_rms_profile", lambda *a, **k: [-32.0] * 50 + [-85.0] * 40
    )
    base = SilenceConfig(auto_noise_db=True)
    base_val = sl.resolve_noise_db("x", base)

    quieter = SilenceConfig(auto_noise_db=True, noise_db_offset=-5.0)
    louder = SilenceConfig(auto_noise_db=True, noise_db_offset=+5.0)
    assert sl.resolve_noise_db("x", quieter) == pytest.approx(base_val - 5.0)
    assert sl.resolve_noise_db("x", louder) == pytest.approx(base_val + 5.0)

    # Offsets pushing past the bounds are clamped.
    extreme = SilenceConfig(auto_noise_db=True, noise_db_offset=+30.0)
    assert sl.resolve_noise_db("x", extreme) == -20.0

    # Ignored in fixed mode.
    fixed = SilenceConfig(noise_db=-40.0, auto_noise_db=False, noise_db_offset=+5.0)
    assert sl.resolve_noise_db("x", fixed) == -40.0


@FFMPEG_SKIP
def test_measure_rms_profile_and_auto_detect(tmp_path):
    """A quiet-tone + digital-silence file: auto threshold must land
    between the two levels and detect the silent tail."""
    from veauto.silence import measure_rms_profile, resolve_noise_db

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    wav = tmp_path / "quiet.wav"
    # 2s of tone attenuated by 35dB (RMS ≈ -39dB), then 2s of true silence.
    proc = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000",
            "-filter_complex",
            "[0:a]volume=-35dB[t];"
            "[1:a]atrim=duration=2,asetpts=PTS-STARTPTS[s];"
            "[t][s]concat=n=2:v=0:a=1[aout]",
            "-map", "[aout]",
            "-ar", "16000", "-ac", "1", str(wav),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    windows = measure_rms_profile(wav)
    assert len(windows) >= 3

    cfg = SilenceConfig(noise_db=-30.0, auto_noise_db=True, min_silence=0.5)
    resolved = resolve_noise_db(wav, cfg)
    # Tone RMS ≈ -39dB → threshold must be below that so the tone is
    # NOT classified as silence (the -30 fixed default fails this).
    assert resolved < -39.0

    intervals = detect_silence(wav, cfg)
    assert intervals, "true-silence tail must still be detected"
    # The tone half (0–2s) must NOT be inside any silence interval.
    for iv in intervals:
        assert iv.end > 1.5, f"voice region cut as silence: {iv}"


@FFMPEG_SKIP
def test_detect_silence_auto_off_uses_fixed_threshold(tmp_path):
    """Regression guard: auto_noise_db=False keeps the exact legacy
    behaviour (fixed -30dB)."""
    wav = tmp_path / "test.wav"
    _make_synthetic_wav(wav, duration=2.0)
    intervals = detect_silence(
        wav,
        SilenceConfig(noise_db=-30.0, auto_noise_db=False,
                      min_silence=0.5, enabled=True),
    )
    assert len(intervals) >= 1


# ---------------------------------------------------------------------------
# Voice Activity Detection (B2 — sync fix)
# ---------------------------------------------------------------------------


class TestDetectVoiceRanges:
    """``detect_voice_ranges`` returns the complement of
    ``detect_silence`` — the non-silent ranges — so the pipeline
    can snap STT-derived subtitle timestamps onto real audio
    onsets.
    """

    def test_empty_silence_gives_one_full_range(self, monkeypatch):
        from veauto import silence as sl
        from veauto.models import SilenceConfig, VoiceRange

        monkeypatch.setattr(sl, "detect_silence", lambda *a, **k: [])
        voices = sl.detect_voice_ranges(
            _DummyPath(), SilenceConfig(noise_db=-30.0, min_silence=0.5),
            total_duration=10.0,
        )
        assert voices == [VoiceRange(source_in=0.0, source_out=10.0)]

    def test_complement_of_silence(self, monkeypatch):
        from veauto import silence as sl
        from veauto.models import SilenceConfig, SilenceInterval, VoiceRange

        silences = [SilenceInterval(start=2.0, end=4.0),
                    SilenceInterval(start=7.0, end=8.0)]
        monkeypatch.setattr(sl, "detect_silence", lambda *a, **k: silences)
        voices = sl.detect_voice_ranges(
            _DummyPath(), SilenceConfig(noise_db=-30.0, min_silence=0.5),
            total_duration=10.0,
        )
        assert voices == [
            VoiceRange(source_in=0.0, source_out=2.0),
            VoiceRange(source_in=4.0, source_out=7.0),
            VoiceRange(source_in=8.0, source_out=10.0),
        ]

    def test_zero_duration_returns_empty(self, monkeypatch):
        from veauto import silence as sl
        from veauto.models import SilenceConfig
        monkeypatch.setattr(sl, "detect_silence", lambda *a, **k: [])
        assert sl.detect_voice_ranges(
            _DummyPath(), SilenceConfig(noise_db=-30.0, min_silence=0.5),
            total_duration=0.0,
        ) == []


class TestSnapToVoice:
    """``snap_to_voice`` adjusts a single timestamp onto the
    nearest real audio edge, with a configurable snap window.
    """

    def test_timestamp_inside_voice_unchanged(self):
        from veauto.models import VoiceRange
        from veauto.silence import snap_to_voice
        voices = [VoiceRange(source_in=0.0, source_out=10.0)]
        assert snap_to_voice(5.0, voices) == 5.0

    def test_timestamp_near_edge_snaps(self):
        from veauto.models import VoiceRange
        from veauto.silence import snap_to_voice
        voices = [VoiceRange(source_in=2.0, source_out=4.0),
                  VoiceRange(source_in=6.0, source_out=8.0)]
        # 0.1 s before a voice edge → snap to it.
        assert snap_to_voice(1.9, voices) == 2.0
        # 0.5 s before a voice edge is within the new 0.7 s snap
        # window (the widen from 0.4 was intentional, to absorb
        # faster-whisper's early-onset bias).
        assert snap_to_voice(1.5, voices) == 2.0
        # 1.5 s away is outside any reasonable snap window.
        assert snap_to_voice(0.5, voices) == 0.5

    def test_no_voice_ranges_keeps_timestamp(self):
        from veauto.silence import snap_to_voice
        assert snap_to_voice(3.0, []) == 3.0


class TestShiftSubtitleTimestamps:
    """``shift_subtitle_timestamps`` is the bulk helper the pipeline
    uses to apply :func:`snap_to_voice` across an entire subtitle
    list. It returns the number of subtitles that actually moved.
    """

    def test_returns_count_of_moved_subtitles(self):
        from veauto.models import SubtitleSegment, VoiceRange
        from veauto.silence import shift_subtitle_timestamps
        voices = [VoiceRange(source_in=2.0, source_out=4.0)]
        subs = [
            # Edge 2.0 is 0.3 s ahead -> start snaps forward to 2.0.
            SubtitleSegment(start=1.7, end=1.9, text="snaps-left"),
            # Inside the voice range -> untouched.
            SubtitleSegment(start=2.5, end=3.5, text="in-voice"),
            # Entirely after the voice range (4.0): a *backward* end
            # snap to 4.0 would invert the cue, so both times stay.
            SubtitleSegment(start=4.3, end=4.5, text="after-voice"),
            # 1.5 s away from 2.0 -> outside the 0.7 s window, unchanged.
            SubtitleSegment(start=0.5, end=0.6, text="too-far"),
        ]
        n = shift_subtitle_timestamps(subs, voices)
        # Only the first subtitle moves.
        assert n == 1
        assert subs[0].start == pytest.approx(2.0)
        assert subs[1].start == pytest.approx(2.5)  # untouched
        # Index 2 starts after the speech ended; nothing to snap to.
        assert subs[2].start == pytest.approx(4.3)
        assert subs[2].end == pytest.approx(4.5)


class _DummyPath:
    """Path stand-in that lets us exercise ``detect_voice_ranges``
    without invoking the real ``detect_silence`` subprocess.
    """
    def __fspath__(self):
        return "/dev/null"


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
