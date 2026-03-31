#!/usr/bin/env python3
"""Analyze collected root cause issues — structural scoring and data preparation.

Reads collected issue data, runs template completeness scoring, text-similarity
duplicate detection, and resolution assessment. Writes results to JSON for
downstream agent assessment (build_prompts.py) and report generation (report.py).

Output: /tmp/triage_analysis.json
"""

import argparse
import json
import os
import re
import sys

ENV_KEYS = ["TRIAGE_OUTPUT_PATH"]

TEMPLATE_SECTIONS = [
    "Background Context",
    "Steps to reproduce",
    "Actual Results",
    "Expected Results",
    "Analysis",
]

PLACEHOLDER_PATTERNS = [
    r"<[^>]+>",
    r"\{[^}]+\}",
    r"TBD",
    r"TODO",
    r"\bN/?A\b",
    r"^\s*-?\s*$",
]

DUPLICATE_THRESHOLD = 0.5
RECURRENCE_THRESHOLD = 0.35

CLOSED_STATUSES = {"done", "closed", "completed", "resolved", "completed / roadmapped", "rejected"}


def parse_args():
    parser = argparse.ArgumentParser(description="Analyze collected root cause issues")
    parser.add_argument("--issue", help="Analyze a single issue by key")
    parser.add_argument("--status", default=None, help="Filter by status (default: all statuses)")
    parser.add_argument("--all-statuses", action="store_true", help="Analyze all statuses")
    parser.add_argument("--output-json", action="store_true", help="(no-op, JSON is always written to /tmp/triage_analysis.json)")
    return parser.parse_args()


