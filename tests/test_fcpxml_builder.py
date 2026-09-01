"""Tests for veauto.fcpxml_builder (pure XML output, no ffmpeg)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from veauto.fcpxml_builder import (
    _assign_subtitles_to_cuts,
    _rational_time,
    build_fcpxml,
)
from veauto.models import (
    CutSegment,
    MediaInfo,
    SubtitleSegment,
    SubtitleStyle,
)


def _make_media():
    return MediaInfo(
        path=__import__("pathlib").Path("/tmp/test.mp4"),
        duration=10.0,
        width=1920,
        height=1080,
        frame_rate=30.0,
    )


def test_rational_time_basic() -> None:
    assert _rational_time(0.0, 30.0) == "0/30s"
    assert _rational_time(1.0, 30.0) == "30/30s"
    assert _rational_time(0.5, 30.0) == "15/30s"


def test_rational_time_rounds() -> None:
    # 0.0333s @ 30fps = 1 frame
    assert _rational_time(0.0333, 30.0) == "1/30s"


def test_rational_time_clamps_negative() -> None:
    assert _rational_time(-1.0, 30.0) == "0/30s"


def test_assign_subtitles_basic():
    cuts = [CutSegment(source_in=0.0, source_out=5.0), CutSegment(source_in=6.0, source_out=10.0)]
    subs = [
        SubtitleSegment(start=0.5, end=2.0, text="A"),
        SubtitleSegment(start=7.0, end=9.0, text="B"),
        SubtitleSegment(start=4.0, end=7.5, text="overlap"),
    ]
    result = _assign_subtitles_to_cuts(cuts, subs)
    assert len(result) == 2
    cut0, subs0 = result[0]
    assert cut0.source_in == 0.0
    assert [(s.text, round(o, 2)) for s, o in subs0] == [("A", 0.5), ("overlap", 4.0)]
    cut1, subs1 = result[1]
    assert [(s.text, round(o, 2)) for s, o in subs1] == [("overlap", 0.0), ("B", 1.0)]


def test_assign_subtitles_drops_outside():
    cuts = [CutSegment(source_in=5.0, source_out=10.0)]
    subs = [SubtitleSegment(start=0.0, end=1.0, text="before")]
    result = _assign_subtitles_to_cuts(cuts, subs)
    assert result[0][1] == []


def test_build_fcpxml_no_subtitles():
    media = _make_media()
    cuts = [CutSegment(source_in=0.0, source_out=10.0)]
    xml = build_fcpxml(media, cuts)
    root = ET.fromstring(xml)
    assert root.tag == "fcpxml"
    assert root.get("version") == "1.10"
    spines = root.findall(".//spine")
    assert len(spines) == 1
    clips = spines[0].findall("asset-clip")
    assert len(clips) == 1
    assert clips[0].get("duration") == "300/30s"
    titles = spines[0].findall(".//title")
    assert titles == []


def test_build_fcpxml_multiple_cuts():
    media = _make_media()
    cuts = [
        CutSegment(source_in=0.0, source_out=2.8),
        CutSegment(source_in=5.2, source_out=10.0),
    ]
    xml = build_fcpxml(media, cuts)
    root = ET.fromstring(xml)
    clips = root.findall(".//spine/asset-clip")
    assert len(clips) == 2
    assert clips[0].get("start") == "0/30s"
    assert clips[0].get("duration") == "84/30s"
    assert clips[1].get("offset") == "84/30s"
    assert clips[1].get("start") == "156/30s"
    assert clips[1].get("duration") == "144/30s"


def test_build_fcpxml_with_subtitles():
    media = _make_media()
    cuts = [CutSegment(source_in=0.0, source_out=10.0)]
    subs = [
        SubtitleSegment(start=0.5, end=2.0, text="Hello"),
        SubtitleSegment(start=3.0, end=5.0, text="World"),
    ]
    xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
    root = ET.fromstring(xml)
    text_defs = root.findall(".//text-style-def")
    assert len(text_defs) == 1
    titles = root.findall(".//spine/asset-clip/title")
    assert len(titles) == 2
    texts = [t.find("text/text-style").text for t in titles]
    assert texts == ["Hello", "World"]
    assert titles[0].get("offset") == "15/30s"
    assert titles[0].get("lane") == "1"


def test_build_fcpxml_subtitle_clipped_to_cut():
    media = _make_media()
    cuts = [CutSegment(source_in=2.0, source_out=8.0)]
    subs = [SubtitleSegment(start=0.0, end=3.0, text="clip-start")]
    xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
    root = ET.fromstring(xml)
    titles = root.findall(".//spine/asset-clip/title")
    assert len(titles) == 1
    assert titles[0].get("offset") == "0/30s"
    assert titles[0].get("duration") == "30/30s"


def test_build_fcpxml_xml_is_valid_utf8():
    media = _make_media()
    cuts = [CutSegment(source_in=0.0, source_out=10.0)]
    subs = [SubtitleSegment(start=1.0, end=2.0, text="안녕하세요 세계")]
    xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
    assert "안녕하세요 세계" in xml
    assert xml.startswith("<?xml")


def test_build_fcpxml_no_cuts():
    media = _make_media()
    xml = build_fcpxml(media, [])
    root = ET.fromstring(xml)
    clips = root.findall(".//spine/asset-clip")
    assert clips == []
