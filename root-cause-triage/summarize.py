#!/usr/bin/env python3
"""Read per-issue JSON from /tmp/triage_collect/ and write Obsidian Markdown files."""

import argparse
import glob
import json
import os
import re
import sys

from jira_client import load_env


ENV_KEYS = ["JIRA_BASE_URL", "TRIAGE_PARENT_ISSUE_KEY", "TRIAGE_OUTPUT_PATH"]


def build_vault_links(vault_base):
    """Walk vault to discover incident pages. Returns dict: lowercase key -> wiki link."""
    links = {}
    if not vault_base or not os.path.isdir(vault_base):
        return links
    inc_dir = os.path.join(vault_base, "Incidents")
    if not os.path.isdir(inc_dir):
        return links
    for f in os.listdir(inc_dir):
        if not f.endswith(".md") or f.startswith("_"):
            continue
        name = f[:-3]
        inc_match = re.search(r"INC-\d+", name, re.IGNORECASE)
        if inc_match:
            key = inc_match.group().upper()
            links[key.lower()] = "[[%s\\|%s]]" % (name, key)
    return links

COLLECT_DIR = "/tmp/triage_collect"

# Section headers in support ticket templates that contain the actual problem
# (everything before these is typically customer/property identification boilerplate)
PROBLEM_SECTION_HEADERS = [
    "issue summary",
    "expected behavior",
    "desired outcome",
    "steps to recreate",
    "steps to reproduce",
    "troubleshooting",
    "investigation",
    "analysis",
    "actual results",
    "expected results",
    "what is behind the flag",
    "rollout and removal",
]

# Section headers that are boilerplate / customer identification — skip these
BOILERPLATE_SECTION_HEADERS = [
    "customer name",
    "happyco customer id",
    "property management company",
    "property name",
    "property folder",
    "property hub link",
    "pmc/business id",
    "happyco user id",
    "screenshots or videos",
    "link to existing slack",
    "zendesk",
]

# Lines matching these patterns are customer-specific identifiers, not problem descriptions
NOISE_PATTERNS = [
    r'^[-•]\s*(Property Management Company|Business Name|HappyCo Admin|HappyCo PMC|HappyCo User ID|Property Name|Property Folder|Property Hub|PMS):?\s',
    r'^[-•]\s*https?://',
    r'^\d{4,}\s+\S',           # e.g. "41358    Martel Park" — property ID + name
    r'^Customer Name\b',
    r'^Property Name\b',
]


def parse_args():
    parser = argparse.ArgumentParser(description="Write Obsidian Markdown from collected JSON")
    parser.add_argument("--issue", help="Process a single issue by key (e.g., PDE-1234)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing Markdown files")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be written without writing")
    return parser.parse_args()


def sanitize_filename(s, max_len=80):
    """Sanitize a string for use in filenames."""
    s = re.sub(r'[/\\:*?"<>|]', '-', s)
    s = s.lstrip(".")
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s or "Untitled"


def is_section_header(line):
    """Check if a line is a template section header. Returns (is_header, is_problem_section)."""
    lower = line.lower().rstrip(':').strip('- \u2022*#\U0001f4dd\u2753\U0001f6f3\ufe0f\U0001f331\u2139\ufe0f\U0001f645\U0001f91d\U0001f914\U0001fa9c').strip()
    for header in PROBLEM_SECTION_HEADERS:
        if header in lower:
            return True, True
    for header in BOILERPLATE_SECTION_HEADERS:
        if header in lower:
            return True, False
    return False, False


def is_noise_line(line):
    """Check if a line is customer-specific boilerplate."""
    for pattern in NOISE_PATTERNS:
        if re.match(pattern, line.strip(), re.IGNORECASE):
            return True
    return False


def summarize_description(desc):
    """Extract the problem description from a linked issue, skipping customer identification boilerplate."""
    if not desc or len(desc.strip()) < 20:
        return None

    # Strip all URLs — internal links aren't useful in summaries
    text = re.sub(r'https?://\S+', '', desc.strip())
    lines = [line.strip() for line in text.split('\n') if line.strip()]

    # Pass 1: split into sections, tagging each as problem-relevant or boilerplate
    sections = []  # list of (is_problem, [content_lines])
    current_is_problem = False  # top of ticket is typically boilerplate
    current_lines = []

    for line in lines:
        is_header, is_problem = is_section_header(line)
        if is_header:
            if current_lines:
                sections.append((current_is_problem, current_lines))
            current_is_problem = is_problem
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_is_problem, current_lines))

    # Pass 2: collect content from problem sections only
    problem_lines = []
    for is_problem, section_lines in sections:
        if not is_problem:
            continue
        for line in section_lines:
            if re.match(r'^<.*[?>]$', line):
                continue
            if len(line) < 10:
                continue
            if is_noise_line(line):
                continue
            cleaned = re.sub(r'^[-\u2022]\s*', '', line).strip()
            # Strip inline template placeholders
            cleaned = re.sub(r'<[^>]{5,}?\?>', '', cleaned).strip()
            if cleaned:
                problem_lines.append(cleaned)

    # Fallback: if no problem sections found (unstructured description),
    # take all non-boilerplate, non-noise lines
    if not problem_lines:
        for line in lines:
            if line.startswith('<') and line.endswith('>'):
                continue
            if line.startswith('#'):
                continue
            if len(line) < 15:
                continue
            if is_noise_line(line):
                continue
            is_header, _ = is_section_header(line)
            if is_header:
                continue
            if not line.strip():
                continue
            cleaned = re.sub(r'^[-\u2022]\s*', '', line).strip()
            if cleaned and len(cleaned) > 15:
                problem_lines.append(cleaned)

    if not problem_lines:
        return None

    selected = problem_lines[:5]
    summary = ' '.join(selected)
    if len(summary) > 600:
        summary = summary[:597] + '...'

    return summary


