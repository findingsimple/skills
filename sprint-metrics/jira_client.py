#!/usr/bin/env python3
"""Shared API client utilities for sprint-metrics scripts."""

import json
import subprocess
import sys
import urllib.request
import base64


def load_env(keys):
    """Load specific environment variables from ~/.obsidian_env and ~/.sprint_summary_env.

    Args:
        keys: list of variable names to extract

    Returns:
        dict mapping variable names to values
    """
    print_vars = " ".join('"$%s"' % k for k in keys)
    result = subprocess.run(
        ["bash", "-c", "source ~/.obsidian_env && source ~/.sprint_summary_env && printf '%%s\\n' %s" % print_vars],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print("ERROR: Failed to load env files: %s" % result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    values = result.stdout.splitlines()
    return dict(zip(keys, values))


def init_auth(env):
    """Create base64 auth string and return (base_url, auth).

    Validates that JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN are present.
    """
    for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        if not env.get(key):
            print("ERROR: Missing env var: %s" % key)
            sys.exit(1)
    base_url = env["JIRA_BASE_URL"]
    auth = base64.b64encode((env["JIRA_EMAIL"] + ":" + env["JIRA_API_TOKEN"]).encode()).decode()
    return base_url, auth


def jira_get(base_url, path, auth):
    """GET a JSON response from the Jira API."""
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def gitlab_get(gitlab_url, path, token):
    """GET a JSON response from the GitLab API."""
    url = gitlab_url + "/api/v4" + path
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())