def parse_frontmatter(text):
    """Parse YAML frontmatter from markdown text. Simple regex-based parser."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip()
    return fm


def parse_issue_file(filepath):
    """Parse a collected issue Markdown file into structured data."""
    with open(filepath, "r") as f:
        text = f.read()

    fm = parse_frontmatter(text)

    # Extract description section
    desc_match = re.search(r"## Description\s*\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
    description = desc_match.group(1).strip() if desc_match else ""

    # Count linked issues by section
    linked_sections = re.findall(r"### (.+?) \((\d+)\)", text)
    linked_counts = {}
    for section_name, count in linked_sections:
        linked_counts[section_name] = int(count)

    return {
        "key": fm.get("key", os.path.basename(filepath).replace(".md", "")),
        "summary": "",  # Will be extracted from heading
        "description": description,
        "status": fm.get("status", ""),
        "issue_type": fm.get("issue_type", ""),
        "priority": fm.get("priority", ""),
        "reporter": fm.get("reporter", ""),
        "created": fm.get("created", ""),
        "linked_issue_count": int(fm.get("linked_issue_count", "0")),
        "linked_counts": linked_counts,
        "collected_at": fm.get("collected_at", ""),
        "full_text": text,
    }


def parse_issue_json(filepath):
    """Parse a collected issue JSON file from /tmp/triage_collect/."""
    with open(filepath, "r") as f:
        return json.load(f)


def extract_section(text, section_name):
    """Extract content of a named section from a description, up to 400 chars.

    Matches section headers in common formats:
    - Markdown: ## Section Name
    - Jira wiki: h2. Section Name
    - Bold: **Section Name**
    - Plain: Section Name (at start of line)
    Only matches at line boundaries with optional header formatting — not mid-sentence.
    """
    if not text:
        return ""
    pattern = r"(?i)(?:^|\n)\s*(?:h\d\.\s+|\*{1,3}\s*|#{1,6}\s+)?" + re.escape(section_name)
    match = re.search(pattern, text)
    if not match:
        return ""
    start = match.end()
    next_header = re.search(
        r"\n\s*(?:h\d\.\s*|\*+\s*|#+\s*)\S|\n\n\s*\S+\s+[A-Z]",
        text[start:],
    )
    content = text[start:start + next_header.start()] if next_header else text[start:]
    return content.strip()[:400]


def analyze_description(description):
    """Check which template sections are present and filled."""
    if not description:
        return {s: False for s in TEMPLATE_SECTIONS}, TEMPLATE_SECTIONS[:]

    results = {}
    missing = []

    for section in TEMPLATE_SECTIONS:
        content = extract_section(description, section)

        if not content:
            results[section] = False
            missing.append(section)
            continue

        # Apply all placeholder patterns cumulatively, then check if anything remains.
        # This catches cases like "- <placeholder text>" where removing angle brackets
        # leaves "-" and removing dashes leaves nothing.
        cleaned = content
        for pp in PLACEHOLDER_PATTERNS:
            cleaned = re.sub(pp, "", cleaned, flags=re.IGNORECASE | re.MULTILINE)
        cleaned = cleaned.strip()

        if not cleaned:
            results[section] = False
            missing.append(section)
        else:
            results[section] = True

    return results, missing


def strip_placeholders(text):
    """Remove placeholder/template content, returning only real substance."""
    if not text:
        return ""
    # Remove placeholder patterns
    cleaned = text
    for pp in PLACEHOLDER_PATTERNS:
        cleaned = re.sub(pp, "", cleaned, flags=re.IGNORECASE)
    # Remove template section headers (emoji + title patterns)
    cleaned = re.sub(r"[^\w\s]*\s*(Background Context|Steps to reproduce|Actual Results|Expected Results|Analysis|User Story|Acceptance Criteria|Release flag)\s*", "", cleaned, flags=re.IGNORECASE)
    # Remove leftover whitespace/dashes
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_linked_summaries(issue):
    """Extract summaries from linked issues to use as comparison signal."""
    linked = issue.get("linked_issues", {})
    if not isinstance(linked, dict):
        return ""
    parts = []
    for group_label, items in linked.items():
        if isinstance(items, list):
            for item in items:
                summary = item.get("summary", "")
                if summary:
                    parts.append(summary)
    # Cap to first 10 linked summaries to keep comparison bounded
    return " ".join(parts[:10])


def comparison_text(summary, description, issue=None):
    """Build comparison text from meaningful content only.

    Uses issue title + cleaned description (placeholders stripped) +
    linked issue summaries. When descriptions are empty/template-only,
    comparison falls back to title + linked issue context.
    """
    parts = [summary]

    # Add description content only if it has real substance after stripping placeholders
    if description:
        cleaned_desc = strip_placeholders(description)
        if len(cleaned_desc) > 20:  # meaningful content threshold
            parts.append(cleaned_desc[:400])

    # Add linked issue summaries as signal
    if issue:
        linked_text = extract_linked_summaries(issue)
        if linked_text:
            parts.append(linked_text)

    return " ".join(parts)


def tokenize(text):
    """Lowercase word tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a, b):
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicates(issue, all_issues):
    """Find duplicate and recurrence candidates from the full knowledge base.

    Compares using title + cleaned description + linked issue summaries.
    This avoids false matches from shared empty template text.
    """
    tokens = tokenize(comparison_text(issue["summary"], issue.get("description", ""), issue))
    best_open_key, best_open_score = None, 0.0
    best_closed_key, best_closed_score = None, 0.0

    for candidate in all_issues:
        if candidate["key"] == issue["key"]:
            continue

        is_closed = candidate["status"].lower() in CLOSED_STATUSES
        candidate_tokens = tokenize(comparison_text(candidate["summary"], candidate.get("description", ""), candidate))
        score = jaccard(tokens, candidate_tokens)

        if is_closed:
            if score > best_closed_score:
                best_closed_score = score
                best_closed_key = candidate["key"]
        else:
            if score > best_open_score:
                best_open_score = score
                best_open_key = candidate["key"]

    dup_key = best_open_key if best_open_score >= DUPLICATE_THRESHOLD else None
    rec_key = best_closed_key if best_closed_score >= RECURRENCE_THRESHOLD else None

    return (
        dup_key, best_open_score if dup_key else 0.0,
        rec_key, best_closed_score if rec_key else 0.0,
    )


def load_issues_from_json(collect_dir="/tmp/triage_collect"):
    """Load all collected issue JSON files."""
    issues = []
    if not os.path.isdir(collect_dir):
        return issues
    for filename in sorted(os.listdir(collect_dir)):
        if filename.endswith(".json") and not filename.startswith("_"):
            filepath = os.path.join(collect_dir, filename)
            try:
                issues.append(parse_issue_json(filepath))
            except Exception as e:
                print("WARNING: Failed to parse %s: %s" % (filename, e), file=sys.stderr)
    return issues


