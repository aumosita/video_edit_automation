"""Speech-to-text via faster-whisper.

This module wraps the ``faster-whisper`` library behind a small surface area:

- :func:`resolve_device` / :func:`resolve_compute_type` — pure functions, easy to unit-test.
- :func:`words_to_subtitle_segments` — group ``Word`` objects into user-facing
  ``SubtitleSegment`` lines respecting ``max_chars_per_line`` / ``max_lines`` /
  ``min_duration`` / ``max_duration`` and a within-line gap threshold.
- :func:`transcribe_with_model` — adapter from a ``WhisperModel``-like object
  to ``list[Word]``.
- :func:`transcribe` — facade that owns model construction. A ``model_factory``
  callable can be injected for tests so the heavy WhisperModel is never
  instantiated in CI.

The ``faster_whisper`` import is deferred to keep unit tests fast: nothing in
this module imports it at module-import time.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from typing import Any, Literal

from .models import SubtitleConfig, SubtitleSegment, Word

DevicePreference = Literal["auto", "cpu", "cuda", "mps"]
ComputePreference = Literal["auto", "int8", "int8_float16", "float16", "float32"]

# Minimum duration (seconds) any computed subtitle line is allowed to have.
# It exists so very short bursts don't flash on screen.
_MIN_DURATION_FLOOR = 0.2


def resolve_device(
    device: DevicePreference,
    *,
    has_cuda: bool | None = None,
    has_mps: bool | None = None,
) -> str:
    """Resolve the ``device`` preference to a concrete faster-whisper device name.

    Parameters
    ----------
    device
        ``"auto"`` prefers MPS (Apple Silicon) → CUDA → CPU; explicit values
        pass through unchanged.
    has_cuda / has_mps
        Optional overrides for tests / exotic environments. When both are
        ``None``, the function probes the live ``torch`` / platform.
    """
    if device != "auto":
        return device
    if has_cuda is None:
        has_cuda = _probe_has_cuda()
    if has_mps is None:
        has_mps = _probe_has_mps()
    if has_mps:
        return "mps"
    if has_cuda:
        return "cuda"
    return "cpu"


def _probe_has_cuda() -> bool:
    try:
        import torch  # type: ignore[import-not-found]

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _probe_has_mps() -> bool:
    try:
        import torch  # type: ignore[import-not-found]

        mps_mod = getattr(torch.backends, "mps", None)
        return bool(mps_mod and mps_mod.is_available())
    except Exception:
        return False


def resolve_compute_type(device: str, compute_type: ComputePreference) -> str:
    """Resolve the ``compute_type`` preference.

    Rules:

    - ``"auto"`` → ``"float16"`` on GPU/MPS, ``"int8"`` on CPU.
    - Explicit values pass through.
    """
    if compute_type != "auto":
        return compute_type
    if device == "cpu":
        return "int8"
    return "float16"


# ---------------------------------------------------------------------------
# Word → SubtitleSegment grouping
# ---------------------------------------------------------------------------


def words_to_subtitle_segments(
    words: Iterable[Word],
    *,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    min_duration: float = 0.8,
    max_duration: float = 6.0,
    max_gap: float = 0.6,
) -> list[SubtitleSegment]:
    """Group transcribed ``Word``s into user-facing subtitle lines.

    The algorithm packs consecutive words into a line until any of these
    conditions would be violated by adding the next word:

    1. The current line's character count (joined by single spaces) would
       exceed ``max_chars_per_line * max_lines``.
    2. The gap between the previous word and the next one exceeds ``max_gap``.
    3. The next word starts after the line would exceed ``max_duration``.

    After packing, each emitted line is clamped to ``[min_duration,
    max_duration]`` by extending ``end`` or by splitting it.
    """
    style_min = max(min_duration, _MIN_DURATION_FLOOR)
    max_chars = max_chars_per_line * max_lines

    segments: list[SubtitleSegment] = []
    buffer: list[Word] = []

    def _flush(buf: list[Word]) -> SubtitleSegment | None:
        if not buf:
            return None
        text = " ".join(w.text for w in buf)
        start = buf[0].start
        end = buf[-1].end
        if end - start < style_min:
            end = start + style_min
        return SubtitleSegment(start=start, end=end, text=text)

    for word in words:
        if not buffer:
            buffer.append(word)
            continue

        prospective_text = " ".join(w.text for w in buffer + [word])
        prospective_duration = word.end - buffer[0].start
        gap = word.start - buffer[-1].end

        would_break = (
            len(prospective_text) > max_chars
            or prospective_duration > max_duration
            or gap > max_gap
        )
        if would_break:
            seg = _flush(buffer)
            if seg is not None:
                segments.append(seg)
            buffer = [word]
        else:
            buffer.append(word)

    tail = _flush(buffer)
    if tail is not None:
        segments.append(tail)

    # Split any segment longer than max_duration.
    final: list[SubtitleSegment] = []
    for seg in segments:
        if seg.duration <= max_duration:
            final.append(seg)
            continue
        final.extend(_split_long_segment(seg, max_duration, max_chars, max_chars_per_line))

    # Merge near-empty segments (< min_duration) into the next one.
    final = _merge_short_segments(final, style_min)
    return final


def _split_long_segment(
    seg: SubtitleSegment,
    max_duration: float,
    max_chars: int,
    max_chars_per_line: int,
) -> list[SubtitleSegment]:
    """Naive duration-based splitter: cuts at the duration boundary."""
    parts: list[SubtitleSegment] = []
    words = seg.text.split()
    if not words:
        return [seg]

    n_parts = max(1, int(seg.duration // max_duration) + 1)
    chunk = max(1, len(words) // n_parts)
    slices: list[list[str]] = []
    for i in range(0, len(words), chunk):
        slices.append(words[i : i + chunk])

    duration = seg.duration
    base_start = seg.start
    per_part = duration / len(slices)
    cursor = base_start
    for idx, slice_words in enumerate(slices):
        text = " ".join(slice_words)
        start = cursor
        end = base_start + per_part * (idx + 1)
        parts.append(SubtitleSegment(start=start, end=end, text=text))
        cursor = end
    return parts


def _merge_short_segments(
    segments: list[SubtitleSegment],
    min_duration: float,
) -> list[SubtitleSegment]:
    """Merge short segments into the next one when possible."""
    if not segments:
        return []
    result: list[SubtitleSegment] = []
    for seg in segments:
        if result and seg.duration < min_duration:
            prev = result[-1]
            merged = SubtitleSegment(
                start=prev.start,
                end=max(prev.end, seg.end),
                text=f"{prev.text} {seg.text}",
            )
            result[-1] = merged
        else:
            result.append(seg)
    return result


# ---------------------------------------------------------------------------
# faster-whisper adapter
# ---------------------------------------------------------------------------


def _import_faster_whisper():
    """Lazy import so unit tests don't require the (heavy) library at import time."""
    return importlib.import_module("faster_whisper")


