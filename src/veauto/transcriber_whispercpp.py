"""Speech-to-text via ``whisper.cpp`` with Metal (Apple GPU) acceleration.

Why this module exists
----------------------

The default backend (:mod:`veauto.transcriber`, built on ``faster-whisper``
/ CTranslate2) has **no Apple GPU support**: CTranslate2 rejects
``device="mps"`` outright with ``ValueError: unsupported device mps`` and
its roadmap has no Metal backend, so on Apple Silicon it silently falls
back to CPU. See https://github.com/SYSTRAN/faster-whisper/issues/911.

``whisper.cpp`` ships a ggml Metal backend that Homebrew *does* build for
arm64 macOS, so the same Whisper weights run on the M-series GPU. On an
M1 this is several times faster than the CPU path for the same model.

Design
------

``whisper.cpp`` is a CLI, not a library, so this module shells out to
``whisper-cli`` and parses its ``--output-json-full`` output. That keeps
the integration dependency-free (no Python bindings to keep in sync) and
means cancellation works through the same ``subprocess`` tracking the web
layer already applies to ffmpeg.

The public surface mirrors :mod:`veauto.transcriber` so the two backends
are drop-in interchangeable:

- :func:`resolve_model_path` — map a ``SubtitleConfig.model`` name onto a
  local ggml ``.bin`` file, downloading it on first use.
- :func:`transcribe` — facade with the same signature as
  :func:`veauto.transcriber.transcribe`, returning ``list[Word]``.
- :func:`parse_whispercpp_json` — pure function turning whisper.cpp JSON
  into ``list[Word]``; unit-testable without invoking the CLI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .models import SubtitleConfig, Word

logger = logging.getLogger(__name__)

# Default location for downloaded ggml weights. Mirrors the convention
# used by whisper.cpp's own ``models/download-ggml-model.sh``.
DEFAULT_MODEL_DIR = Path(
    os.environ.get("VEAUTO_WHISPERCPP_MODEL_DIR")
    or (Path.home() / ".cache" / "whisper-cpp")
)

# Map ``SubtitleConfig.model`` names onto ggml model files.
#
# whisper.cpp distributes a full-precision FP16 build and several
# quantised variants per size. We default to the FP16 build for the
# ``large`` model because Apple Silicon has a working Metal backend now:
# the previous CPU-only deployment needed the smaller turbo/q5_0 variant
# to be tractable, but with the GPU on the table the user almost always
# wants the most accurate weights.
#
# ``distil-large-v3`` still maps to the turbo variant — they are
# functionally equivalent: the turbo *is* the 4-layer-decoder
# distillation of large-v3 that ``distil-large-v3`` refers to in
# faster-whisper's vocabulary.
_MODEL_FILES: dict[str, str] = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
    "medium": "ggml-medium.bin",
    "large-v3": "ggml-large-v3.bin",
    "distil-large-v3": "ggml-large-v3-turbo-q5_0.bin",
}

_HF_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"

# whisper.cpp emits control tokens in the full JSON output. They carry no
# transcript text and must never reach ``Word``.
_SPECIAL_TOKEN_PREFIXES = ("[_", "<|")


class WhisperCppError(RuntimeError):
    """Raised when the ``whisper-cli`` invocation fails."""


class WhisperCppNotInstalled(WhisperCppError):
    """Raised when the ``whisper-cli`` binary cannot be found."""


def find_whisper_cli(explicit: str | None = None) -> str:
    """Locate the ``whisper-cli`` executable.

    Checks, in order: an explicit path, ``$VEAUTO_WHISPER_CLI``, then
    ``PATH``. Raises :class:`WhisperCppNotInstalled` with an actionable
    message when nothing is found — the binary is an external
    (Homebrew) dependency, not a Python one, so the error needs to tell
    the user how to install it.
    """
    candidate = explicit or os.environ.get("VEAUTO_WHISPER_CLI")
    if candidate:
        if Path(candidate).is_file():
            return candidate
        raise WhisperCppNotInstalled(
            f"whisper-cli not found at {candidate!r}. "
            "Unset VEAUTO_WHISPER_CLI or point it at the real binary."
        )
    found = shutil.which("whisper-cli")
    if found:
        return found
    raise WhisperCppNotInstalled(
        "whisper-cli not found on PATH. Install it with:\n"
        "    brew install whisper-cpp\n"
        "or set VEAUTO_WHISPER_CLI to the binary's path."
    )


def model_filename(model: str) -> str:
    """Return the ggml filename for a ``SubtitleConfig.model`` name."""
    try:
        return _MODEL_FILES[model]
    except KeyError:
        raise WhisperCppError(
            f"No whisper.cpp ggml mapping for model {model!r}. "
            f"Known: {sorted(_MODEL_FILES)}"
        ) from None


def resolve_model_path(
    model: str,
    *,
    model_dir: Path | None = None,
    download: bool = True,
) -> Path:
    """Return the local path to the ggml weights for ``model``.

    Downloads the file from the Hugging Face mirror on first use unless
    ``download=False`` (used by tests). The download is written to a
    temporary file and renamed into place so an interrupted transfer
    never leaves a truncated model behind — a truncated ggml file fails
    deep inside the CLI with an unhelpful error.
    """
    directory = Path(model_dir) if model_dir is not None else DEFAULT_MODEL_DIR
    path = directory / model_filename(model)
    if path.is_file():
        return path
    if not download:
        raise WhisperCppError(
            f"Model weights missing: {path}. Download them with:\n"
            f"    curl -L -o {path} {_HF_BASE_URL}{path.name}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    url = _HF_BASE_URL + path.name
    logger.info("Downloading whisper.cpp model %s -> %s", url, path)
    # ``curl`` rather than urllib: it handles the HF redirect chain,
    # resumes, and reports progress to the log without extra code.
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix=".dl-", suffix=".part", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ["curl", "-fsSL", "-o", str(tmp_path), url],
            check=True,
        )
        tmp_path.replace(path)
    except subprocess.CalledProcessError as exc:
        tmp_path.unlink(missing_ok=True)
        raise WhisperCppError(
            f"Failed to download {url} (curl exit {exc.returncode})"
        ) from exc
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("Downloaded %s (%.0f MB)", path, path.stat().st_size / 1e6)
    return path


def _is_special_token(text: str) -> bool:
    """Whether a whisper.cpp token is a control token rather than speech."""
    stripped = text.strip()
    return not stripped or stripped.startswith(_SPECIAL_TOKEN_PREFIXES)


def parse_whispercpp_json(data: dict[str, Any]) -> list[Word]:
    """Convert ``whisper-cli --output-json-full`` output to ``list[Word]``.

    whisper.cpp reports *tokens*, not words, and a single word is often
    several tokens — especially for CJK, where ``"귀환이"`` arrives as
    ``" 귀"``, ``"환"``, ``"이"``. Whisper's tokeniser marks a word
    boundary with a **leading space** on the token text, so tokens are
    merged into words by starting a new word whenever a token's raw text
    begins with a space (and closing the previous one).

    The merged word takes the first token's ``start``, the last token's
    ``end``, and the **minimum** probability of its tokens — the least
    confident piece is the honest confidence for the whole word, and
    downstream code uses it only for diagnostics / filtering.

    Segment-level entries without a ``tokens`` array (i.e. plain
    ``--output-json``) are emitted as a single ``Word`` per segment so
    the caller still gets usable, if coarse, timings.
    """
    words: list[Word] = []
    for seg in data.get("transcription") or []:
        tokens = seg.get("tokens")
        if not tokens:
            # No token detail: fall back to the segment as one "word".
            text = (seg.get("text") or "").strip()
            offs = seg.get("offsets") or {}
            start = _ms_to_s(offs.get("from"))
            end = _ms_to_s(offs.get("to"))
            if text and start is not None and end is not None and end > start:
                words.append(
                    Word(start=start, end=end, text=text, probability=1.0)
                )
            continue
        words.extend(_merge_tokens_to_words(tokens))
    return words


def _ms_to_s(value: Any) -> float | None:
    """Convert whisper.cpp's integer millisecond offset to seconds."""
    try:
        return max(0.0, float(value) / 1000.0)
    except (TypeError, ValueError):
        return None


