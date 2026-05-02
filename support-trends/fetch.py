#!/usr/bin/env python3
"""Support trends fetcher: pulls in-window support tickets + changelog + comments → /tmp/support_trends/data.json"""

import argparse
import os
import re
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import concurrency
import _libpath  # noqa: F401
from jira_client import (
    load_env, init_auth, jira_search_all,
    jira_get_changelog, jira_get_comments, adf_to_text,
    ensure_tmp_dir, atomic_write_json,
)


CACHE_DIR = "/tmp/support_trends"

# Cap stored free-text fields. Long descriptions and comment bodies are mostly
# stack traces / log dumps that bloat /tmp/ caches and blow sub-agent context
# without adding signal. The synthesis sub-agent further trims at bundle build.
MAX_DESCRIPTION_CHARS = 4000
MAX_COMMENT_CHARS = 1500
MAX_COMMENTS_STORED = 20

# Anchored \A...\Z + re.ASCII prevents trailing-newline/Unicode bypasses.
_NUMERIC_ID_RE = re.compile(r"\A\d+\Z", re.ASCII)
_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-]{0,63}\Z", re.ASCII)
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def _filter_match(pattern, values, name):
    """Keep only values matching `pattern`; warn about any dropped."""
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
    p = argparse.ArgumentParser(
        description=(
            "Defaults every JQL/window argument from /tmp/support_trends/setup.json "
            "(written by setup.py). Pass a flag to override a single field; pass "
            "--no-prior to disable the prior window even if setup.json defined one."))
    p.add_argument("--team-vault-dir", default="")
    p.add_argument("--support-project-key", default="")
    p.add_argument("--support-label", default=None)
    p.add_argument("--support-team-field", default=None)
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--prior-start", default="", help="YYYY-MM-DD inclusive (optional; enables prior-window comparison)")
    p.add_argument("--prior-end", default="", help="YYYY-MM-DD inclusive (optional; pair with --prior-start)")
    p.add_argument("--no-prior", action="store_true", help="Skip the prior window even if setup.json defines one.")
    p.add_argument("--max-workers", type=int, default=10)
    return p.parse_args()


