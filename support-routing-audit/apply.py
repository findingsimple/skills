#!/usr/bin/env python3
"""Validate the audit sub-agent's results.json and merge it with data.json
into a single audit.json that report.py reads.

Output shape under /tmp/support-routing-audit/audit.json:
  {
    "focus_team": "TeamA",
    "period": {...},
    "candidates_count": N,
    "kept_count": N,
    "truncated": bool,
    "tickets": [
      {
        "key": "PROJ-123",
        "verdict": "should_be_elsewhere",
        "should_be_at": "TeamB",
        "confidence": "high",
        "reasoning": "...",
        # joined from data.json:
        "summary": "...",
        "current_team": "TeamB",
        "first_team": "TeamA",
        "transition_count": 2,
        "team_transitions": [...],
        "priority": "High",
        "status": "Closed",
        "components": [...],
        "labels": [...],
        "created": "...",
        "resolutiondate": "..."
      }
    ],
    "summary": {<as emitted by sub-agent, validated>}
  }
"""

import json
import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import atomic_write_json


CACHE_DIR = "/tmp/support-routing-audit"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")
RESULTS_PATH = os.path.join(CACHE_DIR, "results.json")
AUDIT_PATH = os.path.join(CACHE_DIR, "audit.json")

ALLOWED_VERDICTS = {
    "belongs_at_focus", "should_be_elsewhere", "split_charter", "insufficient_evidence",
}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_OWNERSHIP = {"single_team", "multi_team", "unclear", "not_applicable"}
ALLOWED_CONTRIBUTION = {"substantive", "minimal", "unclear"}
ALLOWED_ROUTING_CAUSE = {
    "l2_misroute", "accepted_by_focus", "redirected_back", "unclear", "not_applicable",
}

_TICKET_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)

MAX_REASONING = 600
MAX_HEADLINE = 400
TOP_MISROUTES_LIMIT = 5


def norm_team(s, alias_map):
    """Resolve a free-form team name against the alias map (lower→canonical).
    Returns the canonical name on hit, None on miss."""
    if not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    return alias_map.get(raw.lower())


_WS_RE = re.compile(r"\s+")


def _clean(s, n):
    if not isinstance(s, str):
        return ""
    return _WS_RE.sub(" ", s).strip()[:n]


