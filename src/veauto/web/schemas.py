"""Pydantic request/response schemas for the veauto web API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..models import (
    OutputConfig,
    PipelineConfig,
    SilenceConfig,
    SubtitleConfig,
    SubtitleStyle,
)

# ---------------------------------------------------------------------------
# Job options
# ---------------------------------------------------------------------------


class JobOptions(BaseModel):
    """User-controllable options for a single job.

    These mirror the CLI options of ``veauto run`` but without the
    output / audio-dir / report-path fields — those are managed
    server-side and recorded in the job record.
    """

    # Silence stage
    noise_db: float = Field(default=-30.0, ge=-100.0, le=0.0)
    auto_noise_db: bool = Field(
        default=False,
        description=(
            "Derive the silence threshold from the file's own loudness "
            "profile instead of the fixed noise_db. Recommended for quiet "
            "recordings."
        ),
    )
    noise_db_offset: float = Field(
        default=0.0, ge=-30.0, le=30.0,
        description=(
            "Relative adjustment (dB) applied to the auto-derived "
            "threshold. Ignored in fixed mode."
        ),
    )
    min_silence: float = Field(default=1.5, ge=0.0)
    margin: float = Field(default=0.3, ge=0.0, le=5.0)
    min_keep_seconds: float = Field(
        default=0.15, ge=0.0, le=5.0,
        description=(
            "Drop kept segments shorter than this (removes glitch cuts "
            "between two removed silences)."
        ),
    )
    no_silence: bool = False

    # Subtitle stage
    no_subtitles: bool = False
    subtitle_target: Literal["srt", "fcpxml", "both", "none"] | None = Field(
        default=None,
        description=(
            "Where to put subtitles. 'srt' = SRT only, "
            "'fcpxml' = reserved, 'both' = SRT + FCPXML (default), "
            "'none' = skip STT entirely.  When set, overrides "
            "no_subtitles."
        ),
    )
    model: Literal[
        "tiny", "base", "small", "medium", "large-v3", "distil-large-v3"
    ] = "medium"
    language: str | None = None
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    compute_type: Literal[
        "auto", "int8", "int8_float16", "float16", "float32"
    ] = "auto"
    beam_size: int = Field(default=5, ge=1, le=20)
    subtitle_offset: float = Field(
        default=0.0, ge=-2.0, le=2.0,
        description=(
            "Manual subtitle timing offset in seconds. "
            "Positive = captions appear later, negative = earlier. "
            "Applied after STT and VAD snap."
        ),
    )

    # Style
    style_position: Literal["top", "center", "bottom"] = "bottom"
    style_font: str = "Apple SD Gothic Neo"
    style_font_size: int = Field(default=56, ge=8, le=400)
    style_bold: bool = True
    # Hex colour like "#FFFFFF". Converted to FCPXML "R G B 1" floats
    # in ``to_pipeline_config``.
    style_color: str = "#FFFFFF"
    style_offset_y: int = Field(
        default=0, ge=-540, le=540,
        description="Fine-tune Y position in pixels at 1080p.",
    )
    style_template: Literal["text", "lower_third"] = Field(
        default="text",
        description=(
            "'text' = static title, no fade (default). "
            "'lower_third' = animated Lower Third Text."
        ),
    )
    style_max_chars: int = Field(default=42, ge=5, le=200)
    style_max_lines: int = Field(default=2, ge=1, le=4)
    style_split_sentence: bool = Field(
        default=True,
        description=(
            "Start a new subtitle at sentence boundaries (. ! ? …). "
            "An ellipsis only breaks when the next word does not "
            "follow immediately."
        ),
    )
    style_min_duration: float = Field(default=0.8, ge=0.1)
    style_max_duration: float = Field(default=6.0, ge=0.5)

    @staticmethod
    def _hex_to_fcpxml_color(hex_color: str) -> str:
        """Convert ``#RRGGBB`` to an FCPXML ``"R G B 1"`` float string.

        Unparsable input falls back to white so a bad colour value can
        never fail the whole job.
        """
        import re as _re

        m = _re.fullmatch(r"#?([0-9a-fA-F]{6})", (hex_color or "").strip())
        if not m:
            return "1 1 1 1"
        v = m.group(1)
        r, g, b = (int(v[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
        return f"{r:.4f} {g:.4f} {b:.4f} 1"

    def to_pipeline_config(self) -> PipelineConfig:
        """Translate these options into a :class:`PipelineConfig`."""
        # Resolve subtitle target. `subtitle_target` is the modern field;
        # `no_subtitles` is kept for backwards-compat and maps to "none".
        if self.subtitle_target is not None:
            target = self.subtitle_target
        elif self.no_subtitles:
            target = "none"
        else:
            target = "both"

        return PipelineConfig(
            silence=SilenceConfig(
                enabled=not self.no_silence,
                noise_db=self.noise_db,
                auto_noise_db=self.auto_noise_db,
                noise_db_offset=self.noise_db_offset,
                min_silence=self.min_silence,
                margin=self.margin,
                min_keep_seconds=self.min_keep_seconds,
            ),
            subtitle=SubtitleConfig(
                target=target,
                # `enabled` = "should STT run?". The model's
                # `stt_enabled` property treats `target="none"` OR
                # `enabled=False` as "skip". For `target="fcpxml"` the
                # user has opted out of STT (captions are reserved /
                # manually authored) so we also set enabled=False.
                enabled=target in ("srt", "both"),
                model=self.model,
                language=self.language,
                device=self.device,
                compute_type=self.compute_type,
                beam_size=self.beam_size,
                offset=self.subtitle_offset,
                style=SubtitleStyle(
                    position=self.style_position,
                    offset_y=self.style_offset_y,
                    template=self.style_template,
                    font=self.style_font,
                    font_size=self.style_font_size,
                    bold=self.style_bold,
                    color=self._hex_to_fcpxml_color(self.style_color),
                    max_chars_per_line=self.style_max_chars,
                    max_lines=self.style_max_lines,
                    split_on_sentence=self.style_split_sentence,
                    min_duration=self.style_min_duration,
                    max_duration=self.style_max_duration,
                ),
            ),
            output=OutputConfig(),
        )


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


class JobCreateResponse(BaseModel):
    """Response to ``POST /api/jobs``."""

    id: str
    status: str


class JobRecord(BaseModel):
    """Server-side job state, returned by ``GET /api/jobs/{id}`` and broadcast
    over the WebSocket as ``state`` messages."""

    id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    input_name: str
    input_size: int
    input_mime: str | None = None
    input_duration: float | None = None
    options: JobOptions = Field(default_factory=JobOptions)
    progress: float = 0.0
    stage: str = "queued"
    message: str = ""
    # --- Failure diagnostics -----------------------------------------
    # ``error`` is a one-line human summary safe for a badge/tooltip.
    # ``error_traceback`` is the full stack trace (multi-line) for a
    # detail view in the UI, and ``error_stage`` records which pipeline
    # stage the worker was in when the failure happened. ``error_kind``
    # distinguishes a clean cancel from an exception so the UI can show
    # "cancelled by user" instead of "failed" when appropriate.
    error: str | None = None
    error_kind: Literal["exception", "cancelled", "timeout", "unknown"] | None = None
    error_stage: str | None = None
    error_traceback: str | None = None
    # ---------------------------------------------------------------
    fcpxml_name: str | None = None
    report_md_name: str | None = None
    report_json_name: str | None = None
    srt_name: str | None = None
    error_log_name: str | None = None
    # Statistics filled in at the end
    num_silences: int | None = None
    num_cuts: int | None = None
    num_subtitles: int | None = None
    kept_duration: float | None = None
    removed_duration: float | None = None
    # Computed at serialisation time — see ``_attach_urls``.
    fcpxml_url: str | None = None
    report_md_url: str | None = None
    report_json_url: str | None = None
    srt_url: str | None = None
    error_log_url: str | None = None

    def with_download_urls(self) -> JobRecord:
        """Return a shallow copy whose ``*_url`` fields point at this
        server's download endpoints. The base ``/api`` path is the same
        regardless of how the SPA was opened (``localhost`` or
        ``127.0.0.1``) because the browser resolves it against its own
        origin.

        Setting the URLs on a copy avoids mutating the canonical record
        that other WS subscribers might still be looking at.
        """
        base = "/api/jobs/" + self.id + "/download/"
        return self.model_copy(
            update={
                "fcpxml_url": base + self.fcpxml_name if self.fcpxml_name else None,
                "report_md_url": base + self.report_md_name if self.report_md_name else None,
                "report_json_url": base + self.report_json_name if self.report_json_name else None,
                "srt_url": base + self.srt_name if self.srt_name else None,
                "error_log_url": base + self.error_log_name if self.error_log_name else None,
            }
        )


# ---------------------------------------------------------------------------
# WebSocket protocol
# ---------------------------------------------------------------------------


class WSMessage(BaseModel):
    """Envelope for all WebSocket messages in both directions.

    The schema is a tagged union on ``type``. We don't use Pydantic
    discriminators for simplicity — handlers switch on ``type``.
    """

    type: str
    payload: dict | None = None
