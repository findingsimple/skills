#!/usr/bin/env python3
"""Analyze collected root cause issues from Obsidian Markdown files.

Produces two reports:
- Raw Analysis — structural scoring from Jira data only (no enrichment)
- Enriched Analysis — full picture with classification, root cause, and autofill
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone


ENV_KEYS = ["TRIAGE_OUTPUT_PATH", "JIRA_BASE_URL"]

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
    """Extract content of a named section from a description, up to 400 chars."""
    if not text:
        return ""
    pattern = r"(?i)(?:^|\n)\s*(?:h\d\.\s*|\*+\s*|#+\s*)?(?:\S+\s+)?" + re.escape(section_name)
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


def count_support_links(issue):
    """Count linked issues that look like support tickets."""
    linked = issue.get("linked_issues", {}) or issue.get("linked_counts", {})
    count = 0
    if isinstance(linked, dict):
        for group_label, items in linked.items():
            if isinstance(items, list):
                for item in items:
                    key = item.get("key", "")
                    # Support tickets typically have project keys like SUP, SUPPORT, CS, etc.
                    if re.match(r"^(SUP|SUPPORT|CS|COPS)-", key, re.IGNORECASE):
                        count += 1
            elif isinstance(items, int):
                # From Obsidian parsed counts — can't distinguish, just note the label
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


def jira_link(key, base_url):
    """Return a Markdown hyperlink for a Jira issue key."""
    if base_url:
        return "[%s](%s/browse/%s)" % (key, base_url, key)
    return key


def sanitize_filename(s, max_len=80):
    """Sanitize a string for use in filenames (matches summarize.py)."""
    s = re.sub(r'[/\\:*?"<>|]', '-', s)
    if len(s) > max_len:
        s = s[:max_len].rstrip()
    return s


def wiki_link(key, summary):
    """Return an Obsidian wiki-link for an issue: [[KEY — summary|KEY]]."""
    safe_summary = sanitize_filename(summary)
    return "[[%s — %s|%s]]" % (key, safe_summary, key)


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


def load_autofill_data(autofill_dir="/tmp/triage_autofill"):
    """Load autofill results keyed by issue key."""
    autofills = {}
    if not os.path.isdir(autofill_dir):
        return autofills
    for filename in os.listdir(autofill_dir):
        if filename.startswith("result_") and filename.endswith(".json"):
            filepath = os.path.join(autofill_dir, filename)
            try:
                with open(filepath) as f:
                    data = json.load(f)
                autofills[data["key"]] = data
            except Exception:
                pass
    return autofills


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


def build_summary_table(group, base_url):
    """Build a compact summary table for a group of issues with Obsidian wiki-links."""
    lines = []
    lines.append("| Key | Summary | Score | Created | Links | Detail |")
    lines.append("|-----|---------|-------|---------|-------|--------|")
    for r in group:
        summary_short = r["summary"][:60] + ("..." if len(r["summary"]) > 60 else "")
        created = r.get("created", "")[:10]
        lines.append("| %s | %s | %d/%d | %s | %d | %s |" % (
            jira_link(r["key"], base_url), summary_short,
            r["filled_count"], r["total_sections"],
            created, r.get("linked_issue_count", 0),
            wiki_link(r["key"], r["summary"]),
        ))
    lines.append("")
    return lines


def build_recommendations_table(results, base_url, include_classification=False, enrichments=None):
    """Build the recommendations table."""
    lines = []
    if include_classification:
        lines.append("| Key | Summary | Recommendation | Classification | Links | Created |")
        lines.append("|-----|---------|---------------|----------------|-------|---------|")
    else:
        lines.append("| Key | Summary | Recommendation | Links | Created |")
        lines.append("|-----|---------|---------------|-------|---------|")

    for r in results:
        summary_short = r["summary"][:55] + ("..." if len(r["summary"]) > 55 else "")
        created = r.get("created", "")[:10]

        if r.get("duplicate_of"):
            rec_display = "Merge with %s" % r["duplicate_of"]
        elif r["recommendation"] == "ready":
            rec_display = "Ready"
        else:
            rec_display = "More info needed"

        if include_classification and enrichments:
            enrichment = enrichments.get(r["key"], {})
            cls = enrichment.get("classification", "").replace("_", " ") if enrichment else ""
            lines.append("| %s | %s | %s | %s | %d | %s |" % (
                jira_link(r["key"], base_url), summary_short,
                rec_display, cls, r.get("linked_issue_count", 0), created,
            ))
        else:
            lines.append("| %s | %s | %s | %d | %s |" % (
                jira_link(r["key"], base_url), summary_short,
                rec_display, r.get("linked_issue_count", 0), created,
            ))

    lines.append("")
    return lines


def write_raw_report(report_path, results, all_issues, resolution_counts,
                     ready_count, more_info_count, duplicate_count, base_url, args):
    """Write the Raw Analysis report (Jira data only, no enrichment)."""
    now = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now().strftime("%Y-%m-%d")

    lines = [
        "---",
        "type: raw-analysis",
        "generated_at: %s" % now,
        "total_analyzed: %d" % len(results),
        "status_filter: %s" % (args.status if not args.all_statuses else "all"),
        "---",
        "",
        "# Raw Analysis — %s" % date_str,
        "",
        "## Summary",
        "",
        "- **%d** issues analyzed (from %d total collected)" % (len(results), len(all_issues)),
        "",
    ]

    # Resolution breakdown
    lines.append("**By resolution:**")
    for k, v in sorted(resolution_counts.items(), key=lambda x: -x[1]):
        lines.append("- %s: %d" % (k, v))
    lines.append("")

    # Recommendation breakdown
    lines.append("**By recommendation:**")
    lines.append("- Ready for development: %d" % ready_count)
    lines.append("- Needs more information: %d" % more_info_count)
    lines.append("- Potential duplicates (text similarity): %d" % duplicate_count)
    lines.append("")

    # Quality Assessment placeholder
    lines.append("## Quality Assessment")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:RAW_QUALITY — filled by Step A2a -->")
    lines.append("")

    # Duplicate & Overlap Analysis — text-similarity results
    lines.append("## Duplicate & Overlap Analysis")
    lines.append("")

    # Confirmed duplicates from jaccard
    dup_results = [r for r in results if r.get("duplicate_of")]
    lines.append("### Confirmed Duplicates (Text Similarity)")
    lines.append("")
    if dup_results:
        lines.append("| Issue | Duplicate Of | Match |")
        lines.append("|-------|-------------|-------|")
        for r in dup_results:
            lines.append("| %s | %s | %.0f%% |" % (
                jira_link(r["key"], base_url),
                jira_link(r["duplicate_of"], base_url),
                r["duplicate_score"] * 100,
            ))
        lines.append("")
    else:
        lines.append("No text-similarity duplicates detected above %.0f%% threshold." % (DUPLICATE_THRESHOLD * 100))
        lines.append("")

    lines.append("### Related Clusters")
    lines.append("")
    lines.append("*See Enriched Analysis for semantic cluster analysis using enrichment data.*")
    lines.append("")

    # Group issues by recommendation (excluding duplicate as a separate group)
    more_info_group = [r for r in results if r["recommendation"] == "more_info"]
    ready_group = [r for r in results if r["recommendation"] == "ready"]
    # Issues flagged as duplicates still appear in more_info or ready based on score
    for r in results:
        if r["recommendation"] == "duplicate":
            if r["filled_count"] >= 4:
                ready_group.append(r)
            else:
                more_info_group.append(r)

    # Needs More Information section
    lines.append("## Needs More Information (%d)" % len(more_info_group))
    lines.append("")
    lines.extend(build_summary_table(more_info_group, base_url))

    # Ready for Development section
    lines.append("## Ready for Development (%d)" % len(ready_group))
    lines.append("")
    lines.extend(build_summary_table(ready_group, base_url))

    # Recommendations table
    lines.append("## Recommendations")
    lines.append("")
    lines.extend(build_recommendations_table(results, base_url))

    # Top 10 placeholder
    lines.append("## Top 10 Highest Value Ready Issues")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:RAW_TOP10 — filled by Step A3 -->")
    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


def write_enriched_report(report_path, results, enrichments, autofills, all_issues,
                          resolution_counts, classification_counts,
                          ready_count, more_info_count, duplicate_count, base_url, args):
    """Write the Enriched Analysis report (full picture with enrichment + autofill)."""
    now = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now().strftime("%Y-%m-%d")

    lines = [
        "---",
        "type: enriched-analysis",
        "generated_at: %s" % now,
        "total_analyzed: %d" % len(results),
        "enrichment_count: %d" % len(enrichments),
        "autofill_count: %d" % len(autofills),
        "status_filter: %s" % (args.status if not args.all_statuses else "all"),
        "---",
        "",
        "# Enriched Analysis — %s" % date_str,
        "",
        "## Summary",
        "",
        "- **%d** issues analyzed (from %d total collected)" % (len(results), len(all_issues)),
        "- **%d** enrichment results loaded" % len(enrichments),
        "- **%d** autofill results loaded" % len(autofills),
        "",
    ]

    # Resolution breakdown
    lines.append("**By resolution:**")
    for k, v in sorted(resolution_counts.items(), key=lambda x: -x[1]):
        lines.append("- %s: %d" % (k, v))
    lines.append("")

    # Classification breakdown
    if classification_counts:
        lines.append("**By classification:**")
        for k, v in sorted(classification_counts.items(), key=lambda x: -x[1]):
            lines.append("- %s: %d" % (k.replace("_", " "), v))
        lines.append("")

    # Recommendation breakdown — placeholder replaced by Step A3 with post-enrichment counts
    lines.append("<!-- PLACEHOLDER:ENRICHED_SUMMARY — filled by Step A3 with post-enrichment recommendation counts -->")
    lines.append("")

    # Comparison placeholder
    lines.append("### Raw vs Enriched Comparison")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:ENRICHED_COMPARISON — filled by Step A3 -->")
    lines.append("")

    # Quality Assessment placeholder
    lines.append("## Quality Assessment")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:ENRICHED_QUALITY — filled by Step A2b -->")
    lines.append("")

    # Duplicate & Overlap Analysis placeholder
    lines.append("## Duplicate & Overlap Analysis")
    lines.append("")
    lines.append("### Confirmed Duplicates")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:ENRICHED_DUPLICATES — filled by Step A2c -->")
    lines.append("")
    lines.append("### Related Clusters")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:ENRICHED_CLUSTERS — filled by Step A2c -->")
    lines.append("")

    # Group issues (same logic as raw report)
    more_info_group = [r for r in results if r["recommendation"] == "more_info"]
    ready_group = [r for r in results if r["recommendation"] == "ready"]
    for r in results:
        if r["recommendation"] == "duplicate":
            if r["filled_count"] >= 4:
                ready_group.append(r)
            else:
                more_info_group.append(r)

    # Needs More Information section
    lines.append("## Needs More Information (%d)" % len(more_info_group))
    lines.append("")
    lines.extend(build_summary_table(more_info_group, base_url))

    # Ready for Development section
    lines.append("## Ready for Development (%d)" % len(ready_group))
    lines.append("")
    lines.extend(build_summary_table(ready_group, base_url))

    # Recommendations table (with classification)
    lines.append("## Recommendations")
    lines.append("")
    lines.extend(build_recommendations_table(results, base_url, include_classification=True, enrichments=enrichments))

    # Top 10 placeholder
    lines.append("## Top 10 Highest Value Ready Issues")
    lines.append("")
    lines.append("<!-- PLACEHOLDER:ENRICHED_TOP10 — filled by Step A3 -->")
    lines.append("")

    with open(report_path, "w") as f:
        f.write("\n".join(lines))


def main():
    args = parse_args()
    output_path = os.environ.get("TRIAGE_OUTPUT_PATH", "")
    if not output_path:
        print("ERROR: TRIAGE_OUTPUT_PATH not set")
        sys.exit(1)

    base_url = os.environ.get("JIRA_BASE_URL", "")

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

    # Load enrichment data for classification + root cause analysis
    enrichments = load_enrichment_data()
    if enrichments:
        print("Loaded %d enrichment results" % len(enrichments), file=sys.stderr)

    # Load autofill data for template section fills
    autofills = load_autofill_data()
    if autofills:
        print("Loaded %d autofill results" % len(autofills), file=sys.stderr)

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

    # Write JSON output (always — used by agent quality assessment step)
    with open("/tmp/triage_analysis.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nAnalysis saved to /tmp/triage_analysis.json", file=sys.stderr)

    # Write both analysis reports to Obsidian
    analysis_dir = os.path.join(output_path, "Analysis")
    os.makedirs(analysis_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")

    raw_report_path = os.path.join(analysis_dir, "Raw Analysis - %s.md" % date_str)
    enriched_report_path = os.path.join(analysis_dir, "Enriched Analysis - %s.md" % date_str)

    write_raw_report(raw_report_path, results, all_issues, resolution_counts,
                     ready, more_info, duplicates, base_url, args)
    write_enriched_report(enriched_report_path, results, enrichments, autofills, all_issues,
                          resolution_counts, classification_counts,
                          ready, more_info, duplicates, base_url, args)

    print("\nRaw report written to %s" % raw_report_path, file=sys.stderr)
    print("Enriched report written to %s" % enriched_report_path, file=sys.stderr)


if __name__ == "__main__":
    main()
