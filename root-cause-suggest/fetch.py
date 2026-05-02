#!/usr/bin/env python3
"""root-cause-suggest fetch.

Pipeline:
  1. Always fetch the root-cause catalog: children of ROOT_CAUSE_EPICS.
  2. Resolve the input ticket set:
     - explicit_keys: fetch each --keys / --from-file ticket directly.
     - auto_discover: Stage-1 JQL pulls candidate support tickets in the focus
       team's intake over the last N days; Stage-2 client-side filter drops any
       ticket already linked to an RC catalog key.
  3. Slim each kept ticket (summary + capped description + comments + links)
     and write /tmp/root-cause-suggest/data.json atomically.
"""

import argparse
import glob
import json
import os
import re
import sys
import urllib.parse

import _libpath  # noqa: F401
from jira_client import (
    adf_to_text,
    atomic_write_json,
    init_auth,
    jira_get,
    jira_get_comments,
    jira_search_all,
    load_env,
)


CACHE_DIR = "/tmp/root-cause-suggest"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")

MAX_DESCRIPTION_CHARS = 1500
MAX_COMMENTS = 5
MAX_COMMENT_CHARS = 800
MAX_SUPPORT_RC_CHARS = 1500
MAX_DESCRIPTION_SNIPPET = 200
MAX_ENRICHED_SECTION_CHARS = 600
RC_CATALOG_HARD_LIMIT = 500
AUTO_DISCOVER_CANDIDATE_CAP = 400
MAX_UUID_LOOKUP_SCAN = 25

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-&]{0,63}\Z", re.ASCII)
_ISSUE_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
_UUID_RE = re.compile(r"\A[A-Za-z0-9\-]{16,64}\Z", re.ASCII)
_ISSUE_KEY_FINDALL_RE = re.compile(r"\b[A-Z][A-Z0-9_]+-\d+\b", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def _filter_match(pattern, values, name):
    out = []
    for v in values:
        if pattern.match(v):
            out.append(v)
        else:
            print("WARNING: %s: dropping malformed value %r" % (name, v), file=sys.stderr)
    return out


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--rc-catalog-limit", type=int, default=RC_CATALOG_HARD_LIMIT,
                   help="Hard cap on root-cause catalog fetch size (default %d)" % RC_CATALOG_HARD_LIMIT)
    return p.parse_args()


def _wrap_untrusted(text):
    return {"_untrusted": True, "text": text or ""}


def _resolve_triage_output_path():
    """Return the absolute, real path to TRIAGE_OUTPUT_PATH/Issues, or None.
    Symlinks are followed once via realpath; the resolved path must remain
    under the env-supplied root so an attacker cannot redirect the read."""
    raw = os.environ.get("TRIAGE_OUTPUT_PATH", "").strip()
    if not raw or not os.path.isabs(raw):
        return None
    issues_dir = os.path.join(raw, "Issues")
    if not os.path.isdir(issues_dir):
        return None
    real = os.path.realpath(issues_dir)
    real_root = os.path.realpath(raw)
    if not (real == os.path.join(real_root, "Issues") or real.startswith(real_root + os.sep)):
        print("WARNING: TRIAGE_OUTPUT_PATH/Issues resolves outside the configured root; skipping enrichment", file=sys.stderr)
        return None
    return real


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
_AUTOFILL_HEADER_RE = re.compile(r"^##\s+Auto-filled Template Sections\s*$", re.MULTILINE)


