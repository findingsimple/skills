#!/usr/bin/env python3
"""Crawl Confluence retro pages and Jira INC epics, cross-reference, save to /tmp/."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _libpath  # noqa: F401
from confluence_client import (
    load_env,
    init_auth,
    confluence_get_children,
    confluence_get_page,
    confluence_get_page_labels,
    adf_to_text,
    storage_to_text,
)
from jira_client import (
    jira_search_all,
    adf_to_text_rich as jira_adf_to_text,
)

CACHE_DIR = "/tmp/incident_kb"
CONFLUENCE_DIR = os.path.join(CACHE_DIR, "confluence")
JIRA_DIR = os.path.join(CACHE_DIR, "jira")
META_FILE = os.path.join(CACHE_DIR, "meta.json")

INC_KEY_PATTERN = re.compile(r"(INC-\d+)", re.IGNORECASE)
# \A...\Z + re.ASCII defeats trailing-newline and Unicode-lookalike bypasses
# (Python's `$` matches before a trailing \n — `re.compile(r"^X$")` accepts
# "X\n", which would enable JQL injection when interpolated).
PROJECT_KEY_PATTERN = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)


def _read_cache(path):
    """Read a JSON cache file, returning None if missing or corrupt."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print("  WARNING: Corrupt cache file %s, will re-fetch: %s" % (path, e), file=sys.stderr)
        os.remove(path)
        return None