def build_linked_issues_section(linked_issues, jira_base, vault_links=None):
    """Build the Linked Issues section of the Markdown file."""
    if vault_links is None:
        vault_links = {}
    lines = ["## Linked Issues", ""]

    for group_label, issues in linked_issues.items():
        count = len(issues)
        lines.append("### %s (%d)" % (group_label.title(), count))
        lines.append("")

        # Large causes groups get a paragraph summary + compact list
        if group_label.lower() == "causes" and count > 10:
            stub_count = sum(1 for i in issues if not i.get("description"))
            desc_count = count - stub_count
            statuses = {}
            for i in issues:
                s = i.get("status", "Unknown")
                statuses[s] = statuses.get(s, 0) + 1
            status_str = ", ".join(
                "%d %s" % (v, k)
                for k, v in sorted(statuses.items(), key=lambda x: -x[1])
            )

            lines.append(
                "This root cause has %d linked support/bug tickets (%s). "
                "%d have descriptions fetched, %d are stubs only."
                % (count, status_str, desc_count, stub_count)
            )
            lines.append("")

            for issue in issues:
                ikey = issue["key"]
                isummary = issue.get("summary", "")
                istatus = issue.get("status", "Unknown")
                itype = issue.get("issue_type", "Unknown")
                idesc = issue.get("description", "")

                vault_ref = ""
                vl = vault_links.get(ikey.lower())
                if vl:
                    vault_ref = " (%s)" % vl
                lines.append(
                    "- [%s](%s/browse/%s)%s — %s *(%s)* — **%s**"
                    % (ikey, jira_base, ikey, vault_ref, isummary, itype, istatus)
                )

                if idesc:
                    s = summarize_description(idesc)
                    if s:
                        lines.append("  - **Summary:** %s" % s)

            lines.append("")
        else:
            for issue in issues:
                ikey = issue["key"]
                isummary = issue.get("summary", "")
                istatus = issue.get("status", "Unknown")
                itype = issue.get("issue_type", "Unknown")
                idesc = issue.get("description", "")

                vault_ref = ""
                vl = vault_links.get(ikey.lower())
                if vl:
                    vault_ref = " (%s)" % vl
                lines.append(
                    "#### [%s](%s/browse/%s)%s — %s *(%s)*"
                    % (ikey, jira_base, ikey, vault_ref, isummary, itype)
                )
                lines.append("- **Status:** %s" % istatus)

                if idesc:
                    s = summarize_description(idesc)
                    if s:
                        lines.append("- **Summary:** %s" % s)
                    else:
                        lines.append("- *(description too short to summarize)*")
                else:
                    lines.append("- *(stub only — no description fetched)*")

                lines.append("")

    return lines


