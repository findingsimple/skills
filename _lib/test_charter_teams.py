#!/usr/bin/env python3
"""Unit tests for _lib/charter_teams.py."""

import unittest

from charter_teams import parse_charter_teams, norm_team, slugify_team, TEAM_NAME_RE


class ParseCharterTeamsTests(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(parse_charter_teams(""), ([], {}))
        self.assertEqual(parse_charter_teams(None), ([], {}))

    def test_simple_pipe_list(self):
        canonicals, aliases = parse_charter_teams("Alpha|Beta|Gamma")
        self.assertEqual(canonicals, ["Alpha", "Beta", "Gamma"])
        self.assertEqual(aliases, {"alpha": "Alpha", "beta": "Beta", "gamma": "Gamma"})

    def test_with_aliases(self):
        canonicals, aliases = parse_charter_teams("Alpha:a,team-a|Beta")
        self.assertEqual(canonicals, ["Alpha", "Beta"])
        self.assertEqual(aliases["a"], "Alpha")
        self.assertEqual(aliases["team-a"], "Alpha")
        self.assertEqual(aliases["alpha"], "Alpha")
        self.assertEqual(aliases["beta"], "Beta")

    def test_skips_invalid_canonical(self):
        canonicals, _ = parse_charter_teams("Alpha|9bad|Beta")
        self.assertEqual(canonicals, ["Alpha", "Beta"])

    def test_skips_invalid_alias_keeps_canonical(self):
        canonicals, aliases = parse_charter_teams("Alpha:good,9bad,also-good|Beta")
        self.assertEqual(canonicals, ["Alpha", "Beta"])
        self.assertEqual(aliases["good"], "Alpha")
        self.assertEqual(aliases["also-good"], "Alpha")
        self.assertNotIn("9bad", aliases)

    def test_strips_whitespace(self):
        canonicals, _ = parse_charter_teams("  Alpha  |  Beta  ")
        self.assertEqual(canonicals, ["Alpha", "Beta"])

    def test_skips_empty_slots(self):
        canonicals, _ = parse_charter_teams("Alpha||Beta||")
        self.assertEqual(canonicals, ["Alpha", "Beta"])


class NormTeamTests(unittest.TestCase):

    def setUp(self):
        self.aliases = {"alpha": "Alpha", "a": "Alpha", "beta": "Beta"}

    def test_canonical_match(self):
        self.assertEqual(norm_team("Alpha", self.aliases), "Alpha")
        self.assertEqual(norm_team("alpha", self.aliases), "Alpha")
        self.assertEqual(norm_team("ALPHA", self.aliases), "Alpha")

    def test_alias_match(self):
        self.assertEqual(norm_team("a", self.aliases), "Alpha")

    def test_strips_whitespace(self):
        self.assertEqual(norm_team("  alpha  ", self.aliases), "Alpha")

    def test_miss(self):
        self.assertIsNone(norm_team("Gamma", self.aliases))
        self.assertIsNone(norm_team("", self.aliases))
        self.assertIsNone(norm_team("   ", self.aliases))

    def test_non_string(self):
        self.assertIsNone(norm_team(None, self.aliases))
        self.assertIsNone(norm_team(42, self.aliases))


class SlugifyTeamTests(unittest.TestCase):

    def test_simple(self):
        self.assertEqual(slugify_team("ACE"), "ACE")
        self.assertEqual(slugify_team("COPS"), "COPS")

    def test_collapses_spaces(self):
        self.assertEqual(slugify_team("Leasing CRM"), "Leasing_CRM")

    def test_collapses_special_chars(self):
        self.assertEqual(slugify_team("Leasing & CRM"), "Leasing_CRM")

    def test_collision_documented(self):
        # Both forms collide on the same slug — caller must guard against this
        # if both names are present in CHARTER_TEAMS simultaneously.
        self.assertEqual(slugify_team("Leasing & CRM"), slugify_team("Leasing CRM"))

    def test_strips_outer_underscores(self):
        self.assertEqual(slugify_team("&Asset&"), "Asset")

    def test_rejects_unsalvageable(self):
        with self.assertRaises(ValueError):
            slugify_team("&&&")
        with self.assertRaises(ValueError):
            slugify_team("9LeadingDigit")


class TeamNameReTests(unittest.TestCase):

    def test_matches(self):
        for name in ["A", "Alpha", "Team Alpha", "Team-Alpha", "A&B", "team_alpha"]:
            self.assertTrue(TEAM_NAME_RE.match(name), name)

    def test_rejects(self):
        for name in ["", "9bad", "team\nname", "team$name", "a" * 100]:
            self.assertFalse(TEAM_NAME_RE.match(name), name)


if __name__ == "__main__":
    unittest.main()
