"""FCPXML 1.10 builder.

Generates a Final Cut Pro XML file from the pipeline's intermediate data
(cut segments + optional subtitle segments). The output can be imported
directly into DaVinci Resolve, Final Cut Pro, or Adobe Premiere Pro.

Time representation in FCPXML is rational: ``"N/Ds"`` where N and D are
integers. The base time unit is the frame. We use the source media frame
rate as the denominator so that all offsets and durations are exact at
frame boundaries.
"""

from __future__ import annotations

from lxml import etree

# FCPXML uses this attribute on <asset-clip> to mark it as the source media.
_FCPXML_VERSION = "1.10"


def _rational_time(seconds: float, frame_rate: float) -> str:
    """Convert seconds to FCPXML rational time ``"N/Ds"``.

    Uses the source frame rate as the denominator so that all time values
    are exact at frame boundaries.
    """
    if seconds < 0:
        seconds = 0.0
    frames = round(seconds * frame_rate)
    if frames < 0:
        frames = 0
    return f"{frames}/{int(round(frame_rate))}s"


def _assign_subtitles_to_cuts(cuts, subtitles):
    """Assign each subtitle to its containing cut segment."""
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


def _build_resources(media, asset_id, text_style_def_id):
    fr_int = int(round(media.frame_rate))
    resources = etree.Element("resources")

    fmt = etree.SubElement(resources, "format")
    fmt.set("id", "r1")
    fmt.set("name", f"FFVideoFormat{media.width}x{media.height}p{fr_int}")
    fmt.set("frameDuration", f"1/{fr_int}s")
    fmt.set("width", str(media.width))
    fmt.set("height", str(media.height))

    asset = etree.SubElement(resources, "asset")
    asset.set("id", asset_id)
    asset.set("name", media.path.name)
    asset.set("src", f"file://{media.path.resolve()}")
    asset.set("duration", _rational_time(media.duration, media.frame_rate))
    asset.set("hasVideo", "1")
    asset.set("hasAudio", "1" if media.has_audio else "0")
    media_rep = etree.SubElement(asset, "media-rep")
    media_rep.set("kind", "original-media")
    media_rep.set("src", f"file://{media.path.resolve()}")

    return resources


def _build_text_style_def(style, def_id):
    text_def = etree.Element("text-style-def")
    text_def.set("id", def_id)
    text_def.set("name", style.font)
    text_style = etree.SubElement(text_def, "text-style")
    for k, v in style.to_text_style_xml_attrs().items():
        text_style.set(k, v)
    text_style.set("alignment", "center")
    text_style.set("relativeTo", style.position)
    text_style.set("verticalAnchor", style.position)
    text_style.set("horizontalAnchor", "center")
    return text_def


def _build_spine(media, cuts_with_subs, asset_id, text_style_def_id):
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
        if subs:
            _add_titles_to_clip(clip, subs, cut, text_style_def_id, fr)
        cursor += clip_dur
    return spine, cursor


def _add_titles_to_clip(clip, subs, cut, text_style_def_id, fr):
    for sub, offset in subs:
        sub_start = max(sub.start, cut.source_in)
        sub_dur = sub.end - sub_start
        remaining = (cut.source_out - cut.source_in) - offset
        sub_dur = min(sub_dur, remaining)
        if sub_dur <= 0:
            continue
        title = etree.SubElement(clip, "title")
        title.set("name", "Subtitle")
        title.set("lane", "1")
        title.set("offset", _rational_time(offset, fr))
        title.set("ref", text_style_def_id)
        title.set("duration", _rational_time(sub_dur, fr))
        text_el = etree.SubElement(title, "text")
        ts = etree.SubElement(text_el, "text-style")
        ts.set("ref", text_style_def_id)
        ts.text = sub.text


def build_fcpxml(media, cuts, *, subtitles=None, subtitle_style=None, project_name="Auto Edit", event_name="veauto"):
    root = etree.Element("fcpxml", version=_FCPXML_VERSION)
    asset_id = "r2"
    text_style_def_id = "r3"
    resources = _build_resources(media, asset_id, text_style_def_id)
    if subtitles is not None and subtitle_style is not None:
        text_def = _build_text_style_def(subtitle_style, text_style_def_id)
        resources.append(text_def)
    root.append(resources)
    library = etree.SubElement(root, "library")
    event = etree.SubElement(library, "event")
    event.set("name", event_name)
    project = etree.SubElement(event, "project")
    project.set("name", project_name)
    sequence = etree.SubElement(project, "sequence")
    sequence.set("format", "r1")
    cuts_with_subs = _assign_subtitles_to_cuts(cuts, subtitles or [])
    spine, _ = _build_spine(media, cuts_with_subs, asset_id, text_style_def_id)
    sequence.append(spine)
    xml_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return xml_bytes.decode("utf-8")
