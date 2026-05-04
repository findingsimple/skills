#!/usr/bin/env python3
"""Tests for bonusly_client pagination loop.

Run with `python3 _lib/test_bonusly_client.py`. No network — bonusly_get
is monkey-patched.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bonusly_client


class _ScriptedGet:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, token, path, params=None):
        self.calls.append(dict(params or {}))
        if not self.pages:
            raise AssertionError("bonusly_get called more times than scripted")
        return self.pages.pop(0)


class GetAllTests(unittest.TestCase):
    def setUp(self):
        self._orig = bonusly_client.bonusly_get

    def tearDown(self):
        bonusly_client.bonusly_get = self._orig

    def test_single_short_page_terminates(self):
        bonusly_client.bonusly_get = _ScriptedGet([
            {"result": [{"id": str(i)} for i in range(5)]},
        ])
        out = bonusly_client.bonusly_get_all("tok", "/bonuses", {"start_time": "2026-01-01"})
        self.assertEqual(len(out), 5)

    def test_full_page_then_partial_paginates(self):
        full = [{"id": "x%d" % i} for i in range(100)]
        partial = [{"id": "y%d" % i} for i in range(7)]
        scripted = _ScriptedGet([
            {"result": full},
            {"result": partial},
        ])
        bonusly_client.bonusly_get = scripted
        out = bonusly_client.bonusly_get_all("tok", "/bonuses", {})
        self.assertEqual(len(out), 107)
        self.assertEqual(scripted.calls[0]["skip"], 0)
        self.assertEqual(scripted.calls[0]["limit"], 100)
        self.assertEqual(scripted.calls[1]["skip"], 100)

    def test_exact_multiple_of_100_then_empty(self):
        # Edge case: full page returned, but next page is empty (final).
        full = [{"id": str(i)} for i in range(100)]
        scripted = _ScriptedGet([
            {"result": full},
            {"result": []},
        ])
        bonusly_client.bonusly_get = scripted
        out = bonusly_client.bonusly_get_all("tok", "/bonuses", {})
        self.assertEqual(len(out), 100)
        self.assertEqual(len(scripted.calls), 2)

    def test_caller_params_passed_through_each_call(self):
        scripted = _ScriptedGet([
            {"result": [{"id": str(i)} for i in range(100)]},
            {"result": []},
        ])
        bonusly_client.bonusly_get = scripted
        bonusly_client.bonusly_get_all("tok", "/bonuses", {"giver_email": "alice@example.test"})
        for call in scripted.calls:
            self.assertEqual(call.get("giver_email"), "alice@example.test")
            self.assertEqual(call.get("limit"), 100)

    def test_input_params_not_mutated(self):
        bonusly_client.bonusly_get = _ScriptedGet([{"result": []}])
        original = {"start_time": "2026-04-01"}
        bonusly_client.bonusly_get_all("tok", "/bonuses", original)
        self.assertNotIn("limit", original)
        self.assertNotIn("skip", original)

    def test_result_null_does_not_crash(self):
        # Bonusly occasionally returns `result: null` rather than `[]`.
        # `data.get("result", [])` would yield None and crash on `.extend(None)`.
        bonusly_client.bonusly_get = _ScriptedGet([{"result": None}])
        out = bonusly_client.bonusly_get_all("tok", "/bonuses", {})
        self.assertEqual(out, [])

    def test_result_missing_key_treated_as_empty(self):
        bonusly_client.bonusly_get = _ScriptedGet([{}])
        out = bonusly_client.bonusly_get_all("tok", "/bonuses", {})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