def _hydrate_from_setup(args):
    """Fill any unset CLI args from /tmp/support_trends/setup.json. Keeps every
    interpolated field available as a CLI override for ad-hoc re-runs while
    making the orchestrator path a no-arg invocation."""
    setup_path = os.path.join(CACHE_DIR, "setup.json")
    try:
        with open(setup_path) as f:
            import json as _json
            setup = _json.load(f)
    except FileNotFoundError:
        print("ERROR: %s missing — run setup.py first." % setup_path, file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as e:
        print("ERROR: %s unreadable (%s) — re-run setup.py." % (setup_path, e), file=sys.stderr)
        sys.exit(1)

    teams = setup.get("teams") or []
    if not teams:
        print("ERROR: setup.json has no teams entry. Re-run setup.py with --team.", file=sys.stderr)
        sys.exit(1)
    if len(teams) > 1 and not args.team_vault_dir:
        print("ERROR: setup.json has %d teams; pass --team-vault-dir to disambiguate." % len(teams), file=sys.stderr)
        sys.exit(2)

    team = teams[0]
    if args.team_vault_dir:
        match = [t for t in teams if t.get("vault_dir") == args.team_vault_dir]
        if not match:
            print("ERROR: --team-vault-dir %r not present in setup.json." % args.team_vault_dir, file=sys.stderr)
            sys.exit(2)
        team = match[0]
    else:
        args.team_vault_dir = team.get("vault_dir", "")

    env = setup.get("env") or {}
    if not args.support_project_key:
        args.support_project_key = env.get("support_project_key", "")
    # Use `is None` so the user can pass `--support-label ""` to deliberately
    # clear it (e.g. a label-only team config that wants to test team-field-only).
    if args.support_label is None:
        args.support_label = team.get("support_label", "")
    if args.support_team_field is None:
        args.support_team_field = team.get("support_team_field", "")

    window = setup.get("window") or {}
    if not args.start:
        args.start = window.get("start", "")
    if not args.end:
        args.end = window.get("end", "")
    if not args.no_prior:
        if not args.prior_start:
            args.prior_start = window.get("prior_start") or ""
        if not args.prior_end:
            args.prior_end = window.get("prior_end") or ""
    else:
        args.prior_start = ""
        args.prior_end = ""


def build_team_clause(args, base_url, auth):
    """Build the JQL fragment for team filtering: '(labels in (...) OR cf[10600] in (...))'.

    Returns (clause_str, resolved_team_uuids_list).
    """
    team_clauses = []
    resolved_team_uuids = []

    if args.support_label:
        labels = [l.strip() for l in args.support_label.split(",") if l.strip()]
        labels = _filter_match(_LABEL_RE, labels, "--support-label")
        if len(labels) == 1:
            team_clauses.append('labels = "%s"' % labels[0])
        elif len(labels) > 1:
            label_list = ", ".join('"%s"' % l for l in labels)
            team_clauses.append("labels in (%s)" % label_list)

    if args.support_team_field:
        values = [v.strip() for v in args.support_team_field.split(",") if v.strip()]
        values = _filter_match(_TEAM_NAME_RE, values, "--support-team-field")
        # Resolve display names → UUIDs by looking up a known ticket per team.
        for name in values:
            try:
                lookup_jql = (
                    'project = %s AND labels = "team-%s" AND cf[10600] is not EMPTY ORDER BY created DESC'
                    % (args.support_project_key, name.lower())
                )
                lookup = jira_search_all(base_url, auth, lookup_jql, "customfield_10600")
                for item in lookup[:5]:
                    team_obj = item.get("fields", {}).get("customfield_10600")
                    if isinstance(team_obj, dict) and team_obj.get("name", "").upper() == name.upper():
                        resolved_team_uuids.append(team_obj["id"])
                        break
                else:
                    print("WARNING: Could not resolve Team field UUID for '%s'" % name, file=sys.stderr)
            except Exception as e:
                print("WARNING: Team field UUID lookup failed for '%s': %s" % (name, e), file=sys.stderr)
        if len(resolved_team_uuids) == 1:
            team_clauses.append('cf[10600] = "%s"' % resolved_team_uuids[0])
        elif len(resolved_team_uuids) > 1:
            id_list = ", ".join('"%s"' % tid for tid in resolved_team_uuids)
            team_clauses.append("cf[10600] in (%s)" % id_list)

    if not team_clauses:
        print("ERROR: No usable team filter resolved; aborting to avoid querying entire support project.", file=sys.stderr)
        sys.exit(3)

    return "(" + " OR ".join(team_clauses) + ")", resolved_team_uuids


def normalize_ticket(t):
    f = t.get("fields", {}) or {}
    components = [c.get("name", "") for c in (f.get("components") or [])]
    team_obj = f.get("customfield_10600")
    team_name = team_obj.get("name", "") if isinstance(team_obj, dict) else ""
    team_id = team_obj.get("id", "") if isinstance(team_obj, dict) else ""
    resolution_obj = f.get("resolution")
    # Resolution Category (customfield_11695) — single-select, internal L2
    # taxonomy. Carried as descriptive context only; nothing keys off the value.
    resolution_category_obj = f.get("customfield_11695")
    resolution_category = (
        resolution_category_obj.get("value", "")
        if isinstance(resolution_category_obj, dict)
        else ""
    )
    description = adf_to_text(f.get("description") or {})[:MAX_DESCRIPTION_CHARS]
    # issuelinks: store the linked issue keys + link type. We don't fetch the
    # linked-issue type here — that comes from a single bulk JQL after fetch.
    linked = []
    for ln in (f.get("issuelinks") or []):
        link_type = (ln.get("type") or {}).get("name", "")
        outward = ln.get("outwardIssue") or {}
        inward = ln.get("inwardIssue") or {}
        if outward.get("key"):
            linked.append({"key": outward["key"], "direction": "outward",
                           "link_type": link_type,
                           "type": ((outward.get("fields") or {}).get("issuetype") or {}).get("name", "")})
        if inward.get("key"):
            linked.append({"key": inward["key"], "direction": "inward",
                           "link_type": link_type,
                           "type": ((inward.get("fields") or {}).get("issuetype") or {}).get("name", "")})
    return {
        "key": t.get("key", ""),
        "summary": f.get("summary", "") or "",
        "status": (f.get("status") or {}).get("name", ""),
        "status_id": str((f.get("status") or {}).get("id", "")),
        "priority": (f.get("priority") or {}).get("name", "Medium"),
        "assignee": (f.get("assignee") or {}).get("displayName", "") or "",
        "reporter": (f.get("reporter") or {}).get("displayName", "") or "",
        "components": components,
        "labels": list(f.get("labels") or []),
        "created": f.get("created", "") or "",
        "updated": f.get("updated", "") or "",
        "resolutiondate": f.get("resolutiondate", "") or "",
        "resolution": (resolution_obj or {}).get("name", "") if isinstance(resolution_obj, dict) else "",
        "resolution_category": resolution_category,
        "team_field_name": team_name,
        "team_field_id": team_id,
        "description_text": description,
        "linked_issues": linked,
        "issuetype": ((f.get("issuetype") or {}).get("name", "")),
    }


def fetch_window(args, base_url, auth, team_clause, team_uuids, start, end, output_path, label):
    """Run the three JQLs + per-ticket changelog enrichment for one window,
    then atomic-write the merged ticket bundle to output_path. `label` is a
    short tag ("current" / "prior") used only in stderr progress logging."""
    fields = "summary,status,priority,assignee,reporter,components,created,updated,resolutiondate,resolution,labels,customfield_10600,customfield_11695,description,issuelinks,issuetype"

    # Query 1: tickets created in window (regardless of status).
    jql_created = (
        'project = %s AND %s AND created >= "%s" AND created <= "%s 23:59" ORDER BY created ASC'
        % (args.support_project_key, team_clause, start, end)
    )
    print("[%s] Fetching created-in-window tickets..." % label, file=sys.stderr)
    print("  JQL: %s" % jql_created, file=sys.stderr)
    created_in = jira_search_all(base_url, auth, jql_created, fields)
    print("  Found %d tickets created in window" % len(created_in), file=sys.stderr)

    # Query 2: tickets resolved in window but created before window (so the resolved curve is honest).
    jql_resolved = (
        'project = %s AND %s AND resolved >= "%s" AND resolved <= "%s 23:59" AND created < "%s" ORDER BY resolved ASC'
        % (args.support_project_key, team_clause, start, end, start)
    )
    print("[%s] Fetching resolved-in-window tickets created before the window..." % label, file=sys.stderr)
    resolved_pre = jira_search_all(base_url, auth, jql_resolved, fields)
    print("  Found %d tickets resolved in window but created earlier" % len(resolved_pre), file=sys.stderr)

    # Query 3: open backlog at start of window — needed for backlog math at bucket boundaries.
    jql_backlog = (
        'project = %s AND %s AND created < "%s" AND (resolved is EMPTY OR resolved >= "%s") ORDER BY created ASC'
        % (args.support_project_key, team_clause, start, start)
    )
    print("[%s] Fetching backlog at start of window..." % label, file=sys.stderr)
    backlog_open = jira_search_all(base_url, auth, jql_backlog, fields)
    print("  Found %d tickets open at start of window" % len(backlog_open), file=sys.stderr)

    # Merge by key — backlog tickets resolved in window will appear in both
    # backlog_open and resolved_pre, so dedupe.
    by_key = {}
    for t in created_in:
        by_key[t.get("key", "")] = normalize_ticket(t)
    for t in resolved_pre:
        k = t.get("key", "")
        if k not in by_key:
            by_key[k] = normalize_ticket(t)
    backlog_open_keys = []
    for t in backlog_open:
        k = t.get("key", "")
        backlog_open_keys.append(k)
        if k not in by_key:
            by_key[k] = normalize_ticket(t)

    all_tickets = list(by_key.values())
    print("[%s] Total unique tickets to enrich: %d" % (label, len(all_tickets)), file=sys.stderr)

    # Per-ticket changelog + comments in parallel.
    print("[%s] Fetching changelogs + comments in parallel (workers=%d)..." % (label, args.max_workers), file=sys.stderr)

    def fetch_one(key):
        try:
            cl = jira_get_changelog(base_url, auth, key)
        except Exception as e:
            print("WARNING: changelog fetch failed for %s: %s" % (key, e), file=sys.stderr)
            cl = []
        try:
            comments = jira_get_comments(base_url, auth, key)
            trimmed = []
            for c in comments[:MAX_COMMENTS_STORED]:
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

    completed = 0
    with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
        futures = {pool.submit(fetch_one, t["key"]): t["key"] for t in all_tickets}
        for fut in as_completed(futures):
            try:
                key, cl, comments = fut.result()
                if key in by_key:
                    by_key[key]["changelog"] = cl
                    by_key[key]["comments"] = comments
            except Exception as e:
                print("WARNING: enrichment failed for %s: %s" % (futures[fut], e), file=sys.stderr)
            completed += 1
            if completed % 25 == 0:
                print("  [%s] %d / %d enriched" % (label, completed, len(futures)), file=sys.stderr)

    output = {
        "args": {
            "team_vault_dir": args.team_vault_dir,
            "support_project_key": args.support_project_key,
            "support_label": args.support_label,
            "support_team_field": args.support_team_field,
            "start": start,
            "end": end,
        },
        "team_uuids": team_uuids,
        "backlog_open_keys": backlog_open_keys,
        "tickets": list(by_key.values()),
    }
    atomic_write_json(output_path, output)
    print("[%s] Wrote %d in-team tickets to %s" % (
        label, len(by_key), output_path), file=sys.stderr)


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    args = parse_args()
    _hydrate_from_setup(args)

    if not args.team_vault_dir:
        print("ERROR: team-vault-dir is empty after merging setup.json — pass --team-vault-dir.", file=sys.stderr)
        sys.exit(2)
    if not args.support_project_key:
        print("ERROR: support-project-key is empty after merging setup.json — pass --support-project-key.", file=sys.stderr)
        sys.exit(2)
    if not args.start or not args.end:
        print("ERROR: --start / --end are empty after merging setup.json — pass them or re-run setup.py.", file=sys.stderr)
        sys.exit(2)
    _require_match(_VAULT_DIR_RE, args.team_vault_dir, "--team-vault-dir")
    _require_match(_PROJECT_KEY_RE, args.support_project_key, "--support-project-key")
    _validate_date(args.start, "--start")
    _validate_date(args.end, "--end")
    if args.max_workers < 1 or args.max_workers > 32:
        print("ERROR: --max-workers must be between 1 and 32", file=sys.stderr)
        sys.exit(2)

    if args.start > args.end:
        print("ERROR: --start (%s) is after --end (%s)" % (args.start, args.end), file=sys.stderr)
        sys.exit(2)

    # --prior-start and --prior-end must be paired; either both or neither.
    prior_enabled = bool(args.prior_start or args.prior_end)
    if prior_enabled:
        if not (args.prior_start and args.prior_end):
            print("ERROR: --prior-start and --prior-end must be provided together", file=sys.stderr)
            sys.exit(2)
        _validate_date(args.prior_start, "--prior-start")
        _validate_date(args.prior_end, "--prior-end")
        if args.prior_start > args.prior_end:
            print("ERROR: --prior-start (%s) is after --prior-end (%s)" % (args.prior_start, args.prior_end), file=sys.stderr)
            sys.exit(2)
        if args.prior_end >= args.start:
            print("ERROR: --prior-end (%s) must be strictly before --start (%s)" % (args.prior_end, args.start), file=sys.stderr)
            sys.exit(2)
        # Equal-length windows. Δ% comparisons (volume, theme spike, breakdowns)
        # implicitly assume the prior window covers the same number of days as
        # the current window. The orchestrator (SKILL.md Step 2b) computes
        # equal-length prior bounds; a manual override with mismatched lengths
        # produces silently invalid Δ%. WARN rather than abort so legitimate
        # ad-hoc comparisons still work, but make the asymmetry visible.
        from datetime import date as _date
        cur_days = (_date.fromisoformat(args.end) - _date.fromisoformat(args.start)).days + 1
        pri_days = (_date.fromisoformat(args.prior_end) - _date.fromisoformat(args.prior_start)).days + 1
        if cur_days != pri_days:
            print("WARNING: prior window is %d days, current window is %d days. Δ%% comparisons assume equal length — interpret carefully." % (
                pri_days, cur_days), file=sys.stderr)

    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    base_url, auth = init_auth(env)

    team_clause, team_uuids = build_team_clause(args, base_url, auth)

    ensure_tmp_dir(CACHE_DIR)

    # Always remove a stale data_prior.json from a previous --with-prior run
    # so analyze.py's "is prior present?" check (file exists) stays honest
    # when this run is intentionally snapshot-only.
    prior_path = os.path.join(CACHE_DIR, "data_prior.json")
    if not prior_enabled and os.path.exists(prior_path):
        os.remove(prior_path)

    fetch_window(args, base_url, auth, team_clause, team_uuids,
                 args.start, args.end,
                 os.path.join(CACHE_DIR, "data.json"), "current")

    if prior_enabled:
        fetch_window(args, base_url, auth, team_clause, team_uuids,
                     args.prior_start, args.prior_end,
                     prior_path, "prior")


if __name__ == "__main__":
    main()
