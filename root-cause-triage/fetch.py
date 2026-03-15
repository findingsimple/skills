#!/usr/bin/env python3
"""Fetch root cause triage issues, analyze description completeness, output summary."""

import subprocess
import json
import re
import sys
import urllib.request
import urllib.parse
import base64

BOARD_ID = None  # Set from TRIAGE_BOARD_ID env var
PARENT_ISSUE_KEY = None  # Set from TRIAGE_PARENT_ISSUE_KEY env var

TEMPLATE_SECTIONS = [
    "Background Context",
    "Steps to reproduce",
    "Actual Results",
    "Expected Results",
    "Analysis",
]

# Patterns that indicate placeholder/unfilled content
PLACEHOLDER_PATTERNS = [
    r"<[^>]+>",          # <placeholder text>
    r"\{[^}]+\}",        # {placeholder text}
    r"TBD",
    r"TODO",
    r"N/?A",
    r"^\s*-?\s*$",       # empty or just a dash
]


def load_env():
    result = subprocess.run(
        ["bash", "-c", "source ~/.sprint_summary_env && env"],
        capture_output=True,
        text=True,
    )
    return dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )


def jira_get(base_url, path, auth):
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def discover_triage_statuses(base_url, auth):
    """Get board 731 config and find which Jira statuses map to the 'To Triage' column."""
    config = jira_get(base_url, "/rest/agile/1.0/board/%d/configuration" % BOARD_ID, auth)
    columns = config.get("columnConfig", {}).get("columns", [])

    triage_statuses = []
    for col in columns:
        name = col.get("name", "")
        if "triage" in name.lower():
            for status in col.get("statuses", []):
                triage_statuses.append(status["id"])
            break

    if not triage_statuses:
        # Fallback: first column is typically the triage/backlog column
        if columns:
            first_col = columns[0]
            for status in first_col.get("statuses", []):
                triage_statuses.append(status["id"])
            print("WARNING: No 'triage' column found, using first column '%s'" % first_col.get("name"), file=sys.stderr)

    return triage_statuses


def fetch_issues(base_url, auth, status_ids):
    """Fetch issues under PDE-8900 in the discovered triage statuses."""
    status_list = ", ".join(status_ids)
    jql = "parent = %s AND status in (%s) ORDER BY created ASC" % (PARENT_ISSUE_KEY, status_list)
    encoded_jql = urllib.parse.quote(jql, safe="")

    # Use v2 for plain-text description rendering
    path = "/rest/api/2/search?jql=%s&maxResults=50&fields=key,summary,status,description,created,issuetype" % encoded_jql
    data = jira_get(base_url, path, auth)

    issues = data.get("issues", [])
    total = data.get("total", len(issues))

    # Paginate if more than 50
    while len(issues) < total:
        next_path = "/rest/api/2/search?jql=%s&maxResults=50&startAt=%d&fields=key,summary,status,description,created,issuetype" % (
            encoded_jql, len(issues)
        )
        next_data = jira_get(base_url, next_path, auth)
        issues.extend(next_data.get("issues", []))

    return issues


def analyze_description(description):
    """Check which template sections are present and filled in the description."""
    if not description:
        return {s: False for s in TEMPLATE_SECTIONS}, []

    results = {}
    missing = []

    for section in TEMPLATE_SECTIONS:
        # Look for the section header (case-insensitive)
        pattern = r"(?i)(?:^|\n)\s*(?:h\d\.\s*|\*+\s*|#+\s*)?" + re.escape(section)
        match = re.search(pattern, description)

        if not match:
            results[section] = False
            missing.append(section)
            continue

        # Extract content after the header until the next header or end
        start = match.end()
        next_header = re.search(r"\n\s*(?:h\d\.\s*|\*+\s*|#+\s*)\S", description[start:])
        if next_header:
            content = description[start:start + next_header.start()]
        else:
            content = description[start:]

        content = content.strip()

        # Check if content is just a placeholder
        if not content:
            results[section] = False
            missing.append(section)
            continue

        is_placeholder = False
        for pp in PLACEHOLDER_PATTERNS:
            cleaned = re.sub(pp, "", content, flags=re.IGNORECASE).strip()
            if not cleaned:
                is_placeholder = True
                break

        if is_placeholder:
            results[section] = False
            missing.append(section)
        else:
            results[section] = True

    return results, missing


