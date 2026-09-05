"""Tests for the whisper.cpp (Metal-accelerated) transcriber backend.

The real ``whisper-cli`` binary is an external Homebrew dependency and
the ggml weights are ~550 MB, so every test here either exercises a pure
function or injects a fake ``runner``. Nothing in this file spawns the
CLI or touches the network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from veauto.models import SubtitleConfig
from veauto.transcriber_whispercpp import (
    WhisperCppError,
    WhisperCppNotInstalled,
    build_command,
    find_whisper_cli,
    model_filename,
    parse_whispercpp_json,
    resolve_model_path,
    transcribe_with_cli,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tok(text: str, start_ms: int, end_ms: int, p: float = 1.0) -> dict:
    """Build one whisper.cpp full-JSON token entry."""
    return {
        "text": text,
        "offsets": {"from": start_ms, "to": end_ms},
        "p": p,
        "t_dtw": -1,
    }


def _payload(segments: list[dict]) -> dict:
    return {"systeminfo": "", "model": {}, "params": {},
            "result": {}, "transcription": segments}


# ---------------------------------------------------------------------------
# Model name / path resolution
# ---------------------------------------------------------------------------


class TestModelFilename:
    def test_known_models_map_to_ggml_files(self):
        assert model_filename("tiny") == "ggml-tiny.bin"
        assert model_filename("medium") == "ggml-medium.bin"

    def test_large_v3_maps_to_full_precision(self):
        # With the Metal backend on Apple Silicon, the full FP16 weights
        # are tractable (2.6x realtime on M1) so we default to them
        # rather than the smaller turbo distillation. Users who want
        # speed-over-accuracy can still ask for ``distil-large-v3``,
        # which keeps the turbo mapping.
        assert model_filename("large-v3") == "ggml-large-v3.bin"

    def test_distil_large_v3_maps_to_turbo(self):
        # distil-large-v3 is the faster-whisper name for what whisper.cpp
        # ships as the *turbo* distillation: 4-layer decoder, q5_0
        # quantised, ~547 MB. The mapping stays so the legacy option
        # still does the obvious thing.
        assert (
            model_filename("distil-large-v3")
            == "ggml-large-v3-turbo-q5_0.bin"
        )

    def test_unknown_model_raises(self):
        with pytest.raises(WhisperCppError, match="No whisper.cpp ggml mapping"):
            model_filename("gigantic-v9")


class TestResolveModelPath:
    def test_returns_existing_file(self, tmp_path: Path):
        f = tmp_path / "ggml-tiny.bin"
        f.write_bytes(b"\x00")
        assert resolve_model_path("tiny", model_dir=tmp_path) == f

    def test_missing_without_download_raises_with_curl_hint(self, tmp_path: Path):
        with pytest.raises(WhisperCppError) as exc:
            resolve_model_path("tiny", model_dir=tmp_path, download=False)
        # The message must be actionable — the user has to fetch a
        # ~550 MB blob and needs the exact command.
        assert "curl" in str(exc.value)
        assert "ggml-tiny.bin" in str(exc.value)


# ---------------------------------------------------------------------------
# CLI discovery
# ---------------------------------------------------------------------------


class TestFindWhisperCli:
    def test_explicit_path_used_when_present(self, tmp_path: Path):
        fake = tmp_path / "whisper-cli"
        fake.write_text("#!/bin/sh\n")
        assert find_whisper_cli(str(fake)) == str(fake)

    def test_explicit_missing_path_raises(self, tmp_path: Path):
        with pytest.raises(WhisperCppNotInstalled):
            find_whisper_cli(str(tmp_path / "nope"))

    def test_env_var_is_honoured(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "whisper-cli"
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setenv("VEAUTO_WHISPER_CLI", str(fake))
        assert find_whisper_cli() == str(fake)

    def test_missing_everywhere_mentions_brew(self, monkeypatch):
        monkeypatch.delenv("VEAUTO_WHISPER_CLI", raising=False)
        monkeypatch.setattr(
            "veauto.transcriber_whispercpp.shutil.which", lambda _: None
        )
        with pytest.raises(WhisperCppNotInstalled, match="brew install whisper-cpp"):
            find_whisper_cli()


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def _cmd(self, **kw):
        defaults = dict(
            whisper_cli="/usr/bin/whisper-cli",
            model_path=Path("/m/ggml.bin"),
            audio_path=Path("/a/in.wav"),
            out_prefix=Path("/t/out"),
            language="ko",
            beam_size=5,
        )
        defaults.update(kw)
        return build_command(**defaults)

    def test_requires_full_json_output(self):
        # -ojf, not -oj: the plain JSON has no token array and hence no
        # word-level timings, which the subtitle grouper depends on.
        assert "-ojf" in self._cmd()

    def test_core_flags_present(self):
        cmd = self._cmd()
        assert cmd[0] == "/usr/bin/whisper-cli"
        assert "-m" in cmd and "/m/ggml.bin" in cmd
        assert "-f" in cmd and "/a/in.wav" in cmd
        assert cmd[cmd.index("-bs") + 1] == "5"
        assert cmd[cmd.index("-l") + 1] == "ko"

    def test_language_none_becomes_auto(self):
        cmd = self._cmd(language=None)
        assert cmd[cmd.index("-l") + 1] == "auto"

    def test_beam_size_floored_at_one(self):
        cmd = self._cmd(beam_size=0)
        assert cmd[cmd.index("-bs") + 1] == "1"

    def test_threads_omitted_when_none(self):
        assert "-t" not in self._cmd(threads=None)

    def test_threads_included_when_set(self):
        cmd = self._cmd(threads=6)
        assert cmd[cmd.index("-t") + 1] == "6"

    def test_no_context_maps_to_max_context_zero(self):
        # whisper.cpp has no direct equivalent of
        # condition_on_previous_text; zeroing max-context is the lever.
        cmd = self._cmd(condition_on_previous_text=False)
        assert cmd[cmd.index("-mc") + 1] == "0"
        assert "-mc" not in self._cmd(condition_on_previous_text=True)


# ---------------------------------------------------------------------------
# JSON -> Word parsing (the interesting part)
# ---------------------------------------------------------------------------


class TestParseWhisperCppJson:
    """whisper.cpp reports *tokens*; the pipeline needs *words*.

    The merge rule is Whisper's own tokeniser convention: a leading
    space on a token marks the start of a new word. These tests pin the
    behaviour for Latin text, CJK sub-word splits, control tokens and
    the various degenerate timing cases seen in real output.
    """

    def test_empty_payload_returns_empty(self):
        assert parse_whispercpp_json({}) == []
        assert parse_whispercpp_json(_payload([])) == []

    def test_latin_tokens_merge_on_leading_space(self):
        seg = {"tokens": [
            _tok(" Hello", 0, 500, 0.9),
            _tok(" world", 500, 900, 0.8),
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        assert [w.text for w in words] == ["Hello", "world"]
        assert words[0].start == 0.0
        assert words[0].end == 0.5
        assert words[1].start == 0.5

    def test_cjk_subword_tokens_merge_into_one_word(self):
        # Real observed output: "귀환이" arrives as three tokens, only
        # the first of which has a leading space.
        seg = {"tokens": [
            _tok(" 귀", 1160, 1390, 0.85),
            _tok("환", 1440, 1630, 0.94),
            _tok("이", 1630, 1880, 0.99),
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        assert len(words) == 1
        assert words[0].text == "귀환이"
        # Span covers the whole word, not just the first token.
        assert words[0].start == pytest.approx(1.160)
        assert words[0].end == pytest.approx(1.880)

    def test_word_probability_is_min_of_tokens(self):
        seg = {"tokens": [
            _tok(" ab", 0, 100, 0.9),
            _tok("cd", 100, 200, 0.4),
            _tok("ef", 200, 300, 0.7),
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        # The least confident piece is the honest confidence for the
        # merged word.
        assert words[0].probability == pytest.approx(0.4)

    def test_special_tokens_are_dropped(self):
        seg = {"tokens": [
            _tok("[_BEG_]", 0, 0, 0.92),
            _tok(" real", 100, 400, 0.99),
            _tok("<|endoftext|>", 400, 400, 0.5),
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        assert [w.text for w in words] == ["real"]

    def test_zero_length_word_gets_minimal_duration(self):
        # Observed in real output: the first token of a segment can have
        # from == to. Word requires end > 0 and the grouper needs a
        # non-inverted span, so a 1 ms floor is applied.
        seg = {"tokens": [_tok(" 그", 380, 380, 0.59)]}
        words = parse_whispercpp_json(_payload([seg]))
        assert len(words) == 1
        assert words[0].end > words[0].start

    def test_tokens_with_missing_offsets_are_skipped(self):
        seg = {"tokens": [
            {"text": " broken", "p": 0.9},           # no offsets at all
            {"text": " alsobad", "offsets": {}, "p": 0.9},
            _tok(" good", 0, 100, 0.9),
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        assert [w.text for w in words] == ["good"]

    def test_non_numeric_probability_defaults_to_one(self):
        seg = {"tokens": [
            {"text": " hi", "offsets": {"from": 0, "to": 100}, "p": "bogus"},
        ]}
        words = parse_whispercpp_json(_payload([seg]))
        assert words[0].probability == 1.0

    def test_multiple_segments_are_concatenated(self):
        segs = [
            {"tokens": [_tok(" one", 0, 100)]},
            {"tokens": [_tok(" two", 100, 200)]},
        ]
        words = parse_whispercpp_json(_payload(segs))
        assert [w.text for w in words] == ["one", "two"]

    def test_segment_without_tokens_falls_back_to_segment_text(self):
        # Plain --output-json (no -ojf) has no token array. Rather than
        # returning nothing, emit one coarse Word per segment.
        seg = {"text": " a whole segment ",
               "offsets": {"from": 1000, "to": 4000}}
        words = parse_whispercpp_json(_payload([seg]))
        assert len(words) == 1
        assert words[0].text == "a whole segment"
        assert words[0].start == pytest.approx(1.0)
        assert words[0].end == pytest.approx(4.0)

    def test_tokenless_segment_with_bad_span_is_dropped(self):
        seg = {"text": "x", "offsets": {"from": 5000, "to": 5000}}
        assert parse_whispercpp_json(_payload([seg])) == []


# ---------------------------------------------------------------------------
# End-to-end with an injected runner
# ---------------------------------------------------------------------------


class TestTranscribeWithCli:
    """``transcribe_with_cli`` orchestration, with the CLI faked out."""

    @pytest.fixture
    def model_dir(self, tmp_path: Path) -> Path:
        (tmp_path / "ggml-tiny.bin").write_bytes(b"\x00")
        return tmp_path

    @pytest.fixture
    def cli(self, tmp_path: Path) -> str:
        p = tmp_path / "whisper-cli"
        p.write_text("#!/bin/sh\n")
        return str(p)

    def _cfg(self) -> SubtitleConfig:
        return SubtitleConfig(model="tiny", language="ko", beam_size=2)

    def test_parses_words_from_written_json(self, cli, model_dir, tmp_path):
        payload = _payload([{"tokens": [
            _tok(" hello", 0, 400, 0.95),
            _tok(" there", 400, 800, 0.90),
        ]}])

        def fake_run(cmd, **kwargs):
            # Mimic whisper-cli: write "<out_prefix>.json".
            out_prefix = Path(cmd[cmd.index("--output-file") + 1])
            out_prefix.with_suffix(".json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        words = transcribe_with_cli(
            tmp_path / "in.wav", self._cfg(),
            whisper_cli=cli, model_dir=model_dir, runner=fake_run,
        )
        assert [w.text for w in words] == ["hello", "there"]

    def test_nonzero_exit_raises_with_stderr(self, cli, model_dir, tmp_path):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "ggml: boom")

        with pytest.raises(WhisperCppError) as exc:
            transcribe_with_cli(
                tmp_path / "in.wav", self._cfg(),
                whisper_cli=cli, model_dir=model_dir, runner=fake_run,
            )
        assert "rc=3" in str(exc.value)
        assert "ggml: boom" in str(exc.value)

    def test_missing_json_raises(self, cli, model_dir, tmp_path):
        # Exit 0 but no output file — a silent failure mode worth
        # surfacing explicitly rather than returning zero words.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with pytest.raises(WhisperCppError, match="produced no JSON"):
            transcribe_with_cli(
                tmp_path / "in.wav", self._cfg(),
                whisper_cli=cli, model_dir=model_dir, runner=fake_run,
            )

    def test_temp_json_is_cleaned_up(self, cli, model_dir, tmp_path):
        seen: dict[str, Path] = {}

        def fake_run(cmd, **kwargs):
            out_prefix = Path(cmd[cmd.index("--output-file") + 1])
            seen["json"] = out_prefix.with_suffix(".json")
            seen["json"].write_text(json.dumps(_payload([])), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        transcribe_with_cli(
            tmp_path / "in.wav", self._cfg(),
            whisper_cli=cli, model_dir=model_dir, runner=fake_run,
        )
        # The scratch dir must not survive: the JSON for a long video is
        # multi-megabyte and would otherwise accumulate in /tmp.
        assert not seen["json"].exists()
        assert not seen["json"].parent.exists()

    def test_config_options_reach_the_command(self, cli, model_dir, tmp_path):
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            out_prefix = Path(cmd[cmd.index("--output-file") + 1])
            out_prefix.with_suffix(".json").write_text(
                json.dumps(_payload([])), encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        transcribe_with_cli(
            tmp_path / "in.wav", self._cfg(),
            whisper_cli=cli, model_dir=model_dir, runner=fake_run,
        )
        cmd = captured["cmd"]
        assert cmd[cmd.index("-l") + 1] == "ko"
        assert cmd[cmd.index("-bs") + 1] == "2"
        assert str(model_dir / "ggml-tiny.bin") in cmd
