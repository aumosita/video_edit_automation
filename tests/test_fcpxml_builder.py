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
    # No <text-style-def> is emitted (style is inlined per <title>).
    assert root.findall(".//text-style-def") == []
    titles = root.findall(".//spine/asset-clip/title")
    assert len(titles) == 2
    texts = [t.find("text/text-style").text for t in titles]
    assert texts == ["Hello", "World"]
    assert titles[0].get("offset") == "15/30s"
    assert titles[0].get("lane") == "1"


def test_build_fcpxml_subtitle_clipped_to_cut():
    media = _make_media()
    cuts = [CutSegment(source_in=2.0, source_out=5.0)]
    # Subtitle starts in the removed silence (t<2.0) but extends into
    # the kept cut. The builder clips the start to cut.source_in (=2.0)
    # and the duration to the remaining cut length.
    subs = [SubtitleSegment(start=0.0, end=3.0, text="clip-start")]
    xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
    root = ET.fromstring(xml)
    titles = root.findall(".//spine/asset-clip/title")
    assert len(titles) == 1
    assert titles[0].get("offset") == "0/30s"
    # sub_dur (3.0s) is capped at remaining cut (5.0 - 2.0 = 3.0s)
    # → 3.0s = 90 frames at 30 fps.
    assert titles[0].get("duration") == "90/30s"


def test_build_fcpxml_xml_is_valid_utf8():
    media = _make_media()
    cuts = [CutSegment(source_in=0.0, source_out=10.0)]
    subs = [SubtitleSegment(start=1.0, end=2.0, text="안녕하세요 세계")]
    xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
    assert "안녕하세요 세계" in xml
    assert xml.startswith("<?xml")


# ---------------------------------------------------------------------------
# DTD compatibility (regression: FCP "DTD validation failed")
# ---------------------------------------------------------------------------


# Whitelist of attributes that the FCPXML 1.10 DTD allows on
# <text-style>. Source: Apple Final Cut Pro XML 1.10 reference.
_FCPXML_TEXT_STYLE_ATTRS = frozenset({
    "font", "fontSize", "fontFace",
    "fontColor", "strokeColor", "strokeWidth",
    "shadowColor", "shadowOffset", "shadowBlurRadius",
    "kerning", "tracking", "leading", "baseline",
    "bold", "italic",
    "alignment",
})

# Children allowed under <resources> by the FCPXML 1.10 DTD.
_FCPXML_RESOURCES_CHILDREN = frozenset({
    "format", "asset", "effect", "media", "locator",
})


class TestFcpDtdCompat:
    """Pin down the FCPXML 1.10 DTD rules that Final Cut Pro enforces
    on import. A failure here means the importer will reject the file
    with ``DTD validation failed`` — the bug that originally motivated
    this rewrite.
    """

    def test_resources_contains_no_text_style_def(self):
        media = _make_media()
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, [], subtitles=subs, subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        resources = root.find("resources")
        assert resources is not None
        for child in resources:
            assert child.tag in _FCPXML_RESOURCES_CHILDREN, (
                f"<{child.tag}> is not allowed inside <resources> by the "
                f"FCPXML 1.10 DTD; expected one of "
                f"{sorted(_FCPXML_RESOURCES_CHILDREN)}"
            )

    def test_no_text_style_def_in_document(self):
        """The FCPXML 1.10 DTD only allows ``<text-style-def>`` inside
        a ``<title>`` (not under ``<project>`` or ``<resources>``).
        We avoid the complexity by inlining the style into every
        ``<title>`` instead.
        """
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        assert root.find(".//text-style-def") is None

    def test_text_style_has_no_motion_only_attrs(self):
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        ts = root.find(".//spine/asset-clip/title/text/text-style")
        assert ts is not None
        for attr in ts.attrib:
            assert attr in _FCPXML_TEXT_STYLE_ATTRS, (
                f"<text-style> attribute {attr!r} is not in the FCPXML "
                f"1.10 DTD; FCP will reject the import."
            )
        # Explicitly check the three Motion-only attrs we removed.
        assert "relativeTo" not in ts.attrib
        assert "verticalAnchor" not in ts.attrib
        assert "horizontalAnchor" not in ts.attrib

    def test_style_is_inlined_per_title(self):
        """Without a ``<text-style-def>``, the style must be inlined
        into every title so the file is self-contained.
        """
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [
            SubtitleSegment(start=1.0, end=2.0, text="first"),
            SubtitleSegment(start=3.0, end=4.0, text="second"),
        ]
        style = SubtitleStyle(font="Arial", font_size=64)
        xml = build_fcpxml(media, cuts, subtitles=subs, subtitle_style=style)
        root = ET.fromstring(xml)
        titles = root.findall(".//spine/asset-clip/title")
        assert len(titles) == 2
        for t in titles:
            ts = t.find("text/text-style")
            assert ts is not None
            assert ts.get("font") == "Arial"
            assert ts.get("fontSize") == "64"

    def test_title_has_no_position_attribute(self):
        """The DTD does not declare ``position`` on ``<title>`` —
        it is a Motion-only extension. We rely on FCP's default
        placement (lower third) instead.
        """
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs,
                           subtitle_style=SubtitleStyle(position="top"))
        root = ET.fromstring(xml)
        title = root.find(".//spine/asset-clip/title")
        assert title is not None
        assert title.get("position") is None

    def test_title_has_only_dtd_declared_attrs(self):
        """The ``<title>`` element must only carry attributes that
        Apple's FCPXML 1.10 DTD declares. Currently: ``name``,
        ``lane``, ``offset``, ``start``, ``duration``, ``enabled``,
        ``ref``, ``role``.
        """
        ALLOWED = {"name", "lane", "offset", "start", "duration",
                   "enabled", "ref", "role"}
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs,
                           subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        title = root.find(".//spine/asset-clip/title")
        assert title is not None
        for attr in title.attrib:
            assert attr in ALLOWED, (
                f"<title> attribute {attr!r} is not in the FCPXML "
                f"1.10 DTD; FCP will reject the import."
            )

    def test_asset_has_no_src_attribute(self):
        """The DTD does not declare ``src`` on ``<asset>`` — the
        file path belongs on ``<media-rep src=…>``.
        """
        media = _make_media()
        xml = build_fcpxml(media, [])
        root = ET.fromstring(xml)
        asset = root.find("resources/asset")
        assert asset is not None
        assert "src" not in asset.attrib
        # The path lives on <media-rep> instead.
        media_rep = root.find("resources/asset/media-rep")
        assert media_rep is not None
        assert media_rep.get("src") is not None
        assert media_rep.get("src").startswith("file://")


