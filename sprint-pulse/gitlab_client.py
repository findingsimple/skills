#!/usr/bin/env python3
"""GitLab API client utilities for sprint-pulse scripts."""

import json
import os
import sys
import time
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


def _urlopen_with_retry(req, timeout=30, max_retries=3, base_delay=1.0):
    """urlopen with retry and exponential backoff for transient errors."""
    for attempt in range(max_retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                except (ValueError, TypeError):
                    delay = base_delay * (2 ** attempt)
                print("HTTP %d, retrying in %.1fs... (%s)" % (e.code, delay, req.full_url), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print("Network error: %s, retrying in %.1fs... (%s)" % (e, delay, req.full_url), file=sys.stderr)
                time.sleep(delay)
                continue
            raise


def gitlab_get(gitlab_url, path, token):
    """GET a JSON response from the GitLab API."""
    url = gitlab_url + "/api/v4" + path
    req = urllib.request.Request(
        url,
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
    )
    try:
        with _urlopen_with_retry(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("GitLab API %d on %s: %s" % (e.code, path, body)) from None


def gitlab_get_all(gitlab_url, path, token, max_pages=50):
    """GET all pages from a GitLab API endpoint. Returns concatenated list."""
    results = []
    url = gitlab_url + "/api/v4" + path
    page_count = 0
    while url and page_count < max_pages:
        req = urllib.request.Request(
            url,
            headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
        )
        page_count += 1
        try:
            with _urlopen_with_retry(req) as resp:
                data = json.loads(resp.read())
                if isinstance(data, list):
                    results.extend(data)
                else:
                    results.append(data)
                next_page = resp.getheader("x-next-page")
                if next_page:
                    # Build next URL: always use full base with page param
                    base = gitlab_url + "/api/v4" + path
                    if "?" in base:
                        url = base + "&page=" + next_page
                    else:
                        url = base + "?page=" + next_page
                else:
                    url = None
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            raise Exception("GitLab API %d on %s: %s" % (e.code, path, body)) from None
    return results


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
