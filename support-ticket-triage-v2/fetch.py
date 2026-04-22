#!/usr/bin/env python3
"""Fetch a Jira support ticket + linked + similar issues for triage.

Writes /tmp/triage_v2/<KEY>.json atomically. Stdout summary is terse so the
main Claude Code context stays lean; the sub-agent reads the JSON file
directly via `cat`.
"""

import argparse
import json
import os
import re
import stat
import sys
import urllib.parse

from jira_client import (
    adf_to_text,
    init_auth,
    jira_get,
    jira_get_comments,
    jira_search_all,
    load_env,
)

CACHE_DIR = "/tmp/triage_v2"

# Anchored with \A...\Z (re.ASCII) to reject trailing newlines and Unicode
# lookalikes. Interpolated into JQL — bypasses here become injection.
ISSUE_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
EPICS_LIST_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+(,[A-Z][A-Z0-9_]+-\d+)*\Z", re.ASCII)
PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
EXTENSION_RE = re.compile(r"\A[A-Za-z0-9]{1,8}\Z", re.ASCII)
SAFE_PATH_RE = re.compile(r"\A[A-Za-z0-9_./\- ]+\Z", re.ASCII)

SUMMARY_STOPWORDS = frozenset({
    "with", "when", "from", "does", "this", "that", "into", "cannot", "error", "issue",
})

# Characters treated as meta by JQL's `text ~` Lucene search, plus shell metas.
# Used to scrub keyword terms (labels, components, summary tokens).
_JQL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_\- ]")


def parse_args():
    p = argparse.ArgumentParser(
        prog="fetch.py",
        description="Fetch a Jira support ticket + linked + similar issues for triage.",
    )
    p.add_argument("ticket_key", help='Jira issue key (e.g. "PROJ-123")')
    p.add_argument(
        "--similar-limit",
        type=int,
        default=10,
        help="Max similar resolved tickets to fetch (default: 10)",
    )
    return p.parse_args()


def validate_ticket_key(key):
    if not ISSUE_KEY_RE.match(key):
        print("ERROR: invalid ticket key %r (expected format like PROJ-123)" % key, file=sys.stderr)
        sys.exit(2)


def validate_epics_list(value):
    """Validate ROOT_CAUSE_EPICS against a strict pattern before JQL interpolation."""
    if not EPICS_LIST_RE.match(value):
        print(
            "ERROR: ROOT_CAUSE_EPICS is malformed: %r\n"
            "       expected comma-separated issue keys, e.g. PROJ-1234,PROJ-5678" % value,
            file=sys.stderr,
        )
        sys.exit(2)


def validate_project_key(value):
    if not PROJECT_KEY_RE.match(value):
        print(
            "ERROR: SUPPORT_PROJECT_KEY is malformed: %r\n"
            "       expected uppercase letters/digits/underscores, e.g. SUP, ECS" % value,
            file=sys.stderr,
        )
        sys.exit(2)


def validate_codebase_path(value):
    """Reject shell metacharacters even though path is never shelled out directly.

    Defence in depth: the sub-agent sees this value in example grep commands
    and may interpolate it unquoted.
    """
    if not SAFE_PATH_RE.match(value):
        print(
            "ERROR: CODEBASE_PATH contains unsafe characters: %r\n"
            "       allowed: letters, digits, ._/-, and spaces" % value,
            file=sys.stderr,
        )
        sys.exit(2)
    if not os.path.isabs(value):
        print("ERROR: CODEBASE_PATH must be an absolute path: %r" % value, file=sys.stderr)
        sys.exit(2)
    if not os.path.isdir(value):
        print("ERROR: CODEBASE_PATH %r is not a directory." % value, file=sys.stderr)
        sys.exit(1)


