"""Pydantic data models for veauto.

These models represent the intermediate data structures used throughout the
pipeline. They are independent from the FCPXML XML output format and can be
serialized for debugging or testing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# FCPXML uses rational time values like "1001/24000s" (numerator/denominator s).
# We work in *seconds* internally and only convert to rational at serialization
# time (see fcpxml_builder.py).


class SilenceInterval(BaseModel):
    """A detected silent region inside the source media."""

    start: float = Field(ge=0.0, description="Start time in seconds")
    end: float = Field(gt=0.0, description="End time in seconds")

    @property
    def duration(self) -> float:
        return self.end - self.start

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        start = info.data.get("start", 0.0)
        if v <= start:
            raise ValueError(f"end ({v}) must be > start ({start})")
        return v


class Word(BaseModel):
    """A single transcribed word with timing information."""

    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    text: str = Field(min_length=1)
    probability: float = Field(default=1.0, ge=0.0, le=1.0)


class SubtitleSegment(BaseModel):
    """A user-facing subtitle line (one or more words)."""

    start: float = Field(ge=0.0)
    end: float = Field(gt=0.0)
    text: str = Field(min_length=1)

    @property
    def duration(self) -> float:
        return self.end - self.start


class MediaInfo(BaseModel):
    """Lightweight metadata about the input media file."""

    path: Path
    duration: float = Field(gt=0.0, description="Total duration in seconds")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: float = Field(gt=0.0, description="Frames per second (e.g. 30.0)")
    audio_stream_index: int = Field(default=0, ge=0)
    has_audio: bool = True

    @property
    def frame_duration_seconds(self) -> float:
        return 1.0 / self.frame_rate


class CutSegment(BaseModel):
    """A kept (non-silent) segment in the *source* timeline."""

    source_in: float = Field(ge=0.0, description="Start time in source media (seconds)")
    source_out: float = Field(gt=0.0, description="End time in source media (seconds)")

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in


class RemovedSilence(BaseModel):
    """A removed silence gap, kept only for debugging / reporting."""

    source_in: float
    source_out: float

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in


class VoiceRange(BaseModel):
    """A non-silent region in the source media (VAD output).

    Returned by :func:`veauto.silence.detect_voice_ranges`. The
    pipeline uses these to snap subtitle timestamps onto real
    audio onsets, which fixes the residual 100-300 ms drift that
    faster-whisper leaves behind.
    """

    source_in: float
    source_out: float

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in


SubtitlePosition = Literal["top", "center", "bottom"]


class SubtitleStyle(BaseModel):
    """Visual style applied to all generated subtitle titles."""

    position: SubtitlePosition = "bottom"
    # FCP title template used for the captions. ``text`` is the plain
    # static "Basic Text > Text" template — no built-in animation.
    # ``lower_third`` is Apple's "Lower Third Text", which has a
    # built-in fade-in/out animation.
    template: Literal["text", "lower_third"] = Field(
        default="text",
        description=(
            "'text' = static title with no fade (default). "
            "'lower_third' = Apple's animated Lower Third Text."
        ),
    )
    offset_y: int = Field(
        default=0,
        description=(
            "Fine-tune Y position in pixels at 1080p. Negative on bottom moves subtitles up."
        ),
    )
    font: str = "Apple SD Gothic Neo"
    font_size: int = Field(default=56, ge=8, le=400)
    bold: bool = True
    italic: bool = False
    color: str = Field(default="1 1 1 1", description="RGBA in FCPXML 0..1 floats")
    outline_color: str = Field(default="0 0 0 1")
    outline_width: float = Field(default=3.5, ge=0.0)
    shadow_color: str = Field(default="0 0 0 0.65")
    shadow_offset: tuple[float, float] = Field(default=(2.0, 3.0))
    shadow_blur: float = Field(default=8.0, ge=0.0)
    max_chars_per_line: int = Field(default=42, ge=5, le=200)
    max_lines: int = Field(default=2, ge=1, le=4)
    min_duration: float = Field(default=0.8, ge=0.1, description="Minimum display seconds")
    max_duration: float = Field(default=6.0, ge=0.5, description="Maximum display seconds")

    def to_text_style_xml_attrs(self) -> dict[str, str]:
        """Return attributes for the ``<text-style>`` element.

        Only attributes defined in the FCPXML 1.10 DTD are emitted.
        The legacy ``relativeTo`` / ``verticalAnchor`` / ``horizontalAnchor``
        (Motion-only) attributes are NOT included because they trip
        Final Cut Pro's DTD validation on import.

        ``strokeWidth`` sign convention
        -------------------------------
        In Final Cut Pro / Motion the sign of ``strokeWidth`` selects
        *where* the stroke is drawn:

        * **negative** → stroke grows **outward** from the glyph edge
          (what a subtitle outline is meant to be), and
        * **positive** → stroke grows **inward**, painting over the
          glyph's own fill.

        We used to emit the raw positive width, so a 3.5 pt black
        stroke ate most of the stem of 56 pt Apple SD Gothic Neo Bold
        and the captions rendered as **black text with a thin white
        sliver** instead of white text with a black outline.

        ``outline_width`` therefore stays a positive, user-facing
        thickness and is negated here.
        """
        attrs: dict[str, str] = {
            "font": self.font,
            "fontSize": str(self.font_size),
            "fontColor": self.color,
            "strokeColor": self.outline_color,
            "strokeWidth": str(-abs(self.outline_width)),
            "shadowColor": self.shadow_color,
            "shadowOffset": f"{self.shadow_offset[0]} {self.shadow_offset[1]}",
            "shadowBlurRadius": str(self.shadow_blur),
            "alignment": "center",
        }
        if self.bold:
            attrs["bold"] = "1"
        if self.italic:
            attrs["italic"] = "1"
        return attrs


class SilenceConfig(BaseModel):
    """Configuration for silence detection / removal."""

    noise_db: float = Field(default=-30.0, description="Silence threshold in dB (e.g. -30)")
    auto_noise_db: bool = Field(
        default=False,
        description=(
            "Derive the silence threshold from the file's own loudness "
            "profile instead of the fixed noise_db. Recommended for quiet "
            "recordings whose speech level sits close to the fixed threshold."
        ),
    )
    noise_headroom_db: float = Field(
        default=12.0,
        ge=1.0,
        le=40.0,
        description=(
            "When auto_noise_db is on: gap kept below the measured speech "
            "level. Larger = more aggressive silence cuts."
        ),
    )
    noise_db_offset: float = Field(
        default=0.0,
        ge=-30.0,
        le=30.0,
        description=(
            "Relative adjustment (dB) applied to the resolved threshold. "
            "Only used when auto_noise_db is on: negative keeps more audio "
            "(less aggressive), positive cuts more aggressively."
        ),
    )
    min_silence: float = Field(default=1.5, ge=0.1, description="Minimum silence length to cut (s)")
    margin: float = Field(default=0.3, ge=0.0, description="Padding kept on each side of cut (s)")
    min_keep_seconds: float = Field(
        default=0.15,
        ge=0.0,
        description=(
            "Kept segments shorter than this are dropped. Between two removed "
            "silences such tiny clips survive as sub-quarter-second 'glitch' cuts; "
            "dropping them keeps the edit looking intentional."
        ),
    )
    enabled: bool = True


class SubtitleConfig(BaseModel):
    """Configuration for STT and subtitle generation.

    The ``target`` field picks where the resulting subtitles end up:

    * ``"srt"``     – STT runs, SRT is produced, FCPXML stays caption-free.
    * ``"fcpxml"``  – STT is skipped, but the user can still author a
      subtitle ``.srt`` themselves and bake it into FCPXML via a
      downstream step.  (Not currently exposed; reserved.)
    * ``"both"``    – STT runs and the SRT is also embedded in the
      FCPXML.  Default.
    * ``"none"``    – STT is skipped and FCPXML has no captions.  Same
      as the legacy ``--no-subtitles`` flag.

    The ``enabled`` flag is kept for backwards-compat / external code:
    it is treated as the STT gate (``target != "none"``).
    """

    target: Literal["srt", "fcpxml", "both", "none"] = Field(
        default="both",
        description=(
            "Where to put subtitles. 'srt' = SRT only, 'fcpxml' = "
            "reserved (FCPXML only, no STT), 'both' = SRT + FCPXML, "
            "'none' = skip everything."
        ),
    )
    enabled: bool = True
    model: Literal["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"] = "medium"
    language: str | None = Field(
        default=None,
        description="ISO 639-1 code (e.g. 'ko', 'en'). None = auto-detect.",
    )
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    beam_size: int = Field(default=5, ge=1, le=20)
    offset: float = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        description=(
            "Manual subtitle timing offset in seconds. "
            "Positive values make captions appear later (useful when "
            "the source video's audio leads the video track by a known "
            "amount); negative values make them appear earlier. "
            "Applied as the final step in the pipeline, after STT and "
            "after the VAD-based snap-to-voice correction."
        ),
    )
    style: SubtitleStyle = Field(default_factory=SubtitleStyle)

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def stt_enabled(self) -> bool:
        """Run STT at all.

        Honoured when **either** ``target`` is explicitly "none" (the
        modern "skip everything" signal) or the legacy ``enabled`` flag
        is False.  When the caller sets ``target`` to a non-"none" value
        and leaves ``enabled`` alone, ``target`` wins.
        """
        if self.target == "none":
            return False
        return self.enabled

    @property
    def in_fcpxml(self) -> bool:
        """Whether generated subtitles are baked into the FCPXML.

        Requires STT to have run (``stt_enabled``) — you can't bake
        captions that don't exist.  Also requires ``target`` to opt in
        to the FCPXML side (``fcpxml`` or ``both``).
        """
        if not self.stt_enabled:
            return False
        return self.target in ("fcpxml", "both")


class OutputConfig(BaseModel):
    """Output options."""

    fps: float | None = Field(
        default=None,
        description="Output frame rate. If None, inferred from source.",
    )
    project_name: str = "Auto Edit"
    event_name: str = "veauto"
    write_report: bool = True


class PipelineConfig(BaseModel):
    """Top-level configuration combining all stages."""

    silence: SilenceConfig = Field(default_factory=SilenceConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    keep_temp: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> PipelineConfig:
        """Load a YAML config file and return a PipelineConfig instance.

        An empty / missing ``silence`` or ``subtitle`` block is allowed; the
        corresponding sub-config will use its defaults. Unknown keys are
        silently ignored (Pydantic default ``extra='ignore'``); invalid
        *types* still raise a :class:`pydantic.ValidationError`.
        """

        text = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml_string(text)

    @classmethod
    def from_yaml_string(cls, text: str) -> PipelineConfig:
        """Parse a YAML string into a :class:`PipelineConfig`."""
        import yaml

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"PipelineConfig YAML must be a mapping, got {type(data).__name__}"
            )
        return cls.model_validate(data)

    def to_yaml(self, *, sort_keys: bool = False) -> str:
        """Serialize this config to a YAML string.

        Parameters
        ----------
        sort_keys:
            If True, keys are sorted alphabetically (useful for stable
            diffs in tests and version control). Defaults to False to
            preserve the natural field order.
        """
        import yaml

        data = self.model_dump(mode="json")
        return yaml.safe_dump(
            data,
            sort_keys=sort_keys,
            allow_unicode=True,
            default_flow_style=False,
        )

    def write_yaml(self, path: Path, *, sort_keys: bool = False) -> None:
        """Write this config to a YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_yaml(sort_keys=sort_keys), encoding="utf-8")



