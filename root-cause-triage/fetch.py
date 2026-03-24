#!/usr/bin/env python3
"""Fetch root cause triage issues, analyze description completeness, output summary."""

import json
import re
import sys

from jira_client import load_env, init_auth, jira_get, jira_search_all


ENV_KEYS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "TRIAGE_BOARD_ID", "TRIAGE_PARENT_ISSUE_KEY"]

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
    r"\bN/?A\b",
    r"^\s*-?\s*$",       # empty or just a dash
]

# Minimum Jaccard similarity to flag as duplicate (text-based, open tickets)
DUPLICATE_THRESHOLD = 0.5
# Lower threshold for flagging a resolved ticket as a possible recurrence
RECURRENCE_THRESHOLD = 0.35

# Statuses considered "resolved/closed" for recurrence detection
CLOSED_STATUSES = {"done", "closed", "completed", "resolved", "completed / roadmapped", "rejected"}


def discover_triage_statuses(base_url, auth, board_id):
    """Get board config and find which Jira statuses map to the 'To Triage' column."""
    config = jira_get(base_url, "/rest/agile/1.0/board/%d/configuration" % board_id, auth)
    columns = config.get("columnConfig", {}).get("columns", [])

    triage_statuses = []
    for col in columns:
        name = col.get("name", "")
        if "triage" in name.lower():
            for status in col.get("statuses", []):
                triage_statuses.append(status["id"])
            break

    if not triage_statuses:
        if columns:
            first_col = columns[0]
            for status in first_col.get("statuses", []):
                triage_statuses.append(status["id"])
            print("WARNING: No 'triage' column found, using first column '%s'" % first_col.get("name"), file=sys.stderr)

    return triage_statuses


def fetch_issues(base_url, auth, status_ids, parent_issue_key):
    """Fetch issues under the parent epic in the discovered triage statuses."""
    status_list = ", ".join(status_ids)
    jql = "parent = %s AND status in (%s) ORDER BY created ASC" % (parent_issue_key, status_list)
    return jira_search_all(base_url, auth, jql, "key,summary,status,description,created,issuetype,issuelinks,subtasks")


def fetch_subtask_descriptions(base_url, auth, subtasks):
    """Fetch descriptions for subtasks and return as a combined string."""
    parts = []
    for sub in subtasks:
        key = sub.get("key", "")
        if not key:
            continue
        try:
            data = jira_get(base_url, "/rest/api/2/issue/%s?fields=summary,description" % key, auth)
            fields = data.get("fields", {})
            sub_summary = fields.get("summary", "")
            sub_desc = fields.get("description") or ""
            if sub_desc.strip():
                parts.append("[Subtask %s — %s]\n%s" % (key, sub_summary, sub_desc.strip()))
        except Exception as e:
            print("WARNING: Failed to fetch subtask %s: %s" % (key, e), file=sys.stderr)
    return "\n\n".join(parts)


def fetch_all_sibling_issues(base_url, auth, parent_issue_key):
    """Fetch all issues under the parent epic for duplicate comparison (summary + description)."""
    jql = "parent = %s ORDER BY created ASC" % parent_issue_key
    return jira_search_all(base_url, auth, jql, "key,summary,status,description")


def check_issue_links(fields):
    """Return the key of a confirmed linked duplicate, or None."""
    for link in fields.get("issuelinks", []):
        link_type = link.get("type", {}).get("name", "").lower()
        if "duplicate" in link_type:
            linked = link.get("outwardIssue") or link.get("inwardIssue")
            if linked:
                return linked["key"]
    return None


def extract_section(text, section_name):
    """Extract content of a named section from a description, up to 400 chars."""
    if not text:
        return ""
    pattern = r"(?i)(?:^|\n)\s*(?:h\d\.\s*|\*+\s*|#+\s*)?" + re.escape(section_name)
    match = re.search(pattern, text)
    if not match:
        return ""
    start = match.end()
    next_header = re.search(r"\n\s*(?:h\d\.\s*|\*+\s*|#+\s*)\S", text[start:])
    content = text[start:start + next_header.start()] if next_header else text[start:]
    return content.strip()[:400]


