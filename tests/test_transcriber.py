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
