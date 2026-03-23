#!/usr/bin/env python3
"""Sprint metrics: fetches GitLab MR data for sprint issues, calculates engineering metrics."""

import json
import os
import re
import sys
import urllib.parse
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from collections import defaultdict

from jira_client import load_env, init_auth, jira_get, gitlab_get


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--summary-file", help="Path to existing sprint summary .md file (skips Jira sprint report)")
    p.add_argument("--sprint-id", required=False)
    p.add_argument("--sprint-name", required=False)
    p.add_argument("--start-date", required=False)
    p.add_argument("--end-date", required=False)
    p.add_argument("--board-id", required=False)
    p.add_argument("--team-vault-dir", required=False)
    p.add_argument("--team-project-key", required=False)
    p.add_argument("--team-display-name", required=False)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not args.summary_file and not all([args.sprint_id, args.sprint_name, args.start_date, args.end_date, args.board_id, args.team_vault_dir, args.team_project_key, args.team_display_name]):
        p.error("Either --summary-file or all sprint/team arguments are required")
    return args


def parse_summary_file(path):
    """Parse sprint summary markdown to extract frontmatter and issue keys."""
    with open(path, "r") as f:
        content = f.read()

    # Parse YAML frontmatter
    frontmatter = {}
    if content.startswith("---"):
        end = content.index("---", 3)
        for line in content[3:end].strip().splitlines():
            if ": " in line:
                key, val = line.split(": ", 1)
                frontmatter[key.strip()] = val.strip().strip('"')

    # Extract issue keys from markdown links like [COPS-123](...)
    issue_keys = []
    seen = set()
    for match in re.finditer(r'\[([A-Z]+-\d+)\]\(', content):
        key = match.group(1)
        if key not in seen:
            issue_keys.append(key)
            seen.add(key)

    return frontmatter, issue_keys


def parse_dt(dt_str):
    if not dt_str:
        return None
    # Handle various ISO formats
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def fmt_duration(seconds):
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


