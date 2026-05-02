#!/usr/bin/env python3
"""Sprint pulse data fetcher: gathers sprint issues, changelogs, comments, MRs, and support tickets."""

import json
import os
import re
import sys
import argparse
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import _libpath  # noqa: F401
from jira_client import load_env, init_auth, jira_get, jira_search_all, jira_get_changelog, jira_get_comments
from gitlab_client import load_gitlab_env, gitlab_get, gitlab_get_all, search_mrs_for_issue, get_mr_notes


# Anchored \A...\Z + re.ASCII prevents trailing-newline/Unicode bypasses of
# JQL/URL interpolation.
_NUMERIC_ID_RE = re.compile(r"\A\d+\Z", re.ASCII)
_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
# Jira labels: alphanumeric + hyphen/underscore/dot (dots appear in some
# real-world labels like "team.a"). No spaces, no JQL metacharacters.
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}\Z", re.ASCII)
# Team field display names are human-facing but we scope to safe tokens for JQL.
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-]{0,63}\Z", re.ASCII)


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


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--team-vault-dir", required=True)
    p.add_argument("--board-id", required=True)
    p.add_argument("--sprint-id", required=True)
    p.add_argument("--sprint-name", required=True)
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--project-key", required=True)
    p.add_argument("--support-project-key", default="")
    p.add_argument("--support-label", default="")
    p.add_argument("--board-config-json", default="", help="Path to board column config JSON")
    p.add_argument("--support-board-config-json", default="", help="Path to support board column config JSON")
    p.add_argument("--support-team-field", default="", help="Comma-separated Team field values (cf[10600])")
    return p.parse_args()


def identify_active_columns(board_config):
    """Identify column names that represent active work (between To Do and Done)."""
    if not board_config:
        return set()
    column_names = [c["name"] for c in board_config]
    active = set()
    in_active_zone = False
    for name in column_names:
        lower = name.lower().strip()
        if lower in ("to do", "todo", "backlog", "open"):
            in_active_zone = True
            continue
        if lower in ("done", "closed", "resolved", "completed"):
            break
        if in_active_zone:
            active.add(name)
    # Fallback: if no active columns detected, use common names
    if not active:
        for name in column_names:
            lower = name.lower().strip()
            if any(kw in lower for kw in ("progress", "review", "testing", "qa")):
                active.add(name)
    return active


def get_status_column(issue, board_config):
    """Map an issue's status to its board column name."""
    status_id = str(issue.get("fields", {}).get("status", {}).get("id", ""))
    status_name = issue.get("fields", {}).get("status", {}).get("name", "")
    for col in board_config:
        if status_id in col.get("statuses", []):
            return col["name"]
    return status_name


def parse_dt(dt_str):
    """Parse an ISO datetime string to a timezone-aware datetime."""
    if not dt_str:
        return None
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def fmt_duration(seconds):
    """Format seconds as a human-readable duration."""
    if seconds is None or seconds < 0:
        return "-"
    minutes = int(seconds / 60)
    hours = int(minutes / 60)
    days = int(hours / 24)
    if days > 0:
        remaining_hours = hours % 24
        if remaining_hours > 0:
            return "%dd %dh" % (days, remaining_hours)
        return "%dd" % days
    if hours > 0:
        remaining_mins = minutes % 60
        if remaining_mins > 0:
            return "%dh %dm" % (hours, remaining_mins)
        return "%dh" % hours
    return "%dm" % minutes