def _merge_tokens_to_words(tokens: Sequence[dict[str, Any]]) -> list[Word]:
    """Merge a segment's tokens into whole words.

    See :func:`parse_whispercpp_json` for the boundary rule. Tokens whose
    timings are missing or inverted are skipped rather than clamped: a
    bogus timestamp is worse than a slightly shorter transcript, and the
    pipeline's snap-to-voice stage can't fix an inverted cue.
    """
    words: list[Word] = []
    cur_text: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None
    cur_prob: float = 1.0

    def _flush() -> None:
        nonlocal cur_text, cur_start, cur_end, cur_prob
        text = "".join(cur_text).strip()
        if text and cur_start is not None and cur_end is not None:
            # A word whose tokens all shared one timestamp would be
            # zero-length and fail ``Word``'s ``end > 0`` validation;
            # give it a minimal 1 ms so it survives and the subtitle
            # grouper can extend it.
            end = cur_end if cur_end > cur_start else cur_start + 0.001
            words.append(
                Word(
                    start=cur_start,
                    end=end,
                    text=text,
                    probability=max(0.0, min(1.0, cur_prob)),
                )
            )
        cur_text = []
        cur_start = None
        cur_end = None
        cur_prob = 1.0

    for tok in tokens:
        raw = tok.get("text") or ""
        if _is_special_token(raw):
            continue
        offs = tok.get("offsets") or {}
        start = _ms_to_s(offs.get("from"))
        end = _ms_to_s(offs.get("to"))
        if start is None or end is None:
            continue
        try:
            prob = float(tok.get("p", 1.0))
        except (TypeError, ValueError):
            prob = 1.0

        # A leading space marks a new word in Whisper's tokeniser.
        if raw.startswith(" ") and cur_text:
            _flush()
        if cur_start is None:
            cur_start = start
            cur_prob = prob
        else:
            cur_prob = min(cur_prob, prob)
        cur_text.append(raw)
        cur_end = max(cur_end or end, end)

    _flush()
    return words


