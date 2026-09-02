"""Silence detection using FFmpeg silencedetect filter.

The detector shells out to ffmpeg, which is universally available on
macOS and Linux, and parses the silencedetect log lines from stderr.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import SilenceConfig, SilenceInterval, VoiceRange

# How long (seconds) to wait between cancel polls while a subprocess is
# running. 50 ms is small enough to feel snappy in the UI but large
# enough to avoid burning CPU.
_CANCEL_POLL_INTERVAL_S = 0.05

# How long (seconds) to wait after SIGTERM for the process to exit
# before sending SIGKILL.
_TERM_GRACE_S = 1.0

# Regexes for ffmpeg silencedetect log lines, e.g.:
#   [silencedetect @ 0x...] silence_start: 12.345
#   [silencedetect @ 0x...] silence_end: 14.567 | silence_duration: 2.222
_SILENCE_START_RE = re.compile(
    r"silence_start:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE
)


def run_with_cancel(
    cmd: list[str],
    *,
    should_cancel: Callable[[], bool] | None,
    poll_interval: float = _CANCEL_POLL_INTERVAL_S,
    term_grace: float = _TERM_GRACE_S,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` cooperatively, polling ``should_cancel`` periodically.

    If ``should_cancel`` is set while the process is still running, the
    process group is sent ``SIGTERM``, then ``SIGKILL`` after a short
    grace period. ``start_new_session=True`` ensures ffmpeg and any of
    its children share a process group that can be killed atomically.

    The fallback (no ``should_cancel``) is identical to
    ``subprocess.run(cmd, capture_output=True, text=True)``.
    """
    if should_cancel is None:
        return subprocess.run(cmd, capture_output=True, text=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + term_grace
    while True:
        if should_cancel():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                break
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(poll_interval)
            else:
                # Still alive after grace — force kill.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            break
        ret = proc.poll()
        if ret is not None:
            break
        time.sleep(poll_interval)
    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
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
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[SilenceInterval], str]:
    """Run ffmpeg silencedetect, return (intervals, raw_stderr).

    If ``should_cancel`` is provided, it is polled periodically while
    ffmpeg is running. When it returns ``True``, the ffmpeg process
    group is sent ``SIGTERM`` (then ``SIGKILL`` after a short grace
    period) and the function returns whatever has been parsed so far
    along with the partial stderr.
    """
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
    result = run_with_cancel(cmd, should_cancel=should_cancel)
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
    should_cancel: Callable[[], bool] | None = None,
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
    should_cancel
        Optional callable polled periodically while ffmpeg runs. When it
        returns ``True``, the ffmpeg process group is killed and the
        partially-parsed intervals are returned. Use
        ``lambda: cancel_event.is_set()`` from the web worker.
    """
    ffmpeg = ffmpeg_path or ensure_ffmpeg_available()
    intervals, _ = _run_silencedetect(
        ffmpeg,
        path,
        noise_db=config.noise_db,
        min_silence=config.min_silence,
        audio_stream=audio_stream,
        should_cancel=should_cancel,
    )
    return intervals


def detect_voice_ranges(
    path: Path,
    config: SilenceConfig,
    *,
    total_duration: float,
    audio_stream: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[VoiceRange]:
    """Return the *non-silent* ranges of ``path`` (Voice Activity Detection).

    This is the complement of :func:`detect_silence`: a voice range is
    any interval *between* two silence intervals (or between the start
    of the media and the first silence, or between the last silence
    and the end of the media). The result is sorted and non-overlapping.

    Used by the pipeline to snap subtitle ``start`` / ``end`` timestamps
    onto the nearest real audio onset / offset, so STT timing drift
    does not desync the captions from the speech.
    """
    if total_duration <= 0:
        return []

    silences = detect_silence(
        path,
        config,
        audio_stream=audio_stream,
        should_cancel=should_cancel,
    )

    voice: list[VoiceRange] = []
    cursor = 0.0
    for s in silences:
        if s.start > cursor + 1e-3:
            voice.append(VoiceRange(source_in=cursor, source_out=s.start))
        cursor = max(cursor, s.end)
    if cursor < total_duration - 1e-3:
        voice.append(VoiceRange(source_in=cursor, source_out=total_duration))
    return voice


def snap_to_voice(
    t: float,
    voice_ranges: list[VoiceRange],
    *,
    snap_window: float = 0.7,
) -> float:
    """Snap a single timestamp onto the nearest voice range.

    Rules
    -----
    1. If ``t`` is already inside a voice range, return it
       unchanged.
    2. If ``t`` is within ``snap_window`` of a voice edge, snap
       to the edge.
    3. Otherwise return ``t`` unchanged — we never pull a
       subtitle arbitrarily far from its STT time, since that
       would mask real timing errors rather than fix drift.

    Why ``snap_window=0.7``
    ------------------------
    faster-whisper's word-level timestamps systematically fire
    *before* the true audio onset — usually by 100-300 ms, but
    sometimes up to 500 ms. The 0.4 s window we used previously
    wasn't generous enough to absorb that drift on the worst
    clips; 0.7 s reliably catches the early-firing cases while
    still rejecting genuine STT errors (which are usually > 1 s
    away from a real onset).
    """
    if not voice_ranges:
        return t
    best = t
    best_dist = float("inf")
    for vr in voice_ranges:
        for edge in (vr.source_in, vr.source_out):
            d = abs(t - edge)
            if d < best_dist:
                best_dist = d
                best = edge
        if vr.source_in - 1e-6 <= t <= vr.source_out + 1e-6:
            return t
    if best_dist <= snap_window:
        return best
    return t



def shift_subtitle_timestamps(
    subtitles,
    voice_ranges,
    *,
    snap_window: float = 0.7,
) -> int:
    """Apply :func:`snap_to_voice` to every subtitle in place.

    Returns the number of subtitles whose timestamps were
    adjusted. Designed as a single convenience for the pipeline
    so callers don't have to remember to walk the list.
    """
    n = 0
    for sub in subtitles:
        new_start = snap_to_voice(sub.start, voice_ranges,
                                  snap_window=snap_window)
        new_end = snap_to_voice(sub.end, voice_ranges,
                                snap_window=snap_window)
        if new_start != sub.start or new_end != sub.end:
            sub.start = new_start
            sub.end = new_end
            n += 1
    return n
