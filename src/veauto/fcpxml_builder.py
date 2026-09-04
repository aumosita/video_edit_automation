"""FCPXML 1.10 builder.

Generates a Final Cut Pro XML file from the pipeline's intermediate
data (cut segments + optional subtitle segments). The output is
DTD-compatible with Final Cut Pro's importer — earlier versions put
``<text-style-def>`` under ``<resources>`` and emitted Motion-only
attributes (``relativeTo`` etc.) on ``<text-style>``, both of which
caused FCP to reject the import with ``DTD validation failed``.

Current shape
-------------
::

    <fcpxml version="1.10">
      <resources>
        <format id="FFVideoFormat1920x1080p30" .../>
        <asset .../>
      </resources>
      <library>
        <event name="...">
          <project name="...">
            <sequence format="FFVideoFormat1920x1080p30"
                      duration="..." tcStart="0s" tcFormat="NDF">
              <spine>
                <asset-clip ...>
                  <title ref="r3" lane="1" offset="..." duration="...">
                    <param name="Position"
                           key="9999/999166631/999166633/1/100/101"
                           value="0 -300"/>
                    <text>
                      <text-style ref="tsN" font="..." fontSize="56" ...>
                        Hello
                      </text-style>
                    </text>
                    <text-style-def id="tsN">
                      <text-style font="..." fontSize="56" .../>
                    </text-style-def>
                  </title>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </event>
      </library>
    </fcpxml>

Subtitles use the **Basic Title** template — the only Apple title
template with no built-in fade — and are moved to the bottom of the
frame with FCP's real ``Position`` parameter key (taken from a genuine
FCP subtitle export).

Rational times, including fractional (NTSC) frame rates, are emitted on
the exact 1001-based timebase to avoid cumulative drift between audio
and subtitles.
"""

from __future__ import annotations

from lxml import etree

from .models import CutSegment

_FCPXML_VERSION = "1.10"


_NTSC_MULTIPLES = (24, 30, 60, 120)
_NTSC_EPSILON = 0.001
_NTSC_TIMESCALES = frozenset(m * 1000 for m in _NTSC_MULTIPLES)


def _timebase(frame_rate: float) -> tuple[int, int]:
    """Map a frame rate to ``(nominal_fps, timescale)``.

    Fractional (NTSC) rates such as 29.97 are really ``30000/1001``.
    Representing them with a plain ``N/30s`` rational accumulates a
    0.1 % timing error — roughly 0.6 s over a 10-minute video — which
    desynchronises every title against the audio. The correct FCPXML
    representation uses the 1001-based timebase (``1001/30000s`` per
    frame at 29.97 fps), which is what Apple's own exporters emit.

    Returns
    -------
    (nominal_fps, timescale)
        ``nominal_fps`` is the rounded integer rate used to count
        frames; ``timescale`` is the rational denominator (``30000``
        for 29.97 fps, ``30`` for a plain 30 fps).
    """
    for mult in _NTSC_MULTIPLES:
        exact = mult * 1000 / 1001  # 30000/1001 = 29.9697…, etc.
        if abs(frame_rate - exact) < _NTSC_EPSILON:
            return mult, mult * 1000
    nominal = int(round(frame_rate))
    return nominal, nominal


def _rational_time(seconds: float, frame_rate: float) -> str:
    """Convert seconds to FCPXML rational time ``"N/Ds"``.

    Fractional frame rates (23.976 / 29.97 / 59.94 / 119.88) are
    emitted on the 1001-based timebase so that no drift accumulates
    over long timelines.
    """
    if seconds < 0:
        seconds = 0.0
    nominal, timescale = _timebase(frame_rate)
    if timescale in _NTSC_TIMESCALES:
        # NTSC: count frames at the *exact* rate (30000/1001), then
        # emit frames × 1001 / timescale. Counting at the nominal
        # 30 fps would reintroduce the 0.1 % drift this fixes.
        frames = round(seconds * timescale / 1001)
        if frames < 0:
            frames = 0
        return f"{frames * 1001}/{timescale}s"
    frames = round(seconds * nominal)
    if frames < 0:
        frames = 0
    return f"{frames}/{timescale}s"


def _frame_duration(frame_rate: float) -> str:
    """Return the ``frameDuration`` rational for ``frame_rate``."""
    nominal, timescale = _timebase(frame_rate)
    numerator = 1001 if timescale in _NTSC_TIMESCALES else 1
    return f"{numerator}/{timescale}s"


