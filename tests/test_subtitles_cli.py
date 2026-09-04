"""CLI tests for `veauto subtitles`.

The real faster-whisper model is never loaded in CI: ``veauto.cli.transcribe``
and ``veauto.cli.extract_audio`` are monkeypatched with lightweight stubs that
return canned data.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from veauto.cli import app
from veauto.models import MediaInfo, Word

runner = CliRunner()

WORDS = [
    Word(start=0.0, end=0.4, text="hello"),
    Word(start=0.5, end=0.9, text="world"),
    Word(start=1.2, end=1.7, text="this"),
    Word(start=1.8, end=2.3, text="is"),
    Word(start=2.4, end=2.9, text="a"),
    Word(start=3.0, end=3.6, text="test"),
]


def _stub_media_for(src_path) -> MediaInfo:
    return MediaInfo(
        path=src_path,
        duration=5.0,
        width=640,
        height=480,
        frame_rate=30.0,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake_probe(path):
        calls["probe"] = path
        return _stub_media_for(path)

    def fake_extract(video, output, **kwargs):  # noqa: ARG001
        calls.setdefault("extract", []).append((video, output))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFstub")
        return output

    def fake_transcribe(audio_path, config, *, model_factory=None):  # noqa: ARG001
        calls.setdefault("transcribe", []).append((audio_path, config))
        return list(WORDS)

    monkeypatch.setattr("veauto.cli.probe_media_info", fake_probe)
    monkeypatch.setattr("veauto.cli.extract_audio", fake_extract)
    monkeypatch.setattr("veauto.cli.transcribe", fake_transcribe)
    return calls


# ---------------------------------------------------------------------------
# Help / option surface
# ---------------------------------------------------------------------------


def _declared_option_flags(command_name: str) -> set[str]:
    """Return every CLI flag declared on ``veauto <command_name>``.

    We introspect the Click command that Typer builds instead of
    scraping ``--help`` text. Rich renders the help table to the
    detected terminal width and *truncates* long flags with an
    ellipsis (e.g. ``--compute-type`` shows as ``--comput…``), so
    substring assertions on the rendered output pass locally and
    fail on CI runners with a narrower/different terminal. The
    parameter list is the ground truth and is width-independent.
    """
    from typer.main import get_command

    group = get_command(app)
    command = group.commands[command_name]
    flags: set[str] = set()
    for param in command.params:
        flags.update(param.opts)
        flags.update(getattr(param, "secondary_opts", None) or [])
    return flags


def test_subtitles_help_lists_all_options() -> None:
    """Every documented option must exist on the ``subtitles`` command."""
    flags = _declared_option_flags("subtitles")
    for flag in (
        "--output", "--model", "--language", "--device", "--compute-type",
        "--beam-size", "--style-position", "--style-font", "--style-font-size",
        "--style-max-chars", "--style-max-lines", "--style-min-duration",
        "--style-max-duration", "--project-name", "--event-name",
        "--keep-audio", "--audio-dir",
    ):
        assert flag in flags, f"Missing option {flag!r}"
    # --help itself must still render without crashing.
    result = runner.invoke(app, ["subtitles", "--help"])
    assert result.exit_code == 0


def test_subtitles_requires_output(tmp_path) -> None:
    """Omitting ``--output`` must fail before any media work happens."""
    from typer.main import get_command

    # Ground truth: the parameter is declared required.
    command = get_command(app).commands["subtitles"]
    output_param = next(p for p in command.params if p.name == "output")
    assert output_param.required is True

    src = tmp_path / "in.mp4"
    src.write_text("not a real video, but the cli shouldn't reach extraction")
    result = runner.invoke(app, ["subtitles", str(src)])
    # Click exits with code 2 for a usage error; the important part is
    # that it is a *failure* and not a successful run.
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# End-to-end with stubbed transcribe / extract_audio
# ---------------------------------------------------------------------------


def test_subtitles_writes_fcpxml_and_cleans_up(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mp4"
    src.write_text("stub")
    out = tmp_path / "out.fcpxml"
    audio_dir = tmp_path / "audio"

    calls = _patch_common(monkeypatch)
    result = runner.invoke(app, [
        "subtitles", str(src),
        "-o", str(out),
        "--audio-dir", str(audio_dir),
        "--model", "tiny",
    ])

    assert result.exit_code == 0, result.stdout
    assert out.exists(), "FCPXML should be written"

    assert "probe" in calls
    assert calls["extract"][0][1].name == "in.wav"
    assert calls["transcribe"][0][1].model == "tiny"

    wav = audio_dir / "in.wav"
    assert not wav.exists(), "Default: extracted .wav must be removed"


def test_subtitles_keep_audio_preserves_wav(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mp4"
    src.write_text("stub")
    out = tmp_path / "out.fcpxml"
    audio_dir = tmp_path / "audio"

    _patch_common(monkeypatch)
    result = runner.invoke(app, [
        "subtitles", str(src),
        "-o", str(out),
        "--audio-dir", str(audio_dir),
        "--keep-audio",
    ])

    assert result.exit_code == 0, result.stdout
    wav = audio_dir / "in.wav"
    assert wav.exists()


def test_subtitles_output_is_valid_xml(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mp4"
    src.write_text("stub")
    out = tmp_path / "out.fcpxml"

    _patch_common(monkeypatch)
    result = runner.invoke(app, ["subtitles", str(src), "-o", str(out)])
    assert result.exit_code == 0, result.stdout

    tree = ET.fromstring(out.read_text(encoding="utf-8"))
    assert tree.tag == "fcpxml"


def test_subtitles_passes_style_to_builder(tmp_path, monkeypatch) -> None:
    src = tmp_path / "in.mp4"
    src.write_text("stub")
    out = tmp_path / "out.fcpxml"

    _patch_common(monkeypatch)
    result = runner.invoke(app, [
        "subtitles", str(src),
        "-o", str(out),
        "--style-position", "top",
        "--style-font-size", "64",
        "--style-max-chars", "20",
        "--language", "ko",
    ])
    assert result.exit_code == 0, result.stdout

    content = out.read_text(encoding="utf-8")
    assert 'fontSize="64"' in content
    # Subtitle style is inlined into every <title>'s <text-style>
    # (fade-free Basic Title template) so the file passes Apple's
    # FCPXML 1.10 DTD validation. The Motion-only `relativeTo`
    # attribute is gone (FCP rejects it).
    assert '<title' in content
    assert '<text-style' in content
    assert 'relativeTo' not in content


def test_subtitles_handles_no_words(tmp_path, monkeypatch) -> None:
    """No words → 0 subtitle lines, but still writes a valid FCPXML."""
    src = tmp_path / "in.mp4"
    src.write_text("stub")
    out = tmp_path / "out.fcpxml"

    calls = _patch_common(monkeypatch)

    def empty_transcribe(audio_path, config, *, model_factory=None):  # noqa: ARG001
        calls["empty"] = True
        return []

    monkeypatch.setattr("veauto.cli.transcribe", empty_transcribe)

    result = runner.invoke(app, ["subtitles", str(src), "-o", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert calls.get("empty") is True
