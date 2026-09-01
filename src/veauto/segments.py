"""Cut-segment generation from silence intervals.

Given the source media duration and a list of silence intervals detected by
ffmpeg, produce a list of ``CutSegment`` objects representing the parts of the
source timeline that should be kept, plus a list of ``RemovedSilence`` for
reporting/debugging.

The pipeline is:

1. Expand each silence interval by ``margin`` on each side, clamped to
   ``[0, total_duration]``.
2. Merge overlapping/adjacent expanded intervals (after margin, two close
   silences naturally merge into one).
3. Subtract the merged silence regions from ``[0, total_duration]`` to obtain
   the cut segments.
"""

from __future__ import annotations

from .models import CutSegment, RemovedSilence, SilenceInterval


def _expand_with_margin(intervals, *, margin, total_duration):
    """Expand each interval by margin and clamp to media bounds."""
    if not intervals:
        return _merge_overlapping([])
    if margin <= 0:
        clamped = []
        for iv in intervals:
            s = max(0.0, iv.start)
            e = min(total_duration, iv.end)
            if e > s:
                clamped.append(SilenceInterval(start=s, end=e))
        return _merge_overlapping(clamped)
    expanded = []
    for iv in intervals:
        s = max(0.0, iv.start - margin)
        e = min(total_duration, iv.end + margin)
        if e > s:
            expanded.append(SilenceInterval(start=s, end=e))
    return _merge_overlapping(expanded)


def _merge_overlapping(intervals):
    """Merge overlapping or adjacent intervals (assumed sorted by start)."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals, key=lambda iv: iv.start)
    merged = [sorted_ivs[0]]
    for iv in sorted_ivs[1:]:
        last = merged[-1]
        if iv.start <= last.end:
            merged[-1] = SilenceInterval(
                start=last.start, end=max(last.end, iv.end)
            )
        else:
            merged.append(iv)
    return merged


def build_cut_segments(
    total_duration,
    silence_intervals,
    *,
    margin=0.2,
):
    if total_duration <= 0:
        return [], []
    removed = [
        RemovedSilence(source_in=iv.start, source_out=iv.end)
        for iv in silence_intervals
        if 0.0 <= iv.start < iv.end <= total_duration
    ]
    expanded = _expand_with_margin(
        silence_intervals, margin=margin, total_duration=total_duration
    )
    kept = []
    cursor = 0.0
    for sil in expanded:
        if sil.start > cursor:
            kept.append(CutSegment(source_in=cursor, source_out=sil.start))
        cursor = max(cursor, sil.end)
    if cursor < total_duration:
        kept.append(CutSegment(source_in=cursor, source_out=total_duration))
    return kept, removed