def _assign_subtitles_to_cuts(cuts, subtitles):
    """Pair each subtitle with the cut segment that contains it.

    The ``cuts`` and ``subtitles`` must both be on the **same**
    timeline — either the original source timeline, or the
    compacted one produced by :func:`remap_subtitles`. Mixing
    timelines (e.g. comparing a remap subtitle's start against a
    raw cut's source_in) silently mis-orders everything and was
    the source of a long-standing "subtitle out of sync" bug.

    Returns ``[(cut, [(sub, offset_within_cut_seconds), ...]), ...]``,
    sorted by subtitle start time within each cut.
    """
    result = []
    for cut in cuts:
        local = []
        for sub in subtitles:
            if sub.end <= cut.source_in:
                continue
            if sub.start >= cut.source_out:
                continue
            clipped_start = max(sub.start, cut.source_in)
            offset = clipped_start - cut.source_in
            local.append((sub, offset))
        local.sort(key=lambda pair: pair[1])
        result.append((cut, local))
    return result


def _remap_cuts_to_compacted_timeline(cuts):
    """Return a copy of ``cuts`` with ``source_in`` / ``source_out``
    shifted onto the compacted (silence-removed) timeline.

    Useful when the caller already has remapped subtitles and
    needs the cut segments to live on the same timeline for
    pairing.
    """
    cuts_sorted = sorted(cuts, key=lambda c: c.source_in)
    cumulative: list[float] = []
    running = 0.0
    for c in cuts_sorted:
        cumulative.append(running)
        running += c.duration
    remapped = []
    for c, base in zip(cuts_sorted, cumulative, strict=True):
        remapped.append(
            CutSegment(
                source_in=base,
                source_out=base + c.duration,
            )
        )
    return remapped


def _fps_label(frame_rate: float) -> int:
    """Apple-style fps label: 29.97 → 2997, 30 → 30."""
    nominal, timescale = _timebase(frame_rate)
    if timescale in _NTSC_TIMESCALES:
        return int(round(frame_rate * 100))
    return nominal


def _build_resources(media, asset_id, effect_id):
    """Build the ``<resources>`` element: ``<format>``, ``<asset>`` and
    a stub ``<effect>`` pointing at Apple's **Basic Title** template.

    Returns the ``<format id>`` so the caller can wire it into
    ``<sequence format=…>``.

    Template choice — verified against a real Final Cut Pro export
    and the template file itself:

    * **Basic Title** (``.../Titles.localized/Bumper:Opener.localized/
      Basic Title.localized/Basic Title.moti``) contains **no build-in /
      build-out animation whatsoever** (grep of the .moti: zero "Build"
      parameters), so subtitles pop on/off exactly at their audio
      times. The templates tried before all carry a built-in *Fade*:
      "Text" (``Text Build In Animation`` enum default Fade=3) and
      "Lower Third Text". Basic Title is also the template FCP itself
      used for the user's own subtitle titles in a genuine FCP export.
    * Vertical placement is NOT from the template: each title is moved
      to the bottom (or top/center) with a ``<param name="Position">``
      override using FCP's real hierarchical key — see
      ``_TITLE_POSITION_KEY``.
    """
    resources = etree.Element("resources")

    fmt = etree.SubElement(resources, "format")
    # Use a descriptive ID that looks like what Apple's own exporters
    # emit. Some FCP versions reject very short IDs (e.g. "r1") with
    # an "Encountered an unexpected value" warning even though the
    # FCPXML 1.10 DTD allows them. The "<width>x<height>p<fps>"
    # form is what iMovie / Final Cut Pro emit in their own
    # iMovieEffectExportMap.xml resources.
    fmt_id = f"FFVideoFormat{media.width}x{media.height}p{_fps_label(media.frame_rate)}"
    fmt.set("id", fmt_id)
    fmt.set("name", fmt_id)
    # Use the exact NTSC timebase ("1001/30000s" at 29.97 fps) so no
    # drift accumulates between audio and titles on long timelines.
    fmt.set("frameDuration", _frame_duration(media.frame_rate))
    # FCP's strict importer often rejects <format> elements that are
    # missing the standard attributes iMovie always emits. We set
    # them all so the format is unambiguous on import.
    fmt.set("fieldOrder", "progressive")
    fmt.set("width", str(media.width))
    fmt.set("height", str(media.height))
    fmt.set("paspH", "1")
    fmt.set("paspV", "1")
    # Best-guess color space based on resolution. FCP will accept
    # the value and re-encode media against the project setting
    # if the source doesn't match.
    if media.width >= 3840:
        fmt.set("colorSpace", "9-16-9 (Rec. 2020)")
    elif media.width >= 1920:
        fmt.set("colorSpace", "1-1-1 (Rec. 709)")
    else:
        fmt.set("colorSpace", "6-1-6 (Rec. 601 (NTSC))")
    fmt.set("projection", "none")
    fmt.set("stereoscopic", "mono")

    asset = etree.SubElement(resources, "asset")
    asset.set("id", asset_id)
    asset.set("name", media.path.name)
    # NOTE: we deliberately do NOT emit ``src`` on the <asset> element.
    # Apple's FCPXML 1.10 DTD does not declare it on <asset>; the file
    # path belongs on <media-rep src="…"> instead, where the DTD
    # requires it.
    asset.set("duration", _rational_time(media.duration, media.frame_rate))
    asset.set("hasVideo", "1")
    asset.set("hasAudio", "1" if media.has_audio else "0")
    media_rep = etree.SubElement(asset, "media-rep")
    media_rep.set("kind", "original-media")
    media_rep.set("src", f"file://{media.path.resolve()}")

    # Stub effect so <title ref="..."> can point at it. The DTD
    # declares ``<effect id ID #REQUIRED uid CDATA #REQUIRED>``,
    # so both attributes are mandatory.
    #
    # **Basic Title** is the only Apple title template verified to be
    # completely static (no built-in fade — see this function's
    # docstring). Its uid path is the real location inside FCP's
    # PETemplates bundle, and it is exactly the <effect> found in a
    # genuine FCP subtitle export.
    effect = etree.SubElement(resources, "effect")
    effect.set("id", effect_id)
    effect.set("name", "Basic Title")
    effect.set(
        "uid",
        ".../Titles.localized/Bumper:Opener.localized/"
        "Basic Title.localized/Basic Title.moti",
    )

    return resources, fmt_id


