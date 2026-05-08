#!/usr/bin/env python3
"""Unit tests for _lib/prompt_safety.py."""

import unittest

from prompt_safety import smart_truncate, wrap_untrusted


class SmartTruncateTests(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(smart_truncate("hello", 100), "hello")
        self.assertEqual(smart_truncate("", 100), "")

    def test_none_returns_empty(self):
        self.assertEqual(smart_truncate(None, 100), "")

    def test_word_boundary_truncation(self):
        # Pin exact output. The cut lands at the last space before `limit - 1`
        # (rfind end is exclusive), so for limit=20 the result is
        # "the quick brown" + "…" — slightly under-fills rather than risking
        # mid-word chop on the next word.
        self.assertEqual(
            smart_truncate("the quick brown fox jumps over the lazy dog", 20),
            "the quick brown…",
        )

    def test_no_whitespace_hard_cut(self):
        self.assertEqual(smart_truncate("abcdefghijklmnop", 8), "abcdefg…")

    def test_strips_trailing_punctuation_before_ellipsis(self):
        # Cut before "brown", strip the trailing comma off "the quick,".
        self.assertEqual(smart_truncate("the quick, brown fox", 12), "the quick…")

    def test_only_leading_space_hard_cut(self):
        # The only whitespace is at position 0 — rfind returns 0, hits the
        # `cut <= 0` branch, falls through to hard cut.
        self.assertEqual(smart_truncate(" abcdefghij", 5), " abc…")

    def test_zero_limit_quirk(self):
        # Limit=0 produces s[:-1] + "…" because `s[:limit - 1]` is `s[:-1]`,
        # which keeps everything except the last character. This is *not*
        # aggressive truncation — it's a quirk of the off-by-one in the
        # hard-cut branch. Documented here so a future "fix" doesn't slip
        # through. Real call sites always pass a sensible limit (>= 80).
        self.assertEqual(smart_truncate("hello", 0), "hell…")

    def test_non_string_coerced(self):
        self.assertEqual(smart_truncate(42, 100), "42")


class WrapUntrustedTests(unittest.TestCase):

    def test_wraps_string(self):
        self.assertEqual(wrap_untrusted("hello"), {"_untrusted": True, "text": "hello"})

    def test_none_or_empty_becomes_empty_string(self):
        # Pins the `text or ""` coercion — both None and "" produce text="".
        # The dict key stays "_untrusted" — that's the wire-format contract
        # every sub-agent prompt depends on, NOT the function name.
        self.assertEqual(wrap_untrusted(None), {"_untrusted": True, "text": ""})
        self.assertEqual(wrap_untrusted(""), {"_untrusted": True, "text": ""})


if __name__ == "__main__":
    unittest.main()
