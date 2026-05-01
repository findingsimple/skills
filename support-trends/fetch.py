#!/usr/bin/env python3
"""Support trends fetcher: pulls in-window support tickets + changelog + comments → /tmp/support_trends/data.json"""

import argparse
import os
import re
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import concurrency
from jira_client import (
    load_env, init_auth, jira_get, jira_search_all,
    jira_get_changelog, jira_get_comments, jira_get_dev_summary, adf_to_text,
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
    p = argparse.ArgumentParser()
    p.add_argument("--team-vault-dir", required=True)
    p.add_argument("--support-project-key", required=True)
    p.add_argument("--support-label", default="")
    p.add_argument("--support-team-field", default="")
    p.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--prior-start", default="", help="YYYY-MM-DD inclusive (optional; enables prior-window comparison)")
    p.add_argument("--prior-end", default="", help="YYYY-MM-DD inclusive (optional; pair with --prior-start)")
    p.add_argument("--max-workers", type=int, default=10)
    return p.parse_args()


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


_JQL_CHANGED_CACHE_PATH = os.path.join(CACHE_DIR, ".jql_changed_predicate")
# Sentinel written when every candidate fails — skip the probe entirely on
# subsequent fetches in the same /tmp lifetime so we don't waste 4 round-trips
# per fetch on a Jira instance that doesn't support history operators on
# custom fields. Cleared naturally when /tmp/support_trends/ is rotated.
_JQL_CHANGED_NONE_SENTINEL = "__none__"
# Candidate JQL fragments tested in order. The first that returns without HTTP
# 400 is cached for the rest of the run. `cf[10600] CHANGED` should work on
# modern Atlassian Cloud, but some instances reject it as malformed; the
# fallbacks bracket the same intent (only return tickets whose Team field has
# transitioned at some point) using older history-search syntax. If every
# candidate fails the caller falls back to walking everything.
_INTAKE_CHANGED_CANDIDATES = (
    "cf[10600] CHANGED",
    "cf[10600] CHANGED FROM EMPTY",
    'cf[10600] was EMPTY',
    'cf[10600] was not EMPTY',
)


def _read_cached_jql_predicate():
    """Returns either a known candidate string, the failure sentinel
    (`_JQL_CHANGED_NONE_SENTINEL`), or None when no cache exists."""
    try:
        with open(_JQL_CHANGED_CACHE_PATH) as f:
            cached = f.read().strip()
    except (OSError, FileNotFoundError):
        return None
    if cached == _JQL_CHANGED_NONE_SENTINEL:
        return cached
    return cached if cached in _INTAKE_CHANGED_CANDIDATES else None


def _write_cached_jql_predicate(predicate):
    try:
        with open(_JQL_CHANGED_CACHE_PATH + ".tmp", "w") as f:
            f.write(predicate + "\n")
        os.replace(_JQL_CHANGED_CACHE_PATH + ".tmp", _JQL_CHANGED_CACHE_PATH)
    except OSError:
        pass


def _intake_changed_keys(base_url, auth, project_key, start, end, label):
    """Return the set of intake-window ticket keys whose Team field has ever
    changed, or None if every JQL candidate failed (caller falls back to
    walking everything). Probes the candidate list once per run; the working
    syntax is cached in /tmp/support_trends/.jql_changed_predicate. A sentinel
    is also cached for instances where every candidate fails, so subsequent
    fetches in the same /tmp lifetime skip the probe entirely."""
    cached = _read_cached_jql_predicate()
    if cached == _JQL_CHANGED_NONE_SENTINEL:
        # Prior probe in this /tmp lifetime exhausted every candidate against
        # this Jira instance. Don't burn 4 more round-trips proving it again.
        return None
    candidates = ([cached] + [c for c in _INTAKE_CHANGED_CANDIDATES if c != cached]
                  if cached else list(_INTAKE_CHANGED_CANDIDATES))
    last_err = None
    for fragment in candidates:
        jql = (
            'project = %s AND created >= "%s" AND created <= "%s 23:59" AND %s'
            % (project_key, start, end, fragment)
        )
        try:
            raw = jira_search_all(base_url, auth, jql, "key")
        except Exception as e:
            last_err = e
            continue
        keys = {it.get("key", "") for it in raw if it.get("key")}
        if cached != fragment:
            _write_cached_jql_predicate(fragment)
            print("[%s] Intake CHANGED-predicate probe: '%s' worked (cached for this run)." % (
                label, fragment), file=sys.stderr)
        return keys
    print("WARNING: every candidate intake CHANGED-predicate failed (last error: %s); "
          "falling back to full intake changelog walk. Caching failure sentinel "
          "so subsequent fetches in this /tmp lifetime skip the probe." % last_err,
          file=sys.stderr)
    _write_cached_jql_predicate(_JQL_CHANGED_NONE_SENTINEL)
    return None


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

    # ---- Intake share across whole support project ----
    # Fetch every ticket created in the window across the entire support
    # project (no team filter) with just the fields needed to find the
    # FIRST non-null Team-field assignment.
    intake_fields = "customfield_10600,created"
    intake_jql = (
        'project = %s AND created >= "%s" AND created <= "%s 23:59" ORDER BY created ASC'
        % (args.support_project_key, start, end)
    )
    print("[%s] Fetching intake-share (whole-project) tickets..." % label, file=sys.stderr)
    intake_raw = jira_search_all(base_url, auth, intake_jql, intake_fields)
    print("  Found %d total support tickets created in window" % len(intake_raw), file=sys.stderr)

    # For each intake ticket, walk changelog to find first non-null Team value.
    # Skip changelog fetches for tickets we already enriched (in-window subset
    # already in by_key with a changelog).
    intake_records = []
    for it in intake_raw:
        k = it.get("key", "")
        f = it.get("fields", {}) or {}
        cur_team = (f.get("customfield_10600") or {}).get("name", "") if isinstance(f.get("customfield_10600"), dict) else ""
        intake_records.append({
            "key": k,
            "current_team": cur_team or "(unrouted)",
            "first_team": None,
            "created": f.get("created", ""),
        })
    intake_by_key = {r["key"]: r for r in intake_records}

    # Second JQL: only intake tickets whose Team field has *ever changed* —
    # walking the changelog for tickets with no Team transition is wasted work
    # (current_team == first_team by definition). The exact JQL syntax that
    # works varies by Jira instance: `cf[10600] CHANGED` is rejected as
    # malformed by some instances, so we probe a list of candidates and cache
    # the first that works in `/tmp/support_trends/.jql_changed_predicate` so
    # subsequent runs in the same session skip the probe. Cache invalidates
    # naturally with the rest of /tmp/support_trends/.
    changed_keys_set = _intake_changed_keys(
        base_url, auth, args.support_project_key, start, end, label)

    if changed_keys_set is not None:
        needs_changelog = [r["key"] for r in intake_records
                           if r["key"] not in by_key and r["key"] in changed_keys_set]
        skipped_no_change = sum(1 for r in intake_records
                                if r["key"] not in by_key and r["key"] not in changed_keys_set)
        print("[%s] Team field changed on %d/%d intake tickets — skipping changelog for %d unchanged ones." % (
            label, len(changed_keys_set), len(intake_records), skipped_no_change), file=sys.stderr)
    else:
        needs_changelog = [r["key"] for r in intake_records if r["key"] not in by_key]
    print("[%s] Walking changelog for first-team on %d additional intake tickets (workers=%d)..." % (
        label, len(needs_changelog), args.max_workers), file=sys.stderr)

    def fetch_intake_changelog(key):
        try:
            return key, jira_get_changelog(base_url, auth, key)
        except Exception as e:
            print("WARNING: intake-share changelog fetch failed for %s: %s" % (key, e), file=sys.stderr)
            return key, []

    completed_intake = 0
    intake_changelogs = {}
    if needs_changelog:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {pool.submit(fetch_intake_changelog, k): k for k in needs_changelog}
            for fut in as_completed(futures):
                try:
                    k, cl = fut.result()
                    intake_changelogs[k] = cl
                except Exception as e:
                    print("WARNING: intake enrichment failed for %s: %s" % (futures[fut], e), file=sys.stderr)
                completed_intake += 1
                if completed_intake % 50 == 0:
                    print("  [%s intake] %d / %d enriched" % (label, completed_intake, len(futures)), file=sys.stderr)

    def first_team_from_changelog(cl, fallback):
        # Walk entries chronologically; the first Team field set from null/empty
        # to a value is the routing landing team. If no Team change ever
        # happened, fall back to the current team value.
        for entry in (cl or []):
            for item in entry.get("items", []) or []:
                if (item.get("field") or "").lower() != "team":
                    continue
                fr = (item.get("from_string") or "").strip()
                to = (item.get("to_string") or "").strip()
                if not fr and to:
                    return to
        return fallback

    for r in intake_records:
        k = r["key"]
        if k in by_key:
            cl = (by_key[k] or {}).get("changelog") or []
        else:
            cl = intake_changelogs.get(k) or []
        r["first_team"] = first_team_from_changelog(cl, r["current_team"])

    # ---- Linked-issue issuetype lookup ----
    # Collect all linked-issue keys across in-window tickets (only the ones
    # created IN the window — those are the support tickets we're measuring
    # bug-vs-other conversion for). One bulk JQL fetches their issuetype +
    # status + project so report.py can filter to the engineering ones.
    in_window_tickets = [t for t in by_key.values()
                         if t.get("created", "") and start <= t.get("created", "")[:10] <= end]
    link_keys = set()
    for t in in_window_tickets:
        for ln in t.get("linked_issues", []) or []:
            if ln.get("key"):
                link_keys.add(ln["key"])

    # Code-change detection patterns + issue-link types that strongly imply a
    # code artefact exists. Used to decide whether a linked Bug actually
    # involved a code change (real defect) vs was logged as Bug but resolved
    # via config / data fix (intake labelling artefact).
    _CODE_LINK_URL_RE = re.compile(
        r"(gitlab[\w.-]*[/:][^\s)\]]+/-/(merge_requests|commit)/|"
        r"github\.com[/:][^\s)\]]+/(pull|commit)/[A-Za-z0-9]+|"
        r"bitbucket\.org[/:][^\s)\]]+/(pull-requests|commits)/)",
        re.IGNORECASE,
    )
    _CODE_LINK_TYPES = {"implements", "is implemented by", "fixes", "is fixed by",
                        "develops", "is developed by", "delivers", "is delivered by"}

    linked_issue_types = {}
    if link_keys:
        # Chunk to keep JQL under URL length limits — Jira tolerates ~1000
        # keys but conservative chunking is safer.
        keys_list = sorted(link_keys)
        chunk = 100
        print("[%s] Fetching linked-issue types for %d unique linked issues..." % (label, len(keys_list)), file=sys.stderr)
        for i in range(0, len(keys_list), chunk):
            batch = keys_list[i:i+chunk]
            keys_clause = ", ".join('"%s"' % k for k in batch)
            link_jql = "key in (%s)" % keys_clause
            try:
                items = jira_search_all(base_url, auth, link_jql,
                                        "issuetype,status,project,description,issuelinks")
            except Exception as e:
                print("WARNING: linked-issue lookup failed for chunk: %s" % e, file=sys.stderr)
                continue
            for it in items:
                k = it.get("key", "")
                f = it.get("fields", {}) or {}
                desc_text = adf_to_text(f.get("description") or {})[:MAX_DESCRIPTION_CHARS]
                inner_link_types = []
                for ln in (f.get("issuelinks") or []):
                    inner_link_types.append((ln.get("type") or {}).get("name", "") or "")
                linked_issue_types[k] = {
                    "issuetype": ((f.get("issuetype") or {}).get("name", "")),
                    "status": ((f.get("status") or {}).get("name", "")),
                    "project_key": ((f.get("project") or {}).get("key", "")),
                    "_numeric_id": it.get("id", ""),
                    "_description_text": desc_text,
                    "_inner_link_types": inner_link_types,
                    # Initialised here; updated below for Bug-typed links after
                    # the parallel dev-status + comment fetch.
                    "has_code_change": None,
                    "code_change_evidence": "",
                }

    # ---- Code-change detection on linked Bugs ----
    # For each linked issue typed as Bug we look for evidence of a real code
    # change so the report can split "Bug (with code change)" from "Bug
    # (logged as Bug, no code change)". Three signals checked:
    #   1. Code-host URL (GitLab/GitHub/Bitbucket MR/commit/PR) in the linked
    #      issue's description.
    #   2. Code-host URL in any of the linked issue's comments.
    #   3. The linked issue has its own outward issuelinks of types like
    #      "implements" / "is fixed by" / "develops" — strong proxy for an MR
    #      tracking ticket existing.
    # All three are best-effort. Absence of evidence ≠ no code change (commits
    # may not be linked); the report's footer carries this caveat.
    bug_link_keys = [k for k, v in linked_issue_types.items()
                     if (v.get("issuetype") or "").lower() in ("bug",)]
    print("[%s] Checking %d linked Bugs for code-change evidence (workers=%d)..." % (
        label, len(bug_link_keys), args.max_workers), file=sys.stderr)

    def _detect_code_change(rec, dev_summary, comments):
        # 1. Authoritative signal: Jira `Development` panel summary aggregates
        #    branches + PRs + commits across DVCS providers. PRs > 0 is the
        #    cleanest "code change happened" signal.
        if dev_summary:
            pr_count = (dev_summary.get("pullrequest") or {}).get("count", 0) or 0
            branch_count = (dev_summary.get("branch") or {}).get("count", 0) or 0
            commit_count = (dev_summary.get("commit") or {}).get("count", 0) or 0
            if pr_count > 0:
                return True, "dev-panel: %d PR%s linked" % (pr_count, "" if pr_count == 1 else "s")
            if branch_count > 0:
                return True, "dev-panel: %d branch%s linked" % (branch_count, "" if branch_count == 1 else "es")
            if commit_count > 0:
                return True, "dev-panel: %d commit%s linked" % (commit_count, "" if commit_count == 1 else "s")
        # 2. Fallback: code-host URL in description (handles instances where
        #    the dev-status integration isn't connected for this repo).
        if _CODE_LINK_URL_RE.search(rec.get("_description_text", "") or ""):
            return True, "code URL in description"
        # 3. Fallback: issue-link type implies code artefact.
        for t in rec.get("_inner_link_types", []) or []:
            if (t or "").lower() in _CODE_LINK_TYPES:
                return True, "issue link: " + t
        # 4. Fallback: code-host URL in any comment.
        for c in comments or []:
            if _CODE_LINK_URL_RE.search(c.get("body_text", "") or ""):
                return True, "code URL in comments"
        return False, ""

    def fetch_bug_evidence(k):
        rec = linked_issue_types.get(k) or {}
        nid = rec.get("_numeric_id", "")
        dev = jira_get_dev_summary(base_url, auth, nid) if nid else {}
        # Comments only fetched as fallback when dev-panel says no code.
        # This saves ~75% of comment calls without losing accuracy when
        # dev-status is connected (which it is for HappyCo).
        pr = (dev.get("pullrequest") or {}).get("count", 0) or 0
        branch = (dev.get("branch") or {}).get("count", 0) or 0
        commit = (dev.get("commit") or {}).get("count", 0) or 0
        if pr or branch or commit:
            return k, dev, []
        # Dev panel empty — fetch comments for URL-scan fallback.
        try:
            comments = jira_get_comments(base_url, auth, k)
        except Exception as e:
            print("WARNING: bug-comment fetch failed for %s: %s" % (k, e), file=sys.stderr)
            comments = []
        return k, dev, comments

    bug_evidence = {}  # key → (dev_summary, comments)
    if bug_link_keys:
        with ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {pool.submit(fetch_bug_evidence, k): k for k in bug_link_keys}
            for fut in as_completed(futures):
                try:
                    k, dev, comments = fut.result()
                    bug_evidence[k] = (dev, comments)
                except Exception as e:
                    print("WARNING: bug-evidence fetch failed for %s: %s" % (futures[fut], e), file=sys.stderr)

    bugs_with_code = 0
    for k in bug_link_keys:
        rec = linked_issue_types[k]
        dev, comments = bug_evidence.get(k, ({}, []))
        has_code, evidence = _detect_code_change(rec, dev, comments)
        rec["has_code_change"] = has_code
        rec["code_change_evidence"] = evidence
        if has_code:
            bugs_with_code += 1

    # Drop the heavy intermediate fields — we only needed them for detection.
    for v in linked_issue_types.values():
        v.pop("_description_text", None)
        v.pop("_inner_link_types", None)
        v.pop("_numeric_id", None)

    if bug_link_keys:
        print("[%s] Code-change evidence: %d of %d Bugs have a code link" % (
            label, bugs_with_code, len(bug_link_keys)), file=sys.stderr)

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
        "intake_all_teams": list(intake_by_key.values()),
        "linked_issue_types": linked_issue_types,
    }
    atomic_write_json(output_path, output)
    print("[%s] Wrote %d in-team tickets + %d intake records + %d linked-issue types to %s" % (
        label, len(by_key), len(intake_by_key), len(linked_issue_types), output_path), file=sys.stderr)


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    args = parse_args()

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
