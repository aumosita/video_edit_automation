"""Unit tests for ``veauto.web.utils._output_basename``."""

from __future__ import annotations

from pathlib import Path

from veauto.web.utils import _output_basename


class TestOutputBasename:
    def test_simple_filename_strips_extension(self):
        assert _output_basename("talk.mp4") == "talk"

    def test_multiple_dots_only_strips_last_extension(self):
        # ``My.Talk.mp4`` → ``My.Talk`` (only the last extension is
        # removed; mid-name dots are preserved as part of the stem).
        assert _output_basename("My.Talk.mp4") == "My.Talk"

    def test_no_extension(self):
        assert _output_basename("clip") == "clip"

    def test_path_object_input(self):
        assert _output_basename(Path("/some/where/talk.mp4")) == "talk"

    def test_spaces_become_underscore(self):
        assert _output_basename("My Talk.mp4") == "My_Talk"

    def test_korean_falls_back_to_clip(self):
        # Non-ASCII characters are dropped; the stem becomes empty,
        # so the fallback fires.
        result = _output_basename("강의.mp4", fallback_id="abc12345")
        assert result == "clip_abc12345"

    def test_parentheses_and_punctuation(self):
        assert _output_basename("My Talk (v2).mp4") == "My_Talk_v2"

    def test_collapses_repeated_separators(self):
        assert _output_basename("a   b.mp4") == "a_b"
        assert _output_basename("a___b.mp4") == "a_b"
        # Dashes are *kept* (they are in the allowed set) and are
        # *not* collapsed — only underscores and disallowed runs
        # are merged.
        assert _output_basename("a---b.mp4") == "a---b"

    def test_strips_leading_trailing_punctuation(self):
        # Leading and trailing dots/underscores are stripped so the
        # filename doesn't start with a hidden file.
        assert _output_basename("..hidden..mp4") == "hidden"
        assert _output_basename("__weird__.mp4") == "weird"

    def test_empty_string_uses_fallback(self):
        assert _output_basename("", fallback_id="zzz99999") == "clip_zzz99999"

    def test_none_uses_fallback(self):
        assert _output_basename(None, fallback_id="deadbeef") == "clip_deadbeef"

    def test_only_special_chars_falls_back(self):
        assert _output_basename("@#$%.mp4", fallback_id="abc12345") == "clip_abc12345"

    def test_fallback_id_truncated_to_eight_chars(self):
        # Very long ids are clipped to 8 chars for legibility.
        assert _output_basename("", fallback_id="aabbccddeeff0011") == "clip_aabbccdd"

    def test_fallback_id_short_kept_as_is(self):
        assert _output_basename("", fallback_id="abc") == "clip_abc"

    def test_unicode_punctuation_dropped(self):
        # Common smart quotes / em-dash / etc. are all replaced.
        assert _output_basename("hello \u2014 world.mp4") == "hello_world"
        # A pure-ASCII name with a leading non-ASCII character.
        # The non-ASCII \u00bf is replaced with "_", the "_" gets
        # stripped off the front, leaving "Hello".
        assert _output_basename("\u00bfHello.mp4") == "Hello"
        # All-non-ASCII name falls back to the clip_ prefix.
        result = _output_basename("\u00bf\u00e1\u00fc.mp4", fallback_id="abc12345")
        assert result == "clip_abc12345"