_TITLE_POSITION_KEY = (
    "9999/999166631/999166633/1/100/101"
)
"""FCP's real hierarchical key for a title's **Position** parameter.

Ground truth: a genuine Final Cut Pro subtitle export
(``<param name="Position" key="9999/999166631/999166633/1/100/101"
value="0 -300"/>`` on a 1080p timeline placed the title in the lower
third). Simple keys like ``key="100"`` are silently ignored by FCP —
this full path is required for the override to bind.
"""


def _title_position_value(subtitle_style, media_height):
    """Return the ``"X Y"`` Position override for a title.

    Titles anchor at the frame center by default; Motion's Y axis
    points **up**, so bottom placement is a negative offset. Verified
    against a real FCP export: ``0 -300`` on a 1080-high timeline lands
    the title in the lower third (300 / 1080 ≈ 0.278 of the height).

    ``subtitle_style.offset_y`` is added in raw pixels so the web form's
    fine-tune field nudges from the computed base.
    """
    base = {
        "bottom": -round(media_height * 0.278),
        "center": 0,
        "top": round(media_height * 0.278),
    }.get(subtitle_style.position, -round(media_height * 0.278))
    y = base + int(subtitle_style.offset_y)
    return f"0 {y}"


def _add_titles_to_clip(
    clip, subs, cut, fr, effect_id, subtitle_style, media_height,
    timeline_offset=0.0,
):
    """Append one ``<title>`` per subtitle to ``clip`` (an asset-clip).

    Structure mirrors a genuine Final Cut Pro subtitle export exactly:

    * ``<title ref=effect_id>`` pointing at the **Basic Title** template
      — the only Apple title template with no built-in fade.
    * ``<param name="Position">`` with FCP's real hierarchical key moves
      the title off center to the bottom (or top) of the frame.
    * Inline ``<text-style ref>`` + ``<text-style-def id>`` pair carries
      the user's font/size/colour and is DTD-exact.

    Offset semantics (the subtitle-desync bug)
    ------------------------------------------
    An anchored item's ``offset`` is expressed in **its parent's time
    coordinate system**, and for an item attached to an ``asset-clip``
    that system is the clip's *source* timeline — it starts at the
    clip's ``start`` value, not at 0 and not at the clip's sequence
    ``offset``.

    So the correct value is ``cut.source_in + offset_within_cut``.

    Two earlier attempts were both wrong:

    * ``offset_within_cut`` alone → every clip's captions collapse to
      the clip's own head (each cut restarts at 0).
    * ``timeline_offset + offset_within_cut`` (the compacted sequence
      position) → correct only for the first clip, then drifts by
      exactly ``start - offset`` (i.e. the total silence removed so
      far). On a real 6-minute edit that reached −90 s, which is the
      "subtitle shows the wrong line" symptom.

    ``timeline_offset`` is therefore unused for placement and kept only
    to build stable, unique ``text-style-def`` ids.
    """
    style_attrs = subtitle_style.to_text_style_xml_attrs()
    position_value = _title_position_value(subtitle_style, media_height)
    for i, (sub, offset) in enumerate(subs):
        sub_dur = sub.end - sub.start
        remaining = (cut.source_out - cut.source_in) - offset
        sub_dur = min(sub_dur, remaining)
        if sub_dur <= 0:
            continue
        # Parent (clip-local) time coordinate: origin == clip's `start`.
        local = cut.source_in + offset
        uid = timeline_offset + offset
        style_id = f"ts{uid:.3f}-{i}".replace(".", "_").replace("-", "_")
        title = etree.SubElement(clip, "title")
        title.set("name", "Subtitle")
        # DTD requires ``ref`` on every <title> (points at the
        # stub <effect> in <resources>).
        title.set("ref", effect_id)
        title.set("lane", "1")
        title.set("offset", _rational_time(local, fr))
        title.set("duration", _rational_time(sub_dur, fr))
        # DTD content model for <title> is (param*, text*, ...):
        # params must come first.
        param = etree.SubElement(title, "param")
        param.set("name", "Position")
        param.set("key", _TITLE_POSITION_KEY)
        param.set("value", position_value)
        text_el = etree.SubElement(title, "text")
        ts = etree.SubElement(text_el, "text-style")
        ts.set("ref", style_id)
        for k, v in style_attrs.items():
            ts.set(k, v)
        ts.text = sub.text
        tsd = etree.SubElement(title, "text-style-def")
        tsd.set("id", style_id)
        ts_def = etree.SubElement(tsd, "text-style")
        for k, v in style_attrs.items():
            ts_def.set(k, v)


