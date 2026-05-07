#!/usr/bin/env python3
"""Post (or update) an AI-enriched root-cause comment on a Jira ticket.

Reads the enriched vault Markdown for each requested key, builds an ADF body,
and posts a comment on the matching Jira issue. If a prior AI-enriched comment
already exists (detected by header marker), updates it in place via PUT.

The original ticket description and other fields are NEVER modified — this only
adds or updates a single comment.

Usage:
    python3 comment.py --issue PROJ-123 [--dry-run]
    python3 comment.py --keys PROJ-123,PROJ-124 [--dry-run]
    python3 comment.py --from-file /tmp/keys.txt [--dry-run]
"""

import argparse
import json
import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import (
    init_auth,
    jira_get_comments,
    jira_get_myself,
    jira_post,
    jira_put,
    load_env,
)

sys.path.insert(0, os.path.dirname(__file__))
from _vault import find_issue_markdown


ENV_KEYS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "TRIAGE_OUTPUT_PATH"]

# Argument allow-list: standard Jira issue key. Anchored + ASCII per the security checklist.
KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
# Jira comment ids are numeric strings; validate before interpolation into the PUT URL path.
COMMENT_ID_RE = re.compile(r"\A\d+\Z", re.ASCII)

# Marker — must be the first line of any prior AI comment we want to update in place.
COMMENT_HEADER = "🤖 AI enriched root cause information"
COMMENT_INTRO = (
    "Auto-generated from the root-cause-triage knowledge base. "
    "Re-runs update this comment in place."
)

# Canonical autofill section order (matches autofill.py:357).
AUTOFILL_SECTIONS = [
    "Background Context",
    "Steps to reproduce",
    "Actual Results",
    "Expected Results",
    "Analysis",
]

INSUFFICIENT_EVIDENCE_MARKER = "*(insufficient evidence)*"

# Soft cap on the serialized ADF body. Jira enforces a server-side limit on
# comment size; staying well under it keeps the failure mode "skip with a clear
# message" instead of "400 with a raw response body printed to stderr".
MAX_ADF_BYTES = 32_000


def parse_args():
    p = argparse.ArgumentParser(description="Post AI-enriched root cause info as a Jira comment")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--issue", help="Single Jira key, e.g. PROJ-123")
    src.add_argument("--keys", help="Comma-separated Jira keys")
    src.add_argument("--from-file", help="Path to file with one Jira key per line")
    p.add_argument("--dry-run", action="store_true", help="Print ADF body without posting")
    return p.parse_args()


def _resolve_from_file(path, allowed_roots):
    """Open a --from-file path, rejecting symlinks and any path that resolves
    outside the allowed roots. Returns a list of stripped, non-empty lines.
    """
    if os.path.islink(path):
        print("ERROR: --from-file path is a symlink; refusing to read", file=sys.stderr)
        sys.exit(1)
    real = os.path.realpath(path)
    allowed_real = [os.path.realpath(r) for r in allowed_roots if r]
    if not any(real == r or real.startswith(r + os.sep) for r in allowed_real):
        print(
            "ERROR: --from-file path %r is outside the allowed roots (%s)"
            % (path, ", ".join(allowed_real)),
            file=sys.stderr,
        )
        sys.exit(1)
    with open(real) as f:
        return [line.strip() for line in f if line.strip()]


def collect_keys(args, allowed_from_file_roots):
    if args.issue:
        keys = [args.issue.strip()]
    elif args.keys:
        keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    else:
        keys = _resolve_from_file(args.from_file, allowed_from_file_roots)

    invalid = [k for k in keys if not KEY_RE.match(k)]
    if invalid:
        print("ERROR: invalid Jira key(s): %s" % ", ".join(invalid), file=sys.stderr)
        print("Expected pattern: PROJ-123 (uppercase project key, hyphen, digits)", file=sys.stderr)
        sys.exit(1)
    if not keys:
        print("ERROR: no keys supplied", file=sys.stderr)
        sys.exit(1)
    return keys


