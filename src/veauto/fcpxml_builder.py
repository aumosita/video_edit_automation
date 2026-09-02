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
        <format .../>
        <asset .../>
        <effect id="r3" name="Centered"
                uid=".../Titles.localized/.../Centered.moti"/>
      </resources>
      <library>
        <event name="...">
          <project name="...">
            <sequence format="r1">
              <spine>
                <asset-clip ...>
                  <title ref="r3" lane="1"
                          offset="..." duration="...">
                    <text>
                      <text-style font="..." fontSize="48" ...>
                        Hello
                      </text-style>
                    </text>
                  </title>
                </asset-clip>
              </spine>
            </sequence>
          </project>
        </event>
      </library>
    </fcpxml>
"""

from __future__ import annotations

from lxml import etree

_FCPXML_VERSION = "1.10"


def _rational_time(seconds: float, frame_rate: float) -> str:
    """Convert seconds to FCPXML rational time ``"N/Ds"``."""
    if seconds < 0:
        seconds = 0.0
    frames = round(seconds * frame_rate)
    if frames < 0:
        frames = 0
    return f"{frames}/{int(round(frame_rate))}s"


def _assign_subtitles_to_cuts(cuts, subtitles):
    """Pair each subtitle with the cut segment that contains it.

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


def _build_resources(media, asset_id, effect_id):
    """Build the ``<resources>`` element. Only DTD-permitted children:
    ``<format>``, ``<asset>``, ``<effect>``, ``<media>``, ``<locator>``.

    Returns the ``<format id>`` so the caller can wire it into
    ``<sequence format=…>``.

    A stub ``<effect>`` is also emitted because FCPXML 1.10 requires
    every ``<title>`` to carry a ``ref`` attribute pointing at an
    effect. The real visual style lives in each title's inlined
    ``<text-style>`` — the stub effect only exists to satisfy the DTD.
    """
    fr_int = int(round(media.frame_rate))
    resources = etree.Element("resources")

    fmt = etree.SubElement(resources, "format")
    # Use a descriptive ID that looks like what Apple's own exporters
    # emit. Some FCP versions reject very short IDs (e.g. "r1") with
    # an "Encountered an unexpected value" warning even though the
    # FCPXML 1.10 DTD allows them. The "<width>x<height>p<fps>"
    # form is what iMovie / Final Cut Pro emit in their own
    # iMovieEffectExportMap.xml resources.
    fmt_id = f"FFVideoFormat{media.width}x{media.height}p{fr_int}"
    fmt.set("id", fmt_id)
    fmt.set("name", fmt_id)
    fmt.set("frameDuration", f"1/{fr_int}s")
    fmt.set("width", str(media.width))
    fmt.set("height", str(media.height))

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
    # so both attributes are mandatory. We use the well-known
    # Motion "Centered" title UID that ships with Final Cut Pro
    # and iMovie — the same pattern Apple uses in its own
    # exports. FCP will resolve the ref to this built-in effect
    # and ignore our inlined <text-style> (we accept that the
    # title font is whatever FCP picks from this template, since
    # a custom <text-style> cannot be applied to a built-in
    # template via the public FCPXML 1.10 DTD). Position, size,
    # and content are the parts the user actually sees.
    effect = etree.SubElement(resources, "effect")
    effect.set("id", effect_id)
    effect.set("name", "Centered")
    effect.set(
        "uid",
        ".../Titles.localized/Build In:Out.localized/"
        "Centered.localized/Centered.moti",
    )

    return resources, fmt_id


def _add_titles_to_clip(
    clip, subs, cut, fr, effect_id, subtitle_style
):
    """Append one ``<title>`` per subtitle to ``clip`` (an asset-clip).

    Notes
    -----
    * The visual style is inlined into each title's
      ``<text><text-style>...</text-style></text>`` so that the
      document does not need a separate ``<text-style-def>``
      element — Apple's FCPXML 1.10 DTD only allows
      ``<text-style-def>`` inside a ``<title>``.
    * Subtitle placement (top/center/bottom) is NOT emitted on the
      ``<title>`` element: the DTD does not declare a ``position``
      attribute on ``<title>`` (it is a Motion extension). We let FCP
      use its default placement (lower third) and rely on the user
      to drag titles in the NLE if a different position is desired.
      The visual style of each title — which the user does care
      about — is preserved verbatim.
    """
    style_attrs = subtitle_style.to_text_style_xml_attrs()
    for sub, offset in subs:
        sub_dur = sub.end - sub.start
        remaining = (cut.source_out - cut.source_in) - offset
        sub_dur = min(sub_dur, remaining)
        if sub_dur <= 0:
            continue
        title = etree.SubElement(clip, "title")
        title.set("name", "Subtitle")
        # DTD requires ``ref`` on every <title> (points at the
        # stub <effect> in <resources>). The real visual style is
        # the inlined <text-style> below.
        title.set("ref", effect_id)
        title.set("lane", "1")
        title.set("offset", _rational_time(offset, fr))
        title.set("duration", _rational_time(sub_dur, fr))
        text_el = etree.SubElement(title, "text")
        ts = etree.SubElement(text_el, "text-style")
        for k, v in style_attrs.items():
            ts.set(k, v)
        ts.text = sub.text


def _build_spine(media, cuts_with_subs, asset_id, effect_id, subtitle_style):
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

    No ``<text-style-def>`` is emitted. Apple's FCPXML 1.10 DTD only
    permits ``<text-style-def>`` inside a ``<title>``, not at the
    project or resources level. Instead, the visual style is inlined
    into each title's ``<text><text-style>`` so the file is
    self-contained and survives FCP's DTD validation.
    """
    root = etree.Element("fcpxml", version=_FCPXML_VERSION)
    asset_id = "r2"
    effect_id = "r3"  # referenced by every <title>

    # 1. <resources>: format + asset + stub effect (DTD-allowed).
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
    cuts_with_subs = _assign_subtitles_to_cuts(cuts, subtitles or [])
    spine, _ = _build_spine(
        media, cuts_with_subs, asset_id, effect_id, subtitle_style
    )
    sequence.append(spine)
    xml_bytes = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )
    return xml_bytes.decode("utf-8")
