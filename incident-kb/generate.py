#!/usr/bin/env python3
"""Read /tmp/incident_kb/ JSON, write per-incident Obsidian markdown + trend/recurrence reports."""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}

# Build month alternation for regex patterns (exclude "may" — too common as English word)
_SAFE_MONTHS = "|".join(k for k in MONTH_MAP if len(k) == 3 and k != "may")

# Patterns ordered from most specific to least specific
_DATE_PATTERNS = [
    # YYYY-MM-DD (standard ISO — most titles)
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),
     lambda m: (m.group(1), m.group(2), m.group(3))),
    # DD Mon YYYY  e.g. "28 Feb 2025" (word boundary before day to avoid matching trailing digits)
    (re.compile(r"\b(\d{1,2})\s+(" + _SAFE_MONTHS + r"|may)\s+(\d{4})\b", re.IGNORECASE),
     lambda m: (m.group(3), MONTH_MAP[m.group(2).lower()], m.group(1))),
    # Mon YYYY  e.g. "Feb 2025" (day defaults to 01; excludes "may" to avoid "this may 2025")
    (re.compile(r"\b(" + _SAFE_MONTHS + r")\s+(\d{4})\b", re.IGNORECASE),
     lambda m: (m.group(2), MONTH_MAP[m.group(1).lower()], "1")),
]

_VALID_YEAR_RANGE = (2000, 2099)


def extract_date_from_title(title):
    """Extract the earliest incident date from a free-form title.

    Returns YYYY-MM-DD string or empty string if no date found.
    Tries ISO dates first, then natural-language month patterns.
    For date ranges, uses the start date. Rejects years outside 2000-2099.
    """
    if not title:
        return ""
    for pattern, extractor in _DATE_PATTERNS:
        match = pattern.search(title)
        if match:
            year, month, day = extractor(match)
            try:
                y = int(year)
                if y < _VALID_YEAR_RANGE[0] or y > _VALID_YEAR_RANGE[1]:
                    continue
                dt = datetime(y, int(month), int(day))
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                continue
    return ""


CACHE_DIR = "/tmp/incident_kb"
CONFLUENCE_DIR = os.path.join(CACHE_DIR, "confluence")
JIRA_DIR = os.path.join(CACHE_DIR, "jira")
CROSS_REF_PATH = os.path.join(CACHE_DIR, "cross_ref.json")
META_PATH = os.path.join(CACHE_DIR, "meta.json")


def parse_args():
    parser = argparse.ArgumentParser(description="Generate incident KB markdown files")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--force", action="store_true", help="Overwrite existing markdown files")
    parser.add_argument("--report-only", action="store_true", help="Only generate trend/recurrence reports")
    parser.add_argument("--team", default="", help="Team name for output path")
    return parser.parse_args()


_LEADING_DATE_RE = re.compile(
    r"^(?:Incident\s*:\s*)?\d{4}-\d{1,2}-\d{1,2}(?:\s+to\s+\d{4}-\d{1,2}-\d{1,2}|\s+to\s+\d{1,2})?(?:\s*[-:]\s*|\s+)")


