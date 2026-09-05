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
import math
from collections.abc import Callable, Iterable
from typing import Any, Literal

from .models import SubtitleConfig, SubtitleSegment, Word

DevicePreference = Literal["auto", "cpu", "cuda", "mps", "metal"]
ComputePreference = Literal["auto", "int8", "int8_float16", "float16", "float32"]

# Minimum duration (seconds) any computed subtitle line is allowed to have.
# It exists so very short bursts don't flash on screen.
_MIN_DURATION_FLOOR = 0.2

# Word endings that terminate a sentence (used when ``split_on_sentence``
# is on). Covers Latin/CJK punctuation and the typographic ellipsis.
_TERMINAL_PUNCT = (".", "!", "?", "。", "…")
# Ellipsis variants: treated as sentence end, but *suppressed* when the
# next word follows immediately (a trailing-off utterance that continues).
_ELLIPSIS_SUFFIXES = ("...", "…", "..")


def _strip_closing_quotes(text: str) -> str:
    """Drop trailing quote/bracket chars so ``'end.'`` still reads as a
    sentence end."""
    return text.rstrip().rstrip('"\')}›»』」）)]〉》')


def _ends_sentence(text: str) -> bool:
    """Whether a word's text terminates a sentence.

    The check is on the raw word text as produced by faster-whisper,
    which attaches punctuation to the word token (e.g. ``"done."``).
    Any run of terminal punctuation (``?!``, ``...?!``, ``..``) counts
    as exactly one boundary — a word is only tested once.
    """
    stripped = _strip_closing_quotes(text)
    return stripped.endswith(_TERMINAL_PUNCT)


def _ends_with_ellipsis(text: str) -> bool:
    """Whether a word ends in an ellipsis (``...`` / ``…`` / ``..``).

    Must be tested *before* ``_ends_sentence`` matters: ``...`` also
    ends with ``.``, but an ellipsis followed immediately by more
    speech (short gap) usually means the utterance is continuing, so
    the caller may suppress the break.
    """
    return _strip_closing_quotes(text).endswith(_ELLIPSIS_SUFFIXES)


