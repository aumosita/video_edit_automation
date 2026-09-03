"""Shared helpers for the web layer."""

from __future__ import annotations

import re
import uuid
from pathlib import Path


def _output_basename(input_name: str | Path, *, fallback_id: str | None = None) -> str:
    """Derive a filesystem-safe base name for the output artefacts.

    Rules
    -----
    1. Strip the file extension (``talk.mp4`` → ``talk``).
    2. Replace every run of characters outside ``[A-Za-z0-9._-]`` with a
       single underscore. This collapses spaces, parentheses,
       non-ASCII characters etc. to a safe ASCII identifier.
    3. Strip leading / trailing underscores and collapse
       ``..`` / ``__`` chains.
    4. If the result is empty, fall back to ``clip_<short-uuid>`` so we
       never produce an empty filename.

    The result is a *base* — callers append ``.fcpxml``, ``.srt`` etc.
    """
    if isinstance(input_name, Path):
        name = input_name.name
    else:
        name = str(input_name or "")
    # 1. Strip extension
    stem = Path(name).stem
    # 2. Replace disallowed runs with a single underscore
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    # 3. Collapse repeated underscores / leading-trailing _
    stem = re.sub(r"_+", "_", stem).strip("._-")
    if not stem:
        suffix = (fallback_id or uuid.uuid4().hex)[:8]
        return f"clip_{suffix}"
    return stem
