#!/usr/bin/env python3
"""Generate Raw Analysis and Enriched Analysis reports from merged data.

Reads the enriched analysis JSON and duplicate clusters, renders complete
Obsidian Markdown reports. No placeholders, no partial writes.

Usage:
    python3 report.py [--dry-run]

Inputs:
    /tmp/triage_analysis_enriched.json  — merged issue data with agent assessments
    /tmp/triage_duplicates/clusters.json — semantic duplicate/related clusters

Outputs:
    {TRIAGE_OUTPUT_PATH}/Analysis/Raw Analysis - {YYYY-MM-DD}.md
    {TRIAGE_OUTPUT_PATH}/Analysis/Enriched Analysis - {YYYY-MM-DD}.md
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone


ENRICHED_PATH = "/tmp/triage_analysis_enriched.json"
CLUSTERS_PATH = "/tmp/triage_duplicates/clusters.json"

ENV_KEYS = ["TRIAGE_OUTPUT_PATH", "JIRA_BASE_URL"]

CLOSED_STATUSES = {"done", "closed", "completed", "resolved", "completed / roadmapped", "rejected"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """Return an Obsidian wiki-link: [[KEY --- summary|KEY]]."""
    safe_summary = sanitize_filename(summary)
    return "[[%s — %s|%s]]" % (key, safe_summary, key)


def truncate(text, max_len=80):
    """Truncate text to max_len, adding ellipsis if needed."""
    if not text:
        return "--"
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3].rstrip() + "..."


def parse_args():
    parser = argparse.ArgumentParser(description="Generate analysis reports")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report paths without writing files")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_enriched():
    if not os.path.exists(ENRICHED_PATH):
        print("ERROR: %s not found. Run merge_results.py first." % ENRICHED_PATH)
        sys.exit(1)
    with open(ENRICHED_PATH) as f:
        return json.load(f)


def load_clusters():
    if not os.path.exists(CLUSTERS_PATH):
        return []
    with open(CLUSTERS_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Counting / ranking helpers
# ---------------------------------------------------------------------------

def resolution_breakdown(issues):
    counts = {}
    for iss in issues:
        res = iss.get("resolution", "unresolved")
        counts[res] = counts.get(res, 0) + 1
    return counts


def classification_breakdown(issues):
    counts = {}
    for iss in issues:
        cls = iss.get("classification") or "unknown"
        counts[cls] = counts.get(cls, 0) + 1
    return counts


def quality_counts(issues, field="quality"):
    counts = {"good": 0, "thin": 0, "vague": 0}
    for iss in issues:
        val = iss.get(field)
        if val in counts:
            counts[val] += 1
    return counts


def issue_age_days(iss):
    """Return age in days from created date to now."""
    created = iss.get("created", "")
    if not created:
        return 0
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).days
    except (ValueError, TypeError):
        return 0


def rank_top10(issues, clusters, use_enrichment=False):
    """Rank ready issues by value signals. Returns top 10 with reasoning."""
    cluster_keys = set()
    for c in clusters:
        for k in c.get("duplicates", []):
            cluster_keys.add(k)
        for k in c.get("tickets", []):
            cluster_keys.add(k)
        if c.get("primary"):
            cluster_keys.add(c["primary"])

    scored = []
    for iss in issues:
        # Only rank issues that are ready (raw or post-enrichment)
        if use_enrichment:
            action = iss.get("post_enrich_action") or iss.get("recommended_action", "")
        else:
            action = iss.get("recommended_action", "")
        if action not in ("ready",):
            continue

        key = iss["key"]
        links = iss.get("linked_issue_count", 0)
        support = iss.get("linked_support_count", 0)
        age = issue_age_days(iss)
        in_cluster = 1 if key in cluster_keys else 0

        reasons = []
        score = 0.0

        # Link count — wider impact
        score += min(links, 10) * 2
        if links > 0:
            reasons.append("%d linked issues" % links)

        # Support tickets — direct user pain
        score += support * 5
        if support > 0:
            reasons.append("%d support tickets" % support)

        # Age — long-standing problems
        age_score = min(age / 30, 12)  # cap at 12 months
        score += age_score
        if age > 90:
            reasons.append("%d days old" % age)

        # Cluster membership — addressing one helps many
        score += in_cluster * 3
        if in_cluster:
            reasons.append("part of duplicate/related cluster")

        # Board column — already triaged toward development
        resolution = iss.get("resolution", "")
        if resolution == "roadmapped":
            score += 2
            reasons.append("already roadmapped")

        # Enrichment bonuses
        if use_enrichment:
            raw_q = iss.get("quality", "")
            post_q = iss.get("post_enrich_quality", "")
            if raw_q in ("thin", "vague") and post_q == "good":
                score += 3
                reasons.append("quality upgraded by enrichment")

            cls = iss.get("classification", "")
            if cls and "bug" in cls.lower():
                score += 1
                reasons.append("classified as %s" % cls.replace("_", " "))

        if not reasons:
            reasons.append("baseline signals only")

        scored.append((score, iss, "; ".join(reasons)))

    scored.sort(key=lambda x: -x[0])
    return scored[:10]


# ---------------------------------------------------------------------------
# Raw Analysis report
# ---------------------------------------------------------------------------

def build_raw_report(issues, clusters, base_url):
    now = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now().strftime("%Y-%m-%d")

    res_counts = resolution_breakdown(issues)
    q_counts = quality_counts(issues, "quality")
    ready = sum(1 for i in issues if i.get("recommended_action") == "ready")
    more_info = sum(1 for i in issues if i.get("recommended_action") == "more_info")
    duplicates = sum(1 for i in issues if i.get("recommended_action") == "duplicate")
    skip = sum(1 for i in issues if i.get("recommended_action") == "skip")

    lines = [
        "---",
        "type: raw-analysis",
        "generated_at: %s" % now,
        "total_analyzed: %d" % len(issues),
        "---",
        "",
        "# Raw Analysis — %s" % date_str,
        "",
        "## Summary",
        "",
        "- **%d** issues analyzed" % len(issues),
        "",
    ]

    # Resolution breakdown
    lines.append("**By resolution:**")
    for k, v in sorted(res_counts.items(), key=lambda x: -x[1]):
        lines.append("- %s: %d" % (k, v))
    lines.append("")

    # Quality breakdown
    lines.append("**By quality (raw):**")
    for k in ("good", "thin", "vague"):
        lines.append("- %s: %d" % (k, q_counts[k]))
    lines.append("")

    # Recommendation breakdown
    lines.append("**By recommendation:**")
    lines.append("- Ready for development: %d" % ready)
    lines.append("- Needs more information: %d" % more_info)
    if duplicates:
        lines.append("- Duplicate: %d" % duplicates)
    if skip:
        lines.append("- Skip: %d" % skip)
    lines.append("")

    # Quality Assessment table
    lines.append("## Quality Assessment")
    lines.append("")
    lines.append("| Key | Quality | Note | Dup Assessment | Recurrence | Action |")
    lines.append("|-----|---------|------|----------------|------------|--------|")
    for iss in issues:
        note = truncate(iss.get("quality_note"), 80)
        dup_a = iss.get("duplicate_assessment", "n/a") or "n/a"
        rec_a = iss.get("recurrence_assessment", "n/a") or "n/a"
        action = iss.get("recommended_action", "--") or "--"
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            jira_link(iss["key"], base_url),
            iss.get("quality", "--") or "--",
            note, dup_a, rec_a, action,
        ))
    lines.append("")

    # Issues flagged as thin or vague
    flagged = [i for i in issues if i.get("quality") in ("thin", "vague")]
    if flagged:
        lines.append("### Issues Flagged as Thin or Vague")
        lines.append("")
        for iss in flagged:
            note = iss.get("quality_note") or "No detail provided"
            lines.append("- **%s** (%s): %s" % (
                jira_link(iss["key"], base_url), iss.get("quality", ""), note))
        lines.append("")

    # Duplicate & Overlap — text-similarity from structural analysis
    lines.append("## Duplicate & Overlap Analysis")
    lines.append("")
    dup_issues = [i for i in issues if i.get("duplicate_of")]
    lines.append("### Text-Similarity Duplicates")
    lines.append("")
    if dup_issues:
        lines.append("| Issue | Duplicate Of | Match |")
        lines.append("|-------|-------------|-------|")
        for iss in dup_issues:
            lines.append("| %s | %s | %.0f%% |" % (
                jira_link(iss["key"], base_url),
                jira_link(iss["duplicate_of"], base_url),
                iss.get("duplicate_score", 0) * 100,
            ))
        lines.append("")
    else:
        lines.append("No text-similarity duplicates detected.")
        lines.append("")

    lines.append("### Related Clusters")
    lines.append("")
    lines.append("*See Enriched Analysis for semantic cluster analysis.*")
    lines.append("")

    # Needs More Information
    more_info_group = [i for i in issues if i.get("recommended_action") == "more_info"]
    lines.append("## Needs More Information (%d)" % len(more_info_group))
    lines.append("")
    lines.extend(_build_issue_table(more_info_group, base_url))

    # Ready for Development
    ready_group = [i for i in issues if i.get("recommended_action") == "ready"]
    lines.append("## Ready for Development (%d)" % len(ready_group))
    lines.append("")
    lines.extend(_build_issue_table(ready_group, base_url))

    # Top 10
    lines.append("## Top 10 Highest Value Ready Issues")
    lines.append("")
    top10 = rank_top10(issues, clusters, use_enrichment=False)
    if top10:
        lines.append("| # | Key | Summary | Links | Support | Age | Reasoning |")
        lines.append("|---|-----|---------|-------|---------|-----|-----------|")
        for rank, (score, iss, reasoning) in enumerate(top10, 1):
            summary_short = truncate(iss["summary"], 55)
            age = issue_age_days(iss)
            lines.append("| %d | %s | %s | %d | %d | %dd | %s |" % (
                rank, jira_link(iss["key"], base_url), summary_short,
                iss.get("linked_issue_count", 0), iss.get("linked_support_count", 0),
                age, truncate(reasoning, 80),
            ))
        lines.append("")
    else:
        lines.append("No ready issues to rank.")
        lines.append("")

    return "\n".join(lines)


def _build_issue_table(group, base_url):
    """Build a summary table for a group of issues with wiki-links."""
    if not group:
        return ["No issues in this group.", ""]
    lines = []
    lines.append("| Key | Summary | Score | Created | Links | Detail |")
    lines.append("|-----|---------|-------|---------|-------|--------|")
    for iss in group:
        summary_short = truncate(iss.get("summary", ""), 60)
        created = iss.get("created", "")[:10]
        lines.append("| %s | %s | %d/%d | %s | %d | %s |" % (
            jira_link(iss["key"], base_url), summary_short,
            iss.get("filled_count", 0), iss.get("total_sections", 5),
            created, iss.get("linked_issue_count", 0),
            wiki_link(iss["key"], iss.get("summary", "")),
        ))
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Enriched Analysis report
# ---------------------------------------------------------------------------

def build_enriched_report(issues, clusters, base_url):
    now = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now().strftime("%Y-%m-%d")

    res_counts = resolution_breakdown(issues)
    cls_counts = classification_breakdown(issues)
    raw_q = quality_counts(issues, "quality")
    post_q = quality_counts(issues, "post_enrich_quality")
    upgrades = sum(1 for i in issues
                   if i.get("quality") in ("thin", "vague")
                   and i.get("post_enrich_quality") == "good")

    enrichment_count = sum(1 for i in issues if i.get("classification"))
    autofill_count = sum(1 for i in issues if i.get("post_enrich_quality"))

    lines = [
        "---",
        "type: enriched-analysis",
        "generated_at: %s" % now,
        "total_analyzed: %d" % len(issues),
        "enrichment_count: %d" % enrichment_count,
        "---",
        "",
        "# Enriched Analysis — %s" % date_str,
        "",
        "## Summary",
        "",
        "- **%d** issues analyzed" % len(issues),
        "- **%d** enrichment results available" % enrichment_count,
        "",
    ]

    # Resolution breakdown
    lines.append("**By resolution:**")
    for k, v in sorted(res_counts.items(), key=lambda x: -x[1]):
        lines.append("- %s: %d" % (k, v))
    lines.append("")

    # Classification breakdown
    if cls_counts:
        lines.append("**By classification:**")
        for k, v in sorted(cls_counts.items(), key=lambda x: -x[1]):
            lines.append("- %s: %d" % (k.replace("_", " "), v))
        lines.append("")

    # Post-enrichment recommendation counts
    post_ready = sum(1 for i in issues if i.get("post_enrich_action") == "ready")
    post_more = sum(1 for i in issues if i.get("post_enrich_action") == "more_info")
    post_dup = sum(1 for i in issues if i.get("post_enrich_action") == "duplicate")
    post_skip = sum(1 for i in issues if i.get("post_enrich_action") == "skip")

    lines.append("**By recommendation (post-enrichment):**")
    lines.append("- Good (ready for development): %d" % post_ready)
    lines.append("- Thin (needs more information): %d" % post_more)
    if post_dup:
        lines.append("- Duplicate: %d" % post_dup)
    if post_skip:
        lines.append("- Skip: %d" % post_skip)
    lines.append("")

    # Raw vs Enriched Comparison
    lines.append("### Raw vs Enriched Comparison")
    lines.append("")
    lines.append("| Metric | Raw Assessment | Post-Enrichment |")
    lines.append("|--------|---------------|-----------------|")
    for k in ("good", "thin", "vague"):
        lines.append("| %s | %d | %d |" % (k.title(), raw_q[k], post_q[k]))
    lines.append("")
    lines.append("Enrichment upgraded **%d** issues from vague/thin to good." % upgrades)
    lines.append("")

    # Post-Enrichment Quality Assessment table
    lines.append("## Quality Assessment")
    lines.append("")
    lines.append("| Key | Raw Quality | Post-Enrichment | Note | Action |")
    lines.append("|-----|-------------|-----------------|------|--------|")
    for iss in issues:
        raw_val = iss.get("quality", "--") or "--"
        post_val = iss.get("post_enrich_quality", "--") or "--"
        # Mark upgrades with arrow
        upgrade_marker = ""
        if raw_val in ("thin", "vague") and post_val == "good":
            upgrade_marker = " **\u2191**"
        note = truncate(iss.get("post_enrich_note"), 80)
        action = iss.get("post_enrich_action", "--") or "--"
        lines.append("| %s | %s | %s%s | %s | %s |" % (
            jira_link(iss["key"], base_url), raw_val, post_val,
            upgrade_marker, note, action,
        ))
    lines.append("")

    # Duplicate & Overlap Analysis
    lines.append("## Duplicate & Overlap Analysis")
    lines.append("")

    # Confirmed Duplicates (from A2c semantic analysis)
    dup_clusters = [c for c in clusters if c.get("type") == "duplicate"]
    lines.append("### Confirmed Duplicates")
    lines.append("")
    if dup_clusters:
        lines.append("| Primary | Duplicate(s) | Rationale |")
        lines.append("|---------|-------------|-----------|")
        for c in dup_clusters:
            primary = jira_link(c.get("primary", ""), base_url)
            dups = ", ".join(jira_link(k, base_url) for k in c.get("duplicates", []))
            rationale = truncate(c.get("rationale", ""), 80)
            lines.append("| %s | %s | %s |" % (primary, dups, rationale))
        lines.append("")
    else:
        lines.append("No semantic duplicates identified.")
        lines.append("")

    # Related Clusters
    rel_clusters = [c for c in clusters if c.get("type") == "related"]
    lines.append("### Related Clusters")
    lines.append("")
    if rel_clusters:
        for c in rel_clusters:
            theme = c.get("theme", "Untitled")
            tickets = ", ".join(jira_link(k, base_url) for k in c.get("tickets", []))
            rationale = c.get("rationale", "")
            lines.append("**%s:** %s" % (theme, tickets))
            if rationale:
                lines.append("*%s*" % rationale)
            lines.append("")
    else:
        lines.append("No related clusters identified.")
        lines.append("")

    # Needs More Information (post-enrichment)
    more_info_group = [i for i in issues if i.get("post_enrich_action") == "more_info"]
    lines.append("## Needs More Information (%d)" % len(more_info_group))
    lines.append("")
    lines.extend(_build_issue_table(more_info_group, base_url))

    # Ready for Development (post-enrichment)
    ready_group = [i for i in issues if i.get("post_enrich_action") == "ready"]
    lines.append("## Ready for Development (%d)" % len(ready_group))
    lines.append("")
    lines.extend(_build_issue_table(ready_group, base_url))

    # Top 10 (enriched ranking)
    lines.append("## Top 10 Highest Value Ready Issues")
    lines.append("")
    top10 = rank_top10(issues, clusters, use_enrichment=True)
    if top10:
        lines.append("| # | Key | Summary | Classification | Links | Support | Age | Reasoning |")
        lines.append("|---|-----|---------|---------------|-------|---------|-----|-----------|")
        for rank, (score, iss, reasoning) in enumerate(top10, 1):
            summary_short = truncate(iss["summary"], 50)
            cls = (iss.get("classification") or "").replace("_", " ")
            age = issue_age_days(iss)
            lines.append("| %d | %s | %s | %s | %d | %d | %dd | %s |" % (
                rank, jira_link(iss["key"], base_url), summary_short,
                cls, iss.get("linked_issue_count", 0),
                iss.get("linked_support_count", 0), age,
                truncate(reasoning, 80),
            ))
        lines.append("")
    else:
        lines.append("No ready issues to rank.")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    output_path = os.environ.get("TRIAGE_OUTPUT_PATH", "")
    if not output_path:
        print("ERROR: TRIAGE_OUTPUT_PATH not set")
        sys.exit(1)

    base_url = os.environ.get("JIRA_BASE_URL", "")

    issues = load_enriched()
    clusters = load_clusters()

    print("Loaded %d issues, %d clusters" % (len(issues), len(clusters)))

    analysis_dir = os.path.join(output_path, "Analysis")
    date_str = datetime.now().strftime("%Y-%m-%d")

    raw_path = os.path.join(analysis_dir, "Raw Analysis - %s.md" % date_str)
    enriched_path = os.path.join(analysis_dir, "Enriched Analysis - %s.md" % date_str)

    raw_content = build_raw_report(issues, clusters, base_url)
    enriched_content = build_enriched_report(issues, clusters, base_url)

    if args.dry_run:
        print("\n[dry-run] Would write:")
        print("  %s (%d lines)" % (raw_path, raw_content.count("\n")))
        print("  %s (%d lines)" % (enriched_path, enriched_content.count("\n")))
        return

    os.makedirs(analysis_dir, exist_ok=True)

    with open(raw_path, "w") as f:
        f.write(raw_content)
    print("Raw report written to %s" % raw_path)

    with open(enriched_path, "w") as f:
        f.write(enriched_content)
    print("Enriched report written to %s" % enriched_path)


if __name__ == "__main__":
    main()
