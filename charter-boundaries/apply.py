#!/usr/bin/env python3
"""Validate the synthesise sub-agent's results.json and merge into draft.json.

Drops invalid records with WARNING. Strict on:
- evidence_keys reference real tickets in this team's audit snapshot.
- anchored_by_curated reference real curated example ticket_keys for this team.
- target_team is in allowed_teams.
- theme_id is kebab-case ASCII.
- All free-text fields word-boundary-truncated to safe lengths.
"""

import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import atomic_write_json
from json_io import load_json as _load_json
from prompt_safety import smart_truncate as _smart_truncate


CACHE_DIR = "/tmp/charter-boundaries"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
INPUTS_PATH = os.path.join(CACHE_DIR, "inputs.json")
BUNDLE_PATH = os.path.join(CACHE_DIR, "bundle.json")
RESULTS_PATH = os.path.join(CACHE_DIR, "synthesise", "results.json")
DRAFT_PATH = os.path.join(CACHE_DIR, "draft.json")

_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
_THEME_ID_RE = re.compile(r"\A[a-z][a-z0-9]*(-[a-z0-9]+)*\Z", re.ASCII)

MAX_OWNS_ITEM_CHARS = 120
MAX_BOUNDARY_RULE_CHARS = 200
MAX_DESCRIPTION_CHARS = 200
MAX_TITLE_CHARS = 120
MAX_QUESTION_CHARS = 200
MAX_UNDERSTANDING_CHARS = 320
MAX_OWNS_ITEMS = 30
MAX_BOUNDARY_RULES = 15
MAX_CLUSTERS = 10
MAX_EVIDENCE_PER_CLUSTER = 12
MAX_EDGE_CASES = 8
MIN_EVIDENCE_PER_CLUSTER = 2  # Single-ticket "patterns" are noise.
# Hard caps on raw agent output before iteration. A misbehaving sub-agent
# could emit unbounded teams or clusters; we truncate the input list before
# the validation loop so we don't pay validation cost on junk.
MAX_TEAMS_IN_RESULTS = 50
MAX_RAW_CLUSTERS_PER_TEAM = 50


def _string_list(raw, max_items, max_chars):
    out = []
    for item in (raw or []):
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        out.append(_smart_truncate(s, max_chars))
        if len(out) >= max_items:
            break
    return out


def _validate_cluster(rec, allowed_teams, valid_evidence, valid_curated, team):
    theme_id = str(rec.get("theme_id") or "").strip()
    if not _THEME_ID_RE.match(theme_id):
        print("WARNING: %s cluster dropped — invalid theme_id: %r" % (team, theme_id), file=sys.stderr)
        return None
    target = str(rec.get("target_team") or "").strip()
    if target not in allowed_teams:
        print("WARNING: %s cluster %s dropped — target_team %r not in allowed_teams" % (
            team, theme_id, target), file=sys.stderr)
        return None
    raw_keys = rec.get("evidence_keys") or []
    keys = []
    for k in raw_keys:
        if not isinstance(k, str):
            continue
        ks = k.strip()
        if not _KEY_RE.match(ks):
            continue
        if ks not in valid_evidence:
            continue
        keys.append(ks)
        if len(keys) >= MAX_EVIDENCE_PER_CLUSTER:
            break
    if len(keys) < MIN_EVIDENCE_PER_CLUSTER:
        print("WARNING: %s cluster %s dropped — only %d valid evidence_keys (need %d)" % (
            team, theme_id, len(keys), MIN_EVIDENCE_PER_CLUSTER), file=sys.stderr)
        return None
    raw_anchored = rec.get("anchored_by_curated") or []
    anchored = []
    for k in raw_anchored:
        if not isinstance(k, str):
            continue
        ks = k.strip()
        if not _KEY_RE.match(ks) or ks not in valid_curated:
            continue
        anchored.append(ks)
    title = _smart_truncate(rec.get("title") or "", MAX_TITLE_CHARS)
    description = _smart_truncate(rec.get("description") or "", MAX_DESCRIPTION_CHARS)
    boundary_rule = _smart_truncate(rec.get("boundary_rule") or "", MAX_BOUNDARY_RULE_CHARS)
    return {
        "theme_id": theme_id,
        "title": title,
        "target_team": target,
        "description": description,
        "boundary_rule": boundary_rule,
        "evidence_keys": keys,
        "anchored_by_curated": anchored,
    }