def recommend_action(filled_count):
    """4-5 filled sections → Ready for Dev, 0-3 → More Info Required."""
    if filled_count >= 4:
        return "ready"
    return "more_info"


def main():
    env = load_env()

    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "TRIAGE_BOARD_ID", "TRIAGE_PARENT_ISSUE_KEY"]
    missing = [v for v in required if v not in env]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url = env["JIRA_BASE_URL"]
    email = env["JIRA_EMAIL"]
    token = env["JIRA_API_TOKEN"]
    auth = base64.b64encode((email + ":" + token).encode()).decode()

    global BOARD_ID, PARENT_ISSUE_KEY
    BOARD_ID = int(env.get("TRIAGE_BOARD_ID", "0"))
    PARENT_ISSUE_KEY = env.get("TRIAGE_PARENT_ISSUE_KEY", "")
    if not BOARD_ID or not PARENT_ISSUE_KEY:
        print("ERROR: TRIAGE_BOARD_ID and TRIAGE_PARENT_ISSUE_KEY must be set in ~/.sprint_summary_env")
        sys.exit(1)

    # Step 1: Discover triage statuses from board config
    print("Discovering board configuration...", file=sys.stderr)
    status_ids = discover_triage_statuses(base_url, auth)
    if not status_ids:
        print("ERROR: Could not determine triage statuses from board %d configuration" % BOARD_ID)
        sys.exit(1)
    print("Triage status IDs: %s" % ", ".join(status_ids), file=sys.stderr)

    # Step 2: Fetch issues
    print("Fetching issues...", file=sys.stderr)
    issues = fetch_issues(base_url, auth, status_ids)

    if not issues:
        print("\nNo issues found in 'To Triage' status.")
        print("Nothing to triage — the board is clear.")
        # Still write empty JSON for consistency
        with open("/tmp/triage_issues.json", "w") as f:
            json.dump([], f)
        sys.exit(0)

    print("Found %d issues to triage\n" % len(issues), file=sys.stderr)

    # Step 3: Analyze each issue
    results = []
    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = fields.get("description", "")
        status_name = fields.get("status", {}).get("name", "")
        created = (fields.get("created") or "")[:10]

        section_results, missing_sections = analyze_description(description)
        filled_count = sum(1 for v in section_results.values() if v)
        action = recommend_action(filled_count)

        results.append({
            "key": key,
            "summary": summary,
            "status": status_name,
            "created": created,
            "filled_count": filled_count,
            "total_sections": len(TEMPLATE_SECTIONS),
            "missing_sections": missing_sections,
            "section_results": section_results,
            "recommendation": action,
        })

    # Step 4: Output summary table
    print("| # | Key | Summary | Score | Missing | Recommendation |")
    print("|---|-----|---------|-------|---------|----------------|")
    for i, r in enumerate(results, 1):
        summary_short = r["summary"][:50] + ("..." if len(r["summary"]) > 50 else "")
        missing_str = ", ".join(r["missing_sections"]) if r["missing_sections"] else "None"
        rec_display = "Ready for Dev" if r["recommendation"] == "ready" else "More Info Required"
        print("| %d | %s | %s | %d/%d | %s | %s |" % (
            i, r["key"], summary_short, r["filled_count"], r["total_sections"],
            missing_str, rec_display,
        ))

    print("\nSummary: %d ready, %d need more info" % (
        sum(1 for r in results if r["recommendation"] == "ready"),
        sum(1 for r in results if r["recommendation"] == "more_info"),
    ))

    # Step 5: Save to JSON
    with open("/tmp/triage_issues.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nDetails saved to /tmp/triage_issues.json", file=sys.stderr)


if __name__ == "__main__":
    main()
