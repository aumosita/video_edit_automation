"""Report generation for veauto pipelines.

A :class:`veauto.pipeline.PipelineResult` can be serialised to either a
JSON or a Markdown report. The reports are intended for human review
and CI dashboards — they contain high-level statistics (kept / removed
duration, subtitle count, average subtitle length) plus a compact
silence map and subtitle listing.

The module is intentionally side-effect-free: it only transforms an
in-memory :class:`PipelineResult` into strings. File I/O is the caller's
responsibility.
"""

from __future__ import annotations

import json
from typing import Literal

from .pipeline import PipelineResult

ReportFormat = Literal["json", "md", "markdown"]


def build_report_data(result: PipelineResult) -> dict:
    """Return a JSON-serialisable dict describing the run."""
    media = result.media
    removed = sorted(result.removed, key=lambda r: r.source_in)
    silences = [
        {
            "source_in": round(r.source_in, 3),
            "source_out": round(r.source_out, 3),
            "duration": round(r.duration, 3),
        }
        for r in removed
    ]
    cuts = [
        {
            "source_in": round(c.source_in, 3),
            "source_out": round(c.source_out, 3),
            "duration": round(c.duration, 3),
        }
        for c in result.cuts
    ]
    subtitles = [
        {
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "duration": round(s.duration, 3),
            "text": s.text,
        }
        for s in result.subtitles
    ]
    total_chars = sum(len(s.text) for s in result.subtitles)
    avg_chars = (total_chars / len(result.subtitles)) if result.subtitles else 0.0
    avg_dur = (
        sum(s.duration for s in result.subtitles) / len(result.subtitles)
        if result.subtitles
        else 0.0
    )
    return {
        "input": {
            "path": str(media.path),
            "duration_s": round(media.duration, 3),
            "width": media.width,
            "height": media.height,
            "frame_rate": media.frame_rate,
            "has_audio": media.has_audio,
        },
        "silence_removal": {
            "enabled": bool(removed or result.cuts),
            "num_silences": len(removed),
            "num_cuts": len(result.cuts),
            "kept_duration_s": round(result.kept_duration, 3),
            "removed_duration_s": round(result.removed_duration, 3),
            "removed_ratio": round(
                result.removed_duration / result.media.duration, 4
            ),
            "silences": silences,
            "cuts": cuts,
        },
        "subtitles": {
            "enabled": bool(result.subtitles or result.words),
            "num_words": len(result.words),
            "num_subtitles": len(result.subtitles),
            "avg_chars_per_subtitle": round(avg_chars, 2),
            "avg_duration_s": round(avg_dur, 3),
            "items": subtitles,
        },
    }


def render_json_report(result: PipelineResult, *, indent: int = 2) -> str:
    """Return a JSON report."""
    return json.dumps(build_report_data(result), indent=indent, ensure_ascii=False)


def render_markdown_report(result: PipelineResult) -> str:
    """Return a Markdown report."""
    data = build_report_data(result)
    inp = data["input"]
    sil = data["silence_removal"]
    sub = data["subtitles"]

    lines: list[str] = []
    lines.append(f"# veauto report — `{inp['path']}`")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    lines.append(f"- **Duration:** {inp['duration_s']:.2f}s")
    lines.append(f"- **Size:** {inp['width']}×{inp['height']}")
    lines.append(f"- **Frame rate:** {inp['frame_rate']:.3f} fps")
    lines.append(f"- **Has audio:** {inp['has_audio']}")
    lines.append("")

    lines.append("## Silence removal")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Silences detected | {sil['num_silences']} |")
    lines.append(f"| Cut segments | {sil['num_cuts']} |")
    lines.append(f"| Kept duration | {sil['kept_duration_s']:.2f}s |")
    lines.append(f"| Removed duration | {sil['removed_duration_s']:.2f}s |")
    lines.append(f"| Removed ratio | {sil['removed_ratio']*100:.1f}% |")
    lines.append("")

    if sil["silences"]:
        lines.append("### Silence intervals (first 20)")
        lines.append("")
        lines.append("| # | start | end | duration |")
        lines.append("| ---: | ---: | ---: | ---: |")
        for i, s in enumerate(sil["silences"][:20], 1):
            lines.append(
                f"| {i} | {s['source_in']:.2f}s | {s['source_out']:.2f}s | "
                f"{s['duration']:.2f}s |"
            )
        if len(sil["silences"]) > 20:
            lines.append(f"\n_…{len(sil['silences']) - 20} more (see JSON report)_")
        lines.append("")

    lines.append("## Subtitles")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Words (raw) | {sub['num_words']} |")
    lines.append(f"| Subtitle lines | {sub['num_subtitles']} |")
    lines.append(f"| Avg chars / line | {sub['avg_chars_per_subtitle']} |")
    lines.append(f"| Avg duration | {sub['avg_duration_s']:.2f}s |")
    lines.append("")

    if sub["items"]:
        lines.append("### Subtitle preview (first 30 lines)")
        lines.append("")
        lines.append("| # | start | end | duration | text |")
        lines.append("| ---: | ---: | ---: | ---: | --- |")
        for i, s in enumerate(sub["items"][:30], 1):
            text = s["text"].replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {i} | {s['start']:.2f}s | {s['end']:.2f}s | "
                f"{s['duration']:.2f}s | {text} |"
            )
        if len(sub["items"]) > 30:
            lines.append(f"\n_…{len(sub['items']) - 30} more (see JSON report)_")
        lines.append("")

    return "\n".join(lines)


def render_report(result: PipelineResult, fmt: ReportFormat) -> str:
    """Dispatch to :func:`render_json_report` or :func:`render_markdown_report`."""
    if fmt == "json":
        return render_json_report(result)
    if fmt in ("md", "markdown"):
        return render_markdown_report(result)
    raise ValueError(
        f"Unknown report format: {fmt!r}. Expected 'json', 'md', or 'markdown'."
    )


def write_report(result: PipelineResult, output_path, fmt: ReportFormat) -> str:
    """Render and write the report to ``output_path``.

    The file extension is **not** consulted; the caller picks the format
    via ``fmt``. The string that was written is returned.
    """
    from pathlib import Path

    body = render_report(result, fmt)
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return body

