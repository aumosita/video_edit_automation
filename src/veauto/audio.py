"""Audio extraction from video via FFmpeg.

Used as a preprocessing step for STT: faster-whisper expects mono 16 kHz WAV.
We shell out to ``ffmpeg`` instead of pulling in a Python decoder to keep the
dependency surface minimal.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .silence import ensure_ffmpeg_available, run_with_cancel


class AudioExtractionError(RuntimeError):
    """Raised when ffmpeg fails to extract audio from ``input_path``."""


def extract_audio(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    ffmpeg_path: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Path:
    """Extract audio from ``input_path`` to ``output_path`` (WAV / PCM).

    Parameters
    ----------
    input_path
        Source video / audio file.
    output_path
        Destination ``.wav`` path. Parent directories are created.
    sample_rate
        Output sample rate in Hz. ``faster-whisper`` recommends 16 kHz.
    channels
        Number of output channels (``1`` = mono).
    ffmpeg_path
        Optional override for the ffmpeg binary (defaults to PATH lookup).
    should_cancel
        Optional callable invoked periodically while ffmpeg is running.
        If it returns ``True``, the ffmpeg process group is sent
        ``SIGTERM`` (then ``SIGKILL`` after a 1 s grace period). Use
        ``lambda: cancel_event.is_set()`` from the web worker.

    Returns
    -------
    Path
        ``output_path`` on success.

    Raises
    ------
    AudioExtractionError
        If ffmpeg exits with a non-zero status (including a forced
        kill via :data:`SIGKILL`, which reports ``-9``).
    """
    ff = ffmpeg_path or ensure_ffmpeg_available()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str] = [
        ff,
        "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-vn",                       # no video
        "-ac", str(channels),        # channels
        "-ar", str(sample_rate),     # sample rate
        "-c:a", "pcm_s16le",         # explicit PCM encoder (ffmpeg 8 needs it)
        "-f", "wav",                 # force WAV/PCM container
        str(output_path),
    ]
    result = run_with_cancel(cmd, should_cancel=should_cancel)
    if result.returncode != 0:
        raise AudioExtractionError(
            f"ffmpeg failed (rc={result.returncode}) while extracting audio:\n"
            f"{result.stderr.strip()}"
        )
    return output_path


def probe_audio_duration(audio_path: Path) -> float | None:
    """Best-effort duration probe of an audio file via ffmpeg stderr.

    Returns ``None`` if the duration cannot be parsed — callers should fall
    back to the source video's duration in that case.
    """
    ff = ensure_ffmpeg_available()
    result = subprocess.run(
        [ff, "-i", str(audio_path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    m = re.search(
        r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr
    )
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)