def comparison_text(summary, description):
    """Combine summary with Background Context and Steps to reproduce for richer similarity."""
    parts = [summary]
    for section in ("Background Context", "Steps to reproduce"):
        excerpt = extract_section(description or "", section)
        if excerpt:
            parts.append(excerpt)
    if not parts[1:] and description:
        # No named sections found — fall back to first 400 chars of description
        parts.append(description[:400])
    return " ".join(parts)


def tokenize(text):
    """Lowercase word tokens, stripping punctuation."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a, b):
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_text_duplicate(summary, description, candidate_issues, exclude_keys):
    """
    Search open and closed candidates separately.

    Returns:
        duplicate_key, duplicate_score  — best open-issue match above DUPLICATE_THRESHOLD
        recurrence_key, recurrence_score — best closed-issue match above RECURRENCE_THRESHOLD
    """
    tokens = tokenize(comparison_text(summary, description))
    best_open_key, best_open_score = None, 0.0
    best_closed_key, best_closed_score = None, 0.0

    for issue in candidate_issues:
        if issue["key"] in exclude_keys:
            continue
        fields = issue.get("fields", {})
        status_name = fields.get("status", {}).get("name", "").lower()
        is_closed = status_name in CLOSED_STATUSES

        candidate_tokens = tokenize(comparison_text(
            fields.get("summary", ""),
            fields.get("description", ""),
        ))
        score = jaccard(tokens, candidate_tokens)

        if is_closed:
            if score > best_closed_score:
                best_closed_score = score
                best_closed_key = issue["key"]
        else:
            if score > best_open_score:
                best_open_score = score
                best_open_key = issue["key"]

    dup_key = best_open_key if best_open_score >= DUPLICATE_THRESHOLD else None
    rec_key = best_closed_key if best_closed_score >= RECURRENCE_THRESHOLD else None

    return (
        dup_key, best_open_score if dup_key else 0.0,
        rec_key, best_closed_score if rec_key else 0.0,
    )


def analyze_description(description):
    """Check which template sections are present and filled in the description."""
    if not description:
        return {s: False for s in TEMPLATE_SECTIONS}, []

    results = {}
    missing = []

    for section in TEMPLATE_SECTIONS:
        content = extract_section(description, section)

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
    env = load_env(ENV_KEYS)

    missing = [v for v in ENV_KEYS if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url, auth = init_auth(env)

    board_id = int(env.get("TRIAGE_BOARD_ID", "0"))
    parent_issue_key = env.get("TRIAGE_PARENT_ISSUE_KEY", "")
    if not board_id or not parent_issue_key:
        print("ERROR: TRIAGE_BOARD_ID and TRIAGE_PARENT_ISSUE_KEY must be set in ~/.zshrc")
        sys.exit(1)
    if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", parent_issue_key):
        print("ERROR: TRIAGE_PARENT_ISSUE_KEY '%s' does not look like a valid Jira issue key" % parent_issue_key)
        sys.exit(1)

    # Step 1: Discover triage statuses from board config
    print("Discovering board configuration...", file=sys.stderr)
    status_ids = discover_triage_statuses(base_url, auth, board_id)
    if not status_ids:
        print("ERROR: Could not determine triage statuses from board %d configuration" % board_id)
        sys.exit(1)
    print("Triage status IDs: %s" % ", ".join(status_ids), file=sys.stderr)

    # Step 2: Fetch triage issues + all siblings for duplicate detection
    print("Fetching issues...", file=sys.stderr)
    issues = fetch_issues(base_url, auth, status_ids, parent_issue_key)

    if not issues:
        print("\nNo issues found in 'To Triage' status.")
        print("Nothing to triage — the board is clear.")
        with open("/tmp/triage_issues.json", "w") as f:
            json.dump([], f)
        sys.exit(0)

    print("Found %d issues to triage" % len(issues), file=sys.stderr)
    print("Fetching all sibling issues for duplicate detection...", file=sys.stderr)
    all_siblings = fetch_all_sibling_issues(base_url, auth, parent_issue_key)
    triage_keys = {i["key"] for i in issues}
    print("Found %d total sibling issues\n" % len(all_siblings), file=sys.stderr)

    # Step 3: Analyze each issue
    results = []
    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        description = fields.get("description", "")
        status_name = fields.get("status", {}).get("name", "")
        created = (fields.get("created") or "")[:10]

        # Augment thin descriptions with subtask content
        subtasks = fields.get("subtasks", [])
        subtask_content = ""
        if subtasks:
            subtask_content = fetch_subtask_descriptions(base_url, auth, subtasks)
        augmented_description = description or ""
        if subtask_content:
            augmented_description = augmented_description + "\n\n" + subtask_content if augmented_description else subtask_content

        section_results, missing_sections = analyze_description(augmented_description)
        filled_count = sum(1 for v in section_results.values() if v)

        # Duplicate/recurrence detection: Jira links take priority over text similarity
        linked_dup = check_issue_links(fields)
        rec_key, rec_score = None, 0.0
        if linked_dup:
            action = "duplicate"
            dup_key = linked_dup
            dup_score = 1.0
            dup_source = "linked"
        else:
            dup_key, dup_score, rec_key, rec_score = find_text_duplicate(
                summary, augmented_description, all_siblings, exclude_keys={key}
            )
            if dup_key:
                action = "duplicate"
                dup_source = "text-similarity"
            else:
                action = recommend_action(filled_count)
                dup_source = None

        results.append({
            "key": key,
            "summary": summary,
            "description": augmented_description,
            "has_subtasks": len(subtasks) > 0,
            "status": status_name,
            "created": created,
            "filled_count": filled_count,
            "total_sections": len(TEMPLATE_SECTIONS),
            "missing_sections": missing_sections,
            "section_results": section_results,
            "recommendation": action,
            "duplicate_of": dup_key,
            "duplicate_score": round(dup_score, 2),
            "duplicate_source": dup_source,
            "recurrence_of": rec_key,
            "recurrence_score": round(rec_score, 2),
        })

    # Step 4: Output summary table
    print("| # | Key | Summary | Score | Missing | Recommendation | Signals |")
    print("|---|-----|---------|-------|---------|----------------|---------|")
    for i, r in enumerate(results, 1):
        summary_short = r["summary"][:50] + ("..." if len(r["summary"]) > 50 else "")
        missing_str = ", ".join(r["missing_sections"]) if r["missing_sections"] else "None"
        if r["recommendation"] == "duplicate":
            source_tag = " [linked]" if r["duplicate_source"] == "linked" else " [%.0f%% match]" % (r["duplicate_score"] * 100)
            rec_display = "Duplicate of %s%s" % (r["duplicate_of"], source_tag)
        elif r["recommendation"] == "ready":
            rec_display = "Ready for Dev"
        else:
            rec_display = "More Info Required"
        signals = []
        if r.get("recurrence_of"):
            signals.append("Recurrence? %s (%.0f%%)" % (r["recurrence_of"], r["recurrence_score"] * 100))
        if r.get("has_subtasks"):
            signals.append("has subtasks")
        signals_str = ", ".join(signals) if signals else ""
        print("| %d | %s | %s | %d/%d | %s | %s | %s |" % (
            i, r["key"], summary_short, r["filled_count"], r["total_sections"],
            missing_str, rec_display, signals_str,
        ))

    print("\nSummary: %d ready, %d need more info, %d duplicates" % (
        sum(1 for r in results if r["recommendation"] == "ready"),
        sum(1 for r in results if r["recommendation"] == "more_info"),
        sum(1 for r in results if r["recommendation"] == "duplicate"),
    ))

    # Step 5: Save to JSON (description included for agent quality assessment)
    with open("/tmp/triage_issues.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nDetails saved to /tmp/triage_issues.json", file=sys.stderr)


if __name__ == "__main__":
    main()
