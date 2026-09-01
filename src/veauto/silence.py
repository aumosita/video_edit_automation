"""Silence detection using FFmpeg silencedetect filter.

The detector shells out to ffmpeg, which is universally available on
macOS and Linux, and parses the silencedetect log lines from stderr.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import SilenceConfig, SilenceInterval

# Regexes for ffmpeg silencedetect log lines, e.g.:
#   [silencedetect @ 0x...] silence_start: 12.345
#   [silencedetect @ 0x...] silence_end: 14.567 | silence_duration: 2.222
_SILENCE_START_RE = re.compile(
    r"silence_start:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)


@dataclass
class FfmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg is not available on PATH."""

    def __init__(self) -> None:
        super().__init__(
            "ffmpeg executable not found. Install it (e.g. brew install ffmpeg "
            "on macOS) and ensure it is on PATH."
        )


def parse_silencedetect_output(stderr: str) -> list[SilenceInterval]:
    """Parse ffmpeg silencedetect stderr into intervals."""
    starts: list[float] = []
    ends: list[float] = []
    for line in stderr.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            starts.append(float(m_start.group(1)))
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end:
            ends.append(float(m_end.group(1)))
    intervals: list[SilenceInterval] = []
    for i, start in enumerate(starts):
        if i >= len(ends):
            break
        end = ends[i]
        if end > start:
            intervals.append(SilenceInterval(start=start, end=end))
    return intervals


def merge_close_intervals(
    intervals: list[SilenceInterval],
    *,
    min_gap: float = 0.0,
) -> list[SilenceInterval]:
    """Merge silence intervals separated by less than min_gap seconds."""
    if not intervals or min_gap <= 0:
        return list(intervals)
    merged: list[SilenceInterval] = [intervals[0]]
    for current in intervals[1:]:
        last = merged[-1]
        if current.start - last.end < min_gap:
            merged[-1] = SilenceInterval(
                start=last.start, end=max(last.end, current.end)
            )
        else:
            merged.append(current)
    return merged


def ensure_ffmpeg_available() -> str:
    """Return the path to the ffmpeg binary or raise FfmpegNotFoundError."""
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegNotFoundError()
    return path


def _probe_with_ffmpeg(ffmpeg_path: str, path: Path) -> float:
    """Read the total duration by decoding the container header with ffmpeg."""
    result = subprocess.run(
        [ffmpeg_path, "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not m:
        raise RuntimeError(
            f"Could not determine duration of {path}. ffmpeg output:\n{result.stderr}"
        )
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def probe_duration(path: Path) -> float:
    """Return the duration of the media file in seconds.

    Tries ffprobe first, falls back to ffmpeg silencedetect.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        try:
            return float(result.stdout.strip())
        except ValueError:
            pass

    ff = ensure_ffmpeg_available()
    intervals = detect_silence(
        path,
        SilenceConfig(noise_db=-30.0, min_silence=0.1, enabled=True),
        ffmpeg_path=ff,
    )
    if intervals:
        return max(intervals[-1].end, _probe_with_ffmpeg(ff, path))
    return _probe_with_ffmpeg(ff, path)


def _run_silencedetect(
    ffmpeg_path: str,
    path: Path,
    *,
    noise_db: float,
    min_silence: float,
    audio_stream: int | None = None,
) -> tuple[list[SilenceInterval], str]:
    """Run ffmpeg silencedetect, return (intervals, raw_stderr)."""
    cmd: list[str] = [
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
    ]
    if audio_stream is not None:
        cmd.extend(["-map", f"0:a:{audio_stream}"])
    cmd.extend([
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return parse_silencedetect_output(result.stderr), result.stderr


def probe_media_info(path):
    """Return MediaInfo for the given file using ffprobe."""
    from .models import MediaInfo
    width = 0
    height = 0
    frame_rate = 30.0
    duration = probe_duration(path)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1", str(path)],
                capture_output=True, text=True, check=True,
            )
            out = result.stdout
            m = re.search(r"width=(\d+)", out)
            if m:
                width = int(m.group(1))
            m = re.search(r"height=(\d+)", out)
            if m:
                height = int(m.group(1))
            m = re.search(r"(?:avg_frame_rate|r_frame_rate)=(\d+)/(\d+)", out)
            if m:
                num, den = int(m.group(1)), int(m.group(2))
                if den > 0:
                    frame_rate = num / den
            m = re.search(r"duration=([\d.]+)", out)
            if m:
                duration = float(m.group(1))
        except Exception:
            pass
    return MediaInfo(path=path, duration=duration, width=width, height=height, frame_rate=frame_rate)


def detect_silence(
    path: Path,
    config: SilenceConfig,
    *,
    audio_stream: int | None = None,
    ffmpeg_path: str | None = None,
) -> list[SilenceInterval]:
    """Detect silence intervals in path using the given config.

    Parameters
    ----------
    path
        Input media file.
    config
        Silence detection configuration.
    audio_stream
        If set, select 0:a:{audio_stream} from the input.
    ffmpeg_path
        Override the ffmpeg binary path. Defaults to the one on PATH.
    """
    ffmpeg = ffmpeg_path or ensure_ffmpeg_available()
    intervals, _ = _run_silencedetect(
        ffmpeg,
        path,
        noise_db=config.noise_db,
        min_silence=config.min_silence,
        audio_stream=audio_stream,
    )
    return intervals