def _validate_edge_cases(raw):
    out = []
    for rec in (raw or []):
        if not isinstance(rec, dict):
            continue
        question = _smart_truncate(rec.get("question") or "", MAX_QUESTION_CHARS)
        understanding = _smart_truncate(rec.get("current_understanding") or "", MAX_UNDERSTANDING_CHARS)
        if not question:
            continue
        out.append({"question": question, "current_understanding": understanding})
        if len(out) >= MAX_EDGE_CASES:
            break
    return out


def main():
    setup = _load_json(SETUP_PATH)
    inputs = _load_json(INPUTS_PATH)
    bundle = _load_json(BUNDLE_PATH)
    results = _load_json(RESULTS_PATH)
    if not (setup and inputs and bundle):
        print("ERROR: setup.json / inputs.json / bundle.json missing.", file=sys.stderr)
        sys.exit(1)
    if results is None:
        print("ERROR: %s not found — synthesise sub-agent must run first." % RESULTS_PATH,
              file=sys.stderr)
        sys.exit(1)

    allowed_teams = set(setup.get("allowed_teams") or [])
    bundle_by_team = {tr["team"]: tr for tr in bundle.get("teams") or []}

    teams_in = (results.get("teams") if isinstance(results, dict) else None) or []
    if len(teams_in) > MAX_TEAMS_IN_RESULTS:
        print("WARNING: results.teams has %d entries; truncating to %d." % (
            len(teams_in), MAX_TEAMS_IN_RESULTS), file=sys.stderr)
        teams_in = teams_in[:MAX_TEAMS_IN_RESULTS]
    teams_out = []
    for rec in teams_in:
        if not isinstance(rec, dict):
            continue
        team = str(rec.get("team") or "").strip()
        if team not in bundle_by_team:
            print("WARNING: unknown team %r in results — skipped" % team, file=sys.stderr)
            continue
        tr = bundle_by_team[team]
        valid_evidence = {m["key"] for m in tr.get("misroutes") or [] if _KEY_RE.match(m.get("key", ""))}
        valid_curated = {ex.get("ticket_key") for ex in tr.get("curated_examples") or []
                         if _KEY_RE.match(ex.get("ticket_key", ""))}

        owns_seed = _string_list(rec.get("owns_seed"), MAX_OWNS_ITEMS, MAX_OWNS_ITEM_CHARS)
        boundary_rules_seed = _string_list(
            rec.get("boundary_rules_seed"), MAX_BOUNDARY_RULES, MAX_BOUNDARY_RULE_CHARS)

        raw_clusters = (rec.get("does_not_own_clusters") or [])[:MAX_RAW_CLUSTERS_PER_TEAM]
        clusters = []
        for c in raw_clusters:
            v = _validate_cluster(c, allowed_teams, valid_evidence, valid_curated, team)
            if v:
                clusters.append(v)
            if len(clusters) >= MAX_CLUSTERS:
                break
        clusters.sort(key=lambda c: len(c["evidence_keys"]), reverse=True)

        edge_cases = _validate_edge_cases(rec.get("edge_cases_seed"))

        teams_out.append({
            "team": team,
            "vault_dir": tr.get("vault_dir", ""),
            "audit_window": tr.get("audit_window", {}),
            "owns_seed": owns_seed,
            "boundary_rules_seed": boundary_rules_seed,
            "does_not_own_clusters": clusters,
            "edge_cases_seed": edge_cases,
        })

    # Include teams the agent skipped: they get empty seeds but the renderer
    # can still produce a baseline draft.
    seen = {t["team"] for t in teams_out}
    for tr in bundle.get("teams") or []:
        if tr["team"] in seen:
            continue
        teams_out.append({
            "team": tr["team"],
            "vault_dir": tr.get("vault_dir", ""),
            "audit_window": tr.get("audit_window", {}),
            "owns_seed": [],
            "boundary_rules_seed": [],
            "does_not_own_clusters": [],
            "edge_cases_seed": [],
        })

    draft = {
        "schema": "charter-boundaries/v1",
        "period": setup.get("period", {}),
        "charters_source": setup.get("charters_source", ""),
        "examples_source": setup.get("examples_source", ""),
        "teams": teams_out,
    }
    atomic_write_json(DRAFT_PATH, draft)

    print("=== DRAFT ===")
    for t in teams_out:
        print("  %-15s  owns=%2d  boundary_rules=%d  clusters=%d  edge_cases=%d" % (
            t["team"], len(t["owns_seed"]), len(t["boundary_rules_seed"]),
            len(t["does_not_own_clusters"]), len(t["edge_cases_seed"])))
    print("\nDraft saved to %s" % DRAFT_PATH)


if __name__ == "__main__":
    main()
