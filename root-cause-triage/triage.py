#!/usr/bin/env python3
"""Execute confirmed triage transitions and add comments for 'More Info Required' issues."""

import subprocess
import json
import sys
import urllib.request
import urllib.parse
import base64
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

TRANSITION_NAMES = {
    "ready": "Ready for Development",
    "more_info": "More Info Required",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--actions-file", required=True, help="Path to JSON file with confirmed actions")
    p.add_argument("--dry-run", action="store_true", help="Print planned actions without executing")
    return p.parse_args()


def load_env():
    result = subprocess.run(
        ["bash", "-c", "source ~/.sprint_summary_env && env"],
        capture_output=True,
        text=True,
    )
    return dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )


def jira_get(base_url, path, auth):
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def jira_post(base_url, path, auth, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        base_url + path,
        data=body,
        headers={
            "Authorization": "Basic " + auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        response_body = resp.read()
        if response_body:
            return json.loads(response_body)
        return None


def discover_transitions(base_url, auth, issue_key):
    """Get available transitions for an issue. Returns dict of lowercase name → transition id."""
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


def add_comment(base_url, auth, issue_key, missing_sections):
    """Add a wiki-markup comment listing missing sections (v2 API)."""
    section_list = "\n".join("* %s" % s for s in missing_sections)
    body = (
        "This ticket was triaged and requires more information before it can be moved to development.\n\n"
        "The following sections are missing or incomplete:\n"
        "%s\n\n"
        "Please update the description with the missing details and move back to *To Triage* when ready."
    ) % section_list

    jira_post(base_url, "/rest/api/2/issue/%s/comment" % issue_key, auth, {
        "body": body,
    })


def process_action(base_url, auth, action, transition_cache, dry_run):
    """Process a single triage action. Returns (key, status, message)."""
    key = action["key"]
    action_type = action["action"]
    missing = action.get("missing_sections", [])

    target_name = TRANSITION_NAMES.get(action_type)
    if not target_name:
        return key, "error", "Unknown action type: %s" % action_type

    # Discover transitions (use cache if available)
    if not transition_cache:
        transitions = discover_transitions(base_url, auth, key)
        transition_cache.update(transitions)
    else:
        transitions = transition_cache

    transition_id = find_transition_id(transitions, target_name)
    if not transition_id:
        # Try discovering transitions for this specific issue (may differ)
        transitions = discover_transitions(base_url, auth, key)
        transition_id = find_transition_id(transitions, target_name)
        if not transition_id:
            available = ", ".join(transitions.keys())
            return key, "error", "No transition matching '%s' found. Available: %s" % (target_name, available)

    if dry_run:
        msg = "Would transition to '%s'" % target_name
        if action_type == "more_info" and missing:
            msg += " + add comment (missing: %s)" % ", ".join(missing)
        return key, "dry_run", msg

    try:
        execute_transition(base_url, auth, key, transition_id)

        if action_type == "more_info" and missing:
            add_comment(base_url, auth, key, missing)
            return key, "ok", "Moved to '%s' + comment added" % target_name
        return key, "ok", "Moved to '%s'" % target_name
    except Exception as e:
        return key, "error", str(e)


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

    env = load_env()
    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [v for v in required if v not in env]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url = env["JIRA_BASE_URL"]
    email = env["JIRA_EMAIL"]
    token = env["JIRA_API_TOKEN"]
    auth = base64.b64encode((email + ":" + token).encode()).decode()

    # Discover transitions from the first issue (cache for reuse)
    print("Discovering available transitions...", file=sys.stderr)
    transition_cache = discover_transitions(base_url, auth, actions[0]["key"])
    print("Available transitions: %s" % ", ".join(transition_cache.keys()), file=sys.stderr)

    # Execute actions in parallel
    print("Processing %d actions%s...\n" % (len(actions), " (DRY RUN)" if args.dry_run else ""), file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(process_action, base_url, auth, action, transition_cache, args.dry_run): action
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
        status_icon = {"ok": "Done", "dry_run": "Dry Run", "error": "ERROR"}[r["status"]]
        print("| %s | %s | %s |" % (r["key"], status_icon, r["message"]))

    # Summary counts
    ok_count = sum(1 for r in results if r["status"] == "ok")
    dry_count = sum(1 for r in results if r["status"] == "dry_run")
    error_count = sum(1 for r in results if r["status"] == "error")

    ready_count = sum(1 for a in actions if a["action"] == "ready")
    more_info_count = sum(1 for a in actions if a["action"] == "more_info")

    print("\nSummary: %d ready for dev, %d more info required" % (ready_count, more_info_count))

    if args.dry_run:
        print("(DRY RUN — no changes were made)")
    elif error_count > 0:
        print("%d succeeded, %d failed" % (ok_count, error_count))
    else:
        print("All %d transitions completed successfully" % ok_count)


if __name__ == "__main__":
    main()
