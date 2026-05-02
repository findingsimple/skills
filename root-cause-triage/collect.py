#!/usr/bin/env python3
"""Fetch root cause issues from Jira and save per-issue JSON for summarization."""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import _libpath  # noqa: F401
from jira_client import load_env, init_auth, jira_get, jira_search_all, adf_to_text, ensure_tmp_dir


ENV_KEYS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "TRIAGE_BOARD_ID", "TRIAGE_PARENT_ISSUE_KEY", "TRIAGE_OUTPUT_PATH"]

FIELDS = "key,summary,status,resolution,description,issuetype,issuelinks,subtasks,created,reporter,labels,priority"

# Priority order for fetching full linked issue descriptions
LINK_TYPE_PRIORITY = [
    "duplicate",        # duplicates / is duplicated by
    "relates",          # relates to
    "cause",            # causes / is caused by
]

MAX_LINKED_DETAIL_FETCHES = 5

COLLECT_DIR = "/tmp/triage_collect"


def build_status_column_map(base_url, auth, board_id):
    """Build a mapping from Jira status ID to board column name."""
    config = jira_get(base_url, "/rest/agile/1.0/board/%d/configuration" % board_id, auth)
    columns = config.get("columnConfig", {}).get("columns", [])
    mapping = {}
    for col in columns:
        for s in col.get("statuses", []):
            mapping[s["id"]] = col["name"]
    return mapping


def parse_args():
    parser = argparse.ArgumentParser(description="Collect root cause issues from Jira")
    parser.add_argument("--issue", help="Collect a single issue by key (e.g., PDE-1234)")
    parser.add_argument("--status", help="Filter to a specific status (e.g., 'To Triage')")
    parser.add_argument("--include-done", action="store_true", help="Include Closed/Cancelled issues (excluded by default)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing files")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files (default: skip)")
    parser.add_argument("--index-only", action="store_true", help="Regenerate index from cached JSON (no Jira fetch)")
    return parser.parse_args()


def fetch_single_issue(base_url, auth, key):
    """Fetch a single issue by key."""
    data = jira_get(base_url, "/rest/api/3/issue/%s?fields=%s" % (key, FIELDS), auth)
    return [data]


# Jira statuses can contain slashes (e.g. "In Review/QA"), colons (e.g.
# "Blocked: External"), ampersands, dots, and apostrophes. Keep the charset
# strict enough to block quote/paren-based JQL escapes while permitting
# real-world status names.
_STATUS_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-/:&.']*\Z", re.ASCII)


def fetch_all_issues(base_url, auth, parent_key, status_filter=None, include_done=False):
    """Fetch all sibling issues under the parent epic.

    By default, mirrors the board's 'hide older items' behaviour: excludes
    issues in a Done status category that haven't been updated in the last 2 weeks.
    Pass include_done=True to fetch everything.

    `parent_key` must be pre-validated against the Jira key regex by the caller.
    `status_filter`, if provided, is validated here before interpolation.
    """
    jql = "parent = %s" % parent_key
    if status_filter:
        if not _STATUS_RE.match(status_filter):
            print("ERROR: --status %r contains unsupported characters (allowed: letters, digits, spaces, _ and -)" % status_filter, file=sys.stderr)
            sys.exit(2)
        jql += ' AND status = "%s"' % status_filter
    elif not include_done:
        jql += " AND NOT (statusCategory = Done AND NOT (updated >= -2w))"
    jql += " ORDER BY created ASC"
    return jira_search_all(base_url, auth, jql, FIELDS)


def fetch_subtask_descriptions(base_url, auth, subtasks):
    """Fetch descriptions for subtasks and return as list of dicts."""
    results = []
    for sub in subtasks:
        key = sub.get("key", "")
        if not key:
            continue
        try:
            data = jira_get(base_url, "/rest/api/3/issue/%s?fields=summary,description" % key, auth)
            fields = data.get("fields", {})
            raw_desc = fields.get("description")
            desc = adf_to_text(raw_desc) if isinstance(raw_desc, dict) else (raw_desc or "")
            results.append({
                "key": key,
                "summary": fields.get("summary", ""),
                "description": desc.strip(),
            })
        except Exception as e:
            print("WARNING: Failed to fetch subtask %s: %s" % (key, e), file=sys.stderr)
    return results


