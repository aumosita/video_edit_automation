"""Tests for veauto.transcriber.

Unit tests cover the pure logic (device / compute_type / word grouping /
model adapter) using simple stand-in objects — the real faster-whisper model
is never instantiated. Integration with the real library is exercised only
in the ``@requires_faster_whisper`` block, which is skipped if the library
can't be imported.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from veauto.models import SubtitleConfig, Word
from veauto.transcriber import (
    resolve_compute_type,
    resolve_device,
    transcribe,
    transcribe_with_model,
    words_to_subtitle_segments,
)


def _w(start: float, end: float, text: str, p: float = 1.0) -> Word:
    return Word(start=start, end=end, text=text, probability=p)


class _FakeWord:
    def __init__(self, start: float, end: float, word: str, probability: float = 1.0):
        self.start = start
        self.end = end
        self.word = word
        self.probability = probability


class _FakeSegment:
    def __init__(self, start: float, end: float, words: list[_FakeWord]):
        self.start = start
        self.end = end
        self.words = words


class _FakeWhisperModel:
    """Stand-in for faster_whisper.WhisperModel."""

    def __init__(self, segments: list[_FakeSegment]):
        self._segments = segments
        self.calls: dict[str, Any] = {}

    def transcribe(
        self, audio, *, language, beam_size, vad_filter,
        word_timestamps, condition_on_previous_text,
    ):
        self.calls = {
            "audio": audio,
            "language": language,
            "beam_size": beam_size,
            "vad_filter": vad_filter,
            "word_timestamps": word_timestamps,
            "condition_on_previous_text": condition_on_previous_text,
        }
        return iter(self._segments), SimpleNamespace(language=language)


# ---------------------------------------------------------------------------
# resolve_device / resolve_compute_type
# ---------------------------------------------------------------------------


def test_resolve_device_explicit_passthrough() -> None:
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("cuda") == "cuda"
    assert resolve_device("mps") == "mps"


def test_resolve_device_auto_prefers_mps_then_cuda_then_cpu() -> None:
    assert resolve_device("auto", has_cuda=True, has_mps=True) == "mps"
    assert resolve_device("auto", has_cuda=True, has_mps=False) == "cuda"
    assert resolve_device("auto", has_cuda=False, has_mps=False) == "cpu"


def test_resolve_compute_type_explicit_passthrough() -> None:
    assert resolve_compute_type("cpu", "float32") == "float32"
    assert resolve_compute_type("cuda", "int8") == "int8"


def test_resolve_compute_type_auto() -> None:
    assert resolve_compute_type("cpu", "auto") == "int8"
    assert resolve_compute_type("cuda", "auto") == "float16"
    assert resolve_compute_type("mps", "auto") == "float16"


# ---------------------------------------------------------------------------
# words_to_subtitle_segments
# ---------------------------------------------------------------------------


def test_words_to_subtitle_segments_empty() -> None:
    assert words_to_subtitle_segments([]) == []


def test_words_to_subtitle_segments_single_word_extends_to_min_duration() -> None:
    out = words_to_subtitle_segments([_w(0.0, 0.1, "hi")], min_duration=0.8)
    assert len(out) == 1
    assert out[0].start == pytest.approx(0.0)
    assert out[0].end == pytest.approx(0.8)
    assert out[0].text == "hi"


def test_words_to_subtitle_segments_breaks_on_max_chars() -> None:
    words = [
        _w(0.0, 0.3, "a" * 20),
        _w(0.4, 0.7, "b" * 20),
        _w(0.8, 1.1, "c" * 20),
    ]
    out = words_to_subtitle_segments(
        words, max_chars_per_line=20, max_lines=1, min_duration=0.0
    )
    assert len(out) == 3
    assert [s.text for s in out] == ["a" * 20, "b" * 20, "c" * 20]


def test_words_to_subtitle_segments_breaks_on_max_gap() -> None:
    words = [
        _w(0.0, 0.2, "hello"),
        _w(0.3, 0.5, "world"),
        _w(2.0, 2.2, "next"),  # 1.5s gap
    ]
    out = words_to_subtitle_segments(words, max_gap=0.6, min_duration=0.0)
    assert [s.text for s in out] == ["hello world", "next"]


def test_sentence_boundary_splits_subtitles() -> None:
    words = [
        _w(0.0, 0.4, "안녕하세요."),
        _w(0.45, 0.9, "반갑습니다"),
    ]
    out = words_to_subtitle_segments(words, min_duration=0.0)
    assert [s.text for s in out] == ["안녕하세요.", "반갑습니다"]


def test_sentence_split_disabled_keeps_one_cue() -> None:
    words = [
        _w(0.0, 0.4, "안녕하세요."),
        _w(0.45, 0.9, "반갑습니다"),
    ]
    out = words_to_subtitle_segments(
        words, min_duration=0.0, split_on_sentence=False
    )
    assert len(out) == 1
    assert out[0].text == "안녕하세요. 반갑습니다"


def test_ellipsis_breaks_when_speech_resumes_later() -> None:
    words = [
        _w(0.0, 0.4, "거의 다 했어..."),
        _w(1.5, 1.9, "그래"),  # gap > max_gap: real pause
    ]
    out = words_to_subtitle_segments(words, max_gap=0.6, min_duration=0.0)
    assert [s.text for s in out] == ["거의 다 했어...", "그래"]


def test_ellipsis_with_immediate_speech_stays_one_cue() -> None:
    words = [
        _w(0.0, 0.4, "말하고..."),
        _w(0.5, 0.9, "그 다음에"),  # gap <= max_gap: utterance continues
    ]
    out = words_to_subtitle_segments(words, max_gap=0.6, min_duration=0.0)
    assert len(out) == 1
    assert out[0].text == "말하고... 그 다음에"


def test_long_cue_wraps_into_display_lines() -> None:
    words = [
        _w(0.0, 0.3, "word " * 9 + "end"),  # 49 chars, words ≤ 5 chars
        _w(0.4, 0.7, "tail " * 3 + "x"),   # 24 chars
    ]
    out = words_to_subtitle_segments(
        words, max_chars_per_line=42, max_lines=2, min_duration=0.0
    )
    assert len(out) == 1
    lines = out[0].text.split("\n")
    assert len(lines) == 2
    assert all(len(line) <= 42 for line in lines)
    # No content lost by the wrap.
    joined = out[0].text.replace("\n", " ")
    assert joined == ("word " * 9 + "end" + " " + "tail " * 3 + "x")


def test_exclamation_and_question_marks_break_sentences() -> None:
    words = [
        _w(0.0, 0.3, "정말?"),
        _w(0.35, 0.6, "네!"),
        _w(0.65, 1.0, "그렇습니다"),
    ]
    out = words_to_subtitle_segments(words, min_duration=0.0)
    assert [s.text for s in out] == ["정말?", "네!", "그렇습니다"]


def test_words_to_subtitle_segments_breaks_on_max_duration() -> None:
    words = [_w(float(i), float(i) + 0.2, f"w{i}") for i in range(7)]
    out = words_to_subtitle_segments(
        words, max_duration=3.0, min_duration=0.0, max_gap=10.0
    )
    assert len(out) >= 2
    assert all(s.duration <= 3.0 for s in out)


def test_words_to_subtitle_segments_merges_short_segments() -> None:
    out = words_to_subtitle_segments(
        [_w(0.0, 0.1, "hi"), _w(0.5, 2.0, "there")],
        min_duration=0.8,
    )
    assert len(out) == 1
    assert out[0].text == "hi there"


def test_words_to_subtitle_segments_never_overlap() -> None:
    """The min-duration extension must not run past the next line's onset.

    Two adjacent short lines used to both be stretched to ``min_duration``,
    so the first one overlapped the second and FCP showed both at once.
    """
    words = [
        _w(0.0, 0.2, "hello"),
        _w(0.3, 0.5, "world"),
        _w(3.0, 3.2, "next"),  # 2.5s gap -> a fresh line
    ]
    out = words_to_subtitle_segments(words, max_gap=0.6, min_duration=0.8)
    for a, b in zip(out, out[1:], strict=False):
        assert a.end <= b.start + 1e-6, (
            f"subtitle lines overlap: [{a.start:.2f}, {a.end:.2f}] vs "
            f"[{b.start:.2f}, {b.end:.2f}]"
        )
    # First short line is clamped to 0.8s (not stretched to 3.0s).
    assert out[0].start == pytest.approx(0.0)
    assert out[0].end == pytest.approx(0.8)
    # The tail line is allowed to reach min_duration because nothing
    # follows it.
    assert out[-1].end == pytest.approx(3.8)


def test_merge_short_segments_wont_exceed_max_chars() -> None:
    """Over-limit merges keep the short line separate instead of making a long one."""
    from veauto.models import SubtitleSegment
    from veauto.transcriber import _merge_short_segments

    segs = [
        SubtitleSegment(start=0.0, end=1.0, text="a" * 30),
        SubtitleSegment(start=1.2, end=1.25, text="b" * 30),
    ]
    out = _merge_short_segments(segs, min_duration=0.8, max_chars=40)
    # Appending "b"*30 would make 61 chars (> 40), so the lines must
    # not be glued together — but the short one is kept, not erased.
    assert [s.text for s in out] == ["a" * 30, "b" * 30]


def test_merge_short_segments_wont_bridge_long_gap() -> None:
    """Short lines separated by a real silence are kept apart, not merged."""
    from veauto.models import SubtitleSegment
    from veauto.transcriber import _merge_short_segments

    segs = [
        SubtitleSegment(start=0.0, end=0.5, text="hello world"),
        SubtitleSegment(start=3.0, end=3.2, text="next"),
    ]
    out = _merge_short_segments(segs, min_duration=0.8, max_gap=0.6)
    assert [s.text for s in out] == ["hello world", "next"]


# ---------------------------------------------------------------------------
# transcribe_with_model — adapter
# ---------------------------------------------------------------------------


def test_transcribe_with_model_happy_path() -> None:
    seg = _FakeSegment(
        start=0.0,
        end=2.0,
        words=[
            _FakeWord(0.0, 0.5, " hello"),
            _FakeWord(0.5, 1.0, " world"),
            _FakeWord(1.0, 2.0, "!\n"),
        ],
    )
    model = _FakeWhisperModel([seg])
    out = transcribe_with_model(model, "audio.wav", language="en")
    assert [w.text for w in out] == ["hello", "world", "!"]
    assert out[0].start == pytest.approx(0.0)
    assert out[2].end == pytest.approx(2.0)
    assert model.calls["audio"] == "audio.wav"
    assert model.calls["word_timestamps"] is True


def test_transcribe_with_model_skips_empty_words() -> None:
    seg = _FakeSegment(
        start=0.0,
        end=1.0,
        words=[_FakeWord(0.0, 0.5, "  "), _FakeWord(0.5, 1.0, "real")],
    )
    out = transcribe_with_model(_FakeWhisperModel([seg]), "a.wav")
    assert [w.text for w in out] == ["real"]


def test_transcribe_with_model_skips_inverted_words() -> None:
    seg = _FakeSegment(
        start=0.0,
        end=2.0,
        words=[
            _FakeWord(0.0, 0.5, "ok"),
            _FakeWord(1.0, 0.5, "bad"),  # end < start
        ],
    )
    out = transcribe_with_model(_FakeWhisperModel([seg]), "a.wav")
    assert [w.text for w in out] == ["ok"]


def test_transcribe_uses_injected_model_factory() -> None:
    """``transcribe`` must honor ``model_factory`` and never import faster-whisper."""
    captured: dict[str, Any] = {}

    def factory(model_size, device, compute_type):
        captured["model_size"] = model_size
        captured["device"] = device
        captured["compute_type"] = compute_type
        return _FakeWhisperModel(
            [_FakeSegment(0.0, 0.5, [_FakeWord(0.0, 0.5, "test")])]
        )

    cfg = SubtitleConfig(model="tiny", device="cpu", compute_type="auto")
    out = transcribe("ignored.wav", cfg, model_factory=factory)

    assert captured == {"model_size": "tiny", "device": "cpu", "compute_type": "int8"}
    assert [w.text for w in out] == ["test"]


# ---------------------------------------------------------------------------
# Optional integration with real faster-whisper (skipped if not importable)
# ---------------------------------------------------------------------------


try:
    import faster_whisper  # noqa: F401

    _FW_AVAILABLE = True
except Exception:
    _FW_AVAILABLE = False


@pytest.mark.skipif(not _FW_AVAILABLE, reason="faster-whisper not importable")
def test_default_factory_resolves_to_whisper_model_class() -> None:
    """Smoke: the real factory returns an object exposing ``.transcribe``."""
    from veauto.transcriber import _default_model_factory

    model = _default_model_factory("tiny", "cpu", "int8")
    assert hasattr(model, "transcribe")
