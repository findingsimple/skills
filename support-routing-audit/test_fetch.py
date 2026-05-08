#!/usr/bin/env python3
"""Unit tests for support-routing-audit/fetch.py.

Focused on `build_focus_clause` — the Stage 1 JQL OR clause constructor.
Pins the current-value (`=` / `in`) form. We initially tried the JQL `was`
operator to catch warm hand-offs out of the focus team, but Atlassian's
Team custom-field type doesn't support history operators on this Jira
Cloud tenant. See fetch.py docstring for the workaround paths."""

import unittest

import _libpath  # noqa: F401
from fetch import build_focus_clause


class BuildFocusClauseTests(unittest.TestCase):

    def test_single_label(self):
        self.assertEqual(
            build_focus_clause(["team-ace"], []),
            '(labels = "team-ace")',
        )

    def test_multiple_labels(self):
        self.assertEqual(
            build_focus_clause(["team-ace", "team-seco"], []),
            '(labels in ("team-ace", "team-seco"))',
        )

    def test_single_uuid(self):
        self.assertEqual(
            build_focus_clause([], ["uuid-1"]),
            '(cf[10600] = "uuid-1")',
        )

    def test_multiple_uuids(self):
        self.assertEqual(
            build_focus_clause([], ["uuid-1", "uuid-2"]),
            '(cf[10600] in ("uuid-1", "uuid-2"))',
        )

    def test_combined_labels_and_uuids(self):
        self.assertEqual(
            build_focus_clause(["team-ace"], ["uuid-1"]),
            '(labels = "team-ace" OR cf[10600] = "uuid-1")',
        )

    def test_combined_with_lists(self):
        self.assertEqual(
            build_focus_clause(["team-ace", "team-seco"], ["uuid-1", "uuid-2"]),
            '(labels in ("team-ace", "team-seco") '
            'OR cf[10600] in ("uuid-1", "uuid-2"))',
        )

    def test_empty_inputs_aborts(self):
        # No labels, no UUIDs — caller can't run Stage 1. Pin the SystemExit.
        with self.assertRaises(SystemExit) as cm:
            build_focus_clause([], [])
        self.assertEqual(cm.exception.code, 3)

    def test_no_was_regression(self):
        # Probes (May 2026) confirmed Jira's Team custom-field type rejects
        # `was` / `CHANGED`. If anyone reintroduces `was` here without
        # re-probing the API, this catches it before fetches start 400ing
        # in the field.
        for labels, uuids in [
            (["team-ace"], []),
            ([], ["uuid-1"]),
            (["team-ace", "team-seco"], ["uuid-1", "uuid-2"]),
        ]:
            jql = build_focus_clause(labels, uuids)
            self.assertNotIn(" was ", jql,
                             "Team field doesn't support JQL `was` on this tenant — "
                             "see fetch.py docstring before changing this back.")


if __name__ == "__main__":
    unittest.main()
