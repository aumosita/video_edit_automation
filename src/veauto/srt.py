"""SubRip (SRT) subtitle writer.

SRT is the de-facto plain-text subtitle interchange format. It is
recognised by FCP (``File > Import > Captions…``), DaVinci Resolve,
Premiere, VLC, mpv, and the web (``<track kind="subtitles">``).

Compared to our FCPXML builder, SRT has a tiny schema::

    1
    00:00:01,000 --> 00:00:03,500
    Hello world

    2
    00:00:04,200 --> 00:00:07,800
    This is test

Three pieces per cue: ``index`` (1-based), a ``start --> end`` line
in ``HH:MM:SS,mmm`` (comma, *not* period — period is the WebVTT
flavour), and one or more lines of text. Cues are separated by a
blank line.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def seconds_to_srt_time(t: float) -> str:
    """Render a number of seconds as ``HH:MM:SS,mmm``.

    Negative or non-finite values are clamped to 0 so the output is
    always a valid SRT timestamp. Fractions of a millisecond are
    rounded to the nearest millisecond.
    """
    if t < 0.0 or t != t:  # second test is NaN
        t = 0.0
    total_ms = int(round(t * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def format_srt_cue(index: int, start: float, end: float, text: str) -> str:
    """Format a single SubRip cue."""
    end = max(end, start + 0.001)  # SRT requires end > start
    return (
        f"{index}\n"
        f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}\n"
        f"{text}\n"
    )


def write_srt(
    subtitles: Iterable,
    path: Path,
) -> int:
    """Write subtitles to ``path`` in SRT format.

    Returns the number of cues written. Empty input produces an
    empty file (still a valid SRT — many players accept it).
    Multi-line cue text is preserved as-is.
    """
    cues = list(subtitles)
    parts: list[str] = []
    for i, sub in enumerate(cues, start=1):
        parts.append(format_srt_cue(i, sub.start, sub.end, sub.text))
    body = "\n".join(parts)
    if cues:
        body += "\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return len(cues)
