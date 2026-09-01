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


SubtitlePosition = Literal["top", "center", "bottom"]


class SubtitleStyle(BaseModel):
    """Visual style applied to all generated subtitle titles."""

    position: SubtitlePosition = "bottom"
    offset_y: int = Field(
        default=0,
        description=(
            "Fine-tune Y position in pixels at 1080p. Negative on bottom moves subtitles up."
        ),
    )
    font: str = "Apple SD Gothic Neo"
    font_size: int = Field(default=48, ge=8, le=400)
    bold: bool = True
    italic: bool = False
    color: str = Field(default="1 1 1 1", description="RGBA in FCPXML 0..1 floats")
    outline_color: str = Field(default="0 0 0 1")
    outline_width: float = Field(default=2.0, ge=0.0)
    shadow_color: str = Field(default="0 0 0 0.5")
    shadow_offset: tuple[float, float] = Field(default=(2.0, -2.0))
    shadow_blur: float = Field(default=2.0, ge=0.0)
    max_chars_per_line: int = Field(default=42, ge=5, le=200)
    max_lines: int = Field(default=2, ge=1, le=4)
    min_duration: float = Field(default=0.8, ge=0.1, description="Minimum display seconds")
    max_duration: float = Field(default=6.0, ge=0.5, description="Maximum display seconds")

    def to_text_style_xml_attrs(self) -> dict[str, str]:
        """Return attributes for the <text-style> element in FCPXML 1.10."""
        attrs: dict[str, str] = {
            "font": self.font,
            "fontSize": str(self.font_size),
            "fontColor": self.color,
            "strokeColor": self.outline_color,
            "strokeWidth": str(self.outline_width),
            "shadowColor": self.shadow_color,
            "shadowOffset": f"{self.shadow_offset[0]} {self.shadow_offset[1]}",
            "shadowBlurRadius": str(self.shadow_blur),
        }
        if self.bold:
            attrs["bold"] = "1"
        if self.italic:
            attrs["italic"] = "1"
        return attrs


class SilenceConfig(BaseModel):
    """Configuration for silence detection / removal."""

    noise_db: float = Field(default=-30.0, description="Silence threshold in dB (e.g. -30)")
    min_silence: float = Field(default=1.5, ge=0.1, description="Minimum silence length to cut (s)")
    margin: float = Field(default=0.2, ge=0.0, description="Padding kept on each side of cut (s)")
    enabled: bool = True


class SubtitleConfig(BaseModel):
    """Configuration for STT and subtitle generation."""

    enabled: bool = True
    model: Literal["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"] = "medium"
    language: str | None = Field(
        default=None,
        description="ISO 639-1 code (e.g. 'ko', 'en'). None = auto-detect.",
    )
    device: Literal["auto", "cpu", "cuda", "mps"] = "auto"
    compute_type: Literal["auto", "int8", "int8_float16", "float16", "float32"] = "auto"
    beam_size: int = Field(default=5, ge=1, le=20)
    style: SubtitleStyle = Field(default_factory=SubtitleStyle)


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
        """Load a YAML config file and return a PipelineConfig instance."""
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)