def process_issue(data, jira_base, parent_key, vault_links=None):
    """Convert a single issue's JSON data into Markdown content and filename."""
    key = data["key"]
    summary = data["summary"]
    safe_summary = sanitize_filename(summary)
    filename = "%s — %s.md" % (key, safe_summary)

    lines = [
        "---",
        "key: %s" % key,
        "board_column: %s" % data.get("board_column", "Unknown"),
        "status: %s" % data.get("status", "Unknown"),
        "issue_type: %s" % data.get("issue_type", "Unknown"),
        "priority: %s" % data.get("priority", "Unknown"),
        "reporter: %s" % data.get("reporter", "Unknown"),
        "created: %s" % data.get("created", "Unknown"),
        "parent_epic: %s" % parent_key,
        "collected_at: %s" % data.get("collected_at", ""),
        "linked_issue_count: %d" % data.get("linked_issue_count", 0),
        "---",
        "",
        "# [%s](%s/browse/%s) — %s" % (key, jira_base, key, summary),
        "",
        "## Description",
        "",
    ]

    # Description with subtask content
    # Strip template placeholder tags like <what is happening that shouldn't>
    # that break Obsidian formatting due to unmatched angle brackets / apostrophes
    desc = data.get("description", "") or ""
    desc = re.sub(r'<[^>]{5,}>', '', desc)
    subtasks = data.get("subtasks", [])
    if subtasks:
        for st in subtasks:
            st_desc = st.get("description", "")
            if st_desc:
                desc += "\n\n**Subtask [%s](%s/browse/%s) — %s:**\n%s" % (
                    st["key"], jira_base, st["key"], st["summary"], st_desc,
                )
            else:
                desc += "\n\n**Subtask [%s](%s/browse/%s) — %s**" % (
                    st["key"], jira_base, st["key"], st["summary"],
                )

    lines.append(desc.strip() if desc.strip() else "*(No description)*")
    lines.append("")

    # Linked issues
    linked = data.get("linked_issues", {})
    if linked:
        lines.extend(build_linked_issues_section(linked, jira_base, vault_links))

    return filename, '\n'.join(lines)


def find_existing_file(issues_dir, key):
    """Check if a Markdown file for this key already exists (any filename variant)."""
    pattern = os.path.join(issues_dir, "%s — *.md" % key)
    matches = glob.glob(pattern)
    # Also check exact key prefix without em dash
    pattern2 = os.path.join(issues_dir, "%s —*.md" % key)
    matches.extend(glob.glob(pattern2))
    return len(matches) > 0


def main():
    args = parse_args()
    env = load_env(ENV_KEYS)

    missing = [v for v in ENV_KEYS if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: %s" % ", ".join(missing))
        sys.exit(1)

    jira_base = env["JIRA_BASE_URL"]
    parent_key = env["TRIAGE_PARENT_ISSUE_KEY"]
    output_path = env["TRIAGE_OUTPUT_PATH"]
    issues_dir = os.path.join(output_path, "Issues")

    # Build vault links for cross-referencing incident pages
    vault_base = os.path.dirname(output_path)  # parent of triage output is the HappyCo dir
    vault_links = build_vault_links(vault_base)
    if vault_links:
        print("Discovered %d incident pages for cross-referencing" % len(vault_links), file=sys.stderr)

    # Find JSON files to process
    if args.issue:
        json_path = os.path.join(COLLECT_DIR, "%s.json" % args.issue)
        if not os.path.exists(json_path):
            print("ERROR: No collected data for %s at %s" % (args.issue, json_path))
            sys.exit(1)
        json_files = [json_path]
    else:
        json_files = sorted(glob.glob(os.path.join(COLLECT_DIR, "*.json")))

    if not json_files:
        print("No JSON files found in %s/" % COLLECT_DIR)
        print("Run collect.py first to fetch data from Jira.")
        sys.exit(1)

    print("Found %d issue(s) to process" % len(json_files), file=sys.stderr)

    if not args.dry_run:
        os.makedirs(issues_dir, exist_ok=True)

    written = 0
    skipped = 0
    errors = []

    for jf in json_files:
        key = os.path.basename(jf).replace('.json', '')

        try:
            with open(jf) as f:
                data = json.load(f)
        except Exception as e:
            errors.append("%s: failed to read JSON — %s" % (key, e))
            print("  ERROR: %s: %s" % (key, e), file=sys.stderr)
            continue

        # Skip existing unless --force
        if not args.force and find_existing_file(issues_dir, key):
            skipped += 1
            if args.dry_run:
                print("  [skip] %s — already exists" % key)
            continue

        try:
            filename, content = process_issue(data, jira_base, parent_key, vault_links)
        except Exception as e:
            errors.append("%s: failed to generate markdown — %s" % (key, e))
            print("  ERROR: %s: %s" % (key, e), file=sys.stderr)
            continue

        if args.dry_run:
            print("  [write] %s → %s" % (key, filename))
            continue

        filepath = os.path.join(issues_dir, filename)
        tmp_path = filepath + ".tmp"
        with open(tmp_path, 'w') as f:
            f.write(content)
        os.replace(tmp_path, filepath)
        written += 1

    # Summary
    if args.dry_run:
        total = len(json_files)
        would_write = total - skipped - len(errors)
        print("\nDry run complete:")
        print("- %d would be written" % would_write)
        print("- %d would be skipped (already exist)" % skipped)
        if errors:
            print("- %d errors:" % len(errors))
            for e in errors:
                print("  - %s" % e)
    else:
        print("\n--- Summarization Complete ---")
        print("Markdown files written: %d" % written)
        print("Skipped (already existed): %d" % skipped)
        print("Errors: %d" % len(errors))
        if errors:
            for e in errors:
                print("  - %s" % e)
        print("Output: %s/" % issues_dir)


if __name__ == "__main__":
    main()