def main():
    try:
        with open(RESULTS_PATH) as f:
            results = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found — sub-agent did not write results." % RESULTS_PATH, file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print("ERROR: %s is not valid JSON (%s) — sub-agent likely truncated output." % (RESULTS_PATH, e), file=sys.stderr)
        return 1

    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run fetch.py first." % DATA_PATH, file=sys.stderr)
        return 1

    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        return 1

    alias_map = setup.get("team_alias_map") or {}
    focus_team = data.get("focus_team", "")
    by_key = {t["key"]: t for t in (data.get("tickets") or []) if t.get("key")}

    verdicts_out = []
    seen_keys = set()
    dropped_invalid = 0
    dropped_unknown_key = 0
    dropped_duplicate = 0
    for entry in (results.get("tickets") or []):
        key = entry.get("key") or ""
        if not _TICKET_KEY_RE.match(key):
            dropped_invalid += 1
            continue
        if key not in by_key:
            dropped_unknown_key += 1
            continue
        if key in seen_keys:
            dropped_duplicate += 1
            continue
        seen_keys.add(key)

        raw_verdict = entry.get("verdict")
        if raw_verdict not in ALLOWED_VERDICTS:
            print("WARNING: %s unknown verdict %r → defaulting to insufficient_evidence" % (key, raw_verdict), file=sys.stderr)
            verdict = "insufficient_evidence"
        else:
            verdict = raw_verdict

        sb_raw = entry.get("should_be_at")
        sb = norm_team(sb_raw, alias_map) if sb_raw else None
        if sb and focus_team and sb == focus_team:
            # Inconsistent verdict: should_be_at can't equal focus team.
            print("WARNING: %s verdict says should_be_at=%s == focus_team; dropping should_be_at." % (key, sb), file=sys.stderr)
            sb = None
        if verdict == "belongs_at_focus":
            sb = None

        raw_confidence = entry.get("confidence")
        if raw_confidence not in ALLOWED_CONFIDENCE:
            print("WARNING: %s unknown confidence %r → defaulting to low" % (key, raw_confidence), file=sys.stderr)
            confidence = "low"
        else:
            confidence = raw_confidence

        d = by_key[key]
        is_bounced = (d.get("transition_count") or 0) >= 2
        if not is_bounced:
            ownership = "not_applicable"
            ownership_reasoning = ""
        else:
            raw_ownership = entry.get("ownership")
            ownership = raw_ownership if raw_ownership in ALLOWED_OWNERSHIP else "unclear"
            if ownership == "not_applicable":
                ownership = "unclear"
            ownership_reasoning = _clean(entry.get("ownership_reasoning", ""), MAX_REASONING)

        is_misroute_verdict = verdict in ("should_be_elsewhere", "split_charter")
        raw_contribution = entry.get("focus_team_contribution")
        contribution = raw_contribution if raw_contribution in ALLOWED_CONTRIBUTION else "unclear"
        contribution_reasoning = _clean(entry.get("focus_team_contribution_reasoning", ""), MAX_REASONING)

        # routing_cause only meaningful when focus did substantive out-of-charter work;
        # other states collapse to not_applicable so the report doesn't render misleading values.
        if is_misroute_verdict and contribution == "substantive":
            raw_cause = entry.get("routing_cause")
            routing_cause = raw_cause if raw_cause in ALLOWED_ROUTING_CAUSE else "unclear"
            if routing_cause == "not_applicable":
                routing_cause = "unclear"
            routing_cause_reasoning = _clean(entry.get("routing_cause_reasoning", ""), MAX_REASONING)
        else:
            routing_cause = "not_applicable"
            routing_cause_reasoning = ""

        # Derived: focus team did substantive work on a ticket the charter says belongs elsewhere.
        # See report.py: out_of_charter_l2_actionable narrows this to routing_cause == l2_misroute,
        # which is the actionable subset for support feedback.
        out_of_charter_work = is_misroute_verdict and contribution == "substantive"

        verdicts_out.append({
            "key": key,
            "verdict": verdict,
            "should_be_at": sb,
            "confidence": confidence,
            "reasoning": _clean(entry.get("reasoning", ""), MAX_REASONING),
            "focus_team_contribution": contribution,
            "focus_team_contribution_reasoning": contribution_reasoning,
            "routing_cause": routing_cause,
            "routing_cause_reasoning": routing_cause_reasoning,
            "out_of_charter_work": out_of_charter_work,
            "ownership": ownership,
            "ownership_reasoning": ownership_reasoning,
            "summary": d.get("summary", ""),
            "current_team": d.get("current_team", ""),
            "first_team": d.get("first_team", ""),
            "transition_count": d.get("transition_count", 0),
            "team_transitions": d.get("team_transitions") or [],
            "priority": d.get("priority", "Medium"),
            "status": d.get("status", ""),
            "components": d.get("components") or [],
            "labels": d.get("labels") or [],
            "created": d.get("created", ""),
            "resolutiondate": d.get("resolutiondate", ""),
            "issuetype": d.get("issuetype", ""),
        })

    # Add fallback verdicts for any ticket the sub-agent dropped — better to
    # surface them as insufficient_evidence than silently lose them.
    for key, d in by_key.items():
        if key in seen_keys:
            continue
        is_bounced = (d.get("transition_count") or 0) >= 2
        verdicts_out.append({
            "key": key,
            "verdict": "insufficient_evidence",
            "should_be_at": None,
            "confidence": "low",
            "reasoning": "(sub-agent did not return a verdict for this ticket)",
            "focus_team_contribution": "unclear",
            "focus_team_contribution_reasoning": "",
            "routing_cause": "not_applicable",
            "routing_cause_reasoning": "",
            "out_of_charter_work": False,
            "ownership": "unclear" if is_bounced else "not_applicable",
            "ownership_reasoning": "",
            "summary": d.get("summary", ""),
            "current_team": d.get("current_team", ""),
            "first_team": d.get("first_team", ""),
            "transition_count": d.get("transition_count", 0),
            "team_transitions": d.get("team_transitions") or [],
            "priority": d.get("priority", "Medium"),
            "status": d.get("status", ""),
            "components": d.get("components") or [],
            "labels": d.get("labels") or [],
            "created": d.get("created", ""),
            "resolutiondate": d.get("resolutiondate", ""),
            "issuetype": d.get("issuetype", ""),
        })

    # Recompute summary counts from validated verdicts (don't trust whatever
    # the sub-agent reported — re-derive so totals always match what we render).
    counts = {k: 0 for k in ALLOWED_VERDICTS}
    by_target = {}
    for v in verdicts_out:
        counts[v["verdict"]] += 1
        if v["verdict"] == "should_be_elsewhere" and v["should_be_at"]:
            by_target[v["should_be_at"]] = by_target.get(v["should_be_at"], 0) + 1

    by_target_list = sorted(
        ({"should_be_at": k, "count": c} for k, c in by_target.items()),
        key=lambda x: (-x["count"], x["should_be_at"]),
    )

    # Top misroutes: high confidence, priority asc then created desc within bucket.
    # Python sort is stable: sort by created desc first, then by priority — the
    # priority sort preserves the date order within each bucket.
    priority_order = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4, "": 5}
    high_misroutes = [v for v in verdicts_out
                      if v["verdict"] == "should_be_elsewhere" and v["confidence"] == "high"]
    high_misroutes.sort(key=lambda v: v.get("created", ""), reverse=True)
    high_misroutes.sort(key=lambda v: priority_order.get(v.get("priority", ""), 5))
    top_misroutes = [v["key"] for v in high_misroutes[:TOP_MISROUTES_LIMIT]]

    out_of_charter_count = 0
    out_of_charter_by_cause = {k: 0 for k in ALLOWED_ROUTING_CAUSE if k != "not_applicable"}
    for v in verdicts_out:
        if v.get("out_of_charter_work"):
            out_of_charter_count += 1
            cause = v.get("routing_cause") or "unclear"
            if cause in out_of_charter_by_cause:
                out_of_charter_by_cause[cause] += 1

    raw_summary = results.get("summary") or {}
    summary = {
        "focus_team": focus_team or raw_summary.get("focus_team", ""),
        "tickets_total": len(verdicts_out),
        "belongs_at_focus": counts["belongs_at_focus"],
        "should_be_elsewhere": counts["should_be_elsewhere"],
        "split_charter": counts["split_charter"],
        "insufficient_evidence": counts["insufficient_evidence"],
        "by_target_team": by_target_list,
        "top_misroutes": top_misroutes,
        "out_of_charter_work": out_of_charter_count,
        "out_of_charter_l2_actionable": out_of_charter_by_cause["l2_misroute"],
        "out_of_charter_by_cause": out_of_charter_by_cause,
        "headline": _clean(raw_summary.get("headline", ""), MAX_HEADLINE),
    }

    audit = {
        "focus_team": focus_team,
        "focus_team_field_values": data.get("focus_team_field_values") or [],
        "period": data.get("period") or {},
        "candidates_count": data.get("candidates_count", len(verdicts_out)),
        "kept_count": data.get("kept_count", len(verdicts_out)),
        "truncated": data.get("truncated", False),
        "tickets": verdicts_out,
        "summary": summary,
    }
    atomic_write_json(AUDIT_PATH, audit)
    if dropped_invalid or dropped_unknown_key or dropped_duplicate:
        print("WARNING: dropped sub-agent entries — invalid_key=%d, unknown_key=%d, duplicate=%d" % (
            dropped_invalid, dropped_unknown_key, dropped_duplicate), file=sys.stderr)
    print("Wrote audit.json: %d tickets (%d misrouted, %d high-confidence top)" % (
        len(verdicts_out), counts["should_be_elsewhere"], len(top_misroutes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
