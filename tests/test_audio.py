"""Tests for veauto.audio (audio extraction from video).

ffmpeg-dependent tests run only when the binary is on PATH.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from veauto.audio import AudioExtractionError, extract_audio

FFMPEG_SKIP = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed"
)


def _make_test_video_with_audio(path, duration: float = 1.0, size: str = "320x240", rate: int = 30) -> None:
    ff = shutil.which("ffmpeg")
    assert ff is not None
    subprocess.run(
        [
            ff, "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={duration}:size={size}:rate={rate}",
            "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
        ],
        capture_output=True, check=True,
    )


@FFMPEG_SKIP
def test_extract_audio_creates_wav(tmp_path) -> None:
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.wav"
    _make_test_video_with_audio(src, duration=1.0)
    extract_audio(src, out)
    assert out.exists()
    assert out.stat().st_size > 0


@FFMPEG_SKIP
def test_extract_audio_creates_parent_dirs(tmp_path) -> None:
    src = tmp_path / "src.mp4"
    out = tmp_path / "nested" / "deeper" / "out.wav"
    _make_test_video_with_audio(src, duration=0.5)
    extract_audio(src, out)
    assert out.exists()


def test_extract_audio_raises_on_missing_input(tmp_path) -> None:
    with pytest.raises((subprocess.CalledProcessError, AudioExtractionError, FileNotFoundError)):
        extract_audio(tmp_path / "does-not-exist.mp4", tmp_path / "out.wav")