def load_issues_from_obsidian(issues_dir):
    """Load all collected issue Markdown files from Obsidian."""
    issues = []
    if not os.path.isdir(issues_dir):
        return issues
    for filename in sorted(os.listdir(issues_dir)):
        if filename.endswith(".md") and not filename.startswith("_"):
            filepath = os.path.join(issues_dir, filename)
            try:
                issues.append(parse_issue_file(filepath))
            except Exception as e:
                print("WARNING: Failed to parse %s: %s" % (filename, e), file=sys.stderr)
    return issues


def count_support_links(issue, support_project_key=None):
    """Count linked issues from the support project.

    Uses SUPPORT_PROJECT_KEY env var to match issue keys.
    Falls back to common support prefixes if env var is not set.
    """
    if support_project_key is None:
        support_project_key = os.environ.get("SUPPORT_PROJECT_KEY", "")

    linked = issue.get("linked_issues", {}) or issue.get("linked_counts", {})
    count = 0
    if isinstance(linked, dict):
        for group_label, items in linked.items():
            if isinstance(items, list):
                for item in items:
                    key = item.get("key", "")
                    if support_project_key:
                        if key.startswith(support_project_key + "-"):
                            count += 1
                    else:
                        if re.match(r"^(SUP|SUPPORT|CS)-", key, re.IGNORECASE):
                            count += 1
            elif isinstance(items, int):
                # From Obsidian parsed counts — can't distinguish by project key
                pass
    return count


# Link types that indicate active development or resolution work
DEV_LINK_TYPES = {"blocks", "is implemented by", "implements", "is caused by"}


def assess_resolution(issue):
    """Assess the current resolution status of a root cause issue.

    Returns a dict with:
      - resolution: short label (unresolved, in_progress, resolved, roadmapped, rejected)
      - resolution_detail: human-readable summary line
      - resolution_outline: list of strings describing what was/is being done
      - dev_links: list of linked dev/implementation tickets
    """
    board_column = issue.get("board_column", "").lower()
    status = issue.get("status", "").lower()
    jira_resolution = issue.get("resolution", "")  # e.g., "Done", "Won't Do", "Duplicate"

    # Find linked development tickets
    dev_links = []
    linked = issue.get("linked_issues", {})
    if isinstance(linked, dict):
        for group_label, items in linked.items():
            if group_label.lower() not in DEV_LINK_TYPES:
                continue
            if not isinstance(items, list):
                continue
            for item in items:
                dev_links.append({
                    "key": item.get("key", ""),
                    "summary": item.get("summary", ""),
                    "status": item.get("status", ""),
                    "issue_type": item.get("issue_type", ""),
                    "relationship": group_label,
                })

    # Determine resolution status from board column + status
    if board_column in ("completed / roadmapped",):
        resolution = "resolved"
        detail = "Resolved"
        if jira_resolution:
            detail += " (%s)" % jira_resolution
    elif board_column == "rejected":
        resolution = "rejected"
        detail = "Rejected"
        if jira_resolution:
            detail += " (%s)" % jira_resolution
    elif board_column == "in progress" or status in ("in progress", "in review"):
        resolution = "in_progress"
        detail = "In Progress"
    elif board_column == "ready for development" or status == "planned":
        resolution = "roadmapped"
        detail = "Ready for Development"
    elif board_column == "more info required":
        resolution = "blocked"
        detail = "More Info Required"
    else:
        resolution = "unresolved"
        detail = "To Triage"

    # Build resolution outline — what was/is being done
    outline = []

    # Subtasks describe the actual implementation work
    subtasks = issue.get("subtasks", [])
    if subtasks:
        for st in subtasks:
            st_summary = st.get("summary", "")
            st_status = st.get("status", "")
            if st_summary:
                status_tag = " (%s)" % st_status if st_status else ""
                outline.append("Subtask: %s%s" % (st_summary, status_tag))

    # Dev links describe related implementation/blocking work
    if dev_links:
        for d in dev_links:
            outline.append("%s: %s — %s (%s)" % (
                d["relationship"].title(), d["key"], d["summary"][:80], d["status"],
            ))

    # Labels can indicate team ownership or categorisation
    labels = issue.get("labels", [])
    team_labels = [l for l in labels if l.startswith("team-")]
    if team_labels:
        outline.append("Team: %s" % ", ".join(team_labels))

    # Enrich detail line with dev link summary
    if dev_links:
        active = [d for d in dev_links if d["status"].lower() not in CLOSED_STATUSES]
        closed = [d for d in dev_links if d["status"].lower() in CLOSED_STATUSES]
        parts = []
        if active:
            keys = ", ".join(d["key"] for d in active)
            parts.append("%d active dev ticket(s): %s" % (len(active), keys))
        if closed:
            keys = ", ".join(d["key"] for d in closed)
            parts.append("%d closed dev ticket(s): %s" % (len(closed), keys))
        if parts:
            detail += " — " + "; ".join(parts)

    return {
        "resolution": resolution,
        "resolution_detail": detail,
        "resolution_outline": outline,
        "dev_links": dev_links,
    }


