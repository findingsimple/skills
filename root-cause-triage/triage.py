#!/usr/bin/env python3
"""Execute confirmed triage transitions and add comments for 'More Info Required' issues."""

import json
import re
import sys
import os
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from jira_client import load_env, init_auth, jira_get, jira_post

ENV_KEYS = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]

TRANSITION_NAMES = {
    "ready": "Ready for Development",
    "more_info": "More Info Required",
    "duplicate": "Rejected",
}


HISTORY_FILE = os.path.expanduser("~/.claude/skills/root-cause-triage/triage_history.json")
HISTORY_RETENTION_DAYS = 90


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--actions-file", required=True, help="Path to JSON file with confirmed actions")
    p.add_argument("--dry-run", action="store_true", help="Print planned actions without executing")
    return p.parse_args()


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(actions, results):
    """Append successful actions to the history file, pruning entries older than retention window."""
    from datetime import timedelta
    today = datetime.now(timezone.utc).date().isoformat()
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()

    history = load_history()
    # Prune old entries
    history = [h for h in history if h.get("date", "") >= cutoff]

    # Build a map of results for quick lookup
    result_map = {r["key"]: r["status"] for r in results}

    for action in actions:
        key = action["key"]
        if result_map.get(key) not in ("ok", "partial"):
            continue  # Only record transitions that were executed
        history.append({
            "date": today,
            "key": key,
            "summary": action.get("summary", ""),
            "action": action["action"],
            "quality_note": action.get("quality_note"),
            "recurrence_of": action.get("recurrence_of"),
        })

    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp_file = HISTORY_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp_file, HISTORY_FILE)
    print("Triage history updated (%d total entries)" % len(history), file=sys.stderr)


def discover_transitions(base_url, auth, issue_key):
    """Get available transitions for an issue. Returns dict of lowercase name -> transition id."""
    data = jira_get(base_url, "/rest/api/3/issue/%s/transitions" % issue_key, auth)
    transitions = {}
    for t in data.get("transitions", []):
        transitions[t["name"].lower()] = t["id"]
    return transitions


def find_transition_id(transitions, target_name):
    """Case-insensitive match for transition name."""
    target_lower = target_name.lower()
    # Exact match first
    if target_lower in transitions:
        return transitions[target_lower]
    # Partial match
    for name, tid in transitions.items():
        if target_lower in name or name in target_lower:
            return tid
    return None


def execute_transition(base_url, auth, issue_key, transition_id):
    """Transition an issue to a new status."""
    jira_post(base_url, "/rest/api/3/issue/%s/transitions" % issue_key, auth, {
        "transition": {"id": transition_id},
    })


def add_duplicate_comment(base_url, auth, issue_key, duplicate_of):
    """Add a comment noting the issue was rejected as a duplicate (v2 API)."""
    body = (
        "This ticket was triaged and flagged as a likely duplicate of *%s*.\n\n"
        "Please review both tickets. If this is not a duplicate, move it back to *To Triage* with a comment explaining the difference."
    ) % duplicate_of

    jira_post(base_url, "/rest/api/2/issue/%s/comment" % issue_key, auth, {
        "body": body,
    })


def add_comment(base_url, auth, issue_key, missing_sections, quality_note=None):
    """Add a wiki-markup comment listing missing sections or quality note (v2 API)."""
    body = (
        "This ticket was reviewed during triage but needs more detail before it can move to development.\n\n"
        "To progress, the description should clearly explain:\n"
        "* *What the root cause is* — not just the symptom, but what underlying issue caused the behaviour\n"
        "* *Why it matters* — enough context for a product manager to understand the impact and scope a solution\n\n"
    )

    if missing_sections:
        section_list = "\n".join("* %s" % s for s in missing_sections)
        body += "The following template sections appear to be missing or incomplete:\n%s\n\n" % section_list
    elif quality_note:
        body += "Triage note: %s\n\n" % quality_note

    body += "Please update the ticket with the missing details and move it back to *To Triage* when ready."

    jira_post(base_url, "/rest/api/2/issue/%s/comment" % issue_key, auth, {
        "body": body,
    })


