#!/usr/bin/env python3
"""Unit tests for build_prompt.py — _filter_misroutes gating + untrusted wrapping."""

import unittest

import _libpath  # noqa: F401
from build_prompt import (
    _filter_misroutes,
    _filter_boundary_disputes,
    _filter_individual_misroutes,
)


def _ticket(**overrides):
    base = {
        "key": "ECS-100",
        "verdict": "should_be_elsewhere",
        "should_be_at": "Echo",
        "confidence": "high",
        "reasoning": "out-of-charter",
        "out_of_charter_work": True,
        "summary": "ticket title",
        "current_team": "ACE",
        "first_team": "ACE",
        "transition_count": 0,
        "priority": "Medium",
        "status": "Closed",
    }
    base.update(overrides)
    return base


class FilterMisroutesTests(unittest.TestCase):
    """Pin the triple-predicate gating: verdict + confidence + out_of_charter_work.
    A regression in any one would silently reshape the bundle."""

    def test_keeps_high_confidence(self):
        out = _filter_misroutes({"tickets": [_ticket(confidence="high")]})
        self.assertEqual(len(out), 1)

    def test_keeps_medium_confidence(self):
        out = _filter_misroutes({"tickets": [_ticket(confidence="medium")]})
        self.assertEqual(len(out), 1)

    def test_drops_low_confidence(self):
        out = _filter_misroutes({"tickets": [_ticket(confidence="low")]})
        self.assertEqual(out, [])

    def test_drops_other_verdicts(self):
        for v in ["belongs_at_focus", "split_charter", "insufficient_evidence", ""]:
            out = _filter_misroutes({"tickets": [_ticket(verdict=v)]})
            self.assertEqual(out, [], "verdict=%r should be dropped" % v)

    def test_drops_in_charter_work(self):
        out = _filter_misroutes({"tickets": [_ticket(out_of_charter_work=False)]})
        self.assertEqual(out, [])

    def test_drops_missing_out_of_charter(self):
        # Falsy default — `dict.get` returns None, which fails the truth check.
        t = _ticket()
        del t["out_of_charter_work"]
        out = _filter_misroutes({"tickets": [t]})
        self.assertEqual(out, [])

    def test_table_driven(self):
        cases = [
            (_ticket(),                                                 True),
            (_ticket(verdict="belongs_at_focus"),                       False),
            (_ticket(confidence="low"),                                 False),
            (_ticket(out_of_charter_work=False),                        False),
            (_ticket(verdict="split_charter", confidence="high"),       False),
            (_ticket(confidence="medium", out_of_charter_work=True),    True),
        ]
        for t, should_keep in cases:
            out = _filter_misroutes({"tickets": [t]})
            self.assertEqual(
                len(out) == 1, should_keep,
                "expected keep=%s for verdict=%r confidence=%r out_of_charter=%r" % (
                    should_keep, t["verdict"], t["confidence"], t["out_of_charter_work"]),
            )

    def test_untrusted_fields_wrapped(self):
        out = _filter_misroutes({"tickets": [_ticket()]})
        self.assertEqual(len(out), 1)
        rec = out[0]
        # summary + reasoning are reporter-controllable → must be wrapped.
        self.assertEqual(rec["summary"]["_untrusted"], True)
        self.assertEqual(rec["reasoning"]["_untrusted"], True)
        # key + should_be_at + confidence + transitions stay raw — they're agent-validated.
        self.assertEqual(rec["key"], "ECS-100")
        self.assertEqual(rec["should_be_at"], "Echo")


def _split(**overrides):
    base = {
        "key": "ECS-200",
        "verdict": "split_charter",
        "should_be_at": "Asset",
        "confidence": "medium",
        "reasoning": "Inspections are Asset; provisioning is ACE.",
        "out_of_charter_work": True,
        "summary": "Unable to see Inspections for property",
        "current_team": "ACE",
        "first_team": "Seco",
        "transition_count": 1,
        "priority": "High",
        "status": "Open",
    }
    base.update(overrides)
    return base