def _build_spine(media, cuts_with_subs, asset_id, effect_id, subtitle_style,
                 media_height):
    spine = etree.Element("spine")
    cursor = 0
    fr = media.frame_rate
    for cut, subs in cuts_with_subs:
        clip_dur = cut.source_out - cut.source_in
        clip = etree.SubElement(spine, "asset-clip")
        clip.set("name", media.path.name)
        clip.set("offset", _rational_time(cursor, fr))
        clip.set("ref", asset_id)
        clip.set("start", _rational_time(cut.source_in, fr))
        clip.set("duration", _rational_time(clip_dur, fr))
        if subs and subtitle_style is not None:
            _add_titles_to_clip(
                clip, subs, cut, fr, effect_id, subtitle_style,
                media_height, timeline_offset=cursor,
            )
        cursor += clip_dur
    return spine, cursor


def build_fcpxml(
    media,
    cuts,
    *,
    subtitles=None,
    subtitle_style=None,
    project_name="Auto Edit",
    event_name="veauto",
):
    """Build a DTD-compatible FCPXML 1.10 document.

    Subtitles are emitted as ``<title>`` elements anchored inside each
    kept ``asset-clip``, referencing the **Basic Title** template (the
    only Apple title template with no built-in fade) and moved to the
    bottom of the frame via FCP's real ``Position`` parameter key.
    """
    root = etree.Element("fcpxml", version=_FCPXML_VERSION)
    asset_id = "r2"
    effect_id = "r3"  # referenced by every <title>

    # 1. <resources>: format + asset + Basic Title effect stub.
    resources, fmt_id = _build_resources(media, asset_id, effect_id)
    root.append(resources)

    # 2. <library>: holds event → project → sequence → spine.
    library = etree.SubElement(root, "library")
    event = etree.SubElement(library, "event")
    event.set("name", event_name)
    project = etree.SubElement(event, "project")
    project.set("name", project_name)

    sequence = etree.SubElement(project, "sequence")
    sequence.set("format", fmt_id)
    # FCP's strict importer often expects the sequence to carry a
    # duration, tcStart, and tcFormat even though the DTD marks
    # them #IMPLIED. We emit them so the import is unambiguous.
    sequence.set("duration", _rational_time(media.duration, media.frame_rate))
    sequence.set("tcStart", "0s")
    sequence.set("tcFormat", "NDF")
    cuts_with_subs = _assign_subtitles_to_cuts(cuts, subtitles or [])
    spine, _ = _build_spine(
        media, cuts_with_subs, asset_id, effect_id, subtitle_style,
        media_height=media.height,
    )
    sequence.append(spine)
    xml_bytes = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )
    return xml_bytes.decode("utf-8")
