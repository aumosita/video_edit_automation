"""Unit tests for PipelineConfig YAML (de)serialisation (P3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from veauto.models import (
    PipelineConfig,
    SilenceConfig,
    SubtitleConfig,
    SubtitleStyle,
)

# ---------------------------------------------------------------------------
# from_yaml_string / to_yaml round-trip
# ---------------------------------------------------------------------------


class TestYamlRoundTrip:
    def test_default_round_trip(self):
        cfg = PipelineConfig()
        text = cfg.to_yaml()
        cfg2 = PipelineConfig.from_yaml_string(text)
        assert cfg == cfg2

    def test_customised_round_trip(self):
        cfg = PipelineConfig(
            silence=SilenceConfig(noise_db=-25.0, min_silence=2.0, margin=0.3),
            subtitle=SubtitleConfig(
                model="tiny",
                language="ko",
                device="mps",
                style=SubtitleStyle(position="top", font_size=64),
            ),
        )
        text = cfg.to_yaml()
        cfg2 = PipelineConfig.from_yaml_string(text)
        assert cfg == cfg2
        assert cfg2.subtitle.style.position == "top"
        assert cfg2.subtitle.style.font_size == 64
        assert cfg2.subtitle.style.shadow_offset == (2.0, 3.0)

    def test_to_yaml_is_unicode_safe(self):
        cfg = PipelineConfig(subtitle=SubtitleConfig(language="ko"))
        text = cfg.to_yaml()
        assert "ko" in text
        # Korean characters in the report or path should pass through.
        cfg2 = PipelineConfig.from_yaml_string(text)
        assert cfg2.subtitle.language == "ko"

    def test_to_yaml_with_sort_keys(self):
        cfg = PipelineConfig()
        a = cfg.to_yaml(sort_keys=True)
        b = cfg.to_yaml(sort_keys=False)
        # Both should parse to the same model
        assert PipelineConfig.from_yaml_string(a) == PipelineConfig.from_yaml_string(b)

    def test_write_yaml_creates_parent(self, tmp_path: Path):
        cfg = PipelineConfig()
        target = tmp_path / "subdir" / "cfg.yaml"
        cfg.write_yaml(target)
        assert target.exists()
        assert target.parent.is_dir()

    def test_from_yaml_file(self, tmp_path: Path):
        cfg = PipelineConfig(subtitle=SubtitleConfig(model="base"))
        path = tmp_path / "c.yaml"
        cfg.write_yaml(path)
        loaded = PipelineConfig.from_yaml(path)
        assert loaded == cfg

    def test_unknown_key_is_silently_ignored_by_default(self, tmp_path: Path):
        # Pydantic v2 default for BaseModel is extra='ignore'.
        path = tmp_path / "bad.yaml"
        path.write_text("silence:\n  noise_db: -20\nbogus_field: 1\n")
        # Should not raise — unknown keys are ignored.
        cfg = PipelineConfig.from_yaml(path)
        assert cfg.silence.noise_db == -20.0

    def test_invalid_yaml_scalar_type_raises(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text("silence:\n  noise_db: not-a-number\n")
        with pytest.raises(ValidationError):
            PipelineConfig.from_yaml(path)

    def test_non_mapping_root_raises_value_error(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            PipelineConfig.from_yaml_string("- a\n- b\n")

    def test_empty_yaml_returns_defaults(self):
        cfg = PipelineConfig.from_yaml_string("")
        assert cfg == PipelineConfig()
        cfg2 = PipelineConfig.from_yaml_string("{}")
        assert cfg2 == PipelineConfig()


# ---------------------------------------------------------------------------
# Partial overrides (only some blocks provided)
# ---------------------------------------------------------------------------


class TestYamlPartialConfig:
    def test_only_silence_block(self, tmp_path: Path):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump({"silence": {"noise_db": -20}}))
        cfg = PipelineConfig.from_yaml(path)
        assert cfg.silence.noise_db == -20.0
        # Others are defaults
        assert cfg.subtitle.model == "medium"

    def test_only_subtitle_block(self, tmp_path: Path):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump({"subtitle": {"model": "tiny"}}))
        cfg = PipelineConfig.from_yaml(path)
        assert cfg.subtitle.model == "tiny"
        assert cfg.silence.noise_db == -30.0

    def test_nested_style_override(self, tmp_path: Path):
        path = tmp_path / "c.yaml"
        path.write_text(
            yaml.safe_dump(
                {"subtitle": {"style": {"position": "top", "font_size": 96}}}
            )
        )
        cfg = PipelineConfig.from_yaml(path)
        assert cfg.subtitle.style.position == "top"
        assert cfg.subtitle.style.font_size == 96
        # Untouched fields keep their defaults
        assert cfg.subtitle.style.max_chars_per_line == 42