def transcribe_with_model(
    model: Any,
    audio_path: Any,
    *,
    language: str | None = None,
    beam_size: int = 5,
    vad_filter: bool = False,
    word_timestamps: bool = True,
) -> list[Word]:
    """Run inference on an already-loaded ``WhisperModel``-like instance.

    Returns a flat ``list[Word]``. ``model.transcribe`` is expected to return
    ``(segments, info)`` per the faster-whisper API. Each ``SegmentInfo``
    must expose ``.words`` (each with ``start``, ``end``, ``word``,
    ``probability``).
    """
    segments_iter, _info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=beam_size,
        vad_filter=vad_filter,
        word_timestamps=word_timestamps,
    )

    words: list[Word] = []
    for seg in segments_iter:
        for w in getattr(seg, "words", None) or []:
            text = (getattr(w, "word", "") or "").strip()
            if not text:
                continue
            try:
                start = float(getattr(w, "start", seg.start))
                end = float(getattr(w, "end", seg.end))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            words.append(
                Word(
                    start=start,
                    end=end,
                    text=text,
                    probability=float(getattr(w, "probability", 1.0)),
                )
            )
    return words


def transcribe(
    audio_path: Any,
    config: SubtitleConfig,
    *,
    model_factory: Callable[..., Any] | None = None,
) -> list[Word]:
    """Transcribe ``audio_path`` and return ``list[Word]``.

    ``model_factory`` must accept ``(model_size, device, compute_type)`` and
    return an object exposing ``.transcribe(...)``. If ``None``, the real
    ``faster_whisper.WhisperModel`` is used.
    """
    device = resolve_device(config.device)
    compute_type = resolve_compute_type(device, config.compute_type)
    factory = model_factory or _default_model_factory
    model = factory(config.model, device, compute_type)
    return transcribe_with_model(
        model,
        audio_path,
        language=config.language,
        beam_size=config.beam_size,
    )


def _default_model_factory(model_size: str, device: str, compute_type: str) -> Any:
    fw = _import_faster_whisper()
    return fw.WhisperModel(model_size, device=device, compute_type=compute_type)