def strip_frontmatter(content):
    if not content.startswith("---\n"):
        return content
    end = content.find("\n---\n", 4)
    if end == -1:
        return content
    return content[end + len("\n---\n"):]


def extract_rca(body):
    """Return the Root Cause Analysis section content (or empty string)."""
    # `\n+?` (non-greedy) so an empty section doesn't consume the next ## heading.
    m = re.search(
        r"^## Root Cause Analysis\n+?(.*?)(?=\n## |\Z)",
        body,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not m:
        return ""
    return m.group(1).strip()


def extract_autofill(body):
    """Return {section_name: {"confidence": str, "content": str}} for present autofill sections.

    Skips any section whose content is empty or marked as insufficient evidence.
    """
    block_match = re.search(
        r"^## Auto-filled Template Sections\n+?(.*?)(?=\n## |\Z)",
        body,
        flags=re.DOTALL | re.MULTILINE,
    )
    if not block_match:
        return {}
    block = block_match.group(1)

    # Drop the leading Obsidian callout if present.
    block = re.sub(r"^> \[!note\].*?\n+", "", block, count=1, flags=re.DOTALL)

    sections = {}
    # Split on ### headings, capturing each section name + body.
    parts = re.split(r"^### (.+)$", block, flags=re.MULTILINE)
    # parts[0] is anything before the first ###; subsequent items pair (name, body).
    for i in range(1, len(parts), 2):
        name = parts[i].strip()
        if i + 1 >= len(parts):
            continue
        section_body = parts[i + 1]

        confidence = "unknown"
        conf_match = re.search(r"^\*Confidence: (high|medium|low|unknown)\*\s*$", section_body, flags=re.MULTILINE)
        if conf_match:
            confidence = conf_match.group(1)
            section_body = section_body.replace(conf_match.group(0), "", 1)

        section_body = section_body.strip()
        if not section_body or INSUFFICIENT_EVIDENCE_MARKER in section_body:
            continue

        sections[name] = {"confidence": confidence, "content": section_body}
    return sections


def text_node(text, marks=None):
    node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def paragraphs_from_text(text):
    """Split content on blank lines; build one ADF paragraph node per chunk."""
    chunks = [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    return [
        {"type": "paragraph", "content": [text_node(chunk)]}
        for chunk in chunks
    ]


def build_adf(rca, autofill_sections):
    content = [
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [text_node(COMMENT_HEADER)],
        },
        {
            "type": "paragraph",
            "content": [text_node(COMMENT_INTRO, marks=[{"type": "em"}])],
        },
    ]

    if rca:
        content.append({
            "type": "heading",
            "attrs": {"level": 3},
            "content": [text_node("Root Cause Analysis")],
        })
        content.extend(paragraphs_from_text(rca))

    for name in AUTOFILL_SECTIONS:
        section = autofill_sections.get(name)
        if not section:
            continue
        content.append({
            "type": "heading",
            "attrs": {"level": 3},
            "content": [text_node(name)],
        })
        content.append({
            "type": "paragraph",
            "content": [text_node("Confidence: %s" % section["confidence"], marks=[{"type": "em"}])],
        })
        content.extend(paragraphs_from_text(section["content"]))

    return {"version": 1, "type": "doc", "content": content}


def find_ai_comments(comments):
    """Return [(id, author_account_id), …] for comments whose first body line equals COMMENT_HEADER."""
    matches = []
    for c in comments:
        body = (c.get("body_text") or "").strip()
        if not body:
            continue
        if body.splitlines()[0].strip() == COMMENT_HEADER:
            matches.append((c.get("id", ""), c.get("author_account_id", "")))
    return matches


def process_key(key, issues_dir, base_url, auth, my_account_id, dry_run):
    """Returns one of: 'posted', 'updated', 'no-vault', 'no-enrichment'."""
    vault_path = find_issue_markdown(issues_dir, key)
    if not vault_path:
        print("  skipped %s — vault file missing" % key)
        return "no-vault"

    with open(vault_path) as f:
        content = f.read()
    body = strip_frontmatter(content)

    rca = extract_rca(body)
    autofill = extract_autofill(body)

    if not rca and not autofill:
        print("  skipped %s — no enrichment" % key)
        return "no-enrichment"

    adf = build_adf(rca, autofill)
    payload = {"body": adf}

    body_bytes = len(json.dumps(payload).encode("utf-8"))
    if body_bytes > MAX_ADF_BYTES:
        print(
            "  skipped %s — comment body %d bytes exceeds %d-byte cap; trim the vault file"
            % (key, body_bytes, MAX_ADF_BYTES),
            file=sys.stderr,
        )
        return "no-enrichment"

    if dry_run:
        present = []
        if rca:
            present.append("RCA")
        if autofill:
            present.append("autofill (%s)" % ", ".join(autofill.keys()))
        print("  [dry-run] %s — vault: %s — sections: %s" % (key, vault_path, "; ".join(present) or "none"))
        print(json.dumps(adf, indent=2))
        return "dry-run"

    comments = jira_get_comments(base_url, auth, key)
    all_markers = find_ai_comments(comments)
    own_markers = [(cid, aid) for cid, aid in all_markers if aid and aid == my_account_id]
    foreign_markers = [(cid, aid) for cid, aid in all_markers if aid != my_account_id]

    if foreign_markers:
        # Marker is spoofable. Refuse to clobber a comment authored by anyone else;
        # post a new one alongside instead. The duplicate will surface in the next
        # run's warning so a human can clean up.
        print(
            "  WARN: %s — found AI marker on comment(s) %s authored by someone else; "
            "will post a new comment alongside" % (key, ", ".join(cid for cid, _ in foreign_markers)),
            file=sys.stderr,
        )

    if len(own_markers) > 1:
        print(
            "  WARN: multiple AI comments by us on %s (ids: %s); updating the first only"
            % (key, ", ".join(cid for cid, _ in own_markers)),
            file=sys.stderr,
        )

    existing_id = own_markers[0][0] if own_markers else ""
    if existing_id and not COMMENT_ID_RE.match(existing_id):
        # Defensive: Jira comment ids are numeric strings. A non-numeric id (test mock,
        # MITM, future API change) must not be interpolated into the PUT URL path.
        print(
            "  skipped %s — refusing to PUT to non-numeric comment id %r" % (key, existing_id),
            file=sys.stderr,
        )
        return "no-enrichment"

    if existing_id:
        path = "/rest/api/3/issue/%s/comment/%s" % (key, existing_id)
        jira_put(base_url, path, auth, payload)
        print("  updated %s (comment %s)" % (key, existing_id))
        return "updated"
    else:
        path = "/rest/api/3/issue/%s/comment" % key
        jira_post(base_url, path, auth, payload)
        print("  posted %s (new)" % key)
        return "posted"


def main():
    args = parse_args()

    env = load_env(ENV_KEYS)
    base_url, auth = init_auth(env)
    output_path = env["TRIAGE_OUTPUT_PATH"]
    if not output_path:
        print("ERROR: TRIAGE_OUTPUT_PATH not set", file=sys.stderr)
        sys.exit(1)
    issues_dir = os.path.join(output_path, "Issues")

    keys = collect_keys(args, allowed_from_file_roots=[output_path, "/tmp"])

    my_account_id = ""
    if not args.dry_run:
        myself = jira_get_myself(base_url, auth)
        my_account_id = myself.get("account_id", "")
        if not my_account_id:
            print("ERROR: /rest/api/3/myself returned no accountId; cannot verify comment ownership", file=sys.stderr)
            sys.exit(1)

    print("Comment mode — %d ticket(s)%s" % (len(keys), " (dry-run)" if args.dry_run else ""))

    counts = {"posted": 0, "updated": 0, "dry-run": 0, "no-vault": 0, "no-enrichment": 0}
    for key in keys:
        result = process_key(key, issues_dir, base_url, auth, my_account_id, args.dry_run)
        counts[result] = counts.get(result, 0) + 1

    print()
    print("--- Summary ---")
    for k, v in counts.items():
        if v:
            print("  %s: %d" % (k, v))

    actionable = counts["posted"] + counts["updated"] + counts["dry-run"]
    if actionable == 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