def _write_cache(path, data):
    """Write JSON to a cache file atomically (write to .tmp then rename)."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch incident data from Confluence and Jira")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if cache exists")
    parser.add_argument("--team", default="", help="Team name (stored in metadata)")
    return parser.parse_args()


def parse_retro_sections(body_text):
    """Split retro body text into sections based on heading patterns.

    Looks for markdown headings (## ...) or bold section markers (**...**).
    Returns dict of {section_name_lower: content}.
    """
    if not body_text:
        return {}

    sections = {}
    current_section = "preamble"
    current_lines = []

    for line in body_text.split("\n"):
        stripped = line.strip()

        # Detect heading: ## Heading or # Heading
        heading_match = re.match(r"^#{1,4}\s+(.+)$", stripped)
        if not heading_match:
            # Detect bold heading: **Heading**
            heading_match = re.match(r"^\*\*(.+?)\*\*\s*$", stripped)

        if heading_match:
            # Save previous section
            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = heading_match.group(1).strip().lower()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


def extract_inc_keys(title, body_text, labels):
    """Extract INC-xxx keys from title, body, and labels. Returns list of unique keys."""
    keys = set()
    for source in [title, body_text] + labels:
        if source:
            for match in INC_KEY_PATTERN.findall(source):
                keys.add(match.upper())
    return sorted(keys)


def get_page_body_text(base_url, auth, page_id):
    """Fetch page body as plain text, trying ADF first then storage format."""
    page = {}

    # Try ADF format first
    try:
        page = confluence_get_page(base_url, auth, page_id, body_format="atlas_doc_format")
        adf_body = page.get("body", {}).get("atlas_doc_format", {}).get("value", "")
        if adf_body:
            if isinstance(adf_body, str):
                adf_body = json.loads(adf_body)
            text = adf_to_text(adf_body)
            if text.strip():
                return page, text
    except Exception as e:
        print("  ADF fetch failed for page %s: %s" % (page_id, e), file=sys.stderr)

    # Fallback to storage format
    try:
        page = confluence_get_page(base_url, auth, page_id, body_format="storage")
        storage_body = page.get("body", {}).get("storage", {}).get("value", "")
        if storage_body:
            text = storage_to_text(storage_body)
            return page, text
    except Exception as e:
        print("  Storage fetch failed for page %s: %s" % (page_id, e), file=sys.stderr)

    return page, ""


def fetch_retro_pages(base_url, auth, parent_page_id, force, dry_run):
    """Fetch all retro child pages from Confluence.

    Returns list of processed page dicts.
    """
    print("\n--- Fetching Confluence retro pages ---")
    children = confluence_get_children(base_url, auth, parent_page_id)
    print("Found %d child pages under parent %s" % (len(children), parent_page_id))

    pages = []
    skipped = 0

    for i, child in enumerate(children):
        page_id = child.get("id", "")
        title = child.get("title", "")
        cache_path = os.path.join(CONFLUENCE_DIR, "%s.json" % page_id)

        # Check cache
        if not force:
            cached = _read_cache(cache_path)
            if cached is not None:
                pages.append(cached)
                skipped += 1
                continue

        if dry_run:
            print("  [%d/%d] Would fetch: %s — %s" % (i + 1, len(children), page_id, title))
            pages.append({"page_id": page_id, "title": title, "dry_run": True})
            continue

        print("  [%d/%d] Fetching: %s — %s" % (i + 1, len(children), page_id, title))

        # Fetch full page with body
        page_data, body_text = get_page_body_text(base_url, auth, page_id)

        # Fetch labels
        try:
            labels = confluence_get_page_labels(base_url, auth, page_id)
        except Exception:
            labels = []

        # Parse sections
        sections = parse_retro_sections(body_text)

        # Extract INC keys
        inc_keys = extract_inc_keys(title, body_text, labels)

        record = {
            "page_id": page_id,
            "title": title,
            "created_at": page_data.get("createdAt", ""),
            "modified_at": page_data.get("version", {}).get("createdAt", ""),
            "author": page_data.get("version", {}).get("authorId", ""),
            "labels": labels,
            "inc_keys": inc_keys,
            "sections": sections,
            "body_text": body_text,
            "status": page_data.get("status", ""),
        }

        _write_cache(cache_path, record)

        pages.append(record)

        # Rate limit courtesy
        time.sleep(0.5)

    fetched = len(children) - skipped
    print("Fetched: %d, Cached: %d, Total: %d" % (fetched, skipped, len(children)))
    return pages


def fetch_inc_epics(base_url, auth, project_key, force, dry_run):
    """Fetch all INC epics from Jira.

    Returns list of processed epic dicts.
    """
    print("\n--- Fetching Jira INC epics ---")
    jql = "project = %s AND issuetype = Epic ORDER BY created DESC" % project_key
    fields = "key,summary,status,description,created,updated,reporter,labels,priority,issuelinks,fixVersions"

    raw_epics = jira_search_all(base_url, auth, jql, fields)
    print("Found %d epics in %s" % (len(raw_epics), project_key))

    epics = []
    skipped = 0

    for i, epic in enumerate(raw_epics):
        key = epic.get("key", "")
        fields_data = epic.get("fields", {})
        summary = fields_data.get("summary", "")
        cache_path = os.path.join(JIRA_DIR, "%s.json" % key)

        # Check cache
        if not force:
            cached = _read_cache(cache_path)
            if cached is not None:
                epics.append(cached)
                skipped += 1
                continue

        if dry_run:
            print("  [%d/%d] Would fetch: %s — %s" % (i + 1, len(raw_epics), key, summary))
            epics.append({"key": key, "summary": summary, "dry_run": True})
            continue

        print("  [%d/%d] Processing: %s — %s" % (i + 1, len(raw_epics), key, summary))

        # Extract description text
        description = fields_data.get("description")
        desc_text = jira_adf_to_text(description) if description else ""

        # Extract linked issues
        links = []
        for link in fields_data.get("issuelinks", []) or []:
            linked = link.get("outwardIssue") or link.get("inwardIssue")
            if linked:
                links.append({
                    "key": linked.get("key", ""),
                    "summary": linked.get("fields", {}).get("summary", ""),
                    "status": linked.get("fields", {}).get("status", {}).get("name", ""),
                    "type": link.get("type", {}).get("name", ""),
                    "direction": "outward" if link.get("outwardIssue") else "inward",
                })

        # Fetch child issues (stories/tasks under epic)
        child_jql = "'Epic Link' = %s ORDER BY created ASC" % key
        try:
            child_issues = jira_search_all(base_url, auth, child_jql, "key,summary,status,issuetype")
            children = []
            for child in child_issues:
                cf = child.get("fields", {})
                children.append({
                    "key": child.get("key", ""),
                    "summary": cf.get("summary", ""),
                    "status": cf.get("status", {}).get("name", ""),
                    "issuetype": cf.get("issuetype", {}).get("name", ""),
                })
        except Exception as e:
            print("  WARNING: Could not fetch children for %s: %s" % (key, e), file=sys.stderr)
            children = []

        # Determine severity from priority
        priority = fields_data.get("priority", {})
        severity = priority.get("name", "") if priority else ""

        # Determine remediation status from children
        if children:
            statuses = [c["status"].lower() for c in children]
            if all(s == "done" for s in statuses):
                remediation_status = "complete"
            elif any(s in ("in progress", "in review") for s in statuses):
                remediation_status = "in-progress"
            else:
                remediation_status = "not-started"
        else:
            remediation_status = "unknown"

        record = {
            "key": key,
            "summary": summary,
            "status": fields_data.get("status", {}).get("name", ""),
            "severity": severity,
            "description_text": desc_text,
            "created": fields_data.get("created", ""),
            "updated": fields_data.get("updated", ""),
            "reporter": fields_data.get("reporter", {}).get("displayName", "") if fields_data.get("reporter") else "",
            "labels": fields_data.get("labels", []),
            "fix_versions": [v.get("name", "") for v in (fields_data.get("fixVersions") or [])],
            "linked_issues": links,
            "children": children,
            "remediation_status": remediation_status,
        }

        _write_cache(cache_path, record)

        epics.append(record)

    fetched = len(raw_epics) - skipped
    print("Fetched: %d, Cached: %d, Total: %d" % (fetched, skipped, len(raw_epics)))
    return epics


def _parse_date_from_string(s):
    """Extract the first ISO-style or 'DD Mon YYYY' date. Returns date or None."""
    if not s:
        return None
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            pass
    m = re.search(
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})",
        s,
        re.IGNORECASE,
    )
    if m:
        try:
            return datetime.strptime(
                "%s %s %s" % (m.group(1), m.group(2)[:3].title(), m.group(3)),
                "%d %b %Y",
            ).date()
        except ValueError:
            pass
    return None


def _normalize_for_compare(s):
    """Lowercase, strip dates, drop non-alphanumerics — for fuzzy title comparison."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def _infer_matches(orphan_retros, orphan_epics, retro_pages_by_id, epic_by_key):
    """Pair orphan retros to orphan epics by date+title similarity (fallback when
    the retro page has no INC-NNN reference at all in title or body).

    A pair is accepted when both items have a parseable date within ±3 days AND
    the date-stripped, normalised titles have similarity ≥ 0.45 (tightened to
    0.60 when the gap is 2-3 days). Pairs are surfaced as `inferred_matches`
    rather than merged into `matched` so consumers can show the lower confidence
    and the team knows to backfill the missing INC reference into the retro.
    """
    retro_features = []
    for r in orphan_retros:
        page = retro_pages_by_id.get(r.get("retro_page_id", ""), {})
        title = r.get("retro_title") or page.get("title", "")
        date = _parse_date_from_string(title) or _parse_date_from_string(page.get("created_at", ""))
        retro_features.append({
            "retro": r,
            "title": title,
            "date": date,
            "norm": _normalize_for_compare(title),
        })

    epic_features = []
    for e in orphan_epics:
        epic = epic_by_key.get(e.get("epic_key", ""), {})
        summary = e.get("epic_summary") or epic.get("summary", "")
        date = _parse_date_from_string(summary) or _parse_date_from_string(epic.get("created", ""))
        epic_features.append({
            "epic": e,
            "summary": summary,
            "date": date,
            "norm": _normalize_for_compare(summary),
        })

    candidates = []
    for ef in epic_features:
        if not ef["date"]:
            continue
        for rf in retro_features:
            if not rf["date"]:
                continue
            day_gap = abs((ef["date"] - rf["date"]).days)
            if day_gap > 3:
                continue
            sim = SequenceMatcher(None, ef["norm"], rf["norm"]).ratio()
            threshold = 0.45 if day_gap <= 1 else 0.60
            if sim < threshold:
                continue
            confidence = round(sim * (1.0 - day_gap * 0.1), 2)
            candidates.append((confidence, ef, rf, day_gap, sim))

    # Greedy: highest confidence first, each retro/epic claimed at most once
    candidates.sort(key=lambda x: -x[0])
    used_retros = set()
    used_epics = set()
    inferred = []
    for confidence, ef, rf, day_gap, sim in candidates:
        ek = ef["epic"].get("epic_key", "")
        rid = rf["retro"].get("retro_page_id", "")
        if ek in used_epics or rid in used_retros:
            continue
        used_epics.add(ek)
        used_retros.add(rid)
        inferred.append({
            "inc_key": ek,
            "epic_key": ek,
            "epic_summary": ef["summary"],
            "retro_page_id": rid,
            "retro_title": rf["title"],
            "has_retro": True,
            "has_epic": True,
            "match_source": "inferred",
            "confidence": confidence,
            "match_reason": "date_gap=%dd, title_similarity=%.2f" % (day_gap, sim),
        })

    remaining_retros = [r for r in orphan_retros if r.get("retro_page_id", "") not in used_retros]
    remaining_epics = [e for e in orphan_epics if e.get("epic_key", "") not in used_epics]
    return inferred, remaining_retros, remaining_epics


def cross_reference(retro_pages, inc_epics):
    """Match Confluence retro pages to Jira INC epics.

    Returns dict with matched pairs, inferred matches, orphan retros, and orphan epics.
    """
    print("\n--- Cross-referencing ---")

    # Build epic lookup by key
    epic_by_key = {}
    for epic in inc_epics:
        key = epic.get("key", "")
        if key:
            epic_by_key[key] = epic

    matched = []
    orphan_retros = []
    matched_epic_keys = set()

    for page in retro_pages:
        inc_keys = page.get("inc_keys", [])
        if inc_keys:
            # Match each INC key found in the retro to its epic
            any_matched = False
            for inc_key in inc_keys:
                if inc_key in epic_by_key:
                    matched.append({
                        "inc_key": inc_key,
                        "retro_page_id": page.get("page_id", ""),
                        "retro_title": page.get("title", ""),
                        "epic_key": inc_key,
                        "epic_summary": epic_by_key[inc_key].get("summary", ""),
                        "has_retro": True,
                        "has_epic": True,
                    })
                    matched_epic_keys.add(inc_key)
                    any_matched = True
                else:
                    # INC key in retro but no matching epic
                    matched.append({
                        "inc_key": inc_key,
                        "retro_page_id": page.get("page_id", ""),
                        "retro_title": page.get("title", ""),
                        "epic_key": None,
                        "epic_summary": None,
                        "has_retro": True,
                        "has_epic": False,
                    })
        else:
            orphan_retros.append({
                "retro_page_id": page.get("page_id", ""),
                "retro_title": page.get("title", ""),
            })

    # Find epics without retros
    orphan_epics = []
    for epic in inc_epics:
        key = epic.get("key", "")
        if key and key not in matched_epic_keys:
            orphan_epics.append({
                "epic_key": key,
                "epic_summary": epic.get("summary", ""),
            })

    # Fallback: pair remaining orphans by date+title similarity (catches retros
    # that lack any INC-NNN reference). Surfaced separately so callers can show
    # confidence and prompt the team to backfill the missing INC reference.
    retro_pages_by_id = {p.get("page_id", ""): p for p in retro_pages}
    inferred_matches, orphan_retros, orphan_epics = _infer_matches(
        orphan_retros, orphan_epics, retro_pages_by_id, epic_by_key
    )

    result = {
        "matched": matched,
        "inferred_matches": inferred_matches,
        "orphan_retros": orphan_retros,
        "orphan_epics": orphan_epics,
    }

    print("Matched: %d" % len(matched))
    print("Inferred matches (date+title fallback): %d" % len(inferred_matches))
    print("Orphan retros (no INC key, no inferred pair): %d" % len(orphan_retros))
    print("Orphan epics (no retro page): %d" % len(orphan_epics))

    if inferred_matches:
        print("\n  Inferred matches (retro missing INC key — backfill needed):")
        for m in inferred_matches[:10]:
            print("    - %s ↔ %s (conf %.2f, %s)" % (
                m["epic_key"], m["retro_title"], m["confidence"], m["match_reason"],
            ))
        if len(inferred_matches) > 10:
            print("    ... and %d more" % (len(inferred_matches) - 10))

    if orphan_retros:
        print("\n  Orphan retros:")
        for r in orphan_retros[:10]:
            print("    - %s" % r["retro_title"])
        if len(orphan_retros) > 10:
            print("    ... and %d more" % (len(orphan_retros) - 10))

    if orphan_epics:
        print("\n  Orphan epics:")
        for e in orphan_epics[:10]:
            print("    - %s — %s" % (e["epic_key"], e["epic_summary"]))
        if len(orphan_epics) > 10:
            print("    ... and %d more" % (len(orphan_epics) - 10))

    return result


def main():
    args = parse_args()

    env = load_env([
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "RETRO_PARENT_PAGE_ID", "INC_PROJECT_KEY", "INCIDENT_KB_OUTPUT_PATH",
    ])
    if not env.get("INC_PROJECT_KEY"):
        env["INC_PROJECT_KEY"] = "INC"

    # Validate project key format
    inc_project_key = env["INC_PROJECT_KEY"]
    if not PROJECT_KEY_PATTERN.match(inc_project_key):
        print("ERROR: INC_PROJECT_KEY must match [A-Z][A-Z0-9_]+ (got: %s)" % inc_project_key, file=sys.stderr)
        sys.exit(1)

    base_url, auth = init_auth(env)
    parent_page_id = env["RETRO_PARENT_PAGE_ID"]

    # Create cache dirs with restrictive permissions. `exist_ok=True` alone
    # does not repair perms on a pre-existing dir — chmod explicitly, and
    # reject symlinks that could redirect writes.
    if not args.dry_run:
        for d in (CONFLUENCE_DIR, JIRA_DIR):
            if os.path.islink(d):
                print("ERROR: %s is a symlink; refusing to use it." % d, file=sys.stderr)
                sys.exit(1)
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)

    # Fetch
    retro_pages = fetch_retro_pages(base_url, auth, parent_page_id, args.force, args.dry_run)
    inc_epics = fetch_inc_epics(base_url, auth, inc_project_key, args.force, args.dry_run)

    if args.dry_run:
        print("\n--- Dry run complete ---")
        print("Would fetch %d retro pages and %d epics" % (len(retro_pages), len(inc_epics)))
        sys.exit(0)

    # Cross-reference
    cross_ref = cross_reference(retro_pages, inc_epics)

    # Save cross-reference
    _write_cache(os.path.join(CACHE_DIR, "cross_ref.json"), cross_ref)

    # Save metadata
    meta = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "team": args.team,
        "retro_count": len(retro_pages),
        "epic_count": len(inc_epics),
        "matched_count": len(cross_ref["matched"]),
        "inferred_match_count": len(cross_ref.get("inferred_matches", [])),
        "orphan_retro_count": len(cross_ref["orphan_retros"]),
        "orphan_epic_count": len(cross_ref["orphan_epics"]),
    }
    _write_cache(META_FILE, meta)

    print("\n--- Fetch complete ---")
    print("Cache: %s" % CACHE_DIR)
    print("Retros: %d | Epics: %d | Matched: %d | Inferred: %d" % (
        len(retro_pages), len(inc_epics),
        len(cross_ref["matched"]), len(cross_ref.get("inferred_matches", [])),
    ))


if __name__ == "__main__":
    main()