def _wrap_text(text: str, max_chars_per_line: int, max_lines: int) -> str:
    """Wrap a cue's text into up to ``max_lines`` lines joined by ``\\n``.

    The width is chosen as the *smallest* width in
    ``[ceil(len/max_lines), max_chars_per_line]`` that still fits the
    text in ``max_lines`` lines (greedy line packing is optimal for a
    fixed width, so binary search is exact). This yields visually
    balanced lines instead of a full first line + a dangling last
    word. Single-line text is returned unchanged.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars_per_line or max_lines < 2:
        return text
    words = text.split()
    if len(words) < 2:
        return text

    def _count_lines(width: int) -> int:
        n, cur = 1, 0
        for w in words:
            if cur and cur + 1 + len(w) > width:
                n += 1
                cur = len(w)
            else:
                cur = len(w) if cur == 0 else cur + 1 + len(w)
        return n

    width = max_chars_per_line
    if _count_lines(width) <= max_lines:
        lo, hi = max(1, math.ceil(len(text) / max_lines)), width
        while lo < hi:
            mid = (lo + hi) // 2
            if _count_lines(mid) <= max_lines:
                hi = mid
            else:
                lo = mid + 1
        width = lo

    lines: list[str] = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}" if cur else w
        if cur and len(cand) > width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def resolve_device(
    device: DevicePreference,
    *,
    has_cuda: bool | None = None,
    has_mps: bool | None = None,
    has_metal: bool | None = None,
) -> str:
    """Resolve the ``device`` preference to a concrete backend device name.

    Parameters
    ----------
    device
        ``"auto"`` prefers Metal (Apple Silicon, via whisper.cpp) → CUDA
        → CPU; explicit values pass through unchanged.
    has_cuda / has_mps / has_metal
        Optional overrides for tests / exotic environments. When
        ``None``, the function probes the live machine.

    Notes
    -----
    ``auto`` deliberately prefers ``"metal"`` over ``"mps"`` on Apple
    Silicon. ``mps`` would mean "ask CTranslate2 for the Metal
    Performance Shaders backend", which does not exist — that is the
    ``unsupported device mps`` failure. ``metal`` routes to whisper.cpp,
    which does have a working ggml Metal backend. The old MPS probe is
    kept for callers that ask for ``mps`` explicitly.
    """
    if device != "auto":
        return device
    if has_metal is None:
        has_metal = _probe_has_metal()
    if has_cuda is None:
        has_cuda = _probe_has_cuda()
    if has_mps is None:
        has_mps = _probe_has_mps()
    # Apple GPU first: on an M-series Mac it is the only real
    # accelerator, and the whisper.cpp path measurably beats CPU.
    if has_metal:
        return "metal"
    if has_cuda:
        return "cuda"
    if has_mps:
        return "mps"
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


def _probe_has_metal() -> bool:
    """Whether the whisper.cpp Metal path is usable on this machine.

    Two things have to hold: an Apple Silicon host (Metal on Intel Macs
    is not built by the Homebrew ggml formula), and the ``whisper-cli``
    binary being installed. Both are cheap to check and neither imports
    a heavy dependency.
    """
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False
    try:
        from .transcriber_whispercpp import find_whisper_cli

        find_whisper_cli()
        return True
    except Exception:
        return False


def resolve_compute_type(device: str, compute_type: ComputePreference) -> str:
    """Resolve the ``compute_type`` preference.

    Rules:

    - ``"auto"`` → ``"float16"`` on GPU/MPS, ``"int8"`` on CPU.
    - Explicit values pass through.

    ``metal`` is a no-op here: quantisation for the whisper.cpp backend
    is baked into the chosen ggml file (see
    :data:`veauto.transcriber_whispercpp._MODEL_FILES`), not passed at
    runtime. The value is reported as ``"int8"`` because the default
    ggml mapping uses q5_0/q8 weights.
    """
    if compute_type != "auto":
        return compute_type
    if device in ("cpu", "metal"):
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
    split_on_sentence: bool = True,
) -> list[SubtitleSegment]:
    """Group transcribed ``Word``s into user-facing subtitle lines.

    The algorithm packs consecutive words into a line until any of these
    conditions would be violated by adding the next word:

    1. The current line's character count (joined by single spaces) would
       exceed ``max_chars_per_line * max_lines``.
    2. The gap between the previous word and the next one exceeds ``max_gap``.
    3. The next word starts after the line would exceed ``max_duration``.
    4. When ``split_on_sentence`` is on: the previous word ended with
       terminal punctuation (``.`` ``!`` ``?`` ``。`` ``…``). An ellipsis
       is treated as a sentence end *unless* the next word follows
       immediately (gap within ``max_gap``), in which case a trailing-off
       utterance like ``"말하고... 그 다음에"`` stays on one caption.

    After packing, each emitted line is clamped to ``[min_duration,
    max_duration]`` by extending ``end`` or by splitting it. Finally,
    text longer than ``max_chars_per_line`` is wrapped into up to
    ``max_lines`` display lines joined by ``\\n`` (SRT and FCPXML both
    render the newline as a real line break).
    """
    style_min = max(min_duration, _MIN_DURATION_FLOOR)
    max_chars = max_chars_per_line * max_lines

    segments: list[SubtitleSegment] = []
    buffer: list[Word] = []

    # The first word's start of the *next* buffer, set just before a
    # flush. Used to clamp a min-duration extension so a subtitle never
    # runs past the next line's onset (which would overlap in FCP and
    # look out of sync). ``None`` means there is no next line, so the
    # (last) subtitle may extend freely.
    next_start: float | None = None

    def _flush(buf: list[Word]) -> SubtitleSegment | None:
        nonlocal next_start
        if not buf:
            return None
        text = " ".join(w.text for w in buf)
        start = buf[0].start
        end = buf[-1].end
        if end - start < style_min:
            # Short bursts are extended so the viewer has time to read
            # them. When another line follows, the extension is clamped
            # to the next line's onset so consecutive captions never
            # overlap in FCP.
            end = start + style_min
            if next_start is not None:
                end = min(end, next_start - 1e-9)
        if end - start <= 0:
            return None
        next_start = None
        return SubtitleSegment(start=start, end=end, text=text)

    for word in words:
        if not buffer:
            buffer.append(word)
            if next_start is None:
                # First buffer of the run; nothing before it to clamp.
                next_start = word.start
            continue

        prospective_text = " ".join(w.text for w in buffer + [word])
        prospective_duration = word.end - buffer[0].start
        gap = word.start - buffer[-1].end

        # Sentence boundary: the *previous* word ended a sentence. An
        # ellipsis only breaks when the next word doesn't follow
        # immediately (see ``split_on_sentence`` docs).
        sentence_break = False
        if split_on_sentence and _ends_sentence(buffer[-1].text):
            if _ends_with_ellipsis(buffer[-1].text) and gap <= max_gap:
                sentence_break = False
            else:
                sentence_break = True

        would_break = (
            len(prospective_text) > max_chars
            or prospective_duration > max_duration
            or gap > max_gap
            or sentence_break
        )
        if would_break:
            next_start = word.start
            seg = _flush(buffer)
            if seg is not None:
                segments.append(seg)
            buffer = [word]
        else:
            buffer.append(word)

    # The tail is the last line — let its min-duration extension run
    # unclamped (FCP stays in sync because nothing follows it).
    next_start = None
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
    final = _merge_short_segments(
        final, style_min, max_duration, max_chars, max_gap
    )

    # Wrap long cues into display lines (\n). Kept last so the merged /
    # split results above are wrapped consistently.
    if max_lines > 1:
        for seg in final:
            seg.text = _wrap_text(seg.text, max_chars_per_line, max_lines)
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
    max_duration: float | None = None,
    max_chars: int | None = None,
    max_gap: float | None = None,
) -> list[SubtitleSegment]:
    """Merge short segments into the previous one when possible.

    A segment shorter than ``min_duration`` flashes on screen too
    briefly to read. Merging it into the previous line keeps the
    caption legible — but only when the merged line still respects
    ``max_duration`` / ``max_chars`` and the two lines are close
    enough in time (``max_gap``) to feel like one utterance. When a
    merge is *not* allowed, the short segment is kept as-is rather
    than dropped: erasing spoken content is worse than showing a
    brief caption.
    """
    if not segments:
        return []
    result: list[SubtitleSegment] = []
    for seg in segments:
        if result and seg.duration < min_duration:
            prev = result[-1]
            gap_ok = (
                max_gap is None
                or (seg.start - prev.end) <= max_gap + 1e-6
            )
            merged = SubtitleSegment(
                start=prev.start,
                end=max(prev.end, seg.end),
                text=f"{prev.text} {seg.text}",
            )
            over_max = (
                (max_duration is not None and merged.duration > max_duration)
                or (max_chars is not None and len(merged.text) > max_chars)
            )
            if gap_ok and not over_max:
                result[-1] = merged
            else:
                # Merging would create an over-long caption or bridge a
                # big silence — keep the short line on its own instead
                # of deleting it.
                result.append(seg)
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
    condition_on_previous_text: bool = True,
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
        condition_on_previous_text=condition_on_previous_text,
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
    vad_filter: bool | None = None,
    condition_on_previous_text: bool | None = None,
) -> list[Word]:
    """Transcribe ``audio_path`` and return ``list[Word]``.

    ``model_factory`` must accept ``(model_size, device, compute_type)`` and
    return an object exposing ``.transcribe(...)``. If ``None``, the real
    ``faster_whisper.WhisperModel`` is used.

    The ``vad_filter`` and ``condition_on_previous_text`` kwargs, when
    not ``None``, override the (currently absent) defaults on
    ``config``. They are the two fastest wins for *timing accuracy*:

    * ``vad_filter=True`` makes faster-whisper run its built-in
      Silero VAD before decoding, which clips hallucinations to
      non-speech regions and brings word timestamps much closer to
      the actual audio energy.
    * ``condition_on_previous_text=True`` lets the decoder reuse
      the previous segment's text as a soft prompt, which both
      speeds up decoding and reduces the long-tail timing drift
      that bites long videos.
    """
    device = resolve_device(config.device)

    # ---- Apple GPU routing -------------------------------------------
    # CTranslate2 (which faster-whisper wraps) has no Metal/MPS backend
    # and raises ``ValueError: unsupported device mps``, so on Apple
    # Silicon the only way to actually use the GPU is the whisper.cpp
    # CLI, whose ggml Metal backend Homebrew builds for arm64.
    #
    # ``metal`` asks for that backend explicitly. ``mps`` is treated as
    # a synonym rather than an error: it is what a user reaches for on a
    # Mac, and failing with "unsupported device" when a working GPU path
    # exists would be needlessly hostile. ``model_factory`` overrides
    # this (tests inject a fake and must keep the faster-whisper path).
    if device in ("metal", "mps") and model_factory is None:
        from . import transcriber_whispercpp as _wcpp

        return _wcpp.transcribe(
            audio_path,
            config,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )

    compute_type = resolve_compute_type(device, config.compute_type)
    factory = model_factory or _default_model_factory
    model = factory(config.model, device, compute_type)
    return transcribe_with_model(
        model,
        audio_path,
        language=config.language,
        beam_size=config.beam_size,
        vad_filter=True if vad_filter is None else bool(vad_filter),
        condition_on_previous_text=(
            True if condition_on_previous_text is None
            else bool(condition_on_previous_text)
        ),
    )


def _default_model_factory(model_size: str, device: str, compute_type: str) -> Any:
    fw = _import_faster_whisper()
    return fw.WhisperModel(model_size, device=device, compute_type=compute_type)
