"""Tests for veauto.segments (pure logic, no ffmpeg)."""

from __future__ import annotations

import pytest

from veauto.models import SilenceInterval
from veauto.segments import (
    _apply_margin,
    _merge_overlapping,
    build_cut_segments,
)


def test_merge_overlapping_empty():
    assert _merge_overlapping([]) == []


def test_merge_overlapping_singles():
    ivs = [SilenceInterval(start=1.0, end=2.0)]
    out = _merge_overlapping(ivs)
    assert len(out) == 1
    assert out[0].start == 1.0
    assert out[0].end == 2.0


def test_merge_overlapping_overlap():
    ivs = [
        SilenceInterval(start=1.0, end=3.0),
        SilenceInterval(start=2.0, end=4.0),
    ]
    out = _merge_overlapping(ivs)
    assert len(out) == 1
    assert out[0].start == 1.0
    assert out[0].end == 4.0


def test_merge_overlapping_adjacent():
    ivs = [
        SilenceInterval(start=1.0, end=2.0),
        SilenceInterval(start=2.0, end=3.0),
    ]
    out = _merge_overlapping(ivs)
    assert len(out) == 1
    assert out[0].end == 3.0


def test_merge_overlapping_separated():
    ivs = [
        SilenceInterval(start=1.0, end=2.0),
        SilenceInterval(start=4.0, end=5.0),
    ]
    out = _merge_overlapping(ivs)
    assert len(out) == 2


def test_merge_overlapping_unsorted_input():
    ivs = [
        SilenceInterval(start=4.0, end=5.0),
        SilenceInterval(start=1.0, end=2.0),
    ]
    out = _merge_overlapping(ivs)
    assert len(out) == 2
    assert out[0].start == 1.0
    assert out[1].start == 4.0


def test_apply_margin_empty():
    assert _apply_margin([], margin=0.2, total_duration=10.0) == []


def test_apply_margin_zero_margin():
    ivs = [SilenceInterval(start=3.0, end=5.0)]
    out = _apply_margin(ivs, margin=0.0, total_duration=10.0)
    assert len(out) == 1
    assert out[0].start == 3.0
    assert out[0].end == 5.0


def test_apply_margin_basic():
    ivs = [SilenceInterval(start=3.0, end=5.0)]
    out = _apply_margin(ivs, margin=0.2, total_duration=10.0)
    assert len(out) == 1
    assert out[0].start == pytest.approx(3.2)
    assert out[0].end == pytest.approx(4.8)


def test_apply_margin_drops_interval_shorter_than_two_margins():
    ivs = [SilenceInterval(start=3.0, end=3.5)]
    out = _apply_margin(ivs, margin=0.3, total_duration=10.0)
    assert out == []


def test_apply_margin_clamps_to_zero():
    ivs = [SilenceInterval(start=0.0, end=1.0)]
    out = _apply_margin(ivs, margin=0.3, total_duration=10.0)
    assert out[0].start == pytest.approx(0.3)
    assert out[0].end == pytest.approx(0.7)


def test_apply_margin_clamps_to_total():
    ivs = [SilenceInterval(start=9.5, end=10.0)]
    out = _apply_margin(ivs, margin=0.2, total_duration=10.0)
    assert out[0].start == pytest.approx(9.7)
    assert out[0].end == pytest.approx(9.8)


def test_apply_margin_merges_overlapping_after_shrink():
    ivs = [
        SilenceInterval(start=2.0, end=3.5),
        SilenceInterval(start=3.0, end=5.0),
    ]
    out = _apply_margin(ivs, margin=0.25, total_duration=10.0)
    assert len(out) == 1
    assert out[0].start == pytest.approx(2.25)
    assert out[0].end == pytest.approx(4.75)


def test_build_no_silence_returns_full():
    kept, removed = build_cut_segments(10.0, [], margin=0.2)
    assert len(kept) == 1
    assert kept[0].source_in == 0.0
    assert kept[0].source_out == 10.0
    assert removed == []


def test_build_single_silence_with_margin():
    sils = [SilenceInterval(start=3.0, end=5.0)]
    kept, removed = build_cut_segments(10.0, sils, margin=0.2)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 3.2), (4.8, 10.0)]
    assert [(r.source_in, r.source_out) for r in removed] == [(3.0, 5.0)]


def test_build_single_silence_zero_margin():
    sils = [SilenceInterval(start=3.0, end=5.0)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.0)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 3.0), (5.0, 10.0)]


def test_build_two_silences():
    sils = [
        SilenceInterval(start=2.0, end=3.0),
        SilenceInterval(start=5.0, end=6.0),
    ]
    kept, _ = build_cut_segments(10.0, sils, margin=0.2)
    assert [(c.source_in, c.source_out) for c in kept] == [
        (0.0, 2.2), (2.8, 5.2), (5.8, 10.0)
    ]


def test_build_margin_keeps_padding_inside_silence():
    # The margin is padding of *kept* silence; the cut stays inside the
    # detected silence, so speech between two silences is never chopped.
    sils = [
        SilenceInterval(start=2.0, end=3.0),
        SilenceInterval(start=3.0, end=4.0),
    ]
    kept, _ = build_cut_segments(10.0, sils, margin=0.2)
    assert [(c.source_in, c.source_out) for c in kept] == [
        (0.0, 2.2), (2.8, 3.2), (3.8, 10.0)
    ]


def test_build_leading_silence():
    sils = [SilenceInterval(start=0.0, end=2.0)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.2)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 0.2), (1.8, 10.0)]


def test_build_trailing_silence():
    sils = [SilenceInterval(start=8.0, end=10.0)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.2)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 8.2), (9.8, 10.0)]


def test_build_silence_covers_entire_file():
    sils = [SilenceInterval(start=0.0, end=10.0)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.0)
    assert kept == []


def test_build_zero_duration_returns_empty():
    kept, removed = build_cut_segments(0.0, [], margin=0.2)
    assert kept == []
    assert removed == []


def test_build_out_of_bounds_silences_filtered():
    sils = [
        SilenceInterval(start=3.0, end=5.0),
        SilenceInterval(start=0.0, end=2.0),
        SilenceInterval(start=8.0, end=20.0),
    ]
    kept, removed = build_cut_segments(10.0, sils, margin=0.0)
    assert [(c.source_in, c.source_out) for c in kept] == [(2.0, 3.0), (5.0, 8.0)]
    assert [(r.source_in, r.source_out) for r in removed] == [(3.0, 5.0), (0.0, 2.0)]


def test_build_min_keep_drops_glitch_between_silences():
    """A <0.15s 'speech' blip between two removed silences is dropped."""
    sils = [
        SilenceInterval(start=1.0, end=2.0),
        SilenceInterval(start=2.1, end=3.0),  # 0.1s blip between them
    ]
    kept, _ = build_cut_segments(10.0, sils, margin=0.0, min_keep_seconds=0.15)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 1.0), (3.0, 10.0)]


def test_build_min_keep_keeps_first_and_last():
    # The head/tail of the video are kept even when shorter than the
    # threshold — dropping them would silently remove content.
    sils = [SilenceInterval(start=2.0, end=2.05)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.0, min_keep_seconds=5.0)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 2.0), (2.05, 10.0)]


def test_build_min_keep_zero_is_noop():
    sils = [SilenceInterval(start=1.0, end=2.0), SilenceInterval(start=2.1, end=3.0)]
    kept, _ = build_cut_segments(10.0, sils, margin=0.0, min_keep_seconds=0.0)
    assert [(c.source_in, c.source_out) for c in kept] == [(0.0, 1.0), (2.0, 2.1), (3.0, 10.0)]