def test_build_fcpxml_no_cuts():
    media = _make_media()
    xml = build_fcpxml(media, [])
    root = ET.fromstring(xml)
    clips = root.findall(".//spine/asset-clip")
    assert clips == []


class TestFcpTitleRef:
    """The DTD declares ``<title ref IDREF #REQUIRED>``. Each
    ``<title>`` we emit must reference the stub ``<effect>`` in
    ``<resources>``.
    """

    def test_every_title_has_ref_attribute(self):
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [
            SubtitleSegment(start=1.0, end=2.0, text="first"),
            SubtitleSegment(start=3.0, end=4.0, text="second"),
        ]
        xml = build_fcpxml(media, cuts, subtitles=subs,
                           subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        titles = root.findall(".//spine/asset-clip/title")
        assert len(titles) == 2
        for t in titles:
            assert t.get("ref") == "r3"

    def test_ref_resolves_to_a_real_effect(self):
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs,
                           subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        title_ref = root.find(".//spine/asset-clip/title").get("ref")
        effect = root.find(f'resources/effect[@id="{title_ref}"]')
        assert effect is not None, (
            f"title ref={title_ref!r} does not point to a real <effect>"
        )
        # The DTD also requires ``uid`` on every <effect>.
        assert effect.get("uid") is not None


class TestFcpFormatId:
    """The ``<format id>`` must be a meaningful, Apple-style identifier
    (e.g. ``FFVideoFormat1920x1080p30``) rather than the short
    ``r1`` we used to emit, because some FCP versions reject very
    short IDs with "Encountered an unexpected value" during the
    IDREF check.
    """

    def test_format_id_uses_ffvideoformat_pattern(self):
        media = _make_media()
        xml = build_fcpxml(media, [])
        root = ET.fromstring(xml)
        fmt = root.find("resources/format")
        assert fmt is not None
        fmt_id = fmt.get("id")
        assert fmt_id is not None
        assert fmt_id == fmt.get("name")
        assert fmt_id.startswith("FFVideoFormat")
        assert str(media.width) in fmt_id
        assert str(media.height) in fmt_id
        # The <sequence> must reference that same id.
        sequence = root.find("library/event/project/sequence")
        assert sequence is not None
        assert sequence.get("format") == fmt_id

    def test_format_has_standard_attributes(self):
        """Final Cut Pro's strict importer requires the standard set
        of attributes that iMovie always emits on a <format>
        element. Without them FCP shows "Encountered an unexpected
        value" even though the DTD marks them #IMPLIED.
        """
        media = _make_media()
        xml = build_fcpxml(media, [])
        root = ET.fromstring(xml)
        fmt = root.find("resources/format")
        assert fmt is not None
        for attr in ("frameDuration", "fieldOrder", "width", "height",
                     "paspH", "paspV", "colorSpace", "projection",
                     "stereoscopic"):
            assert fmt.get(attr) is not None, (
                f"<format> must carry {attr!r} for FCP strict import"
            )

    def test_sequence_has_duration_and_tc_attrs(self):
        """FCP's strict importer expects <sequence> to carry
        ``duration``, ``tcStart``, and ``tcFormat`` even though the
        DTD marks them #IMPLIED.
        """
        media = _make_media()
        xml = build_fcpxml(media, [])
        root = ET.fromstring(xml)
        sequence = root.find("library/event/project/sequence")
        assert sequence is not None
        for attr in ("duration", "tcStart", "tcFormat"):
            assert sequence.get(attr) is not None, (
                f"<sequence> must carry {attr!r} for FCP strict import"
            )

    def test_effect_uid_uses_apple_pattern(self):
        """The effect must use a ``…/Titles.localized/…/…moti``
        uid so Final Cut Pro recognises it as a built-in title.
        Earlier we emitted ``.../veauto.Subtitle.built-in`` which FCP
        rejected as "item could not be read". We also tried the
        ``Centered`` Build In/Out effect, which is a *transition*
        template — FCP loads it but the title shows no text. We
        now use ``Basic Title``, which is the plain centred title
        that ships in
        ``Final Cut Pro.app/.../PETemplates.localized/Titles.localized/
        Bumper:Opener.localized/Basic Title.localized/Basic Title.moti``.
        """
        media = _make_media()
        cuts = [CutSegment(source_in=0.0, source_out=10.0)]
        subs = [SubtitleSegment(start=1.0, end=2.0, text="hi")]
        xml = build_fcpxml(media, cuts, subtitles=subs,
                           subtitle_style=SubtitleStyle())
        root = ET.fromstring(xml)
        effect = root.find('resources/effect[@id="r3"]')
        assert effect is not None
        uid = effect.get("uid", "")
        assert uid.startswith(".../Titles.localized/"), (
            f"effect uid should follow Apple's .../Titles.localized/... pattern; "
            f"got {uid!r}"
        )
        assert "Basic Title" in uid, (
            f"effect uid should reference the 'Basic Title' template "
            f"so FCP renders actual text; got {uid!r}"
        )
        assert uid.endswith(".moti")


class TestRemapCutsToCompactedTimeline:
    """``_remap_cuts_to_compacted_timeline`` shifts cut ``source_in``
    / ``source_out`` onto a 0-based compacted timeline, the same
    transform :func:`remap_subtitles` applies to subtitle segments.
    The two must live on the same timeline or the per-cut pairing
    in ``_assign_subtitles_to_cuts`` silently mis-orders.
    """

    def test_empty_input_returns_empty(self):
        from veauto.fcpxml_builder import _remap_cuts_to_compacted_timeline
        assert _remap_cuts_to_compacted_timeline([]) == []

    def test_single_cut_starts_at_zero(self):
        from veauto.fcpxml_builder import _remap_cuts_to_compacted_timeline
        cut = CutSegment(source_in=10.0, source_out=15.0)
        out = _remap_cuts_to_compacted_timeline([cut])
        assert len(out) == 1
        assert out[0].source_in == 0.0
        assert out[0].source_out == 5.0

    def test_multiple_cuts_are_contiguous(self):
        from veauto.fcpxml_builder import _remap_cuts_to_compacted_timeline
        cuts = [
            CutSegment(source_in=0.0, source_out=3.0),
            CutSegment(source_in=4.0, source_out=10.0),
            CutSegment(source_in=20.0, source_out=21.0),
        ]
        out = _remap_cuts_to_compacted_timeline(cuts)
        assert [c.source_in for c in out] == [0.0, 3.0, 9.0]
        assert [c.source_out for c in out] == [3.0, 9.0, 10.0]

    def test_unordered_input_is_sorted(self):
        from veauto.fcpxml_builder import _remap_cuts_to_compacted_timeline
        # Cuts arrive out of order; the remap should still produce
        # a contiguous 0-based timeline.
        cuts = [
            CutSegment(source_in=20.0, source_out=21.0),
            CutSegment(source_in=0.0, source_out=3.0),
            CutSegment(source_in=4.0, source_out=10.0),
        ]
        out = _remap_cuts_to_compacted_timeline(cuts)
        assert [c.source_in for c in out] == [0.0, 3.0, 9.0]
