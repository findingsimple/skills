#!/usr/bin/env python3
"""Two-stage fetch for support-routing-audit:

  Stage 1 — JQL net (broad but cheap): every ticket currently OR previously
  assigned to the focus team's label or Team-field UUID, in the period window.

  Stage 2 — per-ticket changelog filter: keep only tickets where the focus team
  appears in the Team field history (or current value). Drops false positives
  where the per-team label was applied but the Team field never resolved.

Persists the kept ticket set + changelog + comments to
`/tmp/support-routing-audit/data.json`.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from jira_client import (
    load_env, init_auth, jira_search_all,
    jira_get_changelog, jira_get_comments, adf_to_text,
    ensure_tmp_dir, atomic_write_json,
)


CACHE_DIR = "/tmp/support-routing-audit"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")

# Tighter than support-trends because we ship every kept ticket to the audit
# sub-agent (not just thematic samples).
MAX_DESCRIPTION_CHARS = 1500
MAX_COMMENT_CHARS = 800
MAX_COMMENTS_STORED = 5
# Cap on the per-label scan when looking up cf[10600] UUIDs — only the most
# recent 25 tickets per label are inspected for a matching display value.
MAX_UUID_LOOKUP_SCAN = 25

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-&]{0,63}\Z", re.ASCII)
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_UUID_RE = re.compile(r"\A[A-Za-z0-9\-]{16,64}\Z", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def _filter_match(pattern, values, name):
    out = []
    for v in values:
        if pattern.match(v):
            out.append(v)
        else:
            print("WARNING: %s: dropping malformed value %r" % (name, v), file=sys.stderr)
    return out


def _validate_date(value, name):
    _require_match(_DATE_RE, value, name)
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print("ERROR: %s is not a valid date: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--max-tickets", type=int, default=250,
                   help="Hard cap on kept ticket count (default 250, max 500)")
    p.add_argument("--max-workers", type=int, default=10)
    return p.parse_args()


def resolve_team_uuids(base_url, auth, project_key, focus_team_field_values, focus_labels):
    """For each focus-team display value, look up its cf[10600] UUID by
    searching a known ticket per (label, value) pair. Mirrors the lookup
    pattern in support-trends/fetch.py:97-117."""
    uuids = []
    for value in focus_team_field_values:
        # Multi-label slots (a single SUPPORT_TEAM_LABEL slot containing two
        # comma-separated labels) mean a display value may live under either
        # label — try each, take the first match.
        found = None
        for label in (focus_labels or [""]):
            label_clause = ('labels = "%s" AND ' % label) if label else ""
            jql = (
                'project = %s AND %scf[10600] is not EMPTY ORDER BY created DESC'
                % (project_key, label_clause)
            )
            try:
                lookup = jira_search_all(base_url, auth, jql, "customfield_10600")
            except Exception as e:
                print("WARNING: UUID lookup search failed for %r: %s" % (value, e), file=sys.stderr)
                continue
            for item in lookup[:MAX_UUID_LOOKUP_SCAN]:
                team_obj = (item.get("fields") or {}).get("customfield_10600")
                if isinstance(team_obj, dict) and team_obj.get("name", "").upper() == value.upper():
                    found = team_obj.get("id", "")
                    break
            if found:
                break
        if found and _UUID_RE.match(found):
            uuids.append(found)
        else:
            print("WARNING: Could not resolve cf[10600] UUID for focus value %r" % value, file=sys.stderr)
    return uuids


def build_focus_clause(focus_labels, focus_uuids):
    """Build the Stage 1 OR clause. Falls back to label-only if no UUIDs
    resolved, or UUID-only if no labels. Aborts if neither."""
    parts = []
    if focus_labels:
        if len(focus_labels) == 1:
            parts.append('labels = "%s"' % focus_labels[0])
        else:
            label_list = ", ".join('"%s"' % l for l in focus_labels)
            parts.append("labels in (%s)" % label_list)
    if focus_uuids:
        if len(focus_uuids) == 1:
            parts.append('cf[10600] = "%s"' % focus_uuids[0])
        else:
            uuid_list = ", ".join('"%s"' % u for u in focus_uuids)
            parts.append("cf[10600] in (%s)" % uuid_list)
    if not parts:
        print("ERROR: No usable focus filter (no labels and no UUIDs resolved). "
              "Cannot run Stage 1 JQL.", file=sys.stderr)
        sys.exit(3)
    return "(" + " OR ".join(parts) + ")"


def normalize_ticket(t):
    f = t.get("fields", {}) or {}
    components = [c.get("name", "") for c in (f.get("components") or [])]
    team_obj = f.get("customfield_10600")
    team_name = team_obj.get("name", "") if isinstance(team_obj, dict) else ""
    resolution_obj = f.get("resolution")
    description = adf_to_text(f.get("description") or {})[:MAX_DESCRIPTION_CHARS]
    return {
        "key": t.get("key", ""),
        "summary": f.get("summary", "") or "",
        "status": (f.get("status") or {}).get("name", ""),
        "priority": (f.get("priority") or {}).get("name", "Medium"),
        "assignee": (f.get("assignee") or {}).get("displayName", "") or "",
        "reporter": (f.get("reporter") or {}).get("displayName", "") or "",
        "components": components,
        "labels": list(f.get("labels") or []),
        "created": f.get("created", "") or "",
        "updated": f.get("updated", "") or "",
        "resolutiondate": f.get("resolutiondate", "") or "",
        "resolution": (resolution_obj or {}).get("name", "") if isinstance(resolution_obj, dict) else "",
        "current_team": team_name,
        "description_text": description,
        "issuetype": ((f.get("issuetype") or {}).get("name", "")),
    }


def extract_team_transitions(changelog):
    """Walk changelog entries, return list of {from, to, when, who} for every
    Team field transition, plus a derived first_team value (first non-empty
    'to' value seen in chronological order)."""
    transitions = []
    first_team = None
    for entry in (changelog or []):
        when = entry.get("created", "")
        who = entry.get("author", "")
        for item in (entry.get("items") or []):
            if (item.get("field") or "").lower() != "team":
                continue
            fr = (item.get("from_string") or "").strip()
            to = (item.get("to_string") or "").strip()
            transitions.append({"from": fr, "to": to, "when": when, "who": who})
            if first_team is None and to:
                first_team = to
    return transitions, first_team


def focus_in_history(transitions, current_team, focus_team_field_values):
    """Returns True iff the focus team (any of its display values) appears in
    the team field's history or as the current value."""
    targets = {v.upper() for v in focus_team_field_values}
    if (current_team or "").upper() in targets:
        return True
    for t in transitions:
        if (t.get("from") or "").upper() in targets:
            return True
        if (t.get("to") or "").upper() in targets:
            return True
    return False


