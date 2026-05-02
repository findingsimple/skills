#!/usr/bin/env python3
"""Validate environment and test API connectivity for incident-kb skill."""

import json
import os
import sys

# Add skill directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _libpath  # noqa: F401
from confluence_client import load_env, init_auth, confluence_get, confluence_get_children
from jira_client import jira_get, jira_search_all


REQUIRED_VARS = [
    "JIRA_BASE_URL",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "RETRO_PARENT_PAGE_ID",
    "INCIDENT_KB_OUTPUT_PATH",
]

OPTIONAL_VARS = {
    "INC_PROJECT_KEY": "INC",
    "RETRO_TEMPLATE_PAGE_ID": "",
}


def main():
    env = load_env(REQUIRED_VARS + list(OPTIONAL_VARS.keys()))

    # Validate required vars
    missing = [k for k in REQUIRED_VARS if not env.get(k)]
    if missing:
        print("ERROR: Missing required env vars: %s" % ", ".join(missing), file=sys.stderr)
        print("Add them to ~/.zshrc and restart your shell.", file=sys.stderr)
        sys.exit(1)

    # Apply defaults for optional vars
    for k, default in OPTIONAL_VARS.items():
        if not env.get(k):
            env[k] = default

    base_url, auth = init_auth(env)
    parent_page_id = env["RETRO_PARENT_PAGE_ID"]
    inc_project_key = env["INC_PROJECT_KEY"]
    output_path = env["INCIDENT_KB_OUTPUT_PATH"]

    print("=" * 50)
    print("incident-kb setup")
    print("=" * 50)

    # Test Jira connectivity
    print("\n--- Jira ---")
    try:
        myself = jira_get(base_url, "/rest/api/3/myself", auth)
        print("Connected as: %s (%s)" % (myself.get("displayName", "?"), myself.get("emailAddress", "?")))
    except Exception as e:
        print("ERROR: Jira connectivity failed: %s" % e, file=sys.stderr)
        sys.exit(1)

    # Test INC project
    try:
        jql = "project = %s AND issuetype = Epic ORDER BY created DESC" % inc_project_key
        epics = jira_search_all(base_url, auth, jql, "key,summary,status")
        print("INC project (%s): %d epics found" % (inc_project_key, len(epics)))
        if epics:
            latest = epics[0]["fields"]
            print("  Latest: %s — %s [%s]" % (
                epics[0]["key"],
                latest.get("summary", "?"),
                latest.get("status", {}).get("name", "?"),
            ))
    except Exception as e:
        print("ERROR: Jira INC project query failed: %s" % e, file=sys.stderr)
        sys.exit(1)

    # Test Confluence connectivity
    print("\n--- Confluence ---")
    try:
        page = confluence_get(base_url, "/api/v2/pages/%s" % parent_page_id, auth)
        print("Retro parent page: %s (id: %s)" % (page.get("title", "?"), page.get("id", "?")))
    except Exception as e:
        print("ERROR: Confluence connectivity failed: %s" % e, file=sys.stderr)
        sys.exit(1)

    # Count child pages
    try:
        children = confluence_get_children(base_url, auth, parent_page_id)
        print("Child pages (retros): %d" % len(children))
        if children:
            print("  First: %s" % children[0].get("title", "?"))
            print("  Last:  %s" % children[-1].get("title", "?"))
    except Exception as e:
        print("ERROR: Failed to list child pages: %s" % e, file=sys.stderr)
        sys.exit(1)

    # Verify output path
    print("\n--- Output ---")
    if os.path.isdir(output_path):
        print("Output path: %s (exists)" % output_path)
    else:
        print("Output path: %s (will be created)" % output_path)

    # Optional: template page
    template_page_id = env.get("RETRO_TEMPLATE_PAGE_ID")
    if template_page_id:
        try:
            tpl = confluence_get(base_url, "/api/v2/pages/%s" % template_page_id, auth)
            print("Retro template: %s (id: %s)" % (tpl.get("title", "?"), tpl.get("id", "?")))
        except Exception as e:
            print("WARNING: Could not fetch retro template page: %s" % e, file=sys.stderr)

    # Save setup data
    setup_data = {
        "base_url": base_url,
        "inc_project_key": inc_project_key,
        "retro_parent_page_id": parent_page_id,
        "retro_template_page_id": template_page_id,
        "output_path": output_path,
        "retro_child_count": len(children),
        "inc_epic_count": len(epics),
    }
    setup_path = "/tmp/incident_kb_setup.json"
    with open(setup_path + ".tmp", "w") as f:
        json.dump(setup_data, f, indent=2)
    os.replace(setup_path + ".tmp", setup_path)
    print("\nSetup saved to %s" % setup_path)
    print("=" * 50)


if __name__ == "__main__":
    main()