def sanitize_filename(s, max_len=80):
    """Sanitize a string for use as a filename. Strips leading dates to avoid duplication."""
    s = _LEADING_DATE_RE.sub("", s)
    s = re.sub(r'[<>:"/\\|?*]', "", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip(".")
    if len(s) > max_len:
        s = s[:max_len].rsplit(" ", 1)[0]
    return s or "Untitled"


def load_cached_data():
    """Load all cached incident data from /tmp/incident_kb/."""
    # Load cross-reference
    if not os.path.exists(CROSS_REF_PATH):
        print("ERROR: No cross-reference data found. Run fetch.py first.", file=sys.stderr)
        sys.exit(1)
    with open(CROSS_REF_PATH, "r") as f:
        cross_ref = json.load(f)

    # Load confluence pages
    confluence_pages = {}
    if os.path.isdir(CONFLUENCE_DIR):
        for fname in os.listdir(CONFLUENCE_DIR):
            if fname.endswith(".json"):
                with open(os.path.join(CONFLUENCE_DIR, fname), "r") as f:
                    page = json.load(f)
                confluence_pages[page.get("page_id", fname.replace(".json", ""))] = page

    # Load jira epics
    jira_epics = {}
    if os.path.isdir(JIRA_DIR):
        for fname in os.listdir(JIRA_DIR):
            if fname.endswith(".json"):
                with open(os.path.join(JIRA_DIR, fname), "r") as f:
                    epic = json.load(f)
                jira_epics[epic.get("key", fname.replace(".json", ""))] = epic

    # Load metadata
    meta = {}
    if os.path.exists(META_PATH):
        with open(META_PATH, "r") as f:
            meta = json.load(f)

    return cross_ref, confluence_pages, jira_epics, meta


def build_incident(match, confluence_pages, jira_epics, base_url):
    """Build a unified incident object from a cross-reference match."""
    retro_page_id = match.get("retro_page_id", "")
    epic_key = match.get("epic_key", "")
    inc_key = match.get("inc_key", "")

    retro = confluence_pages.get(retro_page_id, {})
    epic = jira_epics.get(epic_key, {})

    # Title: prefer epic summary, fall back to retro title
    title = epic.get("summary", "") or retro.get("title", "") or "Unknown Incident"

    # Determine date: prefer date extracted from title, fall back to created timestamps
    date = extract_date_from_title(title) or extract_date_from_title(retro.get("title", ""))
    if not date:
        if epic.get("created"):
            date = epic["created"][:10]
        elif retro.get("created_at"):
            date = retro["created_at"][:10]

    # Sections from retro
    sections = retro.get("sections", {})

    # Build retro URL
    retro_url = ""
    if retro_page_id:
        retro_url = "%s/wiki/pages/%s" % (base_url, retro_page_id)

    # Build epic URL
    epic_url = ""
    if epic_key:
        epic_url = "%s/browse/%s" % (base_url, epic_key)

    return {
        "inc_key": inc_key,
        "title": title,
        "date": date,
        "severity": epic.get("severity", ""),
        "status": epic.get("status", ""),
        "has_retro": match.get("has_retro", False),
        "has_epic": match.get("has_epic", False),
        "retro_page_id": retro_page_id,
        "retro_url": retro_url,
        "epic_url": epic_url,
        "retro_title": retro.get("title", ""),
        "labels": list(set((epic.get("labels", []) or []) + (retro.get("labels", []) or []))),
        "reporter": epic.get("reporter", ""),
        "description": epic.get("description_text", ""),
        "children": epic.get("children", []),
        "linked_issues": epic.get("linked_issues", []),
        "remediation_status": epic.get("remediation_status", "unknown"),
        "fix_versions": epic.get("fix_versions", []),
        "sections": sections,
        "body_text": retro.get("body_text", ""),
    }


def build_orphan_retro_incident(orphan, confluence_pages, base_url):
    """Build an incident object from an orphan retro (no Jira epic)."""
    page_id = orphan.get("retro_page_id", "")
    retro = confluence_pages.get(page_id, {})

    title = retro.get("title", orphan.get("retro_title", "")) or "Unknown"
    date = extract_date_from_title(title)
    if not date and retro.get("created_at"):
        date = retro["created_at"][:10]

    retro_url = "%s/wiki/pages/%s" % (base_url, page_id) if page_id else ""

    return {
        "inc_key": "",
        "title": title,
        "date": date,
        "severity": "",
        "status": "",
        "has_retro": True,
        "has_epic": False,
        "retro_page_id": page_id,
        "retro_url": retro_url,
        "epic_url": "",
        "retro_title": retro.get("title", ""),
        "labels": retro.get("labels", []),
        "reporter": "",
        "description": "",
        "children": [],
        "linked_issues": [],
        "remediation_status": "unknown",
        "fix_versions": [],
        "sections": retro.get("sections", {}),
        "body_text": retro.get("body_text", ""),
    }


def build_orphan_epic_incident(orphan, jira_epics, base_url):
    """Build an incident object from an orphan epic (no Confluence retro)."""
    key = orphan.get("epic_key", "")
    epic = jira_epics.get(key, {})

    title = epic.get("summary", orphan.get("epic_summary", "Unknown"))
    date = extract_date_from_title(title)
    if not date and epic.get("created"):
        date = epic["created"][:10]

    return {
        "inc_key": key,
        "title": title,
        "date": date,
        "severity": epic.get("severity", ""),
        "status": epic.get("status", ""),
        "has_retro": False,
        "has_epic": True,
        "retro_page_id": "",
        "retro_url": "",
        "epic_url": "%s/browse/%s" % (base_url, key) if key else "",
        "retro_title": "",
        "labels": epic.get("labels", []),
        "reporter": epic.get("reporter", ""),
        "description": epic.get("description_text", ""),
        "children": epic.get("children", []),
        "linked_issues": epic.get("linked_issues", []),
        "remediation_status": epic.get("remediation_status", "unknown"),
        "fix_versions": epic.get("fix_versions", []),
        "sections": {},
        "body_text": "",
    }


def strip_template_description(text):
    """Strip retro template description prompts from section content.

    Confluence retro templates store sections as "Description text | Actual content |".
    This strips the leading description and trailing pipe delimiters, returning only
    the actual incident-specific content. Multi-row key-value tables (like postmortem
    summary where most lines have pipes) are reformatted as markdown tables.
    """
    if not text or "|" not in text:
        return text

    lines = text.strip().split("\n")
    non_empty = [l for l in lines if l.strip()]
    lines_with_pipe = sum(1 for l in non_empty if "|" in l)

    # Multi-row key-value table: majority of lines have pipes (e.g. postmortem summary)
    if len(non_empty) >= 3 and lines_with_pipe > len(non_empty) * 0.6:
        rows = []
        for line in non_empty:
            if "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                rows.append("| **%s** | %s |" % (parts[0], parts[1]))
            elif len(parts) == 1:
                rows.append("| **%s** | |" % parts[0])
        if rows:
            return "| | |\n|---|---|\n" + "\n".join(rows)

    # Single description | content pattern: strip the template description
    _, _, content = text.partition("|")
    content = content.strip().rstrip("|").strip()
    return content


def format_incident_markdown(incident):
    """Format a single incident as Obsidian markdown with YAML frontmatter."""
    inc_key = incident["inc_key"]
    title = incident["title"]
    date = incident["date"]
    # Strip template description prompts from all sections up front
    sections = {k: strip_template_description(v) for k, v in incident["sections"].items()}

    # Build frontmatter
    fm_lines = ["---"]
    fm_lines.append("type: incident")
    if inc_key:
        fm_lines.append("inc_key: %s" % inc_key)
    if date:
        fm_lines.append("date: %s" % date)
    if incident["severity"]:
        fm_lines.append("severity: %s" % incident["severity"])
    if incident["status"]:
        fm_lines.append("status: %s" % incident["status"])
    if incident["retro_page_id"]:
        fm_lines.append('retro_page_id: "%s"' % incident["retro_page_id"])
    if incident["retro_url"]:
        fm_lines.append("retro_url: %s" % incident["retro_url"])
    if incident["epic_url"]:
        fm_lines.append("epic_url: %s" % incident["epic_url"])
    fm_lines.append("has_retro: %s" % str(incident["has_retro"]).lower())
    fm_lines.append("has_epic: %s" % str(incident["has_epic"]).lower())
    if incident["labels"]:
        quoted_labels = ['"%s"' % l.replace('"', '\\"') for l in incident["labels"]]
        fm_lines.append("labels: [%s]" % ", ".join(quoted_labels))
    fm_lines.append("remediation_status: %s" % incident["remediation_status"])
    fm_lines.append("synced_at: %s" % datetime.now(timezone.utc).isoformat())
    fm_lines.append("---")

    # Build body
    heading = inc_key + " — " + title if inc_key else title
    body_lines = ["# %s" % heading, ""]

    # Summary section
    summary = (
        sections.get("incident summary", "")
        or sections.get("summary", "")
        or sections.get("preamble", "")
        or incident.get("description", "")
    )
    if summary:
        body_lines.append("## Summary")
        body_lines.append("")
        body_lines.append(summary.strip())
        body_lines.append("")

    # Timeline
    timeline = sections.get("timeline", "") or sections.get("chronology", "")
    if timeline:
        body_lines.append("## Timeline")
        body_lines.append("")
        body_lines.append(timeline.strip())
        body_lines.append("")

    # Root Cause
    root_cause = (
        sections.get("root cause", "")
        or sections.get("root cause analysis", "")
        or sections.get("contributing factors", "")
    )
    if root_cause:
        body_lines.append("## Root Cause")
        body_lines.append("")
        body_lines.append(root_cause.strip())
        body_lines.append("")

    # Impact
    impact = sections.get("impact", "") or sections.get("customer impact", "")
    if impact:
        body_lines.append("## Impact")
        body_lines.append("")
        body_lines.append(impact.strip())
        body_lines.append("")

    # Detection / Response / Recovery
    for section_name in ("detection", "response", "recovery"):
        content = sections.get(section_name, "")
        if content:
            body_lines.append("## %s" % section_name.title())
            body_lines.append("")
            body_lines.append(content.strip())
            body_lines.append("")

    # Remediation (from Jira children)
    children = incident.get("children", [])
    if children:
        body_lines.append("## Remediation")
        body_lines.append("")
        for child in children:
            status_icon = "x" if child["status"].lower() == "done" else " "
            body_lines.append("- [%s] **%s** — %s (%s)" % (
                status_icon, child["key"], child["summary"], child["status"],
            ))
        body_lines.append("")

    # Action Items
    actions = sections.get("action items", "") or sections.get("follow-up actions", "")
    if actions:
        body_lines.append("## Action Items")
        body_lines.append("")
        body_lines.append(actions.strip())
        body_lines.append("")

    # Lessons Learned
    lessons = sections.get("lessons learned", "")
    if lessons:
        body_lines.append("## Lessons Learned")
        body_lines.append("")
        body_lines.append(lessons.strip())
        body_lines.append("")

    # What went well / could be improved
    for section_name in ("what went well", "what could be improved", "prevention"):
        content = sections.get(section_name, "")
        if content:
            body_lines.append("## %s" % section_name.title())
            body_lines.append("")
            body_lines.append(content.strip())
            body_lines.append("")

    # Remaining sections not already covered
    known = {
        "preamble", "incident summary", "summary", "timeline", "chronology",
        "root cause", "root cause analysis", "contributing factors",
        "impact", "customer impact", "detection", "response", "recovery",
        "action items", "follow-up actions", "remediation",
        "lessons learned", "what went well", "what could be improved", "prevention",
    }
    for section_name, content in sections.items():
        if section_name not in known and content.strip():
            body_lines.append("## %s" % section_name.title())
            body_lines.append("")
            body_lines.append(content.strip())
            body_lines.append("")

    # Links
    links = []
    if incident["retro_url"]:
        links.append("- [Confluence Retro](%s)" % incident["retro_url"])
    if incident["epic_url"]:
        links.append("- [Jira Epic](%s)" % incident["epic_url"])
    if links:
        body_lines.append("## Links")
        body_lines.append("")
        body_lines.extend(links)
        body_lines.append("")

    return "\n".join(fm_lines) + "\n\n" + "\n".join(body_lines)


def write_file(path, content, dry_run):
    """Write content to a file using cat heredoc (avoids Write tool / TCC prompts)."""
    if dry_run:
        print("  Would write: %s" % path)
        return

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Write directly from Python (this script runs via Bash, not the Write tool)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def is_test_incident(incident):
    """Detect test/dummy incidents by title keywords."""
    title = (incident.get("title", "") or "").lower()
    test_patterns = ["test incident", "test only", "test retro", "test not real",
                     "test markdown", "test templating", "test rootly", "ignore this"]
    return any(p in title for p in test_patterns)


def generate_incident_files(incidents, output_path, dry_run, force):
    """Generate per-incident markdown files."""
    print("\n--- Generating incident files ---")
    written = 0
    skipped = 0
    test_count = 0

    for incident in incidents:
        inc_key = incident["inc_key"]
        title = incident["title"]
        date = incident.get("date", "") or "0000-00-00"

        # Build date-first filename (INC key or retro page ID ensures uniqueness)
        if inc_key:
            filename = "%s — %s — %s.md" % (date, inc_key, sanitize_filename(title))
        else:
            page_id = incident.get("retro_page_id", "unknown")
            filename = "%s — %s — %s.md" % (date, page_id, sanitize_filename(title))

        # Route test incidents to _test/ subdirectory
        if is_test_incident(incident):
            filepath = os.path.join(output_path, "_test", filename)
            test_count += 1
        else:
            filepath = os.path.join(output_path, filename)

        if not force and os.path.exists(filepath):
            skipped += 1
            continue

        content = format_incident_markdown(incident)
        write_file(filepath, content, dry_run)
        written += 1

    print("Written: %d, Skipped (existing): %d, Test: %d" % (written, skipped, test_count))
    return written


def generate_trend_report(incidents, output_path, dry_run):
    """Generate trend report with deterministic analysis."""
    print("\n--- Generating trend report ---")

    if not incidents:
        print("No incidents to report on.")
        return

    # Count by month
    by_month = Counter()
    by_quarter = Counter()
    by_severity = Counter()
    by_status = Counter()
    by_remediation = Counter()
    service_mentions = Counter()

    for inc in incidents:
        date = inc.get("date", "")
        if date and len(date) >= 7:
            month = date[:7]
            by_month[month] += 1
            year = int(date[:4])
            month_num = int(date[5:7])
            quarter = "Q%d %d" % ((month_num - 1) // 3 + 1, year)
            by_quarter[quarter] += 1

        severity = inc.get("severity", "") or "Unknown"
        by_severity[severity] += 1

        status = inc.get("status", "") or "Unknown"
        by_status[status] += 1

        remediation = inc.get("remediation_status", "unknown")
        by_remediation[remediation] += 1

        # Extract service mentions from title, root cause, labels
        title_lower = inc.get("title", "").lower()
        root_cause = inc.get("sections", {}).get("root cause", "").lower()
        combined = title_lower + " " + root_cause
        for label in inc.get("labels", []):
            service_mentions[label.lower()] += 1
        # Simple keyword extraction for common service terms
        for word in re.findall(r"\b[a-z][\w-]*(?:-service|service)\b", combined):
            service_mentions[word] += 1

    # Build report
    lines = ["---"]
    lines.append("type: incident-trend-report")
    lines.append("generated: %s" % datetime.now(timezone.utc).isoformat())
    lines.append("incident_count: %d" % len(incidents))
    lines.append("---")
    lines.append("")
    lines.append("# Incident Trend Report")
    lines.append("")
    lines.append("Total incidents: **%d**" % len(incidents))
    lines.append("")

    # By quarter
    if by_quarter:
        lines.append("## Incidents by Quarter")
        lines.append("")
        for q in sorted(by_quarter.keys()):
            lines.append("- **%s**: %d" % (q, by_quarter[q]))
        lines.append("")

    # By month
    if by_month:
        lines.append("## Incidents by Month")
        lines.append("")
        for m in sorted(by_month.keys()):
            lines.append("- **%s**: %d" % (m, by_month[m]))
        lines.append("")

    # By severity
    if by_severity:
        lines.append("## Severity Distribution")
        lines.append("")
        for sev, count in by_severity.most_common():
            lines.append("- **%s**: %d" % (sev, count))
        lines.append("")

    # Remediation status
    if by_remediation:
        lines.append("## Remediation Status")
        lines.append("")
        for status, count in by_remediation.most_common():
            lines.append("- **%s**: %d" % (status, count))
        lines.append("")

    # Service heatmap
    if service_mentions:
        lines.append("## Service/Label Heatmap")
        lines.append("")
        lines.append("Services and labels that appear most frequently across incidents:")
        lines.append("")
        for svc, count in service_mentions.most_common(20):
            if count >= 2:
                lines.append("- **%s**: %d incidents" % (svc, count))
        lines.append("")

    content = "\n".join(lines)
    filepath = os.path.join(output_path, "_Trend Report.md")
    write_file(filepath, content, dry_run)


def generate_recurrence_report(incidents, output_path, dry_run):
    """Generate recurrence report with pattern detection."""
    print("\n--- Generating recurrence report ---")

    if not incidents:
        print("No incidents to report on.")
        return

    # Group incidents by extracted keywords from root cause / title
    keyword_to_incidents = defaultdict(list)
    for inc in incidents:
        title = inc.get("title", "").lower()
        root_cause = inc.get("sections", {}).get("root cause", "").lower()
        combined = title + " " + root_cause

        # Extract significant keywords (3+ chars, not common words)
        stop_words = {
            "the", "and", "for", "was", "are", "not", "this", "that", "with",
            "from", "has", "had", "have", "been", "were", "did", "does",
            "inc", "incident", "issue", "caused", "due",
        }
        words = re.findall(r"\b[a-z][a-z-]{2,}\b", combined)
        significant = [w for w in words if w not in stop_words]
        for word in set(significant):
            keyword_to_incidents[word].append(inc)

    # Find recurring keywords (appear in 3+ incidents)
    recurring_keywords = {
        k: v for k, v in keyword_to_incidents.items() if len(v) >= 3
    }

    # Group by labels
    label_to_incidents = defaultdict(list)
    for inc in incidents:
        for label in inc.get("labels", []):
            label_to_incidents[label.lower()].append(inc)
    recurring_labels = {
        k: v for k, v in label_to_incidents.items() if len(v) >= 2
    }

    # Build report
    lines = ["---"]
    lines.append("type: incident-recurrence-report")
    lines.append("generated: %s" % datetime.now(timezone.utc).isoformat())
    lines.append("incident_count: %d" % len(incidents))
    lines.append("---")
    lines.append("")
    lines.append("# Incident Recurrence Report")
    lines.append("")

    # Recurring by label
    if recurring_labels:
        lines.append("## Recurring by Label")
        lines.append("")
        for label, incs in sorted(recurring_labels.items(), key=lambda x: -len(x[1])):
            lines.append("### %s (%d incidents)" % (label, len(incs)))
            lines.append("")
            for inc in sorted(incs, key=lambda x: x.get("date", "")):
                key = inc.get("inc_key", "")
                title = inc.get("title", "Unknown")
                date = inc.get("date", "?")
                if key:
                    lines.append("- **%s** — %s (%s)" % (key, title, date))
                else:
                    lines.append("- %s (%s)" % (title, date))
            lines.append("")

    # Recurring by keyword
    if recurring_keywords:
        lines.append("## Recurring Themes (keyword analysis)")
        lines.append("")
        # Sort by count, take top 15
        for keyword, incs in sorted(recurring_keywords.items(), key=lambda x: -len(x[1]))[:15]:
            unique_keys = set(inc.get("inc_key", inc.get("retro_page_id", "")) for inc in incs)
            lines.append("- **%s** — appears in %d incidents: %s" % (
                keyword, len(incs),
                ", ".join(sorted(k for k in unique_keys if k)[:5]),
            ))
        lines.append("")

    # Incidents without retros (potential gap)
    no_retro = [inc for inc in incidents if not inc.get("has_retro")]
    if no_retro:
        lines.append("## Incidents Missing Retrospectives")
        lines.append("")
        for inc in sorted(no_retro, key=lambda x: x.get("date", "")):
            lines.append("- **%s** — %s (%s)" % (
                inc.get("inc_key", "?"), inc.get("title", "Unknown"), inc.get("date", "?"),
            ))
        lines.append("")

    content = "\n".join(lines)
    filepath = os.path.join(output_path, "_Recurrence Report.md")
    write_file(filepath, content, dry_run)


def main():
    args = parse_args()

    # Load env for base_url
    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    output_path = os.environ.get("INCIDENT_KB_OUTPUT_PATH", "")

    if not output_path:
        print("ERROR: INCIDENT_KB_OUTPUT_PATH not set", file=sys.stderr)
        sys.exit(1)

    # Load cached data
    cross_ref, confluence_pages, jira_epics, meta = load_cached_data()

    # Build unified incident list
    incidents = []

    # Matched incidents
    for match in cross_ref.get("matched", []):
        incidents.append(build_incident(match, confluence_pages, jira_epics, base_url))

    # Orphan retros
    for orphan in cross_ref.get("orphan_retros", []):
        incidents.append(build_orphan_retro_incident(orphan, confluence_pages, base_url))

    # Orphan epics
    for orphan in cross_ref.get("orphan_epics", []):
        incidents.append(build_orphan_epic_incident(orphan, jira_epics, base_url))

    print("Total incidents: %d (matched: %d, orphan retros: %d, orphan epics: %d)" % (
        len(incidents),
        len(cross_ref.get("matched", [])),
        len(cross_ref.get("orphan_retros", [])),
        len(cross_ref.get("orphan_epics", [])),
    ))

    # Generate per-incident files
    if not args.report_only:
        generate_incident_files(incidents, output_path, args.dry_run, args.force)

    # Generate reports
    generate_trend_report(incidents, output_path, args.dry_run)
    generate_recurrence_report(incidents, output_path, args.dry_run)

    print("\n--- Generation complete ---")
    print("Output: %s" % output_path)


if __name__ == "__main__":
    main()
