"""Integration tests for `veauto run` with P3 (YAML config) and P4 (reports)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from veauto import pipeline as pipeline_mod
from veauto.cli import app
from veauto.models import (
    MediaInfo,
    SubtitleConfig,
    Word,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_probe(monkeypatch, **kwargs: Any) -> MediaInfo:
    media = MediaInfo(
        path=Path("/tmp/in.mp4"),
        duration=kwargs.get("duration", 10.0),
        width=1920,
        height=1080,
        frame_rate=30.0,
        has_audio=kwargs.get("has_audio", True),
    )
    # Pipeline module imported the names; patch there.
    monkeypatch.setattr(pipeline_mod, "probe_media_info", lambda *a, **kw: media)
    return media


def _stub_silence(monkeypatch, silences=()):
    def _fake(*a, **kw):
        return list(silences)
    monkeypatch.setattr(pipeline_mod, "detect_silence", _fake)


def _stub_extract_audio(monkeypatch, tmp_path: Path):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(pipeline_mod, "extract_audio", lambda *a, **kw: wav)
    return wav


def _stub_transcriber(monkeypatch, words=()):
    def _fake(audio_path, config: SubtitleConfig, **_):
        return list(words)
    # Pipeline's internal _transcribe is the symbol run_pipeline calls
    # (it's bound at module import time to the original `transcribe`).
    monkeypatch.setattr(pipeline_mod, "_transcribe", _fake)


def _write_input(tmp_path: Path) -> Path:
    p = tmp_path / "in.mp4"
    p.write_bytes(b"fake")
    return p


# ---------------------------------------------------------------------------
# --config: YAML defaults are loaded
# ---------------------------------------------------------------------------


class TestRunWithConfigFile:
    def test_config_file_loads_defaults(
        self, monkeypatch, tmp_path: Path
    ):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "silence": {"noise_db": -25.0, "min_silence": 2.0, "margin": 0.3},
                    "subtitle": {"model": "tiny", "language": "ko"},
                }
            )
        )
        _stub_probe(monkeypatch, duration=10.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch)

        out = tmp_path / "out.fcpxml"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--config", str(cfg_file),
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "Loaded config" in result.stdout
        # The default report path (output.fcpxml + ".report.md") should exist
        # because OutputConfig.write_report is True by default.
        default_report = out.with_name(out.name + ".report.md")
        assert default_report.exists()

    def test_cli_options_override_yaml(
        self, monkeypatch, tmp_path: Path
    ):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {"silence": {"noise_db": -20.0}, "subtitle": {"model": "tiny"}}
            )
        )
        _stub_probe(monkeypatch, duration=10.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch)

        out = tmp_path / "out.fcpxml"
        effective = tmp_path / "effective.yaml"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--config", str(cfg_file),
                "--noise-db", "-40.0",  # override YAML value
                # Dump the merged config so the assertion inspects real
                # values instead of rich-rendered console text (which
                # wraps differently depending on terminal width).
                "--write-config", str(effective),
            ],
        )
        assert result.exit_code == 0, result.stdout
        merged = yaml.safe_load(effective.read_text(encoding="utf-8"))
        # The CLI flag must win over the YAML file's -20.0. (The CLI
        # applies *all* of its options over the config file, so the
        # file acts purely as a defaults layer.)
        assert merged["silence"]["noise_db"] == -40.0

    def test_invalid_config_exits_with_code_2(
        self, monkeypatch, tmp_path: Path
    ):
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("silence:\n  noise_db: not-a-float\n")
        _stub_probe(monkeypatch)
        out = tmp_path / "out.fcpxml"

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--config", str(cfg_file),
            ],
        )
        assert result.exit_code == 2
        assert "Invalid config" in result.stdout


# ---------------------------------------------------------------------------
# --write-config: dump effective config to YAML, exit early
# ---------------------------------------------------------------------------


class TestWriteConfig:
    def test_write_config_creates_yaml_and_exits(
        self, monkeypatch, tmp_path: Path
    ):
        _stub_probe(monkeypatch)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch)

        out = tmp_path / "out.fcpxml"
        cfg_out = tmp_path / "effective.yaml"

        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--write-config", str(cfg_out),
                "--model", "tiny",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert cfg_out.exists()
        loaded = yaml.safe_load(cfg_out.read_text(encoding="utf-8"))
        assert loaded["subtitle"]["model"] == "tiny"
        # Pipeline should not have run — out.fcpxml must NOT exist.
        assert not out.exists()


# ---------------------------------------------------------------------------
# --report: JSON / Markdown
# ---------------------------------------------------------------------------


class TestRunWithReport:
    def test_markdown_report_written(self, monkeypatch, tmp_path: Path):
        _stub_probe(monkeypatch, duration=10.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(
            monkeypatch,
            words=[
                Word(start=0.0, end=0.5, duration=0.5, text="hello"),
                Word(start=0.5, end=1.0, duration=0.5, text="world"),
                Word(start=1.0, end=2.0, duration=1.0, text="foo"),
            ],
        )

        out = tmp_path / "out.fcpxml"
        report = tmp_path / "report.md"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--report", str(report),
                "--language", "en",
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert report.exists()
        text = report.read_text(encoding="utf-8")
        assert "# veauto report" in text
        assert "## Subtitles" in text

    def test_json_report_written(self, monkeypatch, tmp_path: Path):
        _stub_probe(monkeypatch, duration=10.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch)

        out = tmp_path / "out.fcpxml"
        report = tmp_path / "r.json"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
                "--report", str(report),
                "--report-format", "json",
            ],
        )
        assert result.exit_code == 0, result.stdout
        data = json.loads(report.read_text(encoding="utf-8"))
        assert "silence_removal" in data
        assert "subtitles" in data

    def test_report_written_automatically_when_configured(
        self, monkeypatch, tmp_path: Path
    ):
        _stub_probe(monkeypatch, duration=10.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch)

        out = tmp_path / "out.fcpxml"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "run",
                str(_write_input(tmp_path)),
                "-o", str(out),
            ],
        )
        assert result.exit_code == 0, result.stdout
        default_path = out.with_name(out.name + ".report.md")
        assert default_path.exists(), f"expected {default_path}"
        assert "Wrote report" in result.stdout

        assert "Wrote report" in result.stdout


# ---------------------------------------------------------------------------
# --subtitle-target: 4-way subtitle control
# ---------------------------------------------------------------------------

WORDS = [
    Word(start=0.0, end=0.4, text="hello"),
    Word(start=0.5, end=0.9, text="world"),
]

runner = CliRunner()


class TestSubtitleTarget:
    """`--subtitle-target` controls whether STT runs and whether the
    resulting subtitles are baked into the FCPXML.
    """

    def _run(self, monkeypatch, tmp_path, extra_args):
        _stub_probe(monkeypatch, duration=5.0)
        _stub_silence(monkeypatch)
        _stub_extract_audio(monkeypatch, tmp_path)
        _stub_transcriber(monkeypatch, WORDS)
        out = tmp_path / "out.fcpxml"
        result = runner.invoke(
            app,
            ["run", str(_write_input(tmp_path)), "-o", str(out), *extra_args],
        )
        return result, out

    def test_default_target_both_embeds_subtitles(self, monkeypatch, tmp_path):
        result, out = self._run(monkeypatch, tmp_path, [])
        assert result.exit_code == 0, result.stdout
        assert out.exists()
        # Subtitles present in the FCPXML.
        assert "<title" in out.read_text(encoding="utf-8")

    def test_target_srt_keeps_words_but_omits_titles(self, monkeypatch, tmp_path):
        """STT runs, words are recorded, but no <title> elements end up
        in the FCPXML."""
        result, out = self._run(
            monkeypatch, tmp_path, ["--subtitle-target", "srt"]
        )
        assert result.exit_code == 0, result.stdout
        assert out.exists()
        xml = out.read_text(encoding="utf-8")
        assert "<title" not in xml

    def test_target_none_skips_everything(self, monkeypatch, tmp_path):
        result, out = self._run(
            monkeypatch, tmp_path, ["--subtitle-target", "none"]
        )
        assert result.exit_code == 0, result.stdout
        assert out.exists()
        assert "<title" not in out.read_text(encoding="utf-8")

    def test_legacy_no_subtitles_still_works(self, monkeypatch, tmp_path):
        """`--no-subtitles` keeps its old meaning (skip everything)."""
        result, out = self._run(monkeypatch, tmp_path, ["--no-subtitles"])
        assert result.exit_code == 0, result.stdout
        assert "<title" not in out.read_text(encoding="utf-8")

    def test_legacy_no_subtitles_overrides_target(self, monkeypatch, tmp_path):
        """--no-subtitles beats --subtitle-target (legacy wins)."""
        result, out = self._run(
            monkeypatch, tmp_path,
            ["--no-subtitles", "--subtitle-target", "both"],
        )
        assert result.exit_code == 0, result.stdout
        assert "<title" not in out.read_text(encoding="utf-8")

    def test_invalid_target_rejected(self, tmp_path):
        result = runner.invoke(
            app,
            ["run", str(_write_input(tmp_path)),
             "-o", str(tmp_path / "out.fcpxml"),
             "--subtitle-target", "bogus"],
        )
        assert result.exit_code != 0