def build_command(
    whisper_cli: str,
    model_path: Path,
    audio_path: Path,
    out_prefix: Path,
    *,
    language: str | None,
    beam_size: int,
    threads: int | None = None,
    vad_filter: bool = True,
    condition_on_previous_text: bool = True,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """Assemble the ``whisper-cli`` argv.

    Option mapping from the ``faster-whisper`` vocabulary:

    * ``language=None`` → ``-l auto`` (whisper.cpp's auto-detect).
    * ``beam_size``     → ``-bs``.
    * ``condition_on_previous_text=False`` → ``-mc 0``, which zeroes the
      carried-over text context (whisper.cpp has no dedicated flag; the
      max-context knob is the equivalent lever).
    * ``vad_filter`` is **not** mapped to whisper.cpp's ``--vad``: that
      requires a separate Silero VAD model file. The pipeline already
      runs its own ffmpeg-based silence detection and snap-to-voice
      correction, so token timings are corrected downstream regardless.

    ``-ojf`` (``--output-json-full``) is mandatory — the plain ``-oj``
    output has no token array and therefore no word-level timings.
    """
    cmd = [
        whisper_cli,
        "-m", str(model_path),
        "-f", str(audio_path),
        "-bs", str(max(1, int(beam_size))),
        "-ojf",                       # full JSON incl. per-token timings
        "--output-file", str(out_prefix),
        "-l", language or "auto",
        "-np",                        # no progress prints; we log instead
    ]
    if threads and threads > 0:
        cmd += ["-t", str(threads)]
    if not condition_on_previous_text:
        cmd += ["-mc", "0"]
    cmd += list(extra_args)
    return cmd


def transcribe_with_cli(
    audio_path: Any,
    config: SubtitleConfig,
    *,
    whisper_cli: str | None = None,
    model_dir: Path | None = None,
    threads: int | None = None,
    vad_filter: bool = True,
    condition_on_previous_text: bool = True,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    download: bool = True,
) -> list[Word]:
    """Run ``whisper-cli`` on ``audio_path`` and return ``list[Word]``.

    ``runner`` defaults to :func:`subprocess.run` and exists so tests can
    inject a fake without spawning the real binary. It is called with the
    argv list and ``capture_output=True, text=True``.

    The JSON is written next to a temporary prefix and read back, then
    cleaned up: whisper.cpp has no "write JSON to stdout" mode, and
    dropping the file in the audio's directory would litter the user's
    upload folder.
    """
    audio = Path(audio_path)
    cli = find_whisper_cli(whisper_cli)
    model_path = resolve_model_path(
        config.model, model_dir=model_dir, download=download
    )
    run = runner or subprocess.run

    with tempfile.TemporaryDirectory(prefix="veauto-whispercpp-") as tmpdir:
        out_prefix = Path(tmpdir) / "out"
        cmd = build_command(
            cli, model_path, audio, out_prefix,
            language=config.language,
            beam_size=config.beam_size,
            threads=threads,
            vad_filter=vad_filter,
            condition_on_previous_text=condition_on_previous_text,
        )
        logger.info("whisper.cpp: %s", " ".join(cmd))
        proc = run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise WhisperCppError(
                f"whisper-cli failed (rc={proc.returncode}):\n"
                f"{stderr[-2000:]}"
            )
        json_path = out_prefix.with_suffix(".json")
        if not json_path.is_file():
            raise WhisperCppError(
                f"whisper-cli produced no JSON at {json_path}. "
                f"stderr:\n{(proc.stderr or '')[-2000:]}"
            )
        data = json.loads(json_path.read_text(encoding="utf-8"))

    words = parse_whispercpp_json(data)
    logger.info(
        "whisper.cpp produced %d words from %s", len(words), audio.name
    )
    return words


def transcribe(
    audio_path: Any,
    config: SubtitleConfig,
    *,
    vad_filter: bool | None = None,
    condition_on_previous_text: bool | None = None,
    **kwargs: Any,
) -> list[Word]:
    """Signature-compatible drop-in for :func:`veauto.transcriber.transcribe`.

    The pipeline calls the transcriber with ``(audio_path, config,
    vad_filter=..., condition_on_previous_text=...)``; accepting and
    forwarding those keeps this backend swappable with the
    faster-whisper one without touching :mod:`veauto.pipeline`.
    """
    return transcribe_with_cli(
        audio_path,
        config,
        vad_filter=True if vad_filter is None else bool(vad_filter),
        condition_on_previous_text=(
            True if condition_on_previous_text is None
            else bool(condition_on_previous_text)
        ),
        **kwargs,
    )
