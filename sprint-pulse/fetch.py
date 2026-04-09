#!/usr/bin/env python3
"""Sprint pulse data fetcher: gathers sprint issues, changelogs, comments, MRs, and support tickets."""

import json
import re
import sys
import argparse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from jira_client import load_env, init_auth, jira_get, jira_search_all, jira_get_changelog, jira_get_comments
from gitlab_client import load_gitlab_env, gitlab_get, search_mrs_for_issue, get_mr_notes


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


def main():
    args = parse_args()

    # Init Jira
    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    base_url, auth = init_auth(env)

    # Init GitLab
    gitlab_url, gitlab_token, gitlab_project_id = load_gitlab_env()

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
            if len(labels) == 1:
                team_clauses.append('labels = "%s"' % labels[0])
            elif len(labels) > 1:
                label_list = ", ".join('"%s"' % l for l in labels)
                team_clauses.append("labels in (%s)" % label_list)
        if args.support_team_field:
            values = [v.strip() for v in args.support_team_field.split(",") if v.strip()]
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
    }

    with open("/tmp/sprint_pulse_data.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    print("\nData saved to /tmp/sprint_pulse_data.json", file=sys.stderr)
    print("FETCH_COMPLETE|issues:%d|active:%d|mrs:%d|support:%d" % (
        len(all_issues), len(active_issue_keys), mr_count, len(support_data)
    ))


if __name__ == "__main__":
    main()
