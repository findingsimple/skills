#!/usr/bin/env python3
"""GitLab API client utilities for sprint-pulse scripts."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_gitlab_env():
    """Load GitLab environment variables.

    Returns:
        tuple of (gitlab_url, gitlab_token, gitlab_project_id) or exits on error
    """
    gitlab_url = os.environ.get("GITLAB_URL", "")
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")
    gitlab_project_id = os.environ.get("GITLAB_PROJECT_ID", "")

    missing = []
    if not gitlab_url:
        missing.append("GITLAB_URL")
    if not gitlab_token:
        missing.append("GITLAB_TOKEN")
    if not gitlab_project_id:
        missing.append("GITLAB_PROJECT_ID")

    if missing:
        print("ERROR: Missing GitLab env vars: %s" % ", ".join(missing))
        sys.exit(1)

    return gitlab_url, gitlab_token, gitlab_project_id


def gitlab_get(gitlab_url, path, token):
    """GET a JSON response from the GitLab API."""
    url = gitlab_url + "/api/v4" + path
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("GitLab API %d on %s: %s" % (e.code, path, body)) from None


def search_mrs_for_issue(gitlab_url, token, project_id, issue_key):
    """Search GitLab for MRs linked to a Jira issue key.

    Searches MR titles, descriptions, and branch names for the issue key.

    Returns:
        list of MR dicts matching the issue key
    """
    import re

    matches = []
    search_path = "/projects/%s/merge_requests?search=%s&per_page=20&state=opened" % (
        project_id,
        urllib.parse.quote(issue_key, safe=""),
    )
    try:
        mrs = gitlab_get(gitlab_url, search_path, token)
    except Exception as e:
        print("  Warning: GitLab search failed for %s: %s" % (issue_key, e), file=sys.stderr)
        return []

    for mr in mrs:
        title = mr.get("title") or ""
        desc = mr.get("description") or ""
        branch = mr.get("source_branch") or ""
        combined = "%s %s %s" % (title, desc, branch)
        pattern = r'(?i)\b' + re.escape(issue_key) + r'(?!\d)'
        if re.search(pattern, combined):
            matches.append(mr)

    return matches


def get_mr_notes(gitlab_url, token, project_id, mr_iid):
    """Fetch all notes (comments) for a merge request.

    Returns:
        list of note dicts with: author, created_at, body, system
    """
    path = "/projects/%s/merge_requests/%s/notes?sort=asc&per_page=100" % (project_id, mr_iid)
    try:
        notes = gitlab_get(gitlab_url, path, token)
    except Exception as e:
        print("  Warning: Could not fetch notes for MR !%s: %s" % (mr_iid, e), file=sys.stderr)
        return []

    return [
        {
            "author": n.get("author", {}).get("username", ""),
            "author_name": n.get("author", {}).get("name", ""),
            "created_at": n.get("created_at", ""),
            "body": n.get("body", ""),
            "system": n.get("system", False),
        }
        for n in notes
    ]
