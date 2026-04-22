#!/usr/bin/env python3
"""Shared Jira API client utilities for root-cause-triage scripts."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import base64


def load_env(keys):
    """Load specific environment variables from the shell environment.

    Args:
        keys: list of variable names to extract

    Returns:
        dict mapping variable names to values
    """
    return {k: os.environ.get(k, "") for k in keys}


def init_auth(env):
    """Create base64 auth string and return (base_url, auth).

    Validates that JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN are present.
    """
    for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        if not env.get(key):
            print("ERROR: Missing env var: %s" % key, file=sys.stderr)
            sys.exit(1)
    base_url = env["JIRA_BASE_URL"]
    auth = base64.b64encode((env["JIRA_EMAIL"] + ":" + env["JIRA_API_TOKEN"]).encode()).decode()
    return base_url, auth


def _redact_url(url):
    """Drop the query string from retry logs so keywords/tokens don't leak."""
    return url.split("?", 1)[0] if url else url


def _urlopen_with_retry(req, timeout=30, max_retries=3, base_delay=1.0):
    """urlopen with retry and exponential backoff for transient errors."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (429, 503) and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                except (ValueError, TypeError):
                    delay = base_delay * (2 ** attempt)
                print("HTTP %d, retrying in %.1fs... (%s)" % (e.code, delay, _redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print("Network error: %s, retrying in %.1fs... (%s)" % (e, delay, _redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("urlopen retry loop exhausted without returning or raising") from last_exc


def jira_get(base_url, path, auth):
    """GET a JSON response from the Jira API."""
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    try:
        with _urlopen_with_retry(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("Jira API %d on %s: %s" % (e.code, path, body)) from None


def jira_post(base_url, path, auth, data):
    """POST JSON to the Jira API and return the parsed response (or None)."""
    req_body = json.dumps(data).encode()
    req = urllib.request.Request(
        base_url + path,
        data=req_body,
        headers={
            "Authorization": "Basic " + auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read()
            if response_body:
                return json.loads(response_body)
            return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("Jira API %d on POST %s: %s" % (e.code, path, body)) from None


def adf_to_text(node):
    """Convert an Atlassian Document Format (ADF) node to plain text.

    ADF is the JSON document format returned by Jira API v3 for rich text fields.
    This recursively walks the node tree and extracts text content.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type", "")
    text = node.get("text", "")

    if text:
        return text

    parts = []
    for child in node.get("content", []):
        parts.append(adf_to_text(child))

    joined = "".join(parts)

    if node_type in ("paragraph", "heading", "blockquote", "rule"):
        return joined.strip() + "\n\n"
    if node_type == "hardBreak":
        return "\n"
    if node_type == "listItem":
        return "- " + joined.strip() + "\n"
    if node_type in ("bulletList", "orderedList"):
        return joined + "\n"

    return joined


def jira_search_all(base_url, auth, jql, fields):
    """Run a JQL search with automatic pagination. Returns all matching issues.

    Supports both cursor-based pagination (nextPageToken/isLast, used by
    Jira API v3 /search/jql) and offset-based pagination (total/startAt).

    Args:
        base_url: Jira base URL
        auth: base64 auth string
        jql: JQL query string (unencoded)
        fields: comma-separated field names
    """
    encoded_jql = urllib.parse.quote(jql, safe="")
    path = "/rest/api/3/search/jql?jql=%s&maxResults=50&fields=%s" % (encoded_jql, fields)
    data = jira_get(base_url, path, auth)

    issues = data.get("issues", [])

    # Cursor-based pagination (v3 /search/jql endpoint)
    if "nextPageToken" in data:
        while not data.get("isLast", True):
            token = data["nextPageToken"]
            next_path = "/rest/api/3/search/jql?jql=%s&maxResults=50&fields=%s&nextPageToken=%s" % (
                encoded_jql, fields, urllib.parse.quote(token, safe=""),
            )
            data = jira_get(base_url, next_path, auth)
            new_issues = data.get("issues", [])
            if not new_issues:
                print("WARNING: Cursor pagination returned empty page at %d issues, stopping" % len(issues), file=sys.stderr)
                break
            issues.extend(new_issues)
        return issues

    # Offset-based pagination (fallback for older endpoints)
    total = data.get("total", len(issues))
    while len(issues) < total:
        before = len(issues)
        next_path = "/rest/api/3/search/jql?jql=%s&maxResults=50&startAt=%d&fields=%s" % (
            encoded_jql, len(issues), fields
        )
        next_data = jira_get(base_url, next_path, auth)
        issues.extend(next_data.get("issues", []))
        if len(issues) == before:
            print("WARNING: Pagination stalled at %d/%d issues, stopping" % (len(issues), total), file=sys.stderr)
            break

    return issues


def ensure_tmp_dir(path):
    """Create a /tmp/ cache dir with 0o700, rejecting symlinks and repairing
    loose perms on a pre-existing dir. `exist_ok=True` alone doesn't repair perms.
    Shared across collect.py, enrich.py, autofill.py, merge_results.py, build_prompts.py.
    """
    if os.path.islink(path):
        print("ERROR: %s is a symlink; refusing to use it." % path, file=sys.stderr)
        sys.exit(1)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