class FilterBoundaryDisputesTests(unittest.TestCase):
    """Pin the split_charter filter — different from misroutes (no
    out_of_charter_work filter, must reject self-pointing candidates)."""

    def test_keeps_external_candidate(self):
        out = _filter_boundary_disputes({"tickets": [_split()]}, "ACE")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["candidate_team"], "Asset")

    def test_drops_self_pointing(self):
        out = _filter_boundary_disputes(
            {"tickets": [_split(should_be_at="ACE")]}, "ACE")
        self.assertEqual(out, [])

    def test_drops_empty_candidate(self):
        # apply.py upstream blanks should_be_at when it equals focus_team.
        out = _filter_boundary_disputes(
            {"tickets": [_split(should_be_at="")]}, "ACE")
        self.assertEqual(out, [])

    def test_drops_low_confidence(self):
        out = _filter_boundary_disputes(
            {"tickets": [_split(confidence="low")]}, "ACE")
        self.assertEqual(out, [])

    def test_keeps_when_out_of_charter_false(self):
        # Unlike misroutes: split_charter implies shared work by definition,
        # so out_of_charter_work isn't a filter. Pin this distinction.
        out = _filter_boundary_disputes(
            {"tickets": [_split(out_of_charter_work=False)]}, "ACE")
        self.assertEqual(len(out), 1)

    def test_drops_other_verdicts(self):
        for v in ["belongs_at_focus", "should_be_elsewhere", "insufficient_evidence"]:
            out = _filter_boundary_disputes(
                {"tickets": [_split(verdict=v)]}, "ACE")
            self.assertEqual(out, [], "verdict=%r should be dropped" % v)

    def test_untrusted_fields_wrapped(self):
        out = _filter_boundary_disputes({"tickets": [_split()]}, "ACE")
        self.assertEqual(out[0]["summary"]["_untrusted"], True)
        self.assertEqual(out[0]["reasoning"]["_untrusted"], True)
        self.assertEqual(out[0]["candidate_team"], "Asset")  # raw, validated


class FilterIndividualMisroutesTests(unittest.TestCase):
    """Pin the relaxed filter for the learning section.

    Distinct from `_filter_misroutes`: no `out_of_charter_work` requirement
    because even charter-aligned work that ultimately belongs elsewhere is
    a valid learning example for L2."""

    def test_keeps_should_be_elsewhere(self):
        out = _filter_individual_misroutes({"tickets": [_ticket()]})
        self.assertEqual(len(out), 1)

    def test_keeps_when_out_of_charter_false(self):
        # The key distinction from `_filter_misroutes` — pin it.
        out = _filter_individual_misroutes(
            {"tickets": [_ticket(out_of_charter_work=False)]})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["out_of_charter_work"], False)

    def test_drops_other_verdicts(self):
        for v in ["belongs_at_focus", "split_charter", "insufficient_evidence"]:
            out = _filter_individual_misroutes({"tickets": [_ticket(verdict=v)]})
            self.assertEqual(out, [], "verdict=%r should be dropped" % v)

    def test_drops_low_confidence(self):
        out = _filter_individual_misroutes(
            {"tickets": [_ticket(confidence="low")]})
        self.assertEqual(out, [])

    def test_keeps_high_and_medium_confidence(self):
        for c in ["high", "medium"]:
            out = _filter_individual_misroutes(
                {"tickets": [_ticket(confidence=c)]})
            self.assertEqual(len(out), 1, "confidence=%r should be kept" % c)

    def test_carries_current_team_for_re_routed_check(self):
        # The renderer needs current_team to show ✅ re-routed / ⚠ still here.
        out = _filter_individual_misroutes(
            {"tickets": [_ticket(current_team="Asset Management")]})
        self.assertEqual(out[0]["current_team"], "Asset Management")


if __name__ == "__main__":
    unittest.main()
