#!/usr/bin/env python3
"""Unit tests for build_prompt.py — _filter_misroutes gating + untrusted wrapping."""

import unittest

import _libpath  # noqa: F401
from build_prompt import _filter_misroutes


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


if __name__ == "__main__":
    unittest.main()