def main():
    args = parse_args()
    if args.max_tickets < 1 or args.max_tickets > 500:
        print("ERROR: --max-tickets must be between 1 and 500", file=sys.stderr)
        sys.exit(2)

    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        sys.exit(1)

    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    base_url, auth = init_auth(env)

    project_key = setup["env"]["support_project_key"]
    _require_match(_PROJECT_KEY_RE, project_key, "support_project_key")

    focus_team = setup["focus_team"]
    _require_match(_TEAM_NAME_RE, focus_team, "focus_team")

    focus_labels = [l.strip() for l in (setup.get("focus_label") or "").split(",") if l.strip()]
    focus_labels = _filter_match(_LABEL_RE, focus_labels, "focus_label")

    focus_field_values = [v.strip() for v in (setup.get("focus_team_field_value") or "").split(",") if v.strip()]
    focus_field_values = _filter_match(_TEAM_NAME_RE, focus_field_values, "focus_team_field_value")

    start = setup["period"]["start"]
    end = setup["period"]["end"]
    _validate_date(start, "period.start")
    _validate_date(end, "period.end")

    print("[fetch] Resolving cf[10600] UUIDs for focus team values: %s" % (focus_field_values or "(none)"), file=sys.stderr)
    focus_uuids = resolve_team_uuids(base_url, auth, project_key, focus_field_values, focus_labels)
    if focus_uuids:
        print("  Resolved UUIDs: %s" % focus_uuids, file=sys.stderr)

    focus_clause = build_focus_clause(focus_labels, focus_uuids)

    fields = "summary,status,priority,assignee,reporter,components,created,updated,resolutiondate,resolution,labels,customfield_10600,description,issuetype"

    # Stage 1 — JQL net: created OR resolved in window AND (labels OR team field).
    jql = (
        'project = %s AND %s AND ('
        '(created >= "%s" AND created <= "%s 23:59")'
        ' OR '
        '(resolved >= "%s" AND resolved <= "%s 23:59")'
        ') ORDER BY created DESC'
        % (project_key, focus_clause, start, end, start, end)
    )
    print("[fetch] Stage 1 JQL: %s" % jql, file=sys.stderr)
    candidates = jira_search_all(base_url, auth, jql, fields)
    print("[fetch] Stage 1 returned %d candidate tickets" % len(candidates), file=sys.stderr)

    if not candidates:
        ensure_tmp_dir(CACHE_DIR)
        atomic_write_json(DATA_PATH, {
            "focus_team": focus_team,
            "focus_team_field_values": focus_field_values,
            "focus_labels": focus_labels,
            "period": {"start": start, "end": end},
            "tickets": [],
            "truncated": False,
            "candidates_count": 0,
            "kept_count": 0,
        })
        print("No tickets in window; wrote empty data.json.")
        return 0

    # Per-ticket: changelog + comments in parallel.
    by_key = {t.get("key", ""): normalize_ticket(t) for t in candidates if t.get("key")}

    def fetch_one(key):
        try:
            cl = jira_get_changelog(base_url, auth, key)
        except Exception as e:
            print("WARNING: changelog fetch failed for %s: %s" % (key, e), file=sys.stderr)
            cl = []
        try:
            raw_comments = jira_get_comments(base_url, auth, key)
            trimmed = []
            for c in raw_comments[:MAX_COMMENTS_STORED]:
                body = (c.get("body_text") or "")[:MAX_COMMENT_CHARS]
                trimmed.append({
                    "author": c.get("author", ""),
                    "created": c.get("created", ""),
                    "body_text": body,
                })
            comments = trimmed
        except Exception as e:
            print("WARNING: comments fetch failed for %s: %s" % (key, e), file=sys.stderr)
            comments = []
        return key, cl, comments

    print("[fetch] Enriching %d candidates in parallel (workers=%d)..." % (len(by_key), args.max_workers), file=sys.stderr)
    completed = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(fetch_one, k): k for k in by_key.keys()}
        for fut in as_completed(futures):
            try:
                key, cl, comments = fut.result()
                if key in by_key:
                    transitions, first_team = extract_team_transitions(cl)
                    by_key[key]["team_transitions"] = transitions
                    by_key[key]["transition_count"] = len(transitions)
                    by_key[key]["first_team"] = first_team or by_key[key].get("current_team", "")
                    by_key[key]["comments"] = comments
            except Exception as e:
                print("WARNING: enrichment failed for %s: %s" % (futures[fut], e), file=sys.stderr)
            completed += 1
            if completed % 25 == 0:
                print("  [enrich] %d / %d done" % (completed, len(futures)), file=sys.stderr)

    # Stage 2 — keep only tickets where focus team appears in transitions
    # (or current value). Drops false positives from sticky labels.
    kept = []
    dropped_no_focus_in_history = 0
    for ticket in by_key.values():
        if focus_in_history(
            ticket.get("team_transitions") or [],
            ticket.get("current_team", ""),
            focus_field_values or [focus_team],
        ):
            kept.append(ticket)
        else:
            dropped_no_focus_in_history += 1
    print("[fetch] Stage 2 kept %d / %d (dropped %d with no %s in team history)" % (
        len(kept), len(by_key), dropped_no_focus_in_history, focus_team), file=sys.stderr)

    truncated = False
    if len(kept) > args.max_tickets:
        kept.sort(key=lambda t: t.get("created", ""), reverse=True)
        kept = kept[:args.max_tickets]
        truncated = True
        print("[fetch] Truncated to %d tickets (most recent first); --max-tickets=%d" % (
            args.max_tickets, args.max_tickets), file=sys.stderr)

    ensure_tmp_dir(CACHE_DIR)
    atomic_write_json(DATA_PATH, {
        "focus_team": focus_team,
        "focus_team_field_values": focus_field_values,
        "focus_labels": focus_labels,
        "period": {"start": start, "end": end},
        "tickets": kept,
        "truncated": truncated,
        "candidates_count": len(by_key),
        "kept_count": len(kept),
    })
    print("Wrote %d tickets to %s" % (len(kept), DATA_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