def validate_extensions(raw):
    """Parse CODE_SEARCH_EXTENSIONS into a list of safe extensions."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for p in parts:
        if not EXTENSION_RE.match(p):
            print(
                "ERROR: CODE_SEARCH_EXTENSIONS contains an invalid extension: %r\n"
                "       each extension must match [A-Za-z0-9]{1,8}" % p,
                file=sys.stderr,
            )
            sys.exit(2)
    return parts


def scrub_term(term):
    """Replace JQL/shell meta characters with spaces, collapse, strip.

    Returns an empty string if nothing usable remains.
    """
    cleaned = _JQL_UNSAFE_RE.sub(" ", term or "").strip()
    return re.sub(r"\s+", " ", cleaned)


def extract_keywords(ticket):
    """Pick search keywords from labels, components, and summary nouns.

    All tokens pass through `scrub_term` so they are safe to interpolate into
    JQL. Deliberately conservative — better to return fewer, higher-signal
    tokens than a noisy grab-bag.
    """
    fields = ticket.get("fields", {}) if isinstance(ticket, dict) else {}
    keywords = set()

    for label in fields.get("labels", []) or []:
        term = scrub_term(str(label)).lower()
        if term:
            keywords.add(term)

    for component in fields.get("components", []) or []:
        if not isinstance(component, dict):
            continue
        term = scrub_term(component.get("name", "")).lower()
        if term:
            keywords.add(term)

    summary = fields.get("summary", "") or ""
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", summary):
        low = token.lower()
        if low not in SUMMARY_STOPWORDS:
            keywords.add(low)

    return sorted(keywords)


def _wrap_untrusted(text):
    """Tag attacker-controlled fields in the bundle so the sub-agent treats
    them as data, not instructions. See SYNTHESIS_PROMPT.md."""
    return {"_untrusted": True, "text": text or ""}


def simplify_issue(raw):
    """Strip a raw Jira issue down to the fields the sub-agent needs.

    Fields sourced from external reporters (description, comments) are
    wrapped with `_untrusted` sentinels — the sub-agent prompt tells the
    model to treat them as data only.
    """
    fields = (raw.get("fields") or {}) if isinstance(raw, dict) else {}
    description = adf_to_text(fields.get("description") or {})
    return {
        "key": raw.get("key"),
        "summary": fields.get("summary") or "",
        "status": (fields.get("status") or {}).get("name") or "",
        "priority": (fields.get("priority") or {}).get("name") or "",
        "resolution": (fields.get("resolution") or {}).get("name") or "",
        "reporter": (fields.get("reporter") or {}).get("displayName") or "",
        "assignee": (fields.get("assignee") or {}).get("displayName") or "",
        "created": fields.get("created") or "",
        "updated": fields.get("updated") or "",
        "labels": fields.get("labels") or [],
        "components": [c.get("name") for c in (fields.get("components") or []) if isinstance(c, dict) and c.get("name")],
        "description": _wrap_untrusted(description),
    }


def fetch_ticket(base_url, auth, key):
    """Fetch a single issue with its linked issues inline."""
    path = "/rest/api/3/issue/%s" % urllib.parse.quote(key, safe="")
    return jira_get(base_url, path, auth)


def collect_linked_keys(raw):
    keys = set()
    for link in (raw.get("fields", {}) or {}).get("issuelinks") or []:
        inward = (link.get("inwardIssue") or {}).get("key")
        outward = (link.get("outwardIssue") or {}).get("key")
        if inward:
            keys.add(inward)
        if outward:
            keys.add(outward)
    return sorted(keys)


def fetch_similar(base_url, auth, project_key, keywords, limit):
    """Find resolved tickets that share keywords with the target ticket.

    Tries pairs of keywords first (higher specificity), then falls back to
    top single keywords. Deduplicates results. Terms are pre-scrubbed by
    `scrub_term` before interpolation; scope uses a validated project key.
    """
    if not keywords:
        return []

    seen = {}
    top = keywords[:5]
    pairs = [(top[i], top[j]) for i in range(len(top)) for j in range(i + 1, len(top))][:6]
    scope = ('project = "%s" AND ' % project_key) if project_key else ""

    def _search(jql, remaining):
        try:
            return jira_search_all(
                base_url, auth, jql,
                "summary,status,resolution,priority,created,resolutiondate",
                limit=remaining,
            )
        except Exception as e:  # noqa: BLE001 — deliberately broad; one bad keyword shouldn't abort the whole skill
            print("WARN: similar search failed (%s): %s" % (jql, e), file=sys.stderr)
            return []

    for a, b in pairs:
        if len(seen) >= limit:
            break
        jql = '%stext ~ "%s" AND text ~ "%s" AND status in (Done, Closed, Resolved)' % (scope, a, b)
        for issue in _search(jql, limit - len(seen)):
            seen.setdefault(issue.get("key"), issue)
            if len(seen) >= limit:
                break

    if len(seen) < limit:
        for term in top:
            if len(seen) >= limit:
                break
            jql = '%stext ~ "%s" AND status in (Done, Closed, Resolved)' % (scope, term)
            for issue in _search(jql, limit - len(seen)):
                seen.setdefault(issue.get("key"), issue)
                if len(seen) >= limit:
                    break

    return [simplify_issue(i) for i in list(seen.values())[:limit]]


def fetch_root_cause_epic_children(base_url, auth, epics_raw, keywords, limit=10):
    """JQL: parent in (…) AND text ~ <keyword>. Used when ROOT_CAUSE_EPICS is set.

    Caller must validate `epics_raw` against EPICS_LIST_RE before calling.
    """
    keys = epics_raw.split(",")
    epic_list = ",".join('"%s"' % k for k in keys)

    results = {}
    terms = keywords[:3] if keywords else [""]
    for term in terms:
        if term:
            jql = 'parent in (%s) AND text ~ "%s"' % (epic_list, term)
        else:
            jql = 'parent in (%s)' % epic_list
        try:
            issues = jira_search_all(
                base_url, auth, jql,
                "summary,status,resolution,priority,parent",
                limit=limit - len(results),
            )
            for issue in issues:
                results.setdefault(issue.get("key"), issue)
                if len(results) >= limit:
                    break
        except Exception as e:  # noqa: BLE001
            print("WARN: root-cause-epic search failed for %r: %s" % (term, e), file=sys.stderr)
        if len(results) >= limit:
            break
    return [simplify_issue(i) for i in list(results.values())[:limit]]


def atomic_write(path, data):
    """Atomic write with restrictive file permissions (0o600).

    Uses os.open with O_EXCL-style new-file semantics (via O_CREAT|O_WRONLY|O_TRUNC
    and explicit mode) so the file is created with 0o600 regardless of umask.
    """
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def ensure_cache_dir():
    """Create CACHE_DIR with 0o700 and repair loose perms on a pre-existing dir.

    Rejects symlinks — a preexisting symlinked CACHE_DIR could redirect writes.
    """
    if os.path.islink(CACHE_DIR):
        print("ERROR: %s is a symlink; refusing to use it." % CACHE_DIR, file=sys.stderr)
        sys.exit(1)
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    # Repair perms if the dir already existed with wider modes.
    current_mode = stat.S_IMODE(os.stat(CACHE_DIR).st_mode)
    if current_mode != 0o700:
        os.chmod(CACHE_DIR, 0o700)


def main():
    args = parse_args()
    validate_ticket_key(args.ticket_key)

    env = load_env([
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
        "SUPPORT_PROJECT_KEY", "ROOT_CAUSE_EPICS",
        "CODEBASE_PATH", "CODE_SEARCH_EXTENSIONS",
    ])

    if not env.get("CODEBASE_PATH"):
        print("ERROR: CODEBASE_PATH is not set (absolute path to the codebase to investigate).", file=sys.stderr)
        sys.exit(1)
    validate_codebase_path(env["CODEBASE_PATH"])

    extensions_raw = env.get("CODE_SEARCH_EXTENSIONS") or "rb,ts,tsx,go,py,js,jsx,rs,java"
    extensions = validate_extensions(extensions_raw)

    project_key = env.get("SUPPORT_PROJECT_KEY") or ""
    if project_key:
        validate_project_key(project_key)

    root_cause_epics = env.get("ROOT_CAUSE_EPICS") or ""
    if root_cause_epics:
        validate_epics_list(root_cause_epics)

    base_url, auth = init_auth(env)

    ensure_cache_dir()

    raw = fetch_ticket(base_url, auth, args.ticket_key)
    ticket = simplify_issue(raw)
    ticket["comments"] = [
        {**c, "body_text": _wrap_untrusted(c.get("body_text"))}
        for c in jira_get_comments(base_url, auth, args.ticket_key)
    ]

    linked_keys = collect_linked_keys(raw)
    linked = []
    for lk in linked_keys:
        try:
            lr = fetch_ticket(base_url, auth, lk)
            linked_ticket = simplify_issue(lr)
            linked_ticket["comments"] = [
                {**c, "body_text": _wrap_untrusted(c.get("body_text"))}
                for c in jira_get_comments(base_url, auth, lk)
            ]
            linked.append(linked_ticket)
        except Exception as e:  # noqa: BLE001 — one bad linked issue shouldn't abort the whole fetch
            print("WARN: failed to fetch linked issue %s: %s" % (lk, e), file=sys.stderr)

    keywords = extract_keywords(raw)
    similar = fetch_similar(base_url, auth, project_key, keywords, args.similar_limit)

    epic_children = []
    if root_cause_epics:
        epic_children = fetch_root_cause_epic_children(base_url, auth, root_cause_epics, keywords)

    references_path = os.path.join(env["CODEBASE_PATH"], "references")

    bundle = {
        "ticket": ticket,
        "linked": linked,
        "similar": similar,
        "root_cause_epic_children": epic_children,
        "keywords": keywords,
        "investigation_context": {
            "codebase_path": env["CODEBASE_PATH"],
            "references_path": references_path if os.path.isdir(references_path) else None,
            "code_search_extensions": extensions,
            "support_project_key": project_key or None,
        },
    }

    output_path = os.path.join(CACHE_DIR, args.ticket_key + ".json")
    atomic_write(output_path, bundle)

    # Terse stdout (main context budget)
    print("Ticket:    %s — %s" % (ticket["key"], ticket["summary"]))
    print("Status:    %s | Priority: %s | Reporter: %s" % (ticket["status"], ticket["priority"], ticket["reporter"]))
    print("Linked:    %d | Similar resolved: %d | Epic children: %d" % (len(linked), len(similar), len(epic_children)))
    print("Cached:    %s" % output_path)


if __name__ == "__main__":
    main()
