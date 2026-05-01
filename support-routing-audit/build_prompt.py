#!/usr/bin/env python3
"""Build the audit sub-agent bundle: trim per-ticket records, wrap untrusted
fields, attach charters text. Writes /tmp/support-routing-audit/bundle.json."""

import json
import os
import sys

from jira_client import ensure_tmp_dir, atomic_write_json


CACHE_DIR = "/tmp/support-routing-audit"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")
BUNDLE_PATH = os.path.join(CACHE_DIR, "bundle.json")

# Hard cap matches build_charter_prompt.py:42. Current charters.md ~14KB.
MAX_CHARTERS_BYTES = 32_000

# Per-ticket caps for the fields we ship to the sub-agent. Trimming was
# already applied at fetch time; this is belt-and-braces in case someone
# re-runs build_prompt against an older data.json.
MAX_DESCRIPTION_CHARS = 1500
MAX_COMMENT_CHARS = 800
MAX_COMMENTS_SHIPPED = 5
MAX_TRANSITIONS_SHIPPED = 8


def _untrusted(text):
    """Wrap a free-text field that came from a user (reporter, customer,
    description, comment body, etc.). The sub-agent treats `_untrusted: true`
    fields as data, never as instructions — see AUDIT_PROMPT.md security banner."""
    if not isinstance(text, str):
        text = str(text or "")
    return {"_untrusted": True, "text": text}


def _trim(s, n):
    if not isinstance(s, str):
        return ""
    return s.strip()[:n]


def build_ticket_record(t):
    transitions = t.get("team_transitions") or []
    # Cap the transitions sent to the sub-agent — bouncing tickets accumulate
    # entries quickly and most reasoning needs only the recent few + the first.
    if len(transitions) > MAX_TRANSITIONS_SHIPPED:
        kept = transitions[:1] + transitions[-(MAX_TRANSITIONS_SHIPPED - 1):]
    else:
        kept = transitions
    transitions_out = []
    for tr in kept:
        transitions_out.append({
            "from": tr.get("from", "") or "",
            "to": tr.get("to", "") or "",
            "when": tr.get("when", "") or "",
            "who": _untrusted(tr.get("who", "") or ""),
        })

    comments_out = []
    for c in (t.get("comments") or [])[:MAX_COMMENTS_SHIPPED]:
        comments_out.append({
            "author": _untrusted(c.get("author", "") or ""),
            "created": c.get("created", "") or "",
            "body": _untrusted(_trim(c.get("body_text", ""), MAX_COMMENT_CHARS)),
        })

    return {
        "key": t.get("key", ""),
        "summary": _untrusted(t.get("summary", "") or ""),
        "current_team": t.get("current_team", "") or "",
        "first_team": t.get("first_team", "") or "",
        "team_transitions": transitions_out,
        "transition_count": t.get("transition_count", 0),
        "components": list(t.get("components") or []),
        "labels": list(t.get("labels") or []),
        "status": t.get("status", "") or "",
        "resolution": t.get("resolution", "") or "",
        "priority": t.get("priority", "") or "Medium",
        "issuetype": t.get("issuetype", "") or "",
        "reporter": _untrusted(t.get("reporter", "") or ""),
        "assignee": _untrusted(t.get("assignee", "") or ""),
        "created": t.get("created", "") or "",
        "resolutiondate": t.get("resolutiondate", "") or "",
        "description": _untrusted(_trim(t.get("description_text", ""), MAX_DESCRIPTION_CHARS)),
        "comments": comments_out,
    }


def main():
    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        return 1
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run fetch.py first." % DATA_PATH, file=sys.stderr)
        return 1

    charters_path = setup.get("charters_path")
    if not charters_path or not os.path.exists(charters_path):
        print("ERROR: charters_path missing from setup or file gone: %r" % charters_path, file=sys.stderr)
        return 1
    try:
        with open(charters_path) as f:
            charters_text = f.read()
    except OSError as e:
        print("ERROR: could not read charters file %s: %s" % (charters_path, e), file=sys.stderr)
        return 1

    encoded = charters_text.encode("utf-8")
    if len(encoded) > MAX_CHARTERS_BYTES:
        print("WARNING: charters file is %d bytes (cap %d) — truncating." % (
            len(encoded), MAX_CHARTERS_BYTES), file=sys.stderr)
        # Truncate by bytes; decode with errors="ignore" to drop a partial
        # multi-byte char at the cut.
        charters_text = encoded[:MAX_CHARTERS_BYTES].decode("utf-8", errors="ignore")

    tickets = [build_ticket_record(t) for t in (data.get("tickets") or [])]

    bundle = {
        "focus_team": data.get("focus_team") or setup.get("focus_team"),
        "focus_team_field_values": data.get("focus_team_field_values") or [],
        "allowed_teams": setup.get("allowed_teams") or [],
        "period": data.get("period") or setup.get("period"),
        "charters_source": setup.get("charters_source", ""),
        "charters_text": charters_text,
        "tickets": tickets,
    }

    ensure_tmp_dir(CACHE_DIR)
    atomic_write_json(BUNDLE_PATH, bundle)
    print("Wrote %d tickets to %s (charters: %d bytes from %s)" % (
        len(tickets), BUNDLE_PATH, len(charters_text.encode("utf-8")), bundle["charters_source"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