def _extract_section(md, section_titles):
    """Pull the body text immediately under a heading whose title is in
    `section_titles` (case-insensitive). Stops at the next heading of equal
    or shallower depth. Returns '' if not found."""
    lines = md.splitlines()
    target_titles = {t.lower() for t in section_titles}
    out_lines = []
    in_section = False
    section_depth = 0
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            depth = len(m.group(1))
            title = m.group(2).strip().lower()
            if title in target_titles:
                in_section = True
                section_depth = depth
                out_lines = []
                continue
            if in_section and depth <= section_depth:
                break
            if in_section:
                # nested deeper heading inside the target section — keep its text but skip the heading itself.
                continue
        if in_section:
            stripped = line.strip()
            # Skip Obsidian admonition markers and the confidence callout that
            # autofill writes immediately under each subsection.
            if stripped.startswith(">"):
                continue
            if stripped.startswith("*Confidence:") and stripped.endswith("*"):
                continue
            out_lines.append(line)
    text = "\n".join(out_lines).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _load_enriched_rc(issues_dir, rc_key):
    """Find <KEY> — *.md under issues_dir, parse the autofilled sections,
    return the enriched dict or None."""
    if not issues_dir:
        return None
    # rc_key is regex-validated upstream; safe to interpolate into a glob.
    pattern = os.path.join(issues_dir, "%s — *.md" % rc_key)
    matches = glob.glob(pattern)
    if not matches:
        return None
    # Pick the lexicographically first match deterministically.
    md_path = sorted(matches)[0]
    real_md = os.path.realpath(md_path)
    if not real_md.startswith(os.path.realpath(issues_dir) + os.sep):
        print("WARNING: enriched markdown for %s resolves outside Issues dir; skipping" % rc_key, file=sys.stderr)
        return None
    try:
        with open(real_md) as f:
            md = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print("WARNING: failed to read enriched markdown for %s: %s" % (rc_key, e), file=sys.stderr)
        return None

    rca = _extract_section(md, ["Root Cause Analysis"])[:MAX_ENRICHED_SECTION_CHARS]
    background = _extract_section(md, ["Background Context"])[:MAX_ENRICHED_SECTION_CHARS]
    analysis = _extract_section(md, ["Analysis"])[:MAX_ENRICHED_SECTION_CHARS]
    if not (rca or background or analysis):
        return None
    return {
        "root_cause_analysis": rca,
        "background_context": background,
        "analysis": analysis,
    }


def _slim_rc(issue, issues_dir=None):
    fields = issue.get("fields") or {}
    description = adf_to_text(fields.get("description") or {})
    snippet = description[:MAX_DESCRIPTION_SNIPPET]
    rc_key = issue.get("key", "") or ""
    out = {
        "key": rc_key,
        "summary": _wrap_untrusted(fields.get("summary") or ""),
        "status": (fields.get("status") or {}).get("name", "") or "",
        "priority": (fields.get("priority") or {}).get("name", "") or "",
        "components": [c.get("name", "") for c in (fields.get("components") or []) if isinstance(c, dict)],
        "labels": list(fields.get("labels") or []),
        "description_snippet": _wrap_untrusted(snippet),
    }
    enriched = _load_enriched_rc(issues_dir, rc_key) if (issues_dir and _ISSUE_KEY_RE.match(rc_key)) else None
    if enriched:
        # The autofilled sections are agent-synthesized from internal Jira data
        # (descriptions, comments) by /root-cause-triage. They paraphrase customer
        # content rather than reproduce it verbatim, so we treat them as TRUSTED
        # context for matching. The original Jira description is intentionally
        # NOT carried here — it stays in description_snippet (untrusted).
        out["enriched"] = enriched
    return out


def _extract_support_rc_text(fields, support_rc_field_id):
    """Read the L2-authored Root Cause free-text custom field. Jira's per-tenant
    custom-field schema is opaque, so we accept either ADF dict, plain string,
    or null."""
    if not support_rc_field_id:
        return ""
    raw = fields.get(support_rc_field_id)
    if not raw:
        return ""
    if isinstance(raw, dict):
        text = adf_to_text(raw)
    elif isinstance(raw, str):
        text = raw
    else:
        text = str(raw)
    return (text or "").strip()[:MAX_SUPPORT_RC_CHARS]