def classify_link_type(link):
    """Classify an issue link by type name, return (priority_index, type_label, linked_issue_stub)."""
    link_type_name = link.get("type", {}).get("name", "").lower()
    outward = link.get("type", {}).get("outward", "")
    inward = link.get("type", {}).get("inward", "")

    linked = link.get("outwardIssue") or link.get("inwardIssue")
    if not linked:
        return None

    # Determine relationship direction label
    if link.get("outwardIssue"):
        direction_label = outward
    else:
        direction_label = inward

    # Determine priority bucket
    priority = len(LINK_TYPE_PRIORITY)  # default: lowest priority
    for i, pattern in enumerate(LINK_TYPE_PRIORITY):
        if pattern in link_type_name:
            priority = i
            break

    stub = {
        "key": linked.get("key", ""),
        "summary": linked.get("fields", {}).get("summary", ""),
        "status": linked.get("fields", {}).get("status", {}).get("name", ""),
        "issue_type": linked.get("fields", {}).get("issuetype", {}).get("name", ""),
    }

    return priority, direction_label, stub


def process_issue_links(base_url, auth, issuelinks):
    """Process issue links: extract stubs, fetch full descriptions for top priority links."""
    classified = []
    for link in issuelinks:
        result = classify_link_type(link)
        if result:
            classified.append(result)

    # Sort by priority (duplicates first, then relates, then causes)
    classified.sort(key=lambda x: x[0])

    # Group by direction label
    grouped = {}
    for priority, direction_label, stub in classified:
        if direction_label not in grouped:
            grouped[direction_label] = []
        grouped[direction_label].append({"priority": priority, **stub})

    # Fetch full descriptions for top-priority links (up to MAX_LINKED_DETAIL_FETCHES)
    fetched = 0
    all_stubs = []
    for priority, direction_label, stub in classified:
        all_stubs.append(stub)

    for priority, direction_label, stub in classified:
        if fetched >= MAX_LINKED_DETAIL_FETCHES:
            break
        key = stub["key"]
        try:
            data = jira_get(base_url, "/rest/api/3/issue/%s?fields=summary,description" % key, auth)
            raw_desc = data.get("fields", {}).get("description")
            desc = adf_to_text(raw_desc) if isinstance(raw_desc, dict) else (raw_desc or "")
            # Find and update the stub in the grouped data
            for group_label, group_stubs in grouped.items():
                for s in group_stubs:
                    if s["key"] == key:
                        s["description"] = desc.strip()
                        break
            fetched += 1
        except Exception as e:
            print("WARNING: Failed to fetch linked issue %s: %s" % (key, e), file=sys.stderr)

    return grouped


