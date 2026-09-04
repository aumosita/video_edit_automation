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


# ---------------------------------------------------------------------------
# Adaptive (auto) silence threshold
# ---------------------------------------------------------------------------

# Windows whose measured RMS is at or below this value are treated as
# digital silence and still participate in the percentile math (they ARE
# the noise floor), but they need a finite stand-in value.
_DIGITAL_SILENCE_DB = -120.0

_RMS_LINE_RE = re.compile(r"RMS_level=(-?\d+(?:\.\d+)?|-inf)")


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return _DIGITAL_SILENCE_DB
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _probe_sample_rate(path: Path) -> int:
    """Best-effort sample-rate probe via ffprobe (fallback: 48000)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=sample_rate",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, check=True,
            )
            return int(result.stdout.strip() or 48000)
        except Exception:  # noqa: BLE001 — best-effort probe
            pass
    return 48000


def measure_rms_profile(
    path: Path,
    *,
    window_seconds: float = 1.0,
    ffmpeg_path: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[float]:
    """Measure per-window RMS levels (dBFS) of the first audio stream.

    Uses ffmpeg's ``astats`` filter with ``reset=1`` on ``asetnsamples``
    chunks, so every entry covers roughly ``window_seconds`` of audio.
    Digital silence is reported as ``-120``. This is the raw input for
    :func:`estimate_silence_threshold`.
    """
    ff = ffmpeg_path or ensure_ffmpeg_available()
    rate = _probe_sample_rate(path)
    n = max(1, int(rate * window_seconds))
    cmd = [
        ff, "-hide_banner", "-nostats",
        "-i", str(path),
        "-map", "0:a:0",
        "-af",
        (
            f"asetnsamples=n={n}:p=0,"
            "astats=metadata=1:reset=1,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
        ),
        "-f", "null", "-",
    ]
    result = run_with_cancel(cmd, should_cancel=should_cancel)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={result.returncode}) while measuring RMS:\n"
            f"{result.stderr.strip()}"
        )
    windows: list[float] = []
    for m in _RMS_LINE_RE.finditer(result.stdout):
        raw = m.group(1)
        windows.append(
            float(raw) if raw != "-inf" else _DIGITAL_SILENCE_DB
        )
    return windows


def estimate_silence_threshold(
    rms_windows: list[float],
    *,
    headroom_db: float = 12.0,
    floor_gap_db: float = 10.0,
    min_db: float = -70.0,
    max_db: float = -20.0,
) -> float:
    """Derive a silence threshold from a file's loudness distribution.

    The heuristic treats the loud tail of the distribution (95th
    percentile) as "speech level" and the quiet tail (5th percentile) as
    the "noise floor", then picks a threshold that sits ``headroom_db``
    below the speech level while staying at least ``floor_gap_db`` above
    the noise floor (so genuine background hiss is still counted as
    silence). The result is clamped to ``[min_db, max_db]``.

    Why this exists: a fixed ``-30 dB`` threshold misclassifies quiet
    recordings — if the speech level is only ~-32 dB, soft spoken
    passages fall below the threshold and get cut as "silence" while the
    user clearly hears them.
    """
    if not rms_windows:
        return -30.0
    vals = sorted(rms_windows)
    speech_db = _percentile(vals, 95)
    floor_db = _percentile(vals, 5)
    threshold = speech_db - headroom_db
    # Never bury the noise floor inside "voice": keep some gap above it
    # so background hiss remains classified as silence.
    threshold = max(threshold, floor_db + floor_gap_db)
    return min(max(threshold, min_db), max_db)


def resolve_noise_db(
    path: Path,
    config: SilenceConfig,
    *,
    ffmpeg_path: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> float:
    """Return the effective silence threshold for ``config``.

    With ``config.auto_noise_db`` off this is simply ``config.noise_db``.
    With it on, the file's loudness profile is measured once and the
    threshold is derived via :func:`estimate_silence_threshold`.
    """
    if not config.auto_noise_db:
        return config.noise_db
    windows = measure_rms_profile(
        path,
        ffmpeg_path=ffmpeg_path,
        should_cancel=should_cancel,
    )
    resolved = estimate_silence_threshold(
        windows, headroom_db=config.noise_headroom_db,
    )
    # Apply the user's relative adjustment, then re-clamp to the same
    # bounds ``estimate_silence_threshold`` uses so the offset can never
    # push the threshold into nonsense territory.
    return min(
        max(resolved + config.noise_db_offset, -70.0), -20.0,
    )


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
    noise_db = resolve_noise_db(
        path, config, ffmpeg_path=ffmpeg, should_cancel=should_cancel,
    )
    intervals, _ = _run_silencedetect(
        ffmpeg,
        path,
        noise_db=noise_db,
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
    snap_window: float = 0.5,
    kind: str = "start",
) -> float:
    """Snap a single timestamp onto a voice-range edge.

    Rules
    -----
    1. If ``t`` is already inside a voice range, return it unchanged.
    2. ``kind="start"`` (a subtitle onset): snap **forward** onto the
       next voice range's ``source_in`` if it is within
       ``snap_window``. faster-whisper fires word starts 100-500 ms
       *before* the true audio energy rises, so the correction is
       always forward — we never pull an onset backwards onto the
       previous utterance's tail (that would overlap the previous
       caption or desync it further).
    3. ``kind="end"`` (a subtitle offset): snap **backward** onto the
       previous voice range's ``source_out`` if within the window.
    4. Otherwise return ``t`` unchanged — we never pull a subtitle
       arbitrarily far from its STT time, since that would mask real
       timing errors rather than fix drift.

    Parameters
    ----------
    kind:
        ``"start"`` for subtitle onsets, ``"end"`` for offsets.
    """
    if not voice_ranges:
        return t
    for vr in voice_ranges:
        if vr.source_in - 1e-6 <= t <= vr.source_out + 1e-6:
            return t
    best = t
    best_dist = float("inf")
    for vr in voice_ranges:
        edge = vr.source_in if kind == "start" else vr.source_out
        forward_ok = edge >= t if kind == "start" else edge <= t
        if not forward_ok:
            continue
        d = abs(t - edge)
        if d < best_dist:
            best_dist = d
            best = edge
    if best_dist <= snap_window:
        return best
    return t



def shift_subtitle_timestamps(
    subtitles,
    voice_ranges,
    *,
    snap_window: float = 0.5,
) -> int:
    """Apply :func:`snap_to_voice` to every subtitle in place.

    Starts snap forward onto voice onsets, ends snap backward onto
    voice offsets (see :func:`snap_to_voice`). A minimum gap of
    0.05 s between ``start`` and ``end`` is enforced after snapping
    so a subtitle never inverts or vanishes.

    Returns the number of subtitles whose timestamps were
    adjusted. Designed as a single convenience for the pipeline
    so callers don't have to remember to walk the list.
    """
    n = 0
    for sub in subtitles:
        new_start = snap_to_voice(sub.start, voice_ranges,
                                  snap_window=snap_window, kind="start")
        new_end = snap_to_voice(sub.end, voice_ranges,
                                snap_window=snap_window, kind="end")
        if new_end < new_start + 0.05:
            # A backward end-snap that would invert the cue (or make it
            # vanishingly short) is rejected — keep the original end.
            new_end = max(sub.end, new_start + 0.05)
        if new_start != sub.start or new_end != sub.end:
            sub.start = new_start
            sub.end = new_end
            n += 1
    return n