def _slim_ticket(issue, rc_catalog_keys, support_rc_field_id=""):
    """Slim a support ticket for the bundle. Splits issuelinks into
    (rc_links, other_links) — rc_links is what triggers the already-linked
    drop in the caller; other_links are passed to the sub-agent as context."""
    fields = issue.get("fields") or {}
    description = adf_to_text(fields.get("description") or {})[:MAX_DESCRIPTION_CHARS]
    components = [c.get("name", "") for c in (fields.get("components") or []) if isinstance(c, dict)]
    rc_links = []
    other_links = []
    for link in (fields.get("issuelinks") or []):
        link_type = ((link.get("type") or {}).get("name") or "").strip()
        link_inward = ((link.get("type") or {}).get("inward") or "").strip()
        link_outward = ((link.get("type") or {}).get("outward") or "").strip()
        for direction, target in (("inward", link.get("inwardIssue")), ("outward", link.get("outwardIssue"))):
            if not isinstance(target, dict):
                continue
            target_key = target.get("key") or ""
            if not _ISSUE_KEY_RE.match(target_key):
                continue
            target_summary = (target.get("fields") or {}).get("summary", "") or ""
            phrase = link_inward if direction == "inward" else link_outward
            entry = {
                "target_key": target_key,
                "type_name": link_type,
                "phrase": phrase,
                "direction": direction,
                "target_summary": target_summary,
            }
            if target_key in rc_catalog_keys:
                rc_links.append(entry)
            else:
                other_links.append(entry)
    return {
        "key": issue.get("key", ""),
        "summary": _wrap_untrusted(fields.get("summary") or ""),
        "status": (fields.get("status") or {}).get("name", "") or "",
        "priority": (fields.get("priority") or {}).get("name", "Medium"),
        "issuetype": (fields.get("issuetype") or {}).get("name", "") or "",
        "components": components,
        "labels": list(fields.get("labels") or []),
        "created": fields.get("created", "") or "",
        "updated": fields.get("updated", "") or "",
        "resolution": (fields.get("resolution") or {}).get("name", "") if isinstance(fields.get("resolution"), dict) else "",
        "description": _wrap_untrusted(description),
        "support_root_cause": _wrap_untrusted(_extract_support_rc_text(fields, support_rc_field_id)),
        "rc_links": rc_links,
        "other_links": other_links,
    }, rc_links


def _attach_comments(base_url, auth, slim_ticket):
    """Fetch up to MAX_COMMENTS most-recent comments and attach them.

    `jira_get_comments` returns pre-flattened {author, created, body_text}
    records — author is already a display-name string and body_text is
    already adf-decoded plain text.
    """
    try:
        comments_raw = jira_get_comments(base_url, auth, slim_ticket["key"])
    except Exception as e:  # noqa: BLE001
        print("WARNING: failed to fetch comments for %s: %s" % (slim_ticket["key"], e), file=sys.stderr)
        slim_ticket["comments"] = []
        return
    out = []
    for c in (comments_raw or [])[-MAX_COMMENTS:]:
        body_text = (c.get("body_text") or "")[:MAX_COMMENT_CHARS]
        author = c.get("author") or ""
        out.append({
            "author": _wrap_untrusted(author),
            "created": c.get("created", "") or "",
            "body": _wrap_untrusted(body_text),
        })
    slim_ticket["comments"] = out


def fetch_rc_catalog(base_url, auth, rc_epics, limit):
    """JQL: parent in (...) — fetch the full root-cause catalog. Each epic key
    in rc_epics has been validated upstream against the issue-key regex."""
    epic_list = ",".join('"%s"' % k for k in rc_epics)
    jql = "parent in (%s) ORDER BY updated DESC" % epic_list
    issues = jira_search_all(
        base_url, auth, jql,
        "summary,status,priority,components,labels,description",
        limit=limit,
    )
    return issues


