#!/usr/bin/env python3
"""Shared Jira API client utilities for root-cause-triage scripts."""

import json
import subprocess
import sys
import urllib.request
import urllib.parse
import base64


def load_env(keys):
    """Load specific environment variables from ~/.sprint_summary_env.

    Args:
        keys: list of variable names to extract

    Returns:
        dict mapping variable names to values
    """
    print_vars = " ".join('"$%s"' % k for k in keys)
    result = subprocess.run(
        ["bash", "-c", "source ~/.sprint_summary_env && printf '%%s\\n' %s" % print_vars],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print("ERROR: Failed to load ~/.sprint_summary_env: %s" % result.stderr.strip(), file=sys.stderr)
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


def jira_post(base_url, path, auth, data):
    """POST JSON to the Jira API and return the parsed response (or None)."""
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


def jira_search_all(base_url, auth, jql, fields):
    """Run a JQL search with automatic pagination. Returns all matching issues.

    Args:
        base_url: Jira base URL
        auth: base64 auth string
        jql: JQL query string (unencoded)
        fields: comma-separated field names
    """
    encoded_jql = urllib.parse.quote(jql, safe="")
    path = "/rest/api/2/search?jql=%s&maxResults=50&fields=%s" % (encoded_jql, fields)
    data = jira_get(base_url, path, auth)

    issues = data.get("issues", [])
    total = data.get("total", len(issues))

    while len(issues) < total:
        before = len(issues)
        next_path = "/rest/api/2/search?jql=%s&maxResults=50&startAt=%d&fields=%s" % (
            encoded_jql, len(issues), fields
        )
        next_data = jira_get(base_url, next_path, auth)
        issues.extend(next_data.get("issues", []))
        if len(issues) == before:
            print("WARNING: Pagination stalled at %d/%d issues, stopping" % (len(issues), total), file=sys.stderr)
            break

    return issues
