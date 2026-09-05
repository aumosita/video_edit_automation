"""End-to-end pipeline: silence removal + subtitle generation.

This module orchestrates the stages used by the ``veauto run`` subcommand:

1. Probe the input media (duration, dimensions, frame rate, audio).
2. Detect silences via :mod:`veauto.silence`.
3. Build cut segments via :mod:`veauto.segments`.
4. (Optionally) extract audio via :mod:`veauto.audio`.
5. (Optionally) transcribe via :mod:`veauto.transcriber`.
6. (Optionally) group words into subtitle lines.
7. Re-base subtitles onto the cut timeline.
8. Render an FCPXML via :mod:`veauto.fcpxml_builder`.

The driver function (``run_pipeline``) performs I/O. The pure helpers
(``remap_subtitles``) are unit-testable without ffmpeg or faster-whisper.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .audio import extract_audio
from .fcpxml_builder import build_fcpxml
from .models import (
    CutSegment,
    MediaInfo,
    PipelineConfig,
    RemovedSilence,
    SilenceConfig,
    SubtitleSegment,
    Word,
)
from .segments import build_cut_segments
from .silence import detect_silence, probe_media_info
from .transcriber import (
    transcribe as _transcribe,
)
from .transcriber import (
    words_to_subtitle_segments,
)

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """The full output of :func:`run_pipeline`."""

    media: MediaInfo
    cuts: list[CutSegment] = field(default_factory=list)
    removed: list[RemovedSilence] = field(default_factory=list)
    words: list[Word] = field(default_factory=list)
    subtitles: list[SubtitleSegment] = field(default_factory=list)
    audio_path: Path | None = None
    fcpxml: str = ""

    @property
    def total_duration(self) -> float:
        return self.media.duration

    @property
    def kept_duration(self) -> float:
        return sum(c.duration for c in self.cuts)

    @property
    def removed_duration(self) -> float:
        return sum(r.duration for r in self.removed)

    def to_report_data(self) -> dict:
        """Return a JSON-serialisable dict describing this run.

        This is a thin convenience wrapper around
        :func:`veauto.report.build_report_data` so that callers do not
        have to import the report module to get the same data.
        """
        from .report import build_report_data

        return build_report_data(self)


def remap_subtitles(
    cuts: list[CutSegment],
    subtitles: list[SubtitleSegment],
) -> list[SubtitleSegment]:
    """Re-base subtitles from the source timeline to the cut timeline.

    For every subtitle, find the cut segment it falls into (by source
    times) and shift it to the new (compacted) timeline. The relative
    order is preserved.

    Subtitles that start in a removed silence are **dropped**. Subtitles
    that extend past the end of a cut are **clipped** to the cut's end.

    Parameters
    ----------
    cuts:
        The kept segments in the source timeline.
    subtitles:
        The subtitle lines, timed against the source media.

    Returns
    -------
    list[SubtitleSegment]
        Subtitles re-timed against the compacted timeline.
    """
    if not cuts or not subtitles:
        return []

    cuts_sorted = sorted(cuts, key=lambda c: c.source_in)
    # Cumulative offset for each cut's start in the new timeline.
    cumulative: list[float] = []
    running = 0.0
    for c in cuts_sorted:
        cumulative.append(running)
        running += c.duration

    remapped: list[SubtitleSegment] = []
    for sub in subtitles:
        target_idx: int | None = None
        offset_in_cut = 0.0
        for idx, c in enumerate(cuts_sorted):
            if c.source_in <= sub.start < c.source_out:
                target_idx = idx
                offset_in_cut = sub.start - c.source_in
                break
        if target_idx is None:
            # Subtitle starts in a removed silence → drop.
            continue

        target_cut = cuts_sorted[target_idx]
        sub_dur = max(0.0, sub.end - sub.start)
        remaining = target_cut.duration - offset_in_cut
        clipped_dur = min(sub_dur, remaining)
        if clipped_dur <= 0:
            continue

        new_start = cumulative[target_idx] + offset_in_cut
        new_end = new_start + clipped_dur
        remapped.append(
            SubtitleSegment(
                start=new_start,
                end=new_end,
                text=sub.text,
            )
        )
    return remapped


def _cleanup_audio(audio_path: Path | None, *, keep: bool) -> None:
    """Remove the temporary WAV file unless the user opted in to keep it."""
    if audio_path is None or keep:
        return
    try:
        audio_path.unlink(missing_ok=True)
        logger.debug("Removed temp audio: %s", audio_path)
    except OSError as exc:  # pragma: no cover - filesystem errors
        logger.warning("Failed to remove temp audio %s: %s", audio_path, exc)


def run_pipeline(
    input_path: Path,
    config: PipelineConfig,
    *,
    transcriber: Callable[..., list[Word]] | None = None,
) -> PipelineResult:
    """Run the full pipeline and return a :class:`PipelineResult`.

    Parameters
    ----------
    input_path:
        The source media file.
    config:
        The combined pipeline configuration.
    transcriber:
        Optional override for the transcribe function. Used by tests to
        inject a fake. Must have the same signature as
        :func:`veauto.transcriber.transcribe`.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input media not found: {input_path}")

    result = PipelineResult(media=probe_media_info(input_path))

    # 1. Silence detection & cut segments
    if config.silence.enabled:
        silences = detect_silence(input_path, config.silence)
        cuts, removed = build_cut_segments(
            result.media.duration,
            silences,
            margin=config.silence.margin,
            min_keep_seconds=config.silence.min_keep_seconds,
        )
        result.cuts = cuts
        result.removed = removed

    # 2. Subtitle generation
    audio_path: Path | None = None
    if config.subtitle.stt_enabled:
        if not result.media.has_audio:
            logger.warning("Source has no audio; skipping subtitle generation")
        else:
            # Build a temp audio path next to the input so the WAV lives
            # somewhere predictable (caller / web layer can override).
            import tempfile

            with tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, dir=Path(input_path).parent
            ) as tmp:
                tmp_path = Path(tmp.name)
            audio_path = extract_audio(input_path, tmp_path)
            result.audio_path = audio_path

            t = transcriber or _transcribe
            # B1: enable the two faster-whisper flags that most
            # improve *timing* accuracy: VAD prunes hallucinations
            # to speech regions, and conditioning on the previous
            # segment reduces long-tail drift.
            words = t(
                audio_path,
                config.subtitle,
                vad_filter=True,
                condition_on_previous_text=True,
            )
            result.words = words
            result.subtitles = words_to_subtitle_segments(
                words,
                max_chars_per_line=config.subtitle.style.max_chars_per_line,
                max_lines=config.subtitle.style.max_lines,
                min_duration=config.subtitle.style.min_duration,
                max_duration=config.subtitle.style.max_duration,
                split_on_sentence=config.subtitle.style.split_on_sentence,
            )

            # B2: snap every subtitle to the nearest real audio
            # onset / offset so the captions land on the actual
            # speech, not on faster-whisper's 100-300 ms drift.
            # The snap window is 0.7 s, which reliably absorbs
            # faster-whisper's well-known "early onset" tendency
            # (the decoder fires word starts 100-500 ms before the
            # true audio energy rises) while still rejecting
            # genuine STT errors.
            try:
                from .silence import (
                    detect_voice_ranges,
                    shift_subtitle_timestamps,
                )
                # Use a *finer* min-silence than the cutting stage so
                # voice ranges split at short pauses too. With the
                # cutting-stage 1.5 s threshold the ranges are coarse
                # blobs and the snap step below can only correct gross
                # errors; 0.5 s gives it word-gap resolution.
                voice = detect_voice_ranges(
                    input_path,
                    SilenceConfig(
                        noise_db=config.silence.noise_db,
                        auto_noise_db=config.silence.auto_noise_db,
                        noise_headroom_db=config.silence.noise_headroom_db,
                        noise_db_offset=config.silence.noise_db_offset,
                        min_silence=min(config.silence.min_silence, 0.5),
                        enabled=True,
                    ),
                    total_duration=result.media.duration,
                )
                shift_subtitle_timestamps(result.subtitles, voice)
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.debug("voice-snap failed, using STT times: %s", exc)

            # B4: apply the user-controlled manual timing offset.
            # Done *after* the VAD snap so the offset is interpreted
            # in absolute terms, not "relative to the VAD edge".
            if config.subtitle.offset:
                for sub in result.subtitles:
                    sub.start = max(0.0, sub.start + config.subtitle.offset)
                    sub.end = max(sub.start + 0.01, sub.end + config.subtitle.offset)

    # 3. Render FCPXML (must use ORIGINAL-timeline subtitles so
    # the per-cut pairing in fcpxml_builder sees matching
    # source_in / source_out vs subtitle.start / subtitle.end).
    # The remap for the report runs *after* this so the report
    # shows the user-facing compacted timeline.
    #
    # `target="srt"` keeps the SRT but drops subtitles from the
    # FCPXML; `target="none"` runs no STT so there are none anyway.
    fcpxml_subs = (
        result.subtitles
        if (result.subtitles and config.subtitle.in_fcpxml)
        else None
    )
    result.fcpxml = build_fcpxml(
        result.media,
        result.cuts,
        subtitles=fcpxml_subs,
        subtitle_style=config.subtitle.style if fcpxml_subs else None,
        project_name=config.output.project_name,
        event_name=config.output.event_name,
    )

    # After the FCPXML is built, re-time the subtitles onto the
    # compacted timeline for the human-readable report.
    if result.cuts and result.subtitles:
        result.subtitles = remap_subtitles(result.cuts, result.subtitles)

    # 4. Cleanup temp audio
    _cleanup_audio(audio_path, keep=config.keep_temp)
    return result