def resolve_team_uuids(base_url, auth, project_key, focus_team_field_values, focus_labels):
    """Look up cf[10600] UUIDs by display name. Same pattern as
    support-routing-audit/fetch.py:resolve_team_uuids."""
    uuids = []
    for value in focus_team_field_values:
        found = None
        for label in (focus_labels or [""]):
            label_clause = ('labels = "%s" AND ' % label) if label else ""
            jql = ('project = %s AND %scf[10600] is not EMPTY ORDER BY created DESC'
                   % (project_key, label_clause))
            try:
                lookup = jira_search_all(base_url, auth, jql, "customfield_10600")
            except Exception as e:  # noqa: BLE001
                print("WARNING: UUID lookup failed for %r: %s" % (value, e), file=sys.stderr)
                continue
            for item in lookup[:MAX_UUID_LOOKUP_SCAN]:
                team_obj = (item.get("fields") or {}).get("customfield_10600")
                if isinstance(team_obj, dict) and team_obj.get("name", "").upper() == value.upper():
                    found = team_obj.get("id", "")
                    break
            if found:
                break
        if found and _UUID_RE.match(found):
            uuids.append(found)
        else:
            print("WARNING: Could not resolve cf[10600] UUID for %r" % value, file=sys.stderr)
    return uuids


def build_focus_clause(focus_labels, focus_uuids):
    parts = []
    if focus_labels:
        if len(focus_labels) == 1:
            parts.append('labels = "%s"' % focus_labels[0])
        else:
            label_list = ", ".join('"%s"' % l for l in focus_labels)
            parts.append("labels in (%s)" % label_list)
    if focus_uuids:
        if len(focus_uuids) == 1:
            parts.append('cf[10600] = "%s"' % focus_uuids[0])
        else:
            uuid_list = ", ".join('"%s"' % u for u in focus_uuids)
            parts.append("cf[10600] in (%s)" % uuid_list)
    if not parts:
        print("ERROR: No usable focus filter (no labels and no UUIDs).", file=sys.stderr)
        sys.exit(3)
    return "(" + " OR ".join(parts) + ")"


def fetch_explicit_tickets(base_url, auth, keys):
    """One-by-one GET; per-ticket failure is non-fatal but logged."""
    out = []
    for k in keys:
        try:
            path = "/rest/api/3/issue/%s" % urllib.parse.quote(k, safe="")
            issue = jira_get(base_url, path, auth)
        except Exception as e:  # noqa: BLE001
            print("WARNING: failed to fetch %s: %s" % (k, e), file=sys.stderr)
            continue
        out.append(issue)
    return out


