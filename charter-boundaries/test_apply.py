#!/usr/bin/env python3
"""Unit tests for apply.py validation helpers."""

import unittest

import _libpath  # noqa: F401
from apply import (
    _boundary_disputes_for,
    _curated_examples_for,
    _individual_reroutings_for,
    _smart_truncate,
    _string_list,
    _validate_cluster,
    _validate_edge_cases,
    MAX_OWNS_ITEM_CHARS,
    MIN_EVIDENCE_PER_CLUSTER,
)


class SmartTruncateTests(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(_smart_truncate("hello", 100), "hello")

    def test_word_boundary(self):
        s = "the quick brown fox jumps over the lazy dog"
        truncated = _smart_truncate(s, 20)
        self.assertTrue(truncated.endswith("…"))
        self.assertNotIn(" …", truncated)
        self.assertLessEqual(len(truncated), 20)

    def test_no_whitespace_falls_back_to_hard_cut(self):
        self.assertTrue(_smart_truncate("abcdefghijklmnop", 8).endswith("…"))


class StringListTests(unittest.TestCase):

    def test_filters_non_strings_and_empty(self):
        out = _string_list(["foo", "", None, 42, "bar"], max_items=10, max_chars=10)
        self.assertEqual(out, ["foo", "bar"])

    def test_caps_items(self):
        out = _string_list(["a", "b", "c", "d"], max_items=2, max_chars=10)
        self.assertEqual(out, ["a", "b"])


class ValidateClusterTests(unittest.TestCase):

    def setUp(self):
        self.allowed_teams = {"ACE", "Echo", "PAPI"}
        self.valid_evidence = {"ECS-100", "ECS-101", "ECS-102"}
        self.valid_curated = {"ECS-5354"}

    def _ok_cluster(self, **overrides):
        base = {
            "theme_id": "reporting-screens",
            "title": "Reporting screens",
            "target_team": "Echo",
            "description": "tickets about reporting screens",
            "boundary_rule": "If chart data → Echo. If filter UI → ACE.",
            "evidence_keys": ["ECS-100", "ECS-101"],
            "anchored_by_curated": [],
        }
        base.update(overrides)
        return base

    def test_valid(self):
        v = _validate_cluster(self._ok_cluster(), self.allowed_teams,
                              self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNotNone(v)
        self.assertEqual(v["theme_id"], "reporting-screens")
        self.assertEqual(v["target_team"], "Echo")

    def test_drops_realistic_agent_typo_theme_ids(self):
        # Most likely agent misses: snake_case, TitleCase, and trailing punctuation.
        # The strict kebab-case rule keeps theme_ids stable across runs.
        for bad in ["reporting_screens", "Reporting-Screens", "auth-and-sso-"]:
            v = _validate_cluster(self._ok_cluster(theme_id=bad), self.allowed_teams,
                                  self.valid_evidence, self.valid_curated, "ACE")
            self.assertIsNone(v, "expected None for theme_id=%r" % bad)

    def test_unknown_target_team(self):
        v = _validate_cluster(self._ok_cluster(target_team="Bogus"), self.allowed_teams,
                              self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNone(v)

    def test_evidence_filtered_to_valid_only(self):
        v = _validate_cluster(
            self._ok_cluster(evidence_keys=["ECS-100", "ECS-999", "not a key", "ECS-101"]),
            self.allowed_teams, self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNotNone(v)
        self.assertEqual(v["evidence_keys"], ["ECS-100", "ECS-101"])

    def test_dropped_when_too_few_evidence(self):
        v = _validate_cluster(self._ok_cluster(evidence_keys=["ECS-100"]),
                              self.allowed_teams, self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNone(v)
        # Sanity: confirms our minimum.
        self.assertEqual(MIN_EVIDENCE_PER_CLUSTER, 2)

    def test_anchored_filtered_to_valid_only(self):
        v = _validate_cluster(
            self._ok_cluster(anchored_by_curated=["ECS-5354", "ECS-9999", "not a key"]),
            self.allowed_teams, self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNotNone(v)
        self.assertEqual(v["anchored_by_curated"], ["ECS-5354"])

    def test_long_description_truncated(self):
        long = "x" * 500 + " end"
        v = _validate_cluster(self._ok_cluster(description=long), self.allowed_teams,
                              self.valid_evidence, self.valid_curated, "ACE")
        self.assertIsNotNone(v)
        self.assertTrue(v["description"].endswith("…"))
        self.assertLessEqual(len(v["description"]), 200)


class CuratedExamplesForTests(unittest.TestCase):
    """Pin the inbound-drift carry-through: bundle.curated_examples should
    arrive in draft.json as should_own_examples with regex-validated keys."""

    def test_carries_valid_examples(self):
        tr = {
            "curated_examples": [
                {"ticket_key": "ECS-5354", "from_team": "Asset", "to_team": "ACE",
                 "url": {"_untrusted": True, "text": "https://..."},
                 "raw": {"_untrusted": True, "text": "Assigned to Asset..."}},
                {"ticket_key": "ECS-5330", "from_team": "Asset", "to_team": "ACE",
                 "url": {"_untrusted": True, "text": "https://..."},
                 "raw": {"_untrusted": True, "text": "Assigned to Asset..."}},
            ],
        }
        out = _curated_examples_for(tr)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["ticket_key"], "ECS-5354")
        self.assertEqual(out[0]["from_team"], "Asset")
        self.assertEqual(out[0]["to_team"], "ACE")
        # url and raw are NOT carried through — they're untrusted-wrapped
        # Markdown the renderer doesn't need.
        self.assertNotIn("url", out[0])
        self.assertNotIn("raw", out[0])

    def test_drops_invalid_ticket_key(self):
        tr = {"curated_examples": [
            {"ticket_key": "not-a-key", "from_team": "Asset", "to_team": "ACE"},
            {"ticket_key": "ECS-1", "from_team": "Asset", "to_team": "ACE"},
        ]}
        out = _curated_examples_for(tr)
        self.assertEqual([e["ticket_key"] for e in out], ["ECS-1"])

    def test_drops_missing_from_or_to(self):
        tr = {"curated_examples": [
            {"ticket_key": "ECS-1", "from_team": "", "to_team": "ACE"},
            {"ticket_key": "ECS-2", "from_team": "Asset", "to_team": ""},
            {"ticket_key": "ECS-3", "from_team": "Asset", "to_team": "ACE"},
        ]}
        out = _curated_examples_for(tr)
        self.assertEqual([e["ticket_key"] for e in out], ["ECS-3"])

    def test_empty_input(self):
        self.assertEqual(_curated_examples_for({}), [])
        self.assertEqual(_curated_examples_for({"curated_examples": []}), [])


class BoundaryDisputesForTests(unittest.TestCase):
    """Pin the apply-side validation: split_charter cases land in
    draft.json with regex-validated keys, allow-listed candidate team,
    unwrapped + truncated free-text."""

    def setUp(self):
        self.allowed = {"ACE", "Asset", "Echo", "PAPI"}

    def _bundle_dispute(self, **overrides):
        base = {
            "key": "ECS-5269",
            "candidate_team": "Asset",
            "confidence": "medium",
            "summary": {"_untrusted": True, "text": "Unable to see Inspections"},
            "reasoning": {"_untrusted": True, "text": "Asset owns inspections; ACE owns provisioning."},
            "current_team": "ACE",
            "priority": "High",
            "status": "Open",
        }
        base.update(overrides)
        return base

    def test_carries_valid_dispute(self):
        tr = {"boundary_disputes": [self._bundle_dispute()]}
        out = _boundary_disputes_for(tr, self.allowed, "ACE")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], "ECS-5269")
        self.assertEqual(out[0]["candidate_team"], "Asset")
        # Free-text unwrapped from {_untrusted, text}.
        self.assertEqual(out[0]["summary"], "Unable to see Inspections")
        self.assertIn("Asset owns inspections", out[0]["reasoning"])

    def test_drops_invalid_ticket_key(self):
        tr = {"boundary_disputes": [self._bundle_dispute(key="not-a-key")]}
        self.assertEqual(_boundary_disputes_for(tr, self.allowed, "ACE"), [])

    def test_drops_unknown_candidate(self):
        tr = {"boundary_disputes": [self._bundle_dispute(candidate_team="Bogus")]}
        self.assertEqual(_boundary_disputes_for(tr, self.allowed, "ACE"), [])

    def test_drops_self_candidate(self):
        # If somehow a focus-team candidate slipped past build_prompt.py,
        # apply.py rejects it too.
        tr = {"boundary_disputes": [self._bundle_dispute(candidate_team="ACE")]}
        self.assertEqual(_boundary_disputes_for(tr, self.allowed, "ACE"), [])

    def test_drops_low_confidence(self):
        tr = {"boundary_disputes": [self._bundle_dispute(confidence="low")]}
        self.assertEqual(_boundary_disputes_for(tr, self.allowed, "ACE"), [])

    def test_truncates_long_reasoning(self):
        long = "Asset owns this " * 100
        tr = {"boundary_disputes": [self._bundle_dispute(
            reasoning={"_untrusted": True, "text": long})]}
        out = _boundary_disputes_for(tr, self.allowed, "ACE")
        self.assertTrue(out[0]["reasoning"].endswith("…"))
        self.assertLessEqual(len(out[0]["reasoning"]), 280)


class IndividualReroutingsForTests(unittest.TestCase):
    """Pin the apply-side handling of individual_reroutings: validates,
    skips ones already in clusters, surfaces re_routed flag."""

    def setUp(self):
        self.allowed = {"ACE", "Asset", "Echo", "PAPI"}

    def _bundle_individual(self, **overrides):
        base = {
            "key": "ECS-9001",
            "should_be_at": "Echo",
            "confidence": "high",
            "summary": {"_untrusted": True, "text": "SMS opt-out failing"},
            "reasoning": {"_untrusted": True, "text": "Resident SMS — Echo owns this."},
            "current_team": "Echo",  # already re-routed
            "priority": "Medium",
            "status": "Closed",
        }
        base.update(overrides)
        return base

    def test_carries_valid_individual(self):
        tr = {"individual_misroutes": [self._bundle_individual()]}
        out = _individual_reroutings_for(tr, self.allowed, "ACE", set())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["key"], "ECS-9001")
        self.assertEqual(out[0]["should_be_at"], "Echo")
        self.assertTrue(out[0]["re_routed"])

    def test_skips_keys_in_clusters(self):
        tr = {"individual_misroutes": [self._bundle_individual()]}
        # ECS-9001 is in a cluster already — should be skipped.
        out = _individual_reroutings_for(tr, self.allowed, "ACE", {"ECS-9001"})
        self.assertEqual(out, [])

    def test_re_routed_false_when_still_at_focus(self):
        # current_team == focus → not yet re-routed.
        tr = {"individual_misroutes": [
            self._bundle_individual(current_team="ACE")]}
        out = _individual_reroutings_for(tr, self.allowed, "ACE", set())
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]["re_routed"])

    def test_drops_unknown_target(self):
        tr = {"individual_misroutes": [
            self._bundle_individual(should_be_at="Bogus")]}
        out = _individual_reroutings_for(tr, self.allowed, "ACE", set())
        self.assertEqual(out, [])

    def test_drops_self_target(self):
        # If should_be_at == focus_team (apply.py upstream blanks it for
        # the clean misroutes path; this catches any leak).
        tr = {"individual_misroutes": [
            self._bundle_individual(should_be_at="ACE")]}
        out = _individual_reroutings_for(tr, self.allowed, "ACE", set())
        self.assertEqual(out, [])

    def test_drops_low_confidence(self):
        tr = {"individual_misroutes": [
            self._bundle_individual(confidence="low")]}
        out = _individual_reroutings_for(tr, self.allowed, "ACE", set())
        self.assertEqual(out, [])


class ValidateEdgeCasesTests(unittest.TestCase):

    def test_drops_empty_question(self):
        out = _validate_edge_cases([{"question": "", "current_understanding": "ok"}])
        self.assertEqual(out, [])

    def test_keeps_valid(self):
        out = _validate_edge_cases([
            {"question": "Q1", "current_understanding": "U1"},
            {"question": "Q2"},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["current_understanding"], "")


if __name__ == "__main__":
    unittest.main()