def process_action(base_url, auth, action, transition_cache, cache_lock, dry_run):
    """Process a single triage action. Returns (key, status, message)."""
    key = action["key"]
    action_type = action["action"]
    missing = action.get("missing_sections", [])

    target_name = TRANSITION_NAMES.get(action_type)
    if not target_name:
        return key, "error", "Unknown action type: %s" % action_type

    with cache_lock:
        transition_id = find_transition_id(transition_cache, target_name)
    if not transition_id:
        # Try discovering transitions for this specific issue (may differ from first issue)
        issue_transitions = discover_transitions(base_url, auth, key)
        transition_id = find_transition_id(issue_transitions, target_name)
        if transition_id:
            with cache_lock:
                transition_cache.update(issue_transitions)
        else:
            available = ", ".join(issue_transitions.keys())
            return key, "error", "No transition matching '%s' found. Available: %s" % (target_name, available)

    if dry_run:
        msg = "Would transition to '%s'" % target_name
        if action_type == "more_info" and missing:
            msg += " + add comment (missing: %s)" % ", ".join(missing)
        if action_type == "duplicate":
            msg += " + add comment (duplicate of %s)" % action.get("duplicate_of", "unknown")
        return key, "dry_run", msg

    try:
        execute_transition(base_url, auth, key, transition_id)
    except Exception as e:
        return key, "error", "Transition failed: %s" % e

    try:
        if action_type == "more_info":
            quality_note = action.get("quality_note")
            add_comment(base_url, auth, key, missing, quality_note=quality_note)
            return key, "ok", "Moved to '%s' + comment added" % target_name
        if action_type == "duplicate":
            duplicate_of = action.get("duplicate_of", "unknown")
            add_duplicate_comment(base_url, auth, key, duplicate_of)
            return key, "ok", "Rejected as duplicate of %s + comment added" % duplicate_of
        return key, "ok", "Moved to '%s'" % target_name
    except Exception as e:
        return key, "partial", "Moved to '%s' but comment failed: %s" % (target_name, e)


def main():
    args = parse_args()

    # Load actions
    try:
        with open(args.actions_file, "r") as f:
            actions = json.load(f)
    except Exception as e:
        print("ERROR: Could not read actions file: %s" % e)
        sys.exit(1)

    if not actions:
        print("No actions to execute.")
        sys.exit(0)

    for action in actions:
        key = action.get("key", "")
        if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", key):
            print("ERROR: Invalid issue key '%s' in actions file" % key)
            sys.exit(1)

    env = load_env(ENV_KEYS)
    missing = [v for v in ENV_KEYS if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url, auth = init_auth(env)

    # Discover transitions from the first issue (cache for reuse, updated on miss)
    print("Discovering available transitions...", file=sys.stderr)
    transition_cache = discover_transitions(base_url, auth, actions[0]["key"])
    cache_lock = threading.Lock()
    print("Available transitions: %s" % ", ".join(transition_cache.keys()), file=sys.stderr)

    # Execute actions in parallel
    print("Processing %d actions%s...\n" % (len(actions), " (DRY RUN)" if args.dry_run else ""), file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(process_action, base_url, auth, action, transition_cache, cache_lock, args.dry_run): action
            for action in actions
        }
        for future in as_completed(futures):
            key, status, message = future.result()
            results.append({"key": key, "status": status, "message": message})

    # Sort results by key for consistent output
    results.sort(key=lambda r: r["key"])

    # Output results table
    print("| Key | Status | Details |")
    print("|-----|--------|---------|")
    for r in results:
        status_icon = {"ok": "Done", "dry_run": "Dry Run", "partial": "PARTIAL", "error": "ERROR"}.get(r["status"], "UNKNOWN")
        print("| %s | %s | %s |" % (r["key"], status_icon, r["message"]))

    # Summary counts
    ok_count = sum(1 for r in results if r["status"] == "ok")
    partial_count = sum(1 for r in results if r["status"] == "partial")
    dry_count = sum(1 for r in results if r["status"] == "dry_run")
    error_count = sum(1 for r in results if r["status"] == "error")

    ready_count = sum(1 for a in actions if a["action"] == "ready")
    more_info_count = sum(1 for a in actions if a["action"] == "more_info")
    duplicate_count = sum(1 for a in actions if a["action"] == "duplicate")

    print("\nSummary: %d ready for dev, %d more info required, %d duplicates" % (ready_count, more_info_count, duplicate_count))

    if args.dry_run:
        print("(DRY RUN — no changes were made)")
    elif error_count > 0 or partial_count > 0:
        print("%d succeeded, %d partial (comment failed), %d failed" % (ok_count, partial_count, error_count))
        save_history(actions, results)
    else:
        print("All %d transitions completed successfully" % ok_count)
        save_history(actions, results)


if __name__ == "__main__":
    main()