def median(values):
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def percentile(values, p):
    """Compute the p-th percentile of a list of values."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(s):
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


def dora_deploy_rating(deploys_per_day):
    """Return DORA rating for deployment frequency."""
    if deploys_per_day >= 1.0:
        return "Elite"
    if deploys_per_day >= 1 / 7:
        return "High"
    if deploys_per_day >= 1 / 30:
        return "Medium"
    return "Low"


def dora_lead_time_rating(median_seconds):
    """Return DORA rating for lead time based on median seconds."""
    if median_seconds is None:
        return None
    if median_seconds < 86400:
        return "Elite"
    if median_seconds < 604800:
        return "High"
    if median_seconds < 2592000:
        return "Medium"
    return "Low"


def fetch_dora_snapshot(gitlab_url, gitlab_token, gitlab_project_id,
                        all_issue_keys, start_date, end_date,
                        known_authors=None):
    """Fetch DORA deployment frequency and lead time for the sprint window.

    Args:
        known_authors: set of GitLab usernames already discovered from Step 3 MR search
    """
    empty_result = lambda branch: {
        "deploy_count": 0, "days_with_deploys": 0, "sprint_days": 0,
        "elapsed_days": 0, "deploys_per_day": 0, "deploy_rating": "Low",
        "lead_time_median_s": None, "lead_time_median_display": "-",
        "lead_time_p90_s": None, "lead_time_p90_display": "-",
        "lead_time_rating": None, "default_branch": branch,
        "team_authors": [],
    }

    # Check if sprint has started yet
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today_str < start_date[:10]:
        print("Sprint hasn't started yet — skipping DORA snapshot", file=sys.stderr)
        return empty_result("main")

    # Fetch default branch
    default_branch = "main"
    try:
        project_info = gitlab_get(gitlab_url, "/projects/%s" % gitlab_project_id, gitlab_token)
        default_branch = project_info.get("default_branch", "main")
    except Exception as e:
        print("Warning: Could not fetch default branch, using 'main': %s" % e, file=sys.stderr)

    # Discover team authors by searching all sprint issue keys (any MR state).
    # Step 3 only searches state=opened, so known_authors may miss authors with
    # merged MRs on active issues. We search all keys here to catch them.
    team_authors = set(known_authors or [])
    print("Discovering team authors from %d sprint issue MRs (%d already known from active issues)..." % (
        len(all_issue_keys), len(team_authors)), file=sys.stderr)

    def search_mrs_any_state(key):
        path = "/projects/%s/merge_requests?search=%s&per_page=20" % (
            gitlab_project_id, urllib.parse.quote(key, safe=""))
        try:
            mrs = gitlab_get(gitlab_url, path, gitlab_token)
            authors = set()
            pattern = r'(?i)\b' + re.escape(key) + r'(?!\d)'
            for mr in mrs:
                combined = "%s %s %s" % (mr.get("title") or "", mr.get("description") or "", mr.get("source_branch") or "")
                if re.search(pattern, combined):
                    username = mr.get("author", {}).get("username", "")
                    if username:
                        authors.add(username)
            return authors
        except Exception:
            return set()

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_key = {pool.submit(search_mrs_any_state, key): key for key in all_issue_keys}
        for future in as_completed(future_to_key):
            try:
                team_authors.update(future.result())
            except Exception as e:
                print("WARNING: Author search failed for %s: %s" % (future_to_key[future], e), file=sys.stderr)

    if not team_authors:
        print("No team authors found — DORA snapshot will show zero deployments", file=sys.stderr)
        return empty_result(default_branch)

    print("Team authors: %s" % ", ".join(sorted(team_authors)), file=sys.stderr)

    # Fetch all merged MRs to default branch during sprint window
    # Use min(today, end_date) for elapsed days since pulse runs mid-sprint
    today_date = datetime.strptime(today_str, "%Y-%m-%d")
    end_date_parsed = datetime.strptime(end_date[:10], "%Y-%m-%d")
    effective_end = min(end_date_parsed, today_date).strftime("%Y-%m-%d")

    print("Fetching all merged MRs to '%s' for DORA (%s to %s)..." % (
        default_branch, start_date[:10], effective_end), file=sys.stderr)
    # GitLab API doesn't support filtering by merged_at directly, so we filter on
    # updated_after/updated_before as a proxy and post-filter on merged_at below.
    # This may over-fetch MRs updated (but not merged) in the window.
    path = "/projects/%s/merge_requests?target_branch=%s&state=merged&updated_after=%sT00:00:00Z&updated_before=%sT23:59:59Z&per_page=100" % (
        gitlab_project_id, urllib.parse.quote(default_branch, safe=""), start_date[:10], effective_end)
    all_merged = gitlab_get_all(gitlab_url, path, gitlab_token)

    # Post-filter on merged_at within sprint window
    start_dt = parse_dt(start_date[:10] + "T00:00:00Z")
    end_dt = parse_dt(effective_end + "T23:59:59Z")
    team_merged = []
    for mr in all_merged:
        merged_at = mr.get("merged_at")
        if not merged_at:
            continue
        merged_dt = parse_dt(merged_at)
        if not merged_dt or not (start_dt <= merged_dt <= end_dt):
            continue
        if mr.get("author", {}).get("username", "") in team_authors:
            team_merged.append(mr)

    # Deployment frequency: count distinct days with at least one deploy
    start_dt_dora = parse_dt(start_date[:10] + "T00:00:00Z")
    end_dt_dora = parse_dt(effective_end + "T00:00:00Z")
    end_dt_full = parse_dt(end_date[:10] + "T00:00:00Z")
    sprint_days_full = max((end_dt_full - start_dt_dora).days, 1)
    elapsed_days = max((end_dt_dora - start_dt_dora).days, 1)
    deploy_count = len(team_merged)

    deploy_dates = set()
    for m in team_merged:
        merged_at = m.get("merged_at", "")
        if merged_at:
            deploy_dates.add(merged_at[:10])
    days_with_deploys = len(deploy_dates)
    deploys_per_day = deploy_count / elapsed_days

    # Lead time: first authored commit to merge for each team MR
    print("Fetching commits for %d team MRs (lead time)..." % len(team_merged), file=sys.stderr)
    lead_times = []

    def fetch_lead_time(mr):
        iid = mr["iid"]
        merged_at = parse_dt(mr.get("merged_at", ""))
        if not merged_at:
            return None
        try:
            commits = gitlab_get(gitlab_url, "/projects/%s/merge_requests/%s/commits?per_page=100" % (
                gitlab_project_id, iid), gitlab_token)
            if not commits:
                return None
            earliest = None
            for c in commits:
                cdt = parse_dt(c.get("authored_date") or c.get("committed_date") or c.get("created_at"))
                if cdt and (earliest is None or cdt < earliest):
                    earliest = cdt
            if earliest:
                return (merged_at - earliest).total_seconds()
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        future_to_iid = {pool.submit(fetch_lead_time, mr): mr["iid"] for mr in team_merged}
        for future in as_completed(future_to_iid):
            try:
                result = future.result()
                if result is not None and result >= 0:
                    lead_times.append(result)
            except Exception as e:
                print("WARNING: Lead time fetch failed for MR !%s: %s" % (future_to_iid[future], e), file=sys.stderr)

    lead_time_med = median(lead_times)
    lead_time_p90 = percentile(lead_times, 90)

    dora = {
        "deploy_count": deploy_count,
        "days_with_deploys": days_with_deploys,
        "sprint_days": sprint_days_full,
        "elapsed_days": elapsed_days,
        "deploys_per_day": round(deploys_per_day, 2),
        "deploy_rating": dora_deploy_rating(deploys_per_day),
        "lead_time_median_s": lead_time_med,
        "lead_time_median_display": fmt_duration(lead_time_med),
        "lead_time_p90_s": lead_time_p90,
        "lead_time_p90_display": fmt_duration(lead_time_p90),
        "lead_time_rating": dora_lead_time_rating(lead_time_med),
        "default_branch": default_branch,
        "team_authors": sorted(team_authors),
    }

    print("DORA: %d deployments on %d/%d days (%.2f/day, %s) | Lead time median: %s (%s)" % (
        deploy_count, days_with_deploys, elapsed_days, deploys_per_day, dora["deploy_rating"],
        fmt_duration(lead_time_med), dora["lead_time_rating"] or "N/A",
    ), file=sys.stderr)

    return dora


def main():
    args = parse_args()

    # Init Jira
    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    base_url, auth = init_auth(env)

    # Init GitLab
    gitlab_url, gitlab_token, gitlab_project_id = load_gitlab_env()

    # Validate identifiers used in JQL/URL interpolation.
    _require_match(_NUMERIC_ID_RE, args.board_id, "--board-id")
    _require_match(_NUMERIC_ID_RE, args.sprint_id, "--sprint-id")
    _require_match(_PROJECT_KEY_RE, args.project_key, "--project-key")
    if args.support_project_key:
        _require_match(_PROJECT_KEY_RE, args.support_project_key, "--support-project-key")

    # Load board config
    board_config = []
    if args.board_config_json:
        with open(args.board_config_json, "r") as f:
            board_config = json.load(f)

    active_columns = identify_active_columns(board_config)
    print("Active columns: %s" % ", ".join(sorted(active_columns)) if active_columns else "Active columns: (using status names)", file=sys.stderr)

    # Step 1: Fetch sprint report for the active sprint
    print("Fetching sprint report...", file=sys.stderr)
    report = jira_get(
        base_url,
        "/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s" % (args.board_id, args.sprint_id),
        auth,
    )
    contents = report.get("contents", {})
    completed_issues = contents.get("completedIssues", [])
    not_completed = contents.get("issuesNotCompletedInCurrentSprint", [])
    added_during = set(contents.get("issueKeysAddedDuringSprint", {}).keys())

    all_issues = completed_issues + not_completed
    print("Sprint issues: %d completed, %d in progress" % (len(completed_issues), len(not_completed)), file=sys.stderr)

    # Build issue summary list
    issues_data = []
    active_issue_keys = []

    for issue in all_issues:
        key = issue.get("key", "")
        fields = issue if "fields" not in issue else issue
        # Sprint report format differs from search API — adapt
        summary = issue.get("summary", "") or (issue.get("fields", {}) or {}).get("summary", "")
        status_name = issue.get("statusName", "") or (issue.get("fields", {}) or {}).get("status", {}).get("name", "")
        assignee = issue.get("assignee", "") or ((issue.get("fields", {}) or {}).get("assignee") or {}).get("displayName", "Unassigned")
        type_name = issue.get("typeName", "") or (issue.get("fields", {}) or {}).get("issuetype", {}).get("name", "")
        story_points = issue.get("estimateStatistic", {}).get("statFieldValue", {}).get("value")
        if story_points is None:
            story_points = issue.get("currentEstimateStatistic", {}).get("statFieldValue", {}).get("value")

        # Determine column
        column = status_name
        if board_config:
            status_id = str(issue.get("statusId", "") or (issue.get("fields", {}) or {}).get("status", {}).get("id", ""))
            for col in board_config:
                if status_id in col.get("statuses", []):
                    column = col["name"]
                    break

        is_completed = issue in completed_issues
        is_active = not is_completed and (
            column in active_columns or
            any(kw in column.lower() for kw in ("progress", "review"))
        )

        issue_data = {
            "key": key,
            "summary": summary,
            "status": status_name,
            "column": column,
            "assignee": assignee,
            "type": type_name,
            "story_points": story_points,
            "is_completed": is_completed,
            "is_active": is_active,
            "added_during_sprint": key in added_during,
            "changelog": [],
            "comments": [],
            "merge_requests": [],
        }
        issues_data.append(issue_data)
        if is_active:
            active_issue_keys.append(key)

    print("Active (in-progress/review) issues: %d" % len(active_issue_keys), file=sys.stderr)

    # Step 2: Fetch changelogs and comments for active issues (parallel)
    print("Fetching changelogs and comments for active issues...", file=sys.stderr)
    issue_map = {i["key"]: i for i in issues_data}

    def fetch_issue_details(key):
        changelog = jira_get_changelog(base_url, auth, key)
        comments = jira_get_comments(base_url, auth, key)
        return key, changelog, comments

    with ThreadPoolExecutor(max_workers=10) as pool:
        future_to_key = {pool.submit(fetch_issue_details, key): key for key in active_issue_keys}
        for future in as_completed(future_to_key):
            try:
                key, changelog, comments = future.result()
                if key in issue_map:
                    issue_map[key]["changelog"] = changelog
                    issue_map[key]["comments"] = comments
            except Exception as e:
                print("WARNING: Failed to fetch details for %s: %s" % (future_to_key[future], e), file=sys.stderr)

    # Step 3: Fetch GitLab MRs for active issues (parallel)
    print("Searching GitLab for MRs linked to active issues...", file=sys.stderr)

    def fetch_mrs_for_key(key):
        mrs = search_mrs_for_issue(gitlab_url, gitlab_token, gitlab_project_id, key)
        mr_data_list = []
        for mr in mrs:
            iid = mr["iid"]
            notes = get_mr_notes(gitlab_url, gitlab_token, gitlab_project_id, iid)
            mr_data_list.append({
                "iid": iid,
                "title": mr.get("title", ""),
                "author": mr.get("author", {}).get("username", ""),
                "author_name": mr.get("author", {}).get("name", ""),
                "state": mr.get("state", ""),
                "created_at": mr.get("created_at", ""),
                "updated_at": mr.get("updated_at", ""),
                "web_url": mr.get("web_url", ""),
                "source_branch": mr.get("source_branch", ""),
                "notes": [n for n in notes if not n.get("system", False)],
            })
        return key, mr_data_list

    with ThreadPoolExecutor(max_workers=10) as pool:
        future_to_key = {pool.submit(fetch_mrs_for_key, key): key for key in active_issue_keys}
        for future in as_completed(future_to_key):
            try:
                key, mr_data_list = future.result()
                if key in issue_map:
                    issue_map[key]["merge_requests"] = mr_data_list
            except Exception as e:
                print("WARNING: Failed to fetch MRs for %s: %s" % (future_to_key[future], e), file=sys.stderr)

    mr_count = sum(len(i["merge_requests"]) for i in issues_data)
    print("Found %d MRs linked to active issues" % mr_count, file=sys.stderr)

    # Step 4: Fetch support tickets if configured
    # Requires both a support project AND team filtering (label or Team field).
    # Without team filtering, the query would return ALL tickets in the project.
    support_data = []
    has_team_filter = bool(args.support_label or args.support_team_field)
    if args.support_project_key and has_team_filter:
        print("Fetching support tickets...", file=sys.stderr)

        jql_parts = ["project = %s" % args.support_project_key]

        # Build team filter: labels OR Team custom field (cf[10600])
        team_clauses = []
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
            # The Team field (atlassian-team type) requires UUIDs in JQL, not display names.
            # Resolve each name to its UUID by looking up a known ticket with that team label.
            resolved_ids = []
            for name in values:
                try:
                    lookup_jql = 'project = %s AND labels = "team-%s" AND cf[10600] is not EMPTY ORDER BY created DESC' % (
                        args.support_project_key, name.lower())
                    lookup = jira_search_all(base_url, auth, lookup_jql, "customfield_10600")
                    for item in lookup[:5]:
                        team_obj = item.get("fields", {}).get("customfield_10600")
                        if isinstance(team_obj, dict) and team_obj.get("name", "").upper() == name.upper():
                            resolved_ids.append(team_obj["id"])
                            break
                    else:
                        print("WARNING: Could not resolve Team field UUID for '%s'" % name, file=sys.stderr)
                except Exception as e:
                    print("WARNING: Team field UUID lookup failed for '%s': %s" % (name, e), file=sys.stderr)
            if len(resolved_ids) == 1:
                team_clauses.append('cf[10600] = "%s"' % resolved_ids[0])
            elif len(resolved_ids) > 1:
                id_list = ", ".join('"%s"' % tid for tid in resolved_ids)
                team_clauses.append("cf[10600] in (%s)" % id_list)
        if team_clauses:
            jql_parts.append("(%s)" % " OR ".join(team_clauses))

        jql = " AND ".join(jql_parts) + " ORDER BY created DESC"
        fields = "summary,status,priority,assignee,created,updated,labels"

        try:
            tickets = jira_search_all(base_url, auth, jql, fields)
            for t in tickets:
                f = t.get("fields", {})
                support_data.append({
                    "key": t.get("key", ""),
                    "summary": f.get("summary", ""),
                    "status": f.get("status", {}).get("name", ""),
                    "status_id": str(f.get("status", {}).get("id", "")),
                    "priority": f.get("priority", {}).get("name", ""),
                    "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                    "created": f.get("created", ""),
                    "updated": f.get("updated", ""),
                    "labels": f.get("labels", []),
                })
            print("Found %d support tickets for team" % len(support_data), file=sys.stderr)

            # Fetch comments only for tickets worth checking for outstanding questions.
            # Exclude: closed, awaiting customer, and tickets older than 30 days.
            skip_status_ids = set()
            if args.support_board_config_json:
                try:
                    with open(args.support_board_config_json, "r") as f:
                        sbc = json.load(f)
                    for col in sbc:
                        name = col.get("name", "").lower().strip()
                        if name in ("closed", "done", "resolved", "completed") or "awaiting" in name or "waiting" in name:
                            skip_status_ids.update(col.get("statuses", []))
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            open_tickets = []
            for t in support_data:
                if t["status_id"] in skip_status_ids:
                    continue
                created_str = t.get("created", "")
                if created_str:
                    clean = re.sub(r"\.\d+", "", str(created_str)).replace("Z", "+00:00")
                    m = re.match(r"(.*[+-])(\d{2})(\d{2})$", clean)
                    if m:
                        clean = "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
                    try:
                        created_dt = datetime.fromisoformat(clean)
                        if created_dt < cutoff:
                            continue
                    except Exception:
                        pass
                open_tickets.append(t)
            if open_tickets:
                print("Fetching comments for %d recent active support tickets (of %d total)..." % (
                    len(open_tickets), len(support_data)), file=sys.stderr)

                def fetch_support_comments(key):
                    return key, jira_get_comments(base_url, auth, key)

                support_map = {t["key"]: t for t in support_data}
                with ThreadPoolExecutor(max_workers=10) as pool:
                    future_to_key = {pool.submit(fetch_support_comments, t["key"]): t["key"] for t in open_tickets}
                    for future in as_completed(future_to_key):
                        try:
                            key, comments = future.result()
                            if key in support_map:
                                support_map[key]["comments"] = comments
                        except Exception as e:
                            print("WARNING: Failed to fetch comments for %s: %s" % (future_to_key[future], e), file=sys.stderr)

        except Exception as e:
            print("WARNING: Could not fetch support tickets: %s" % e, file=sys.stderr)
    elif args.support_project_key and not has_team_filter:
        print("Skipping support tickets: no team filter configured (SUPPORT_TEAM_LABEL / SUPPORT_TEAM_FIELD_VALUES)", file=sys.stderr)

    # Step 4.5: Fetch DORA snapshot
    # Collect known MR authors from Step 3 to avoid re-searching active issue keys
    known_authors = set()
    for i in issues_data:
        for mr in i.get("merge_requests", []):
            author = mr.get("author", "")
            if author:
                known_authors.add(author)
    all_issue_keys = [i["key"] for i in issues_data]
    dora = fetch_dora_snapshot(gitlab_url, gitlab_token, gitlab_project_id,
                               all_issue_keys, args.start_date, args.end_date,
                               known_authors=known_authors)

    # Step 5: Save all data
    output = {
        "sprint": {
            "id": args.sprint_id,
            "name": args.sprint_name,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "board_id": args.board_id,
            "project_key": args.project_key,
        },
        "summary": {
            "total_issues": len(all_issues),
            "completed": len(completed_issues),
            "not_completed": len(not_completed),
            "active": len(active_issue_keys),
            "added_during_sprint": len(added_during),
            "support_tickets": len(support_data),
            "merge_requests": mr_count,
        },
        "issues": issues_data,
        "support_tickets": support_data,
        "dora": dora,
    }

    with open("/tmp/sprint_pulse_data.json.tmp", "w") as f:
        json.dump(output, f, indent=2, default=str)
    os.replace("/tmp/sprint_pulse_data.json.tmp", "/tmp/sprint_pulse_data.json")

    print("\nData saved to /tmp/sprint_pulse_data.json", file=sys.stderr)
    print("FETCH_COMPLETE|issues:%d|active:%d|mrs:%d|support:%d|dora_deploys:%d" % (
        len(all_issues), len(active_issue_keys), mr_count, len(support_data),
        dora.get("deploy_count", 0)
    ))


if __name__ == "__main__":
    main()