def main():
    args = parse_args()
    env = load_env([
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "OBSIDIAN_TEAMS_PATH",
        "GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID",
    ])

    base_url, auth = init_auth(env)
    teams_path = env["OBSIDIAN_TEAMS_PATH"]
    gitlab_url = env["GITLAB_URL"]
    gitlab_token = env["GITLAB_TOKEN"]
    gitlab_project_id = env["GITLAB_PROJECT_ID"]

    # Step 1: Get sprint metadata and issue keys
    if args.summary_file:
        print("Reading sprint summary from: %s" % args.summary_file, file=sys.stderr)
        frontmatter, issue_keys = parse_summary_file(args.summary_file)
        sprint_id = args.sprint_id or frontmatter.get("sprint_id", "")
        sprint_name = args.sprint_name or frontmatter.get("sprint_name", "")
        start_date = (args.start_date or frontmatter.get("start_date", ""))[:10]
        end_date = (args.end_date or frontmatter.get("end_date", ""))[:10]
        board_id = args.board_id or ""
        vault_dir = args.team_vault_dir or frontmatter.get("team", "")
        project_key = args.team_project_key or frontmatter.get("project_key", "")
        # Resolve display name from SPRINT_TEAMS env var
        display_name = args.team_display_name or ""
        if not display_name:
            for t in env.get("SPRINT_TEAMS", "").split(","):
                parts = t.strip().split("|")
                if len(parts) == 4 and parts[0] == vault_dir:
                    display_name = parts[3]
                    break
            if not display_name:
                display_name = vault_dir
    else:
        sprint_id = args.sprint_id
        sprint_name = args.sprint_name
        start_date = args.start_date[:10]
        end_date = args.end_date[:10]
        board_id = args.board_id
        vault_dir = args.team_vault_dir
        project_key = args.team_project_key
        display_name = args.team_display_name

        print("Fetching sprint report for issue keys...", file=sys.stderr)
        report = jira_get(
            base_url,
            "/rest/greenhopper/1.0/rapid/charts/sprintreport?rapidViewId=%s&sprintId=%s" % (board_id, sprint_id),
            auth,
        )
        contents = report["contents"]
        completed_issues = contents.get("completedIssues", [])
        not_completed_issues = contents.get("issuesNotCompletedInCurrentSprint", [])
        all_parent_issues = completed_issues + not_completed_issues
        issue_keys = [i["key"] for i in all_parent_issues]

    print("Found %d sprint issues: %s" % (len(issue_keys), ", ".join(issue_keys[:10])), file=sys.stderr)
    if len(issue_keys) > 10:
        print("  ... and %d more" % (len(issue_keys) - 10), file=sys.stderr)

    # Step 2: Search GitLab for MRs linked to these issues (parallel)
    print("Searching GitLab for linked MRs...", file=sys.stderr)
    mr_map = {}  # iid -> MR data
    issue_mr_links = defaultdict(list)  # issue_key -> [mr_iid, ...]

    def search_mrs_for_key(key):
        """Search GitLab for MRs matching a single issue key."""
        matches = []
        try:
            search_path = "/projects/%s/merge_requests?search=%s&per_page=20" % (
                gitlab_project_id,
                urllib.parse.quote(key, safe=""),
            )
            mrs = gitlab_get(gitlab_url, search_path, gitlab_token)
            for mr in mrs:
                title = mr.get("title") or ""
                desc = mr.get("description") or ""
                branch = mr.get("source_branch") or ""
                combined = "%s %s %s" % (title, desc, branch)
                pattern = r'(?i)\b' + re.escape(key) + r'(?!\d)'
                if re.search(pattern, combined):
                    matches.append(mr)
        except Exception as e:
            print("  Warning: GitLab search failed for %s: %s" % (key, e), file=sys.stderr)
        return key, matches

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(search_mrs_for_key, key) for key in issue_keys]
        for future in as_completed(futures):
            key, matches = future.result()
            for mr in matches:
                iid = mr["iid"]
                mr_map[iid] = mr
                issue_mr_links[key].append(iid)

    unique_mrs = list(mr_map.values())
    print("Found %d unique MRs linked to sprint issues" % len(unique_mrs), file=sys.stderr)

    if not unique_mrs:
        print("No MRs found — generating empty metrics file.", file=sys.stderr)

    # Step 3: For each MR, fetch commits, approvals, and notes (parallel)
    print("Fetching MR details (%d MRs)..." % len(unique_mrs), file=sys.stderr)

    def fetch_mr_metrics(mr):
        """Fetch commits, approvals, and notes for a single MR. Returns enriched mr_data dict."""
        iid = mr["iid"]
        mr_data = {
            "iid": iid,
            "title": mr.get("title", ""),
            "author": mr.get("author", {}).get("username", "unknown"),
            "state": mr.get("state", ""),
            "created_at": mr.get("created_at"),
            "merged_at": mr.get("merged_at"),
            "source_branch": mr.get("source_branch", ""),
            "web_url": mr.get("web_url", ""),
            "linked_issues": [],
            "time_to_merge_s": None,
            "review_turnaround_s": None,
            "time_to_approval_s": None,
            "cycle_time_s": None,
        }

        # Which issues link to this MR
        for key, iids in issue_mr_links.items():
            if iid in iids:
                mr_data["linked_issues"].append(key)

        created_dt = parse_dt(mr.get("created_at"))
        merged_dt = parse_dt(mr.get("merged_at"))

        # Time to merge
        if created_dt and merged_dt:
            mr_data["time_to_merge_s"] = (merged_dt - created_dt).total_seconds()

        # Fetch commits, approvals, and notes in parallel
        commits_data = []
        approvals_data = {}
        notes_data = []

        def fetch_commits():
            if not merged_dt:
                return
            try:
                return gitlab_get(
                    gitlab_url,
                    "/projects/%s/merge_requests/%s/commits?per_page=100" % (gitlab_project_id, iid),
                    gitlab_token,
                )
            except Exception as e:
                print("  Warning: Could not fetch commits for MR !%s: %s" % (iid, e), file=sys.stderr)
                return []

        def fetch_approvals():
            try:
                return gitlab_get(
                    gitlab_url,
                    "/projects/%s/merge_requests/%s/approvals" % (gitlab_project_id, iid),
                    gitlab_token,
                )
            except Exception as e:
                print("  Warning: Could not fetch approvals for MR !%s: %s" % (iid, e), file=sys.stderr)
                return {}

        def fetch_notes():
            try:
                return gitlab_get(
                    gitlab_url,
                    "/projects/%s/merge_requests/%s/notes?sort=asc&per_page=100" % (gitlab_project_id, iid),
                    gitlab_token,
                )
            except Exception as e:
                print("  Warning: Could not fetch notes for MR !%s: %s" % (iid, e), file=sys.stderr)
                return []

        with ThreadPoolExecutor(max_workers=3) as inner_pool:
            f_commits = inner_pool.submit(fetch_commits)
            f_approvals = inner_pool.submit(fetch_approvals)
            f_notes = inner_pool.submit(fetch_notes)
            commits_data = f_commits.result() or []
            approvals_data = f_approvals.result() or {}
            notes_data = f_notes.result() or []

        # Cycle time from commits
        if merged_dt and commits_data:
            commit_dates = []
            for c in commits_data:
                cdt = parse_dt(c.get("created_at") or c.get("committed_date") or c.get("authored_date"))
                if cdt:
                    commit_dates.append(cdt)
            if commit_dates:
                first_commit = min(commit_dates)
                mr_data["cycle_time_s"] = (merged_dt - first_commit).total_seconds()

        # Approval dates
        approval_dates = []
        for approver in approvals_data.get("approved_by", []):
            approval_dt = parse_dt(approver.get("approved_at"))
            if approval_dt:
                approval_dates.append(approval_dt)
        approval_dates.sort()

        # First non-author comment
        first_comment_dt = None
        author_username = mr_data["author"]
        for note in notes_data:
            if note.get("system", False):
                continue
            note_author = note.get("author", {}).get("username", "")
            if note_author == author_username:
                continue
            note_dt = parse_dt(note.get("created_at"))
            if note_dt:
                first_comment_dt = note_dt
                break

        # Review turnaround = earliest of first approval or first comment
        if created_dt:
            review_candidates = []
            if approval_dates:
                review_candidates.append(approval_dates[0])
            if first_comment_dt:
                review_candidates.append(first_comment_dt)
            if review_candidates:
                mr_data["review_turnaround_s"] = (min(review_candidates) - created_dt).total_seconds()

            # Time to approval = 1st approval
            if approval_dates:
                mr_data["time_to_approval_s"] = (approval_dates[0] - created_dt).total_seconds()

        return mr_data

    mr_metrics = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(fetch_mr_metrics, mr) for mr in unique_mrs]
        for future in as_completed(futures):
            mr_metrics.append(future.result())

    # Step 4: Exclude stale MRs (open + created before sprint start) from metrics
    sprint_start_dt = parse_dt(start_date + "T00:00:00Z")
    excluded_mrs = []
    included_mrs = []
    for m in mr_metrics:
        created_dt = parse_dt(m.get("created_at"))
        if m["state"] == "opened" and created_dt and sprint_start_dt and created_dt < sprint_start_dt:
            m["excluded_reason"] = "Open MR created before sprint start (%s)" % m["created_at"][:10]
            excluded_mrs.append(m)
        else:
            included_mrs.append(m)

    if excluded_mrs:
        print("Excluded %d stale MR(s) from metrics" % len(excluded_mrs), file=sys.stderr)

    # Calculate aggregate metrics (using included MRs only)
    merged_mrs = [m for m in included_mrs if m["state"] == "merged"]
    open_mrs = [m for m in included_mrs if m["state"] == "opened"]

    ttm_values = [m["time_to_merge_s"] for m in merged_mrs if m["time_to_merge_s"] is not None]
    review_values = [m["review_turnaround_s"] for m in included_mrs if m["review_turnaround_s"] is not None]
    approval_values = [m["time_to_approval_s"] for m in included_mrs if m["time_to_approval_s"] is not None]
    cycle_values = [m["cycle_time_s"] for m in merged_mrs if m["cycle_time_s"] is not None]

    avg_ttm = sum(ttm_values) / len(ttm_values) if ttm_values else None
    med_ttm = median(ttm_values)
    avg_review = sum(review_values) / len(review_values) if review_values else None
    med_review = median(review_values)
    avg_approval = sum(approval_values) / len(approval_values) if approval_values else None
    med_approval = median(approval_values)
    avg_cycle = sum(cycle_values) / len(cycle_values) if cycle_values else None
    med_cycle = median(cycle_values)

    issues_with_mrs = len(issue_mr_links)
    issues_without_mrs = len(issue_keys) - issues_with_mrs

    # Step 5: Generate markdown
    print("Generating markdown...", file=sys.stderr)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def format_date_display(date_str):
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return dt.strftime("%-d %b %Y")
        except Exception:
            return date_str[:10]

    start_display = format_date_display(start_date)
    end_display = format_date_display(end_date)

    lines = []
    lines.append("---")
    lines.append("type: sprint-metrics")
    lines.append("team: " + vault_dir)
    lines.append("project_key: " + project_key)
    lines.append('sprint_name: "%s"' % sprint_name)
    lines.append("sprint_id: " + sprint_id)
    lines.append("start_date: " + start_date)
    lines.append("end_date: " + end_date)
    lines.append("mrs_merged: %d" % len(merged_mrs))
    lines.append("mrs_open: %d" % len(open_mrs))
    lines.append("avg_time_to_merge_h: %s" % (round(avg_ttm / 3600, 1) if avg_ttm else "null"))
    lines.append("avg_review_turnaround_h: %s" % (round(avg_review / 3600, 1) if avg_review else "null"))
    lines.append("avg_time_to_approval_h: %s" % (round(avg_approval / 3600, 1) if avg_approval else "null"))
    lines.append("avg_cycle_time_h: %s" % (round(avg_cycle / 3600, 1) if avg_cycle else "null"))
    lines.append("generated: " + now_utc)
    lines.append("source: gitlab")
    lines.append("---")
    lines.append("")
    lines.append("# Sprint Metrics \u2014 %s" % sprint_name)
    lines.append("")
    lines.append("**%s** | **%s to %s**" % (display_name, start_display, end_display))
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append("| MRs Merged | %d |" % len(merged_mrs))
    lines.append("| MRs Open | %d |" % len(open_mrs))
    lines.append("| Total MRs | %d |" % len(included_mrs))
    if excluded_mrs:
        lines.append("| Excluded MRs | %d |" % len(excluded_mrs))
    lines.append("| Issues with MRs | %d / %d |" % (issues_with_mrs, len(issue_keys)))
    lines.append("")

    # Timing metrics
    lines.append("## Timing")
    lines.append("")
    lines.append("| Metric | Average | Median |")
    lines.append("|--------|---------|--------|")
    lines.append("| Time to Merge | %s | %s |" % (fmt_duration(avg_ttm), fmt_duration(med_ttm)))
    lines.append("| Review Turnaround | %s | %s |" % (fmt_duration(avg_review), fmt_duration(med_review)))
    lines.append("| Time to Approval | %s | %s |" % (fmt_duration(avg_approval), fmt_duration(med_approval)))
    lines.append("| Cycle Time | %s | %s |" % (fmt_duration(avg_cycle), fmt_duration(med_cycle)))
    lines.append("")
    lines.append("> **Time to Merge (TTM):** MR created to MR merged. The total elapsed time a merge request was open.")
    lines.append(">")
    lines.append("> **Review Turnaround:** MR created to first response from a non-author (comment or approval, whichever is earlier). Measures how quickly the team picks up reviews.")
    lines.append(">")
    lines.append("> **Time to Approval:** MR created to first approval. Measures how long until the MR is approved and ready to merge.")
    lines.append(">")
    lines.append("> **Cycle Time:** First commit on the MR branch to MR merged. Measures the time from when code work began to when it shipped.")
    lines.append("")

    # Per-author breakdown
    author_stats = defaultdict(lambda: {"merged": 0, "open": 0, "ttm": [], "review": [], "approval": [], "cycle": []})
    for m in included_mrs:
        a = m["author"]
        if m["state"] == "merged":
            author_stats[a]["merged"] += 1
        elif m["state"] == "opened":
            author_stats[a]["open"] += 1
        if m["time_to_merge_s"] is not None:
            author_stats[a]["ttm"].append(m["time_to_merge_s"])
        if m["review_turnaround_s"] is not None:
            author_stats[a]["review"].append(m["review_turnaround_s"])
        if m["time_to_approval_s"] is not None:
            author_stats[a]["approval"].append(m["time_to_approval_s"])
        if m["cycle_time_s"] is not None:
            author_stats[a]["cycle"].append(m["cycle_time_s"])
    if author_stats:
        lines.append("## By Author")
        lines.append("")
        lines.append("| Author | Merged | Open | Avg TTM | Avg Review | Avg Approval | Avg Cycle |")
        lines.append("|--------|--------|------|---------|------------|--------------|-----------|")
        for author in sorted(author_stats.keys()):
            s = author_stats[author]
            a_ttm = fmt_duration(sum(s["ttm"]) / len(s["ttm"])) if s["ttm"] else "-"
            a_rev = fmt_duration(sum(s["review"]) / len(s["review"])) if s["review"] else "-"
            a_apr = fmt_duration(sum(s["approval"]) / len(s["approval"])) if s["approval"] else "-"
            a_cyc = fmt_duration(sum(s["cycle"]) / len(s["cycle"])) if s["cycle"] else "-"
            lines.append("| %s | %d | %d | %s | %s | %s | %s |" % (author, s["merged"], s["open"], a_ttm, a_rev, a_apr, a_cyc))
        lines.append("")

    # MR detail table
    lines.append("## Merge Requests")
    lines.append("")
    lines.append("| MR | Issue(s) | Author | State | TTM | Review | Approval | Cycle |")
    lines.append("|----|----------|--------|-------|-----|--------|----------|-------|")

    # Sort: merged first (by time to merge desc), then open
    sorted_mrs = sorted(included_mrs, key=lambda m: (
        0 if m["state"] == "merged" else 1,
        -(m["time_to_merge_s"] or 0),
    ))

    for m in sorted_mrs:
        mr_link = "[!%d](%s)" % (m["iid"], m["web_url"]) if m["web_url"] else "!%d" % m["iid"]
        issues_str = ", ".join(m["linked_issues"]) if m["linked_issues"] else "-"
        state_icon = "Merged" if m["state"] == "merged" else "Open"
        ttm = fmt_duration(m["time_to_merge_s"])
        rev = fmt_duration(m["review_turnaround_s"])
        apr = fmt_duration(m["time_to_approval_s"])
        cyc = fmt_duration(m["cycle_time_s"])
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            mr_link, issues_str, m["author"], state_icon, ttm, rev, apr, cyc,
        ))

    lines.append("")

    # Excluded MRs section
    if excluded_mrs:
        lines.append("## Excluded from Metrics")
        lines.append("")
        lines.append("The following MRs were found linked to sprint issues but excluded from metric calculations.")
        lines.append("")
        lines.append("| MR | Issue(s) | Author | Reason |")
        lines.append("|----|----------|--------|--------|")
        for m in excluded_mrs:
            mr_link = "[!%d](%s)" % (m["iid"], m["web_url"]) if m["web_url"] else "!%d" % m["iid"]
            issues_str = ", ".join(m["linked_issues"]) if m["linked_issues"] else "-"
            lines.append("| %s | %s | %s | %s |" % (mr_link, issues_str, m["author"], m["excluded_reason"]))
        lines.append("")

    md = "\n".join(lines)

    # Write or dry-run
    output_dir = os.path.join(teams_path, vault_dir, "Sprints")
    filename = "%s - %s - Metrics.md" % (sprint_name, end_date)
    file_path = os.path.join(output_dir, filename)

    if args.dry_run:
        print("\n**DRY RUN** -- would write to: %s\n" % file_path)
        print(md)
    else:
        os.makedirs(output_dir, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(md)
        print("\nSprint metrics written to: " + file_path)

    print("%d MRs | TTM: %s | Review: %s | Cycle: %s" % (
        len(merged_mrs), fmt_duration(avg_ttm), fmt_duration(avg_review), fmt_duration(avg_cycle),
    ))


if __name__ == "__main__":
    main()
