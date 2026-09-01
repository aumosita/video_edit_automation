"""Unit tests for veauto.pipeline.

Covers:
- remap_subtitles (drop, clip, order preservation)
- _cleanup_audio (idempotent, honours keep)
- run_pipeline (mock-based, no ffmpeg / faster-whisper required)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from veauto.models import (
    CutSegment,
    MediaInfo,
    PipelineConfig,
    SubtitleSegment,
    Word,
)
from veauto.pipeline import (
    PipelineResult,
    _cleanup_audio,
    remap_subtitles,
    run_pipeline,
)


def _fake_media(tmp_path: Path) -> MediaInfo:
    f = tmp_path / "in.mp4"
    f.write_text("placeholder")
    return MediaInfo(
        path=f,
        duration=10.0,
        width=1920,
        height=1080,
        frame_rate=30.0,
        has_audio=True,
    )


def _fake_words() -> list[Word]:
    return [
        Word(start=0.5, end=1.0, text="hello"),
        Word(start=3.0, end=3.5, text="world"),
        Word(start=6.0, end=6.5, text="again"),
    ]


def _patch_pipeline_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    silences: list,
    cuts: list[CutSegment],
    words: list[Word] | None = None,
    audio_wav: Path | None = None,
) -> dict[str, Any]:
    """Replace all I/O bound functions so run_pipeline is pure-OS-testable."""
    media = _fake_media(tmp_path)
    calls: dict[str, Any] = {"transcribe": None, "build_fcpxml": None}

    monkeypatch.setattr("veauto.pipeline.probe_media_info", lambda p: media)
    monkeypatch.setattr("veauto.pipeline.detect_silence", lambda *a, **k: silences)
    monkeypatch.setattr(
        "veauto.pipeline.build_cut_segments",
        lambda *a, **k: (cuts, list(silences)),
    )

    if audio_wav is not None:
        monkeypatch.setattr(
            "veauto.pipeline.extract_audio", lambda p, out=None: audio_wav
        )

    def _transcribe(audio, cfg, **kwargs):
        calls["transcribe"] = (audio, cfg)
        return words if words is not None else _fake_words()

    monkeypatch.setattr("veauto.pipeline._transcribe", _transcribe)
    monkeypatch.setattr(
        "veauto.pipeline.words_to_subtitle_segments",
        lambda ws, **k: [SubtitleSegment(start=w.start, end=w.end, text=w.text) for w in ws],
    )

    def _build(media, cuts_, **kw):
        calls["build_fcpxml"] = (media, cuts_, kw)
        return "<fcpxml/>"

    monkeypatch.setattr("veauto.pipeline.build_fcpxml", _build)
    return calls


# ---------------------------------------------------------------------------
# remap_subtitles
# ---------------------------------------------------------------------------


class TestRemapSubtitles:
    def test_empty_inputs_return_empty(self):
        assert remap_subtitles([], []) == []
        assert remap_subtitles(
            [CutSegment(source_in=0, source_out=1)], []
        ) == []
        assert remap_subtitles(
            [], [SubtitleSegment(start=0, end=1, text="x")]
        ) == []

    def test_subtitle_in_first_cut(self):
        cuts = [CutSegment(source_in=0, source_out=2)]
        subs = [SubtitleSegment(start=0.5, end=1.5, text="hi")]
        out = remap_subtitles(cuts, subs)
        assert len(out) == 1
        assert out[0].start == 0.5
        assert out[0].end == 1.5
        assert out[0].text == "hi"

    def test_subtitle_in_later_cut_is_shifted(self):
        cuts = [
            CutSegment(source_in=0, source_out=2),
            CutSegment(source_in=5, source_out=8),
        ]
        subs = [SubtitleSegment(start=5.5, end=6.5, text="b")]
        out = remap_subtitles(cuts, subs)
        assert len(out) == 1
        assert out[0].start == pytest.approx(2.5)
        assert out[0].end == pytest.approx(3.5)

    def test_subtitle_in_removed_silence_is_dropped(self):
        cuts = [
            CutSegment(source_in=0, source_out=2),
            CutSegment(source_in=5, source_out=8),
        ]
        subs = [SubtitleSegment(start=3.0, end=3.5, text="silenced")]
        assert remap_subtitles(cuts, subs) == []

    def test_subtitle_spanning_cut_boundary_is_clipped(self):
        cuts = [CutSegment(source_in=0, source_out=2)]
        subs = [SubtitleSegment(start=1.5, end=2.5, text="overflow")]
        out = remap_subtitles(cuts, subs)
        assert len(out) == 1
        assert out[0].start == pytest.approx(1.5)
        assert out[0].end == pytest.approx(2.0)

    def test_subtitle_ending_exactly_at_cut_end_kept(self):
        cuts = [CutSegment(source_in=0, source_out=2)]
        subs = [SubtitleSegment(start=0, end=2, text="full")]
        out = remap_subtitles(cuts, subs)
        assert len(out) == 1
        assert out[0].end == pytest.approx(2.0)

    def test_order_preserved(self):
        cuts = [
            CutSegment(source_in=0, source_out=2),
            CutSegment(source_in=5, source_out=8),
            CutSegment(source_in=10, source_out=12),
        ]
        subs = [
            SubtitleSegment(start=1.0, end=1.5, text="A"),
            SubtitleSegment(start=5.5, end=6.0, text="B"),
            SubtitleSegment(start=10.5, end=11.0, text="C"),
        ]
        out = remap_subtitles(cuts, subs)
        assert [s.text for s in out] == ["A", "B", "C"]
        # New timeline: cut0 [0..2], cut1 [2..5], cut2 [5..7]
        # A source 1.0 → cut0 offset 1.0 → new 1.0
        # B source 5.5 → cut1 offset 0.5 → new 2.5
        # C source 10.5 → cut2 offset 0.5 → new 5.5
        assert [s.start for s in out] == [
            pytest.approx(1.0),
            pytest.approx(2.5),
            pytest.approx(5.5),
        ]

    def test_subtitle_starts_in_silence_dropped(self):
        cuts = [
            CutSegment(source_in=0, source_out=2),
            CutSegment(source_in=5, source_out=8),
        ]
        subs = [SubtitleSegment(start=2.1, end=3.0, text="dead")]
        assert remap_subtitles(cuts, subs) == []


# ---------------------------------------------------------------------------
# _cleanup_audio
# ---------------------------------------------------------------------------


class TestCleanupAudio:
    def test_none_path_is_noop(self, tmp_path):
        _cleanup_audio(None, keep=False)

    def test_keep_true_does_not_delete(self, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_text("x")
        _cleanup_audio(wav, keep=True)
        assert wav.exists()

    def test_keep_false_deletes(self, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_text("x")
        _cleanup_audio(wav, keep=False)
        assert not wav.exists()

    def test_missing_file_is_silent(self, tmp_path):
        _cleanup_audio(tmp_path / "nope.wav", keep=False)


# ---------------------------------------------------------------------------
# run_pipeline (mocked I/O)
# ---------------------------------------------------------------------------


class TestRunPipelineSilenceOnly:
    def test_silence_only(self, monkeypatch, tmp_path):
        cuts = [
            CutSegment(source_in=0, source_out=2),
            CutSegment(source_in=5, source_out=10),
        ]
        _patch_pipeline_io(monkeypatch, tmp_path, silences=[], cuts=cuts)

        cfg = PipelineConfig()
        cfg.subtitle.enabled = False
        result = run_pipeline(tmp_path / "in.mp4", cfg)

        assert isinstance(result, PipelineResult)
        assert result.cuts == cuts
        assert result.subtitles == []
        assert result.words == []
        assert result.audio_path is None
        assert result.fcpxml == "<fcpxml/>"

    def test_kept_and_removed_durations(self, monkeypatch, tmp_path):
        cuts = [CutSegment(source_in=0, source_out=8)]
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=cuts,
        )
        cfg = PipelineConfig()
        cfg.subtitle.enabled = False
        result = run_pipeline(tmp_path / "in.mp4", cfg)
        assert result.kept_duration == pytest.approx(8.0)
        assert result.removed_duration == 0.0


class TestRunPipelineSubtitles:
    def test_subtitles_only_keeps_timeline(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = False
        cfg.subtitle.enabled = True
        cfg.keep_temp = True

        wav = tmp_path / "fake.wav"
        wav.write_text("x")
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=[CutSegment(source_in=0, source_out=10)],
            audio_wav=wav,
        )
        result = run_pipeline(tmp_path / "in.mp4", cfg)
        assert len(result.subtitles) == 3
        assert [s.text for s in result.subtitles] == ["hello", "world", "again"]

    def test_subtitles_remapped_onto_cuts(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = True
        cfg.subtitle.enabled = True
        cfg.keep_temp = True

        wav = tmp_path / "fake.wav"
        wav.write_text("x")
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=[
                CutSegment(source_in=0, source_out=3),
                CutSegment(source_in=4, source_out=10),
            ],
            audio_wav=wav,
        )
        result = run_pipeline(tmp_path / "in.mp4", cfg)
        # hello 0.5..1.0 in first cut, unchanged
        # world 3..3.5 sits in the gap (2..3 cut ends, silence 3..4, next
        # cut starts 4) — sub.start=3 is in the silence, so dropped
        # again 6..6.5 in second cut: offset=2, cumulative=3, new=5..5.5
        assert result.subtitles[0].start == pytest.approx(0.5)
        assert result.subtitles[-1].start == pytest.approx(5.0)
        assert result.subtitles[-1].end == pytest.approx(5.5)

    def test_subtitle_in_silence_is_dropped(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = True
        cfg.subtitle.enabled = True
        cfg.keep_temp = True

        wav = tmp_path / "fake.wav"
        wav.write_text("x")
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=[
                CutSegment(source_in=0, source_out=2),
                CutSegment(source_in=5, source_out=10),
            ],
            audio_wav=wav,
        )
        result = run_pipeline(tmp_path / "in.mp4", cfg)
        texts = [s.text for s in result.subtitles]
        assert "world" not in texts
        assert "hello" in texts
        assert "again" in texts

    def test_audio_cleaned_after_run(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = True
        cfg.subtitle.enabled = True
        cfg.keep_temp = False

        wav = tmp_path / "fake.wav"
        wav.write_text("x")
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=[CutSegment(source_in=0, source_out=10)],
            audio_wav=wav,
        )
        run_pipeline(tmp_path / "in.mp4", cfg)
        assert not wav.exists()

    def test_no_audio_source_skips_subtitles(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = True
        cfg.subtitle.enabled = True

        f = tmp_path / "in.mp4"
        f.write_text("x")
        media = MediaInfo(
            path=f, duration=10.0, width=1920, height=1080,
            frame_rate=30.0, has_audio=False,
        )
        monkeypatch.setattr("veauto.pipeline.probe_media_info", lambda p: media)
        monkeypatch.setattr("veauto.pipeline.detect_silence", lambda *a, **k: [])
        monkeypatch.setattr(
            "veauto.pipeline.build_cut_segments",
            lambda *a, **k: ([CutSegment(source_in=0, source_out=10)], []),
        )
        called = {"extract": 0}
        def _extract(*a, **k):
            called["extract"] += 1
            return tmp_path / "should.wav"
        monkeypatch.setattr("veauto.pipeline.extract_audio", _extract)
        monkeypatch.setattr("veauto.pipeline.build_fcpxml", lambda *a, **k: "<x/>")
        result = run_pipeline(f, cfg)
        assert called["extract"] == 0
        assert result.subtitles == []

    def test_transcriber_injection(self, monkeypatch, tmp_path):
        cfg = PipelineConfig()
        cfg.silence.enabled = False
        cfg.subtitle.enabled = True
        cfg.keep_temp = True

        wav = tmp_path / "fake.wav"
        wav.write_text("x")
        _patch_pipeline_io(
            monkeypatch, tmp_path,
            silences=[],
            cuts=[CutSegment(source_in=0, source_out=10)],
            audio_wav=wav,
        )

        def _custom(audio, sub_cfg):
            return [Word(start=1.0, end=2.0, text="CUSTOM")]

        result = run_pipeline(
            tmp_path / "in.mp4", cfg, transcriber=_custom
        )
        assert [s.text for s in result.subtitles] == ["CUSTOM"]


class TestRunPipelineErrors:
    def test_missing_input_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_pipeline(tmp_path / "ghost.mp4", PipelineConfig())



