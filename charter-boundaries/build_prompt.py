#!/usr/bin/env python3
"""Aggregate per-team audit snapshots + parsed charter blurbs + curated examples
into /tmp/charter-boundaries/bundle.json for the synthesis sub-agent.

Per-team record shape:
  {
    "team": "ACE",
    "vault_dir": "ACE",
    "charter_blurb": {"_untrusted": true, "text": "..."},
    "curated_examples": [
        {"ticket_key": "ECS-5354", "from_team": "Asset", "to_team": "ACE",
         "url": "...", "raw": {"_untrusted": true, "text": "..."}}
    ],
    "misroutes": [
        {"key": "ECS-...", "summary": {"_untrusted": ..., "text": "..."},
         "should_be_at": "Echo", "confidence": "high",
         "reasoning": {"_untrusted": ..., "text": "..."},
         "current_team": "ACE", "first_team": "ACE", "transitions": N}
    ]
  }
"""

import json
import os
import sys

import _libpath  # noqa: F401
from charter_teams import slugify_team
from jira_client import atomic_write_json
from prompt_safety import smart_truncate as _smart_truncate, wrap_untrusted


CACHE_DIR = "/tmp/charter-boundaries"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
INPUTS_PATH = os.path.join(CACHE_DIR, "inputs.json")
AUDIT_DIR = os.path.join(CACHE_DIR, "audits")
BUNDLE_PATH = os.path.join(CACHE_DIR, "bundle.json")

# Truncate large free-text fields before sending to the sub-agent. Reasoning
# from the routing-audit agent is already capped at ~600 chars, so this is
# mostly a belt-and-braces cap on summary.
MAX_SUMMARY_CHARS = 300
MAX_REASONING_CHARS = 800
MAX_BLURB_CHARS = 4000  # individual charter blurbs are typically 400–1600 chars
MAX_EXAMPLE_RAW_CHARS = 400

ALLOWED_CONFIDENCES = {"high", "medium"}