def process_issue(base_url, auth, issue, status_column_map=None):
    """Process a single issue into a structured dict for JSON output."""
    fields = issue.get("fields", {})
    key = issue.get("key", "") or fields.get("key", "")
    summary = fields.get("summary", "")
    raw_desc = fields.get("description")
    description = adf_to_text(raw_desc) if isinstance(raw_desc, dict) else (raw_desc or "")

    status = fields.get("status", {}).get("name", "")
    status_id = fields.get("status", {}).get("id", "")
    board_column = (status_column_map or {}).get(status_id, "") or status
    resolution = fields.get("resolution", {}).get("name", "") if fields.get("resolution") else ""
    issue_type = fields.get("issuetype", {}).get("name", "")
    priority = fields.get("priority", {}).get("name", "") if fields.get("priority") else ""
    reporter = fields.get("reporter", {}).get("displayName", "") if fields.get("reporter") else ""
    created = (fields.get("created") or "")[:10]
    labels = fields.get("labels", [])

    # Fetch subtask descriptions
    subtasks = fields.get("subtasks", [])
    subtask_data = []
    if subtasks:
        subtask_data = fetch_subtask_descriptions(base_url, auth, subtasks)

    # Augment description with subtask content
    augmented_description = description.strip()
    if subtask_data:
        subtask_text = "\n\n".join(
            "[Subtask %s — %s]\n%s" % (s["key"], s["summary"], s["description"])
            for s in subtask_data if s["description"]
        )
        if subtask_text:
            augmented_description = (augmented_description + "\n\n" + subtask_text) if augmented_description else subtask_text

    # Process issue links
    issuelinks = fields.get("issuelinks", [])
    linked_issues = process_issue_links(base_url, auth, issuelinks) if issuelinks else {}

    total_links = sum(len(stubs) for stubs in linked_issues.values())

    return {
        "key": key,
        "summary": summary,
        "description": augmented_description,
        "status": status,
        "status_id": status_id,
        "board_column": board_column,
        "resolution": resolution,
        "issue_type": issue_type,
        "priority": priority,
        "reporter": reporter,
        "created": created,
        "labels": labels,
        "subtasks": subtask_data,
        "linked_issues": linked_issues,
        "linked_issue_count": total_links,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_issue_file_map(issues_dir):
    """Scan Issues directory to map Jira keys to actual filenames on disk."""
    file_map = {}
    if not os.path.isdir(issues_dir):
        return file_map
    for f in os.listdir(issues_dir):
        if not f.endswith(".md") or f.startswith("_"):
            continue
        key_match = re.match(r"^([A-Z]+-\d+)", f)
        if key_match:
            file_map[key_match.group(1)] = f[:-3]  # strip .md
    return file_map


def write_index(issues_data, output_path, parent_key):
    """Write/update the index file at {output_path}/Issues/_Index.md."""
    issues_dir = os.path.join(output_path, "Issues")
    os.makedirs(issues_dir, exist_ok=True)
    index_path = os.path.join(issues_dir, "_Index.md")

    # Map keys to actual filenames for wiki links
    file_map = _build_issue_file_map(issues_dir)

    now = datetime.now(timezone.utc).isoformat()
    lines = [
        "---",
        "type: root-cause-index",
        "parent_epic: %s" % parent_key,
        "generated_at: %s" % now,
        "total_issues: %d" % len(issues_data),
        "---",
        "",
        "# Root Cause Issues — %s" % parent_key,
        "",
        "| Key | Summary | Board Column | Status | Links | Collected |",
        "|-----|---------|--------------|--------|-------|-----------|",
    ]

    for issue in issues_data:
        key = issue["key"]
        filename = file_map.get(key)
        if filename:
            key_cell = "[[%s\\|%s]]" % (filename, key)
        else:
            key_cell = key
        summary_short = issue["summary"][:60] + ("..." if len(issue["summary"]) > 60 else "")
        board_col = issue.get("board_column", "") or ""
        lines.append("| %s | %s | %s | %s | %d | %s |" % (
            key_cell,
            summary_short,
            board_col,
            issue["status"],
            issue["linked_issue_count"],
            issue["collected_at"][:10],
        ))

    tmp_path = index_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp_path, index_path)

    return index_path


