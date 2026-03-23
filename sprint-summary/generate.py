#!/usr/bin/env python3
"""Sprint generate: fetches sprint data, calculates metrics, writes markdown."""

import json
import os
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections import defaultdict

from jira_client import load_env, init_auth, jira_get


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--sprint-id", required=True)
    p.add_argument("--sprint-name", required=True)
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--goal", default="")
    p.add_argument("--board-id", required=True)
    p.add_argument("--team-vault-dir", required=True)
    p.add_argument("--team-project-key", required=True)
    p.add_argument("--team-display-name", required=True)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def safe_pts(estimate_sum):
    if isinstance(estimate_sum, dict):
        val = estimate_sum.get("value")
        if val is not None:
            return float(val)
    return 0.0


def fmt_pts(val):
    if val == int(val):
        return int(val)
    return val


def format_date_display(date_str):
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%-d %b %Y")
    except Exception:
        return date_str[:10]


def main():
    args = parse_args()
    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "OBSIDIAN_TEAMS_PATH"])

    base_url, auth = init_auth(env)
    teams_path = env["OBSIDIAN_TEAMS_PATH"]

    sprint_id = args.sprint_id
    sprint_name = args.sprint_name
    start_date = args.start_date[:10]
    end_date = args.end_date[:10]
    goal = args.goal
    board_id = args.board_id
    vault_dir = args.team_vault_dir
    project_key = args.team_project_key
    display_name = args.team_display_name

    # Fetch Sprint Report
    print("Fetching sprint report...", file=sys.stderr)
    report = jira_get(
        base_url,
        "/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s" % (board_id, sprint_id),
        auth,
    )
    contents = report["contents"]

    completed_issues = contents.get("completedIssues", [])
    not_completed_issues = contents.get("issuesNotCompletedInCurrentSprint", [])
    punted_issues = contents.get("puntedIssues", [])
    added_keys = contents.get("issueKeysAddedDuringSprint", {})

    completed_keys = set(i["key"] for i in completed_issues)
    all_parent_issues = completed_issues + not_completed_issues

    points_completed = safe_pts(contents.get("completedIssuesEstimateSum", {}))
    points_not_completed = safe_pts(contents.get("issuesNotCompletedEstimateSum", {}))
    points_committed = points_completed + points_not_completed
    completion_rate = round(points_completed / points_committed * 100) if points_committed > 0 else 0

    added_count = len(added_keys)
    punted_count = len(punted_issues)
    punted_pts = safe_pts(contents.get("puntedIssuesEstimateSum", {}))

    # Issue counts by type
    by_type = defaultdict(lambda: {"completed": 0, "total": 0, "points": 0.0})
    for issue in completed_issues:
        tn = issue.get("typeName", "Unknown")
        by_type[tn]["completed"] += 1
        by_type[tn]["total"] += 1
        est = issue.get("currentEstimateStatistic", {}).get("statFieldValue", {})
        if est and est.get("value") is not None:
            by_type[tn]["points"] += float(est["value"])
    for issue in not_completed_issues:
        tn = issue.get("typeName", "Unknown")
        by_type[tn]["total"] += 1
        est = issue.get("currentEstimateStatistic", {}).get("statFieldValue", {})
        if est and est.get("value") is not None:
            by_type[tn]["points"] += float(est["value"])

    # Fetch subtasks and ECS tickets in parallel
    print("Fetching subtasks and ECS tickets...", file=sys.stderr)

    def fetch_subtasks():
        """Fetch subtasks with parallel pagination after first page."""
        jql = "sprint=%s AND issuetype in subtaskIssueTypes() ORDER BY parent,key" % sprint_id
        encoded_jql = urllib.request.quote(jql, safe="")
        fields = "summary,status,issuetype,assignee,priority,parent"

        # First page to get total count
        path = "/rest/api/3/search/jql?jql=%s&fields=%s&maxResults=50&startAt=0" % (encoded_jql, fields)
        data = jira_get(base_url, path, auth)
        results = data.get("issues", [])
        total = data.get("total", 0)

        if total > 50:
            # Fire off remaining pages in parallel
            offsets = list(range(50, total, 50))

            def fetch_page(offset):
                p = "/rest/api/3/search/jql?jql=%s&fields=%s&maxResults=50&startAt=%d" % (encoded_jql, fields, offset)
                return jira_get(base_url, p, auth).get("issues", [])

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(fetch_page, o) for o in offsets]
                for future in as_completed(futures):
                    results.extend(future.result())

        return results

    def fetch_ecs():
        """Fetch ECS support tickets."""
        try:
            jql = "sprint=%s AND project=ECS ORDER BY priority DESC,status" % sprint_id
            path = "/rest/api/3/search/jql?jql=%s&fields=summary,status,issuetype,assignee,priority&maxResults=50" % (
                urllib.request.quote(jql, safe=""),
            )
            return jira_get(base_url, path, auth).get("issues", [])
        except Exception as e:
            print("Note: Could not fetch ECS tickets: %s" % e, file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_subtasks = pool.submit(fetch_subtasks)
        f_ecs = pool.submit(fetch_ecs)
        subtasks = f_subtasks.result()
        ecs_issues = f_ecs.result()

    subtask_map = defaultdict(list)
    for st in subtasks:
        parent_key = st.get("fields", {}).get("parent", {}).get("key", "")
        if parent_key:
            subtask_map[parent_key].append(st)

    # Build markdown
    print("Generating markdown...", file=sys.stderr)

    start_display = format_date_display(start_date)
    end_display = format_date_display(end_date)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed_count = len(completed_issues)
    total_count = len(all_parent_issues)

    lines = []
    lines.append("---")
    lines.append("type: sprint-summary")
    lines.append("team: " + vault_dir)
    lines.append("project_key: " + project_key)
    lines.append('sprint_name: "%s"' % sprint_name)
    lines.append("sprint_id: " + sprint_id)
    lines.append("start_date: " + start_date)
    lines.append("end_date: " + end_date)
    lines.append("points_committed: %s" % fmt_pts(points_committed))
    lines.append("points_completed: %s" % fmt_pts(points_completed))
    lines.append("completion_rate: %s" % completion_rate)
    lines.append("scope_added: %s" % added_count)
    lines.append("scope_removed_pts: %s" % fmt_pts(punted_pts))
    lines.append("generated: " + now_utc)
    lines.append("source: jira")
    lines.append("---")
    lines.append("")
    lines.append("# %s \u2014 %s" % (sprint_name, display_name))
    lines.append("")
    lines.append("**%s to %s**" % (start_display, end_display))
    lines.append("")
    lines.append("## Sprint Goals")
    lines.append("")
    lines.append(goal if goal else "<!-- Add sprint goals here -->")
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append("| Points Committed | %s |" % fmt_pts(points_committed))
    lines.append("| Points Completed | %s |" % fmt_pts(points_completed))
    lines.append("| Completion Rate | %s%% |" % completion_rate)
    lines.append("| Issues Completed | %d / %d |" % (completed_count, total_count))

    if added_count > 0:
        lines.append("| Scope Added | %d issues |" % added_count)
    if punted_count > 0:
        lines.append("| Scope Removed | %d issues (%s pts) |" % (punted_count, fmt_pts(punted_pts)))

    lines.append("")
    lines.append("### By Issue Type")
    lines.append("")
    lines.append("| Type | Completed | Total | Points |")
    lines.append("|------|-----------|-------|--------|")
    for tn in sorted(by_type.keys()):
        d = by_type[tn]
        lines.append("| %s | %d | %d | %s |" % (tn, d["completed"], d["total"], fmt_pts(d["points"])))

    # Sprint Work
    lines.append("")
    lines.append("## Sprint Work")
    lines.append("")
    lines.append("| Key | Type | Summary | Status | Assignee | Points | Added |")
    lines.append("|-----|------|---------|--------|----------|--------|-------|")

    def get_issue_pts(issue):
        est = issue.get("currentEstimateStatistic", {}).get("statFieldValue", {})
        if est and est.get("value") is not None:
            v = float(est["value"])
            return int(v) if v == int(v) else v
        return "-"

    def sort_key(issue):
        is_completed = 0 if issue["key"] in completed_keys else 1
        pts = 0
        est = issue.get("currentEstimateStatistic", {}).get("statFieldValue", {})
        if est and est.get("value") is not None:
            pts = float(est["value"])
        return (is_completed, -pts, issue["key"])

    sorted_parents = sorted(all_parent_issues, key=sort_key)

    for issue in sorted_parents:
        key = issue["key"]
        itype = issue.get("typeName", "")
        summary = issue.get("summary", "")
        status = issue.get("statusName", "")
        assignee = issue.get("assigneeName", "Unassigned") or "Unassigned"
        pts = get_issue_pts(issue)
        added = "Yes" if key in added_keys else ""
        lines.append("| [%s](%s/browse/%s) | %s | %s | %s | %s | %s | %s |" % (key, base_url, key, itype, summary, status, assignee, pts, added))

        if key in subtask_map:
            status_order = {"done": 0, "in review": 1, "in progress": 2, "to do": 3}
            subs = sorted(
                subtask_map[key],
                key=lambda s: (
                    status_order.get(s.get("fields", {}).get("status", {}).get("name", "").lower(), 4),
                    s["key"],
                ),
            )
            for st in subs:
                st_key = st["key"]
                st_fields = st.get("fields", {})
                st_type = st_fields.get("issuetype", {}).get("name", "")
                st_summary = st_fields.get("summary", "")
                st_status = st_fields.get("status", {}).get("name", "")
                st_assignee = "Unassigned"
                if st_fields.get("assignee"):
                    st_assignee = st_fields["assignee"].get("displayName", "Unassigned")
                lines.append(
                    "| [%s](%s/browse/%s) | %s | \u21b3 %s | %s | %s | - | |"
                    % (st_key, base_url, st_key, st_type, st_summary, st_status, st_assignee)
                )

    # Support
    if ecs_issues:
        lines.append("")
        lines.append("## Support")
        lines.append("")
        lines.append("| Key | Type | Summary | Status | Priority | Assignee |")
        lines.append("|-----|------|---------|--------|----------|----------|")
        for issue in ecs_issues:
            f = issue.get("fields", {})
            key = issue["key"]
            itype = f.get("issuetype", {}).get("name", "")
            summary = f.get("summary", "")
            status = f.get("status", {}).get("name", "")
            priority = f.get("priority", {}).get("name", "")
            assignee = (
                f.get("assignee", {}).get("displayName", "Unassigned")
                if f.get("assignee")
                else "Unassigned"
            )
            lines.append(
                "| [%s](%s/browse/%s) | %s | %s | %s | %s | %s |"
                % (key, base_url, key, itype, summary, status, priority, assignee)
            )

    md = "\n".join(lines) + "\n"

    # Write or dry-run
    output_dir = os.path.join(teams_path, vault_dir, "Sprints")
    filename = "%s - %s.md" % (sprint_name, end_date)
    file_path = os.path.join(output_dir, filename)

    if args.dry_run:
        print("\n**DRY RUN** -- would write to: %s\n" % file_path)
        print(md)
    else:
        os.makedirs(output_dir, exist_ok=True)
        tmp_file = file_path + ".tmp"
        with open(tmp_file, "w") as f:
            f.write(md)
        os.replace(tmp_file, file_path)
        print("\nSprint summary written to: " + file_path)

    print("%s (%s%%, %s/%s pts)" % (sprint_name, completion_rate, fmt_pts(points_completed), fmt_pts(points_committed)))


if __name__ == "__main__":
    main()