def load_enrichment_data(enrich_dir="/tmp/triage_enrich"):
    """Load enrichment results (classification, root cause analysis) keyed by issue key."""
    enrichments = {}
    if not os.path.isdir(enrich_dir):
        return enrichments
    for filename in os.listdir(enrich_dir):
        if filename.startswith("result_") and filename.endswith(".json"):
            filepath = os.path.join(enrich_dir, filename)
            try:
                with open(filepath) as f:
                    data = json.load(f)
                enrichments[data["key"]] = data
            except Exception:
                pass
    return enrichments


def summarize_linked_issues(issue):
    """Build a compact summary of linked issue activity."""
    linked = issue.get("linked_issues", {})
    if not isinstance(linked, dict):
        return []
    parts = []
    for group_label, items in linked.items():
        if not isinstance(items, list) or not items:
            continue
        # Skip dev link types — those are shown separately
        if group_label.lower() in DEV_LINK_TYPES:
            continue
        statuses = {}
        for item in items:
            s = item.get("status", "Unknown")
            statuses[s] = statuses.get(s, 0) + 1
        status_summary = ", ".join("%d %s" % (v, k) for k, v in sorted(statuses.items(), key=lambda x: -x[1]))
        parts.append("%s: %d (%s)" % (group_label.title(), len(items), status_summary))
    return parts