def main():
    args = parse_args()
    env = load_env(ENV_KEYS)

    missing = [v for v in ENV_KEYS if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url, auth = init_auth(env)
    parent_key = env["TRIAGE_PARENT_ISSUE_KEY"]
    output_path = env["TRIAGE_OUTPUT_PATH"]

    if not re.match(r"\A[A-Z][A-Z0-9_]+-\d+\Z", parent_key, re.ASCII):
        print("ERROR: TRIAGE_PARENT_ISSUE_KEY '%s' does not look like a valid Jira issue key" % parent_key, file=sys.stderr)
        sys.exit(1)

    # Fast path: regenerate index from cached per-issue JSON files
    if args.index_only:
        issues_data = []
        if os.path.isdir(COLLECT_DIR):
            for f in sorted(os.listdir(COLLECT_DIR)):
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(COLLECT_DIR, f)) as fh:
                        issues_data.append(json.load(fh))
                except Exception as e:
                    print("WARNING: failed to read %s: %s" % (f, e), file=sys.stderr)
        if not issues_data:
            print("ERROR: No cached issue data in %s — run a full collect first" % COLLECT_DIR)
            sys.exit(1)
        index_path = write_index(issues_data, output_path, parent_key)
        print("Index regenerated from %d cached issues: %s" % (len(issues_data), index_path))
        return

    # Step 0: Build board column mapping
    board_id = int(env.get("TRIAGE_BOARD_ID", "0"))
    status_column_map = {}
    if board_id:
        print("Loading board column mapping...", file=sys.stderr)
        status_column_map = build_status_column_map(base_url, auth, board_id)

    # Step 1: Fetch issues
    if args.issue:
        if not re.match(r"\A[A-Z][A-Z0-9_]+-\d+\Z", args.issue, re.ASCII):
            print("ERROR: '%s' does not look like a valid Jira issue key" % args.issue, file=sys.stderr)
            sys.exit(1)
        print("Fetching issue %s..." % args.issue, file=sys.stderr)
        raw_issues = fetch_single_issue(base_url, auth, args.issue)
    else:
        status_note = ' (status: "%s")' % args.status if args.status else ""
        done_note = "" if args.include_done else " (hiding stale done items)"
        print("Fetching all sibling issues under %s%s%s..." % (
            parent_key, status_note, done_note,
        ), file=sys.stderr)
        raw_issues = fetch_all_issues(base_url, auth, parent_key, args.status, args.include_done)

    if not raw_issues:
        print("No issues found.")
        sys.exit(0)

    print("Found %d issues" % len(raw_issues), file=sys.stderr)

    # Step 2: Save raw issue list
    if not args.dry_run:
        ensure_tmp_dir(COLLECT_DIR)
        with open("/tmp/triage_collect_issues.json.tmp", "w") as f:
            json.dump([{"key": i.get("key", i.get("fields", {}).get("key", "")),
                        "summary": i.get("fields", {}).get("summary", "")}
                       for i in raw_issues], f, indent=2)
        os.replace("/tmp/triage_collect_issues.json.tmp", "/tmp/triage_collect_issues.json")

    # Step 3: Process each issue
    issues_data = []
    skipped = 0
    errors = 0

    for idx, raw_issue in enumerate(raw_issues):
        key = raw_issue.get("key", raw_issue.get("fields", {}).get("key", ""))
        json_path = os.path.join(COLLECT_DIR, "%s.json" % key)

        # Skip existing unless --force
        if not args.force and not args.dry_run and os.path.exists(json_path):
            print("  [%d/%d] %s — skipped (already exists)" % (idx + 1, len(raw_issues), key), file=sys.stderr)
            # Still load existing data for the index
            try:
                with open(json_path) as f:
                    issues_data.append(json.load(f))
            except Exception:
                pass
            skipped += 1
            continue

        if args.dry_run:
            fields = raw_issue.get("fields", {})
            summary = fields.get("summary", "")
            status = fields.get("status", {}).get("name", "")
            links = len(fields.get("issuelinks", []))
            subtasks = len(fields.get("subtasks", []))
            print("  [%d/%d] %s — %s (status: %s, links: %d, subtasks: %d)" % (
                idx + 1, len(raw_issues), key, summary[:50], status, links, subtasks,
            ))
            continue

        print("  [%d/%d] Processing %s..." % (idx + 1, len(raw_issues), key), file=sys.stderr)
        try:
            processed = process_issue(base_url, auth, raw_issue, status_column_map)
            issues_data.append(processed)

            tmp_path = json_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(processed, f, indent=2)
            os.replace(tmp_path, json_path)
        except Exception as e:
            print("  ERROR processing %s: %s" % (key, e), file=sys.stderr)
            errors += 1

    if args.dry_run:
        print("\nDry run complete. %d issues would be collected." % len(raw_issues))
        return

    # Step 4: Write index
    index_path = write_index(issues_data, output_path, parent_key)

    # Step 5: Summary
    collected = len(issues_data) - skipped
    with_links = sum(1 for i in issues_data if i.get("linked_issue_count", 0) > 0)

    print("\n--- Collection Summary ---")
    print("Total issues: %d" % len(raw_issues))
    print("Collected: %d" % collected)
    print("Skipped (existing): %d" % skipped)
    print("Errors: %d" % errors)
    print("Issues with linked issues: %d" % with_links)
    print("Per-issue JSON: %s/" % COLLECT_DIR)
    print("Index written: %s" % index_path)
    print("\nReady for summarization step.")


if __name__ == "__main__":
    main()
