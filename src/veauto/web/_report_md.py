"""Compact Markdown reporter used by the web layer.

The web UI ships a small Markdown report with each job (in addition to
the JSON one). This module is kept separate from
:mod:`veauto.report` to avoid circular imports (the web layer is
allowed to call into ``veauto.report`` for the JSON form).
"""

from __future__ import annotations

from typing import Any


def render_markdown_report(data: dict[str, Any]) -> str:
    inp = data.get("input", {})
    sil = data.get("silence_removal", {})
    sub = data.get("subtitles", {})

    lines: list[str] = []
    lines.append(f"# veauto report — `{inp.get('path', '?')}`")
    lines.append("")
    lines.append("## Input")
    lines.append("")
    lines.append(f"- **Duration:** {inp.get('duration_s', 0):.2f}s")
    lines.append(
        f"- **Size:** {inp.get('width', 0)}×{inp.get('height', 0)}"
    )
    lines.append(
        f"- **Frame rate:** {inp.get('frame_rate', 0):.3f} fps"
    )
    lines.append(f"- **Has audio:** {inp.get('has_audio', False)}")
    lines.append("")

    lines.append("## Silence removal")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Silences detected | {sil.get('num_silences', 0)} |")
    lines.append(f"| Cut segments | {sil.get('num_cuts', 0)} |")
    lines.append(f"| Kept duration | {sil.get('kept_duration_s', 0):.2f}s |")
    lines.append(
        f"| Removed duration | {sil.get('removed_duration_s', 0):.2f}s |"
    )
    lines.append(
        f"| Removed ratio | "
        f"{sil.get('removed_ratio', 0) * 100:.1f}% |"
    )
    lines.append("")

    lines.append("## Subtitles")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Words (raw) | {sub.get('num_words', 0)} |")
    lines.append(f"| Subtitle lines | {sub.get('num_subtitles', 0)} |")
    lines.append(
        f"| Avg chars / line | "
        f"{sub.get('avg_chars_per_subtitle', 0)} |"
    )
    lines.append(f"| Avg duration | {sub.get('avg_duration_s', 0):.2f}s |")
    lines.append("")

    return "\n".join(lines)
