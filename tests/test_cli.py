"""CLI tests using Typer's CliRunner."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from veauto.cli import app

runner = CliRunner()

FFMPEG_SKIP = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_test_video(path, duration=2.0):
    ff = shutil.which("ffmpeg")
    subprocess.run(
        [
            ff, "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size=640x480:rate=30",
            "-c:v", "libx264", str(path),
        ],
        capture_output=True, check=True,
    )


def test_cli_help():
    """``--help`` renders, and every subcommand is registered.

    The subcommand names are checked against the Click group rather
    than the rendered help text: rich wraps/truncates the help table
    to the detected terminal width, which differs between local
    machines and CI runners.
    """
    from typer.main import get_command

    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0

    commands = set(get_command(app).commands)
    for name in ("info", "trim", "subtitles", "run"):
        assert name in commands, f"Missing subcommand {name!r}"


@FFMPEG_SKIP
def test_cli_info(tmp_path):
    video = tmp_path / "test.mp4"
    _make_test_video(video)
    result = runner.invoke(app, ["info", str(video)])
    assert result.exit_code == 0
    assert "Duration:" in result.stdout
    assert "640x480" in result.stdout


@FFMPEG_SKIP
def test_cli_trim_no_silence(tmp_path):
    video = tmp_path / "test.mp4"
    _make_test_video(video, duration=2.0)
    output = tmp_path / "out.fcpxml"
    result = runner.invoke(app, ["trim", str(video), "-o", str(output)])
    assert result.exit_code == 0, result.stdout
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "<?xml" in content
    assert "fcpxml" in content


@FFMPEG_SKIP
def test_cli_trim_with_silence(tmp_path):
    video = tmp_path / "silence.mp4"
    _make_test_video(video, duration=2.0)
    output = tmp_path / "out.fcpxml"
    result = runner.invoke(app, ["trim", str(video), "-o", str(output), "--min-silence", "1.5"])
    assert result.exit_code == 0, result.stdout
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert content.count("asset-clip") == 1