def main():
    args = parse_args()
    output_path = os.environ.get("TRIAGE_OUTPUT_PATH", "")
    if not output_path:
        print("ERROR: TRIAGE_OUTPUT_PATH not set")
        sys.exit(1)

    # Try loading from /tmp/triage_collect/ first (richer data), fall back to Obsidian
    issues_dir = os.path.join(output_path, "Issues")
    collect_dir = "/tmp/triage_collect"

    if os.path.isdir(collect_dir) and os.listdir(collect_dir):
        print("Loading issues from %s..." % collect_dir, file=sys.stderr)
        all_issues = load_issues_from_json(collect_dir)
    elif os.path.isdir(issues_dir):
        print("Loading issues from %s..." % issues_dir, file=sys.stderr)
        all_issues = load_issues_from_obsidian(issues_dir)
    else:
        print("ERROR: No collected data found. Run collect mode first.")
        sys.exit(1)

    if not all_issues:
        print("No issues found.")
        sys.exit(0)

    print("Loaded %d issues" % len(all_issues), file=sys.stderr)

    # Load enrichment data for classification in stdout summary
    enrichments = load_enrichment_data()
    if enrichments:
        print("Loaded %d enrichment results" % len(enrichments), file=sys.stderr)

    # Filter issues for analysis
    if args.issue:
        target_issues = [i for i in all_issues if i["key"] == args.issue]
        if not target_issues:
            print("ERROR: Issue %s not found in collected data" % args.issue)
            sys.exit(1)
    elif args.all_statuses or args.status is None:
        target_issues = all_issues
    else:
        target_issues = [i for i in all_issues if i.get("status", "").lower() == args.status.lower()]

    if not target_issues:
        print("No issues match the filter (status: %s). Use --all-statuses to analyze everything." % args.status)
        sys.exit(0)

    print("Analyzing %d issues (comparing against %d total)...\n" % (len(target_issues), len(all_issues)), file=sys.stderr)

    # Analyze each issue
    results = []
    for issue in target_issues:
        key = issue["key"]
        description = issue.get("description", "")
        summary = issue.get("summary", "")

        section_results, missing_sections = analyze_description(description)
        filled_count = sum(1 for v in section_results.values() if v)

        dup_key, dup_score, rec_key, rec_score = find_duplicates(issue, all_issues)

        linked_count = issue.get("linked_issue_count", 0)
        support_count = count_support_links(issue)

        resolution_info = assess_resolution(issue)

        # Summarize linked issues for richer context
        linked_summary_parts = summarize_linked_issues(issue)

        # Determine recommendation
        if dup_key:
            recommendation = "duplicate"
        elif filled_count >= 4:
            recommendation = "ready"
        else:
            recommendation = "more_info"

        results.append({
            "key": key,
            "summary": summary,
            "description": description[:800],
            "status": issue.get("status", ""),
            "issue_type": issue.get("issue_type", ""),
            "created": issue.get("created", ""),
            "filled_count": filled_count,
            "total_sections": len(TEMPLATE_SECTIONS),
            "missing_sections": missing_sections,
            "section_results": section_results,
            "recommendation": recommendation,
            "duplicate_of": dup_key,
            "duplicate_score": round(dup_score, 2),
            "recurrence_of": rec_key,
            "recurrence_score": round(rec_score, 2),
            "linked_issue_count": linked_count,
            "linked_support_count": support_count,
            "linked_summary_parts": linked_summary_parts,
            "resolution": resolution_info["resolution"],
            "resolution_detail": resolution_info["resolution_detail"],
            "resolution_outline": resolution_info["resolution_outline"],
            "dev_links": resolution_info["dev_links"],
        })

    # Output summary table to stdout
    print("| # | Key | Type | Summary | Score | Resolution | Recommendation | Signals |")
    print("|---|-----|------|---------|-------|------------|----------------|---------|")
    for i, r in enumerate(results, 1):
        summary_short = r["summary"][:50] + ("..." if len(r["summary"]) > 50 else "")

        if r["recommendation"] == "duplicate":
            rec_display = "Duplicate of %s [%.0f%%]" % (r["duplicate_of"], r["duplicate_score"] * 100)
        elif r["recommendation"] == "ready":
            rec_display = "Ready"
        else:
            rec_display = "More Info Required"

        signals = []
        if r.get("recurrence_of"):
            signals.append("Recurrence? %s (%.0f%%)" % (r["recurrence_of"], r["recurrence_score"] * 100))
        if r.get("linked_issue_count", 0) > 0:
            signals.append("%d links" % r["linked_issue_count"])
        if r.get("linked_support_count", 0) > 0:
            signals.append("%d support" % r["linked_support_count"])
        signals_str = ", ".join(signals) if signals else ""

        print("| %d | %s | %s | %s | %d/%d | %s | %s | %s |" % (
            i, r["key"], r.get("issue_type", ""), summary_short,
            r["filled_count"], r["total_sections"],
            r.get("resolution_detail", ""), rec_display, signals_str,
        ))

    # Summary counts
    ready = sum(1 for r in results if r["recommendation"] == "ready")
    more_info = sum(1 for r in results if r["recommendation"] == "more_info")
    duplicates = sum(1 for r in results if r["recommendation"] == "duplicate")

    # Resolution breakdown
    resolution_counts = {}
    for r in results:
        res = r.get("resolution", "unresolved")
        resolution_counts[res] = resolution_counts.get(res, 0) + 1

    # Classification breakdown (from enrichment data)
    classification_counts = {}
    for r in results:
        enrichment = enrichments.get(r["key"], {})
        cls = enrichment.get("classification", "unknown")
        classification_counts[cls] = classification_counts.get(cls, 0) + 1

    print("\nSummary: %d ready, %d need more info, %d duplicates" % (ready, more_info, duplicates))
    print("Resolution: %s" % ", ".join(
        "%d %s" % (v, k) for k, v in sorted(resolution_counts.items(), key=lambda x: -x[1])
    ))
    if classification_counts:
        print("Classification: %s" % ", ".join(
            "%d %s" % (v, k.replace("_", " ")) for k, v in sorted(classification_counts.items(), key=lambda x: -x[1])
        ))

    # Write JSON output — used by build_prompts.py and report.py
    with open("/tmp/triage_analysis.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nAnalysis saved to /tmp/triage_analysis.json", file=sys.stderr)


if __name__ == "__main__":
    main()