def main():
    args = parse_args()
    if args.rc_catalog_limit < 1 or args.rc_catalog_limit > RC_CATALOG_HARD_LIMIT:
        print("ERROR: --rc-catalog-limit must be 1..%d" % RC_CATALOG_HARD_LIMIT, file=sys.stderr)
        sys.exit(2)

    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        sys.exit(1)

    env = load_env(["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    base_url, auth = init_auth(env)

    support_project_key = setup["env"]["support_project_key"]
    _require_match(_PROJECT_KEY_RE, support_project_key, "support_project_key")

    support_rc_field_id = (setup["env"].get("support_root_cause_field") or "").strip()
    if support_rc_field_id and not re.match(r"\Acustomfield_\d{4,8}\Z", support_rc_field_id):
        print("WARNING: support_root_cause_field %r malformed; ignoring." % support_rc_field_id, file=sys.stderr)
        support_rc_field_id = ""

    focus_team = setup["focus_team"]
    _require_match(_TEAM_NAME_RE, focus_team, "focus_team")

    rc_epics = setup.get("rc_epics") or []
    rc_epics = _filter_match(_ISSUE_KEY_RE, rc_epics, "rc_epics")
    if not rc_epics:
        print("ERROR: rc_epics empty after validation.", file=sys.stderr)
        sys.exit(1)

    mode = setup.get("mode") or "auto_discover"
    if mode not in ("auto_discover", "explicit_keys"):
        print("ERROR: setup.json has invalid mode %r" % mode, file=sys.stderr)
        sys.exit(1)

    print("[fetch] Fetching root-cause catalog (parent in %d epic(s)) ..." % len(rc_epics), file=sys.stderr)
    rc_raw = fetch_rc_catalog(base_url, auth, rc_epics, args.rc_catalog_limit)
    issues_dir = _resolve_triage_output_path()
    if issues_dir:
        print("[fetch] Loading enriched RC analyses from %s" % issues_dir, file=sys.stderr)
    else:
        print("[fetch] No TRIAGE_OUTPUT_PATH/Issues found — using raw Jira fields only", file=sys.stderr)
    rc_catalog = [_slim_rc(i, issues_dir=issues_dir) for i in rc_raw]
    rc_catalog_keys = {rc["key"] for rc in rc_catalog if rc.get("key")}
    enriched_count = sum(1 for rc in rc_catalog if rc.get("enriched"))
    print("[fetch] Catalog has %d root-cause tickets (%d with enriched analysis)" % (
        len(rc_catalog), enriched_count), file=sys.stderr)

    candidate_issues = []
    if mode == "explicit_keys":
        keys = setup.get("explicit_keys") or []
        keys = _filter_match(_ISSUE_KEY_RE, keys, "explicit_keys")
        if not keys:
            print("ERROR: explicit_keys empty after validation.", file=sys.stderr)
            sys.exit(1)
        print("[fetch] Explicit mode: fetching %d ticket(s) ..." % len(keys), file=sys.stderr)
        candidate_issues = fetch_explicit_tickets(base_url, auth, keys)
        period = None
    else:
        focus_labels_raw = (setup.get("focus_label") or "").split(",") if setup.get("focus_label") else []
        focus_labels = _filter_match(_LABEL_RE, [l.strip() for l in focus_labels_raw if l.strip()], "focus_label")
        focus_field_values_raw = (setup.get("focus_team_field_value") or "").split(",") if setup.get("focus_team_field_value") else []
        focus_field_values = _filter_match(_TEAM_NAME_RE, [v.strip() for v in focus_field_values_raw if v.strip()], "focus_team_field_value")

        focus_uuids = []
        if focus_field_values:
            print("[fetch] Resolving cf[10600] UUIDs for %s" % focus_field_values, file=sys.stderr)
            focus_uuids = resolve_team_uuids(base_url, auth, support_project_key, focus_field_values, focus_labels)
            print("[fetch] Resolved UUIDs: %s" % focus_uuids, file=sys.stderr)

        focus_clause = build_focus_clause(focus_labels, focus_uuids)
        since_days = int(setup.get("since_days") or 30)
        if since_days < 1 or since_days > 90:
            print("ERROR: setup.json since_days out of range: %r" % since_days, file=sys.stderr)
            sys.exit(2)

        # statusCategory = Done filters to closed/resolved/done terminal states
        # without enumerating them. In-progress tickets are excluded — the
        # engineer working them may still link a root cause themselves.
        jql = (
            'project = %s AND %s AND statusCategory = Done '
            'AND resolved >= -%dd ORDER BY resolved DESC'
            % (support_project_key, focus_clause, since_days)
        )
        print("[fetch] Stage 1 JQL: %s" % jql, file=sys.stderr)
        stage1_fields = "summary,status,priority,components,labels,created,updated,resolution,issuetype,description,issuelinks"
        if support_rc_field_id:
            stage1_fields += "," + support_rc_field_id
        candidate_issues = jira_search_all(
            base_url, auth, jql, stage1_fields,
            limit=AUTO_DISCOVER_CANDIDATE_CAP,
        )
        print("[fetch] Stage 1 returned %d candidate(s)" % len(candidate_issues), file=sys.stderr)
        period = {"since_days": since_days}

    # Stage 2 / explicit-mode common path: slim, drop in-progress tickets,
    # then split out already-linked tickets.
    kept_tickets = []
    already_linked = []
    open_skipped = []
    pre_decided = []
    pre_decided_out_of_catalog = []
    for issue in candidate_issues:
        slim, rc_links = _slim_ticket(issue, rc_catalog_keys, support_rc_field_id)
        # statusCategory comes back as the Jira object's category key (e.g.
        # "done", "indeterminate", "new"). Only "done" is terminal.
        status_category = (((issue.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key", "")
        if status_category != "done":
            open_skipped.append({
                "key": slim["key"],
                "summary": slim["summary"],
                "status": slim["status"],
            })
            continue
        if rc_links:
            already_linked.append({
                "key": slim["key"],
                "summary": slim["summary"],
                "rc_links": rc_links,
            })
            continue
        # If L2 typed a Jira key directly into the support_root_cause field,
        # split into pre_decided (key in catalog → 1:1 link, skip sub-agent)
        # and pre_decided_out_of_catalog (key not in catalog → surface for the
        # operator to either expand ROOT_CAUSE_EPICS or apply manually).
        sr_text = (slim.get("support_root_cause") or {}).get("text", "") or ""
        seen = set()
        all_named = [k for k in _ISSUE_KEY_FINDALL_RE.findall(sr_text)
                     if not (k in seen or seen.add(k))]
        catalog_named = [k for k in all_named if k in rc_catalog_keys]
        non_catalog_named = [k for k in all_named if k not in rc_catalog_keys]
        if catalog_named:
            slim["support_rc_named_keys"] = catalog_named
            pre_decided.append(slim)
            continue
        if non_catalog_named:
            slim["support_rc_named_keys_out_of_catalog"] = non_catalog_named
            pre_decided_out_of_catalog.append(slim)
            continue
        kept_tickets.append(slim)
    if open_skipped:
        print("[fetch] Skipped %d still-open ticket(s) — engineers may still link root cause themselves:" % len(open_skipped),
              file=sys.stderr)
        for entry in open_skipped[:10]:
            print("  - %s [%s]" % (entry["key"], entry["status"]), file=sys.stderr)
        if len(open_skipped) > 10:
            print("  - …and %d more" % (len(open_skipped) - 10), file=sys.stderr)

    truncated = False
    max_tickets = int(setup.get("max_tickets") or 50)
    if len(kept_tickets) > max_tickets:
        kept_tickets.sort(key=lambda t: t.get("created", ""), reverse=True)
        kept_tickets = kept_tickets[:max_tickets]
        truncated = True
        print("WARNING: truncated kept_tickets to --max-tickets=%d" % max_tickets, file=sys.stderr)

    print("[fetch] After RC-link filter: %d kept, %d already linked, %d pre-decided via L2 Root Cause field, %d pre-decided out-of-catalog" % (
        len(kept_tickets), len(already_linked), len(pre_decided), len(pre_decided_out_of_catalog)), file=sys.stderr)

    print("[fetch] Fetching comments for %d kept ticket(s) ..." % len(kept_tickets), file=sys.stderr)
    for slim in kept_tickets:
        _attach_comments(base_url, auth, slim)

    data = {
        "focus_team": focus_team,
        "vault_dir": setup.get("vault_dir") or focus_team,
        "mode": mode,
        "period": period,
        "rc_epics": rc_epics,
        "rc_catalog": rc_catalog,
        "rc_catalog_count": len(rc_catalog),
        "tickets": kept_tickets,
        "pre_decided": pre_decided,
        "pre_decided_out_of_catalog": pre_decided_out_of_catalog,
        "already_linked": already_linked,
        "open_skipped": open_skipped,
        "candidates_count": len(candidate_issues),
        "kept_count": len(kept_tickets),
        "truncated": truncated,
    }
    atomic_write_json(DATA_PATH, data)
    print("Wrote %d ticket(s) + %d catalog entries to %s" % (
        len(kept_tickets), len(rc_catalog), DATA_PATH))


if __name__ == "__main__":
    main()
