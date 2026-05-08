#!/usr/bin/env python3
"""Unit tests for _lib/text_utils.py."""

import unittest

from text_utils import smart_truncate, untrusted


class SmartTruncateTests(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(smart_truncate("hello", 100), "hello")
        self.assertEqual(smart_truncate("", 100), "")

    def test_none_returns_empty(self):
        self.assertEqual(smart_truncate(None, 100), "")

    def test_word_boundary_truncation(self):
        s = "the quick brown fox jumps over the lazy dog"
        truncated = smart_truncate(s, 20)
        self.assertTrue(truncated.endswith("…"))
        # Result should not end with "<word> …" (extra space before ellipsis).
        self.assertNotIn(" …", truncated)
        self.assertLessEqual(len(truncated), 20)

    def test_no_whitespace_hard_cut(self):
        s = "abcdefghijklmnop"
        truncated = smart_truncate(s, 8)
        self.assertTrue(truncated.endswith("…"))
        self.assertEqual(len(truncated), 8)

    def test_strips_trailing_punctuation_before_ellipsis(self):
        # "the quick, brown fox" with limit 12 should cut before "brown" and
        # strip the trailing comma off "the quick,".
        truncated = smart_truncate("the quick, brown fox", 12)
        self.assertTrue(truncated.endswith("…"))
        self.assertNotIn(",…", truncated)

    def test_non_string_coerced(self):
        self.assertEqual(smart_truncate(42, 100), "42")


class UntrustedTests(unittest.TestCase):

    def test_wraps_string(self):
        self.assertEqual(untrusted("hello"), {"_untrusted": True, "text": "hello"})

    def test_none_becomes_empty_string(self):
        self.assertEqual(untrusted(None), {"_untrusted": True, "text": ""})

    def test_empty_string_preserved(self):
        self.assertEqual(untrusted(""), {"_untrusted": True, "text": ""})


if __name__ == "__main__":
    unittest.main()