def _load_audit(canonical):
    slug = slugify_team(canonical)
    path = os.path.join(AUDIT_DIR, slug + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _filter_misroutes(audit):
    """Keep only tickets the audit flagged as `should_be_elsewhere` with at
    least medium confidence and out_of_charter_work == true."""
    out = []
    for t in audit.get("tickets", []):
        if t.get("verdict") != "should_be_elsewhere":
            continue
        if t.get("confidence") not in ALLOWED_CONFIDENCES:
            continue
        if not t.get("out_of_charter_work"):
            continue
        out.append({
            "key": t.get("key", ""),
            "summary": wrap_untrusted(_smart_truncate(t.get("summary", ""), MAX_SUMMARY_CHARS)),
            "should_be_at": t.get("should_be_at", ""),
            "confidence": t.get("confidence", ""),
            "reasoning": wrap_untrusted(_smart_truncate(t.get("reasoning", ""), MAX_REASONING_CHARS)),
            "current_team": t.get("current_team", ""),
            "first_team": t.get("first_team", ""),
            "transitions": t.get("transition_count", 0),
            "priority": t.get("priority", ""),
            "status": t.get("status", ""),
        })
    return out


def _filter_individual_misroutes(audit):
    """Keep all `should_be_elsewhere` tickets with at least medium confidence.

    Distinct from `_filter_misroutes`: skips the `out_of_charter_work` check.
    Single-ticket re-routings are surfaced individually for L2 learning even
    when the focus team's work on them WAS within charter — the audit's
    "this belongs elsewhere" call still teaches L2 something for next time.
    Single tickets per-target don't cluster (clusters need 2+ evidence keys)
    so this list is the renderer's only path to surface them."""
    out = []
    for t in audit.get("tickets", []):
        if t.get("verdict") != "should_be_elsewhere":
            continue
        if t.get("confidence") not in ALLOWED_CONFIDENCES:
            continue
        out.append({
            "key": t.get("key", ""),
            "summary": wrap_untrusted(_smart_truncate(t.get("summary", ""), MAX_SUMMARY_CHARS)),
            "should_be_at": t.get("should_be_at", ""),
            "confidence": t.get("confidence", ""),
            "reasoning": wrap_untrusted(_smart_truncate(t.get("reasoning", ""), MAX_REASONING_CHARS)),
            "current_team": t.get("current_team", ""),
            "first_team": t.get("first_team", ""),
            "transitions": t.get("transition_count", 0),
            "out_of_charter_work": bool(t.get("out_of_charter_work")),
            "priority": t.get("priority", ""),
            "status": t.get("status", ""),
        })
    return out


def _filter_boundary_disputes(audit, focus_team):
    """Keep `split_charter` tickets where the audit identified an external
    candidate team — the user takes this list to other teams' standups for
    ownership conversations. Skips disputes that point back at the focus
    team itself (apply.py upstream blanks should_be_at to "" in that case)
    and skips low-confidence cases (noisy). out_of_charter_work is NOT a
    filter here — split_charter implies shared work by definition."""
    out = []
    for t in audit.get("tickets", []):
        if t.get("verdict") != "split_charter":
            continue
        if t.get("confidence") not in ALLOWED_CONFIDENCES:
            continue
        candidate = (t.get("should_be_at") or "").strip()
        if not candidate or candidate == focus_team:
            continue
        out.append({
            "key": t.get("key", ""),
            "summary": wrap_untrusted(_smart_truncate(t.get("summary", ""), MAX_SUMMARY_CHARS)),
            "candidate_team": candidate,
            "confidence": t.get("confidence", ""),
            "reasoning": wrap_untrusted(_smart_truncate(t.get("reasoning", ""), MAX_REASONING_CHARS)),
            "current_team": t.get("current_team", ""),
            "priority": t.get("priority", ""),
            "status": t.get("status", ""),
        })
    return out


def main():
    if not os.path.exists(SETUP_PATH):
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(INPUTS_PATH):
        print("ERROR: %s not found. Run parse_inputs.py first." % INPUTS_PATH, file=sys.stderr)
        sys.exit(1)
    with open(SETUP_PATH, "r", encoding="utf-8") as f:
        setup = json.load(f)
    with open(INPUTS_PATH, "r", encoding="utf-8") as f:
        inputs = json.load(f)

    teams_records = []
    skipped = []
    for ft in setup["focus_teams"]:
        canonical = ft["canonical"]
        audit = _load_audit(canonical)
        if audit is None:
            skipped.append(canonical)
            continue
        team_inputs = inputs["teams"].get(canonical, {"charter_blurb": "", "examples": []})
        misroutes = _filter_misroutes(audit)
        individual_misroutes = _filter_individual_misroutes(audit)
        boundary_disputes = _filter_boundary_disputes(audit, canonical)
        curated = []
        for ex in team_inputs.get("examples", []):
            curated.append({
                "ticket_key": ex.get("ticket_key", ""),
                "from_team": ex.get("from_team", ""),
                "to_team": ex.get("to_team", ""),
                "url": wrap_untrusted(ex.get("url", "")),
                "raw": wrap_untrusted(_smart_truncate(ex.get("raw", ""), MAX_EXAMPLE_RAW_CHARS)),
            })
        teams_records.append({
            "team": canonical,
            "vault_dir": ft["vault_dir"],
            "charter_blurb": wrap_untrusted(_smart_truncate(team_inputs.get("charter_blurb", ""), MAX_BLURB_CHARS)),
            "curated_examples": curated,
            "misroutes": misroutes,
            "individual_misroutes": individual_misroutes,
            "boundary_disputes": boundary_disputes,
            "audit_window": audit.get("period", {}),
            "audit_candidates_count": audit.get("candidates_count", 0),
        })

    if skipped:
        print("WARNING: no audit snapshot for: %s — run the per-team audit + snapshot.py first." % (
            ", ".join(skipped)), file=sys.stderr)
    if not teams_records:
        print("ERROR: no usable team records — bundle.json not written.", file=sys.stderr)
        sys.exit(1)

    bundle = {
        "schema": "charter-boundaries/v1",
        "period": setup.get("period", {}),
        "allowed_teams": setup.get("allowed_teams", []),
        "teams": teams_records,
    }
    atomic_write_json(BUNDLE_PATH, bundle)

    print("=== BUNDLE ===")
    for tr in teams_records:
        print("  %-15s  charter=%4d chars  examples=%d  misroutes=%d  individuals=%d  disputes=%d" % (
            tr["team"], len(tr["charter_blurb"]["text"]),
            len(tr["curated_examples"]), len(tr["misroutes"]),
            len(tr["individual_misroutes"]), len(tr["boundary_disputes"])))
    print("\nBundle saved to %s" % BUNDLE_PATH)


if __name__ == "__main__":
    main()
