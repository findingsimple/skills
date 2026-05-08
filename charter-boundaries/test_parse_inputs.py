#!/usr/bin/env python3
"""Unit tests for parse_inputs.py — heading splitter, alias matching, example parser."""

import unittest

import _libpath  # noqa: F401
from parse_inputs import (
    _normalise_heading,
    _split_charter_sections,
    _parse_charters,
    _parse_examples,
    _attach_examples_to_targets,
)


SAMPLE_ALIAS_MAP = {
    "ace": "ACE",
    "cops": "COPS",
    "echo": "Echo",
    "asset": "Asset",
    "papi": "PAPI",
    "delivery": "Delivery",
    "mobile": "Mobile",
    "leasing & crm": "Leasing & CRM",
    "data": "Data",
    "optigo": "Optigo",
}


class NormaliseHeadingTests(unittest.TestCase):

    def test_strips_emoji_and_team_prefix(self):
        self.assertEqual(_normalise_heading("♠️ Team ACE (Admin Configuration & Experience)"), "ACE")
        self.assertEqual(_normalise_heading("🚢 Team Delivery"), "Delivery")
        self.assertEqual(_normalise_heading("🤖 Team PAPI (Platform & API)"), "PAPI")

    def test_no_emoji(self):
        self.assertEqual(_normalise_heading("Team Asset"), "Asset")
        self.assertEqual(_normalise_heading("Asset"), "Asset")

    def test_slash_suffix_stripped(self):
        self.assertEqual(_normalise_heading("Optigo / Lending"), "Optigo")


class SplitCharterSectionsTests(unittest.TestCase):

    def test_splits_on_h2_with_horizontal_rule(self):
        text = (
            "# Top-level\n\n"
            "## Team A\n\nbody A line 1\nbody A line 2\n\n"
            "---\n\n"
            "## Team B\n\nbody B\n"
        )
        sections = list(_split_charter_sections(text))
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0][0], "Team A")
        self.assertIn("body A line 1", sections[0][1])
        self.assertNotIn("body B", sections[0][1])
        self.assertEqual(sections[1][0], "Team B")
        self.assertIn("body B", sections[1][1])

    def test_no_h2_returns_empty(self):
        self.assertEqual(list(_split_charter_sections("# Just an H1\nbody")), [])


class ParseChartersTests(unittest.TestCase):

    def test_per_team_blurbs_and_unmatched(self):
        text = (
            "## ♠️ Team ACE (Admin Configuration & Experience)\n"
            "\n"
            "**Leadership:** ...\n"
            "<aside>Charter blurb for ACE.</aside>\n"
            "\n"
            "---\n"
            "\n"
            "## Team Bogus\n"
            "body\n"
            "\n"
            "---\n"
            "\n"
            "## 📊 Team Data\n"
            "data blurb\n"
        )
        per_team, unmatched = _parse_charters(text, SAMPLE_ALIAS_MAP)
        self.assertIn("ACE", per_team)
        self.assertIn("Charter blurb for ACE", per_team["ACE"])
        self.assertIn("Data", per_team)
        self.assertIn("data blurb", per_team["Data"])
        self.assertNotIn("Bogus", per_team)
        self.assertEqual(unmatched, ["Team Bogus"])

    def test_missing_team_yields_empty_blurb(self):
        text = "## Team ACE\nbody\n"
        per_team, _ = _parse_charters(text, SAMPLE_ALIAS_MAP)
        self.assertNotIn("Echo", per_team)  # absent — caller fills with empty string


class ParseExamplesTests(unittest.TestCase):

    def test_extracts_from_to_and_key(self):
        text = (
            "## Existing Examples\n\n"
            "Assigned to Asset, but should belong to Echo (we rerouted): "
            "https://happyco.atlassian.net/browse/ECS-5275\n"
            "Assigned to Asset but should belong to ACE (we rerouted):  "
            "https://happyco.atlassian.net/browse/ECS-5354\n"
            "Ticket was missing key details, like business id, folder id, and behaviors seen: "
            "https://happyco.atlassian.net/browse/ECS-535\n"
        )
        examples, unmatched = _parse_examples(text, SAMPLE_ALIAS_MAP)
        self.assertEqual(len(examples), 2)
        self.assertEqual(examples[0]["from_team"], "Asset")
        self.assertEqual(examples[0]["to_team"], "Echo")
        self.assertEqual(examples[0]["ticket_key"], "ECS-5275")
        self.assertEqual(examples[1]["from_team"], "Asset")
        self.assertEqual(examples[1]["to_team"], "ACE")
        self.assertEqual(examples[1]["ticket_key"], "ECS-5354")
        # The "missing details" line is not a misroute; should not be flagged as a parse failure.
        self.assertEqual(unmatched, [])

    def test_unrecognised_team_falls_into_unmatched(self):
        text = "Assigned to Frontend, but should belong to Backend: https://happyco.atlassian.net/browse/ECS-9999\n"
        examples, unmatched = _parse_examples(text, SAMPLE_ALIAS_MAP)
        self.assertEqual(examples, [])
        self.assertEqual(len(unmatched), 1)


class AttachExamplesTests(unittest.TestCase):

    def test_attaches_to_target_team(self):
        examples = [
            {"from_team": "Asset", "to_team": "ACE", "ticket_key": "ECS-1", "url": "u1", "raw": "r1"},
            {"from_team": "Asset", "to_team": "Echo", "ticket_key": "ECS-2", "url": "u2", "raw": "r2"},
            {"from_team": "Asset", "to_team": "ACE", "ticket_key": "ECS-3", "url": "u3", "raw": "r3"},
        ]
        by_target = _attach_examples_to_targets(["ACE", "Echo", "PAPI"], examples)
        self.assertEqual([e["ticket_key"] for e in by_target["ACE"]], ["ECS-1", "ECS-3"])
        self.assertEqual([e["ticket_key"] for e in by_target["Echo"]], ["ECS-2"])
        self.assertEqual(by_target["PAPI"], [])


if __name__ == "__main__":
    unittest.main()
