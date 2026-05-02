#!/usr/bin/env python3
"""Shared Jira API client utilities.

Single source of truth for all skills under ~/.claude/skills/. Skills reach
this module via the per-skill `_libpath.py` shim that prepends ../_lib to
sys.path.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import base64


def load_env(keys):
    """Load specific environment variables from the shell environment."""
    return {k: os.environ.get(k, "") for k in keys}


def init_auth(env):
    """Create base64 auth string and return (base_url, auth)."""
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
    # Should be unreachable — every path above either returns or raises.
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


def jira_search_all(base_url, auth, jql, fields, limit=None):
    """Run a JQL search with automatic pagination. Returns matching issues
    (or up to `limit` if supplied — pagination stops once limit is reached).

    Supports both cursor-based pagination (nextPageToken/isLast, used by
    Jira API v3 /search/jql) and offset-based pagination (total/startAt).
    """
    encoded_jql = urllib.parse.quote(jql, safe="")
    page_size = min(50, limit) if limit else 50
    path = "/rest/api/3/search/jql?jql=%s&maxResults=%d&fields=%s" % (encoded_jql, page_size, fields)
    data = jira_get(base_url, path, auth)

    issues = data.get("issues", [])
    if limit and len(issues) >= limit:
        return issues[:limit]

    if "nextPageToken" in data:
        while not data.get("isLast", True):
            if limit and len(issues) >= limit:
                break
            token = data["nextPageToken"]
            remaining_page = min(50, limit - len(issues)) if limit else 50
            next_path = "/rest/api/3/search/jql?jql=%s&maxResults=%d&fields=%s&nextPageToken=%s" % (
                encoded_jql, remaining_page, fields, urllib.parse.quote(token, safe=""),
            )
            data = jira_get(base_url, next_path, auth)
            new_issues = data.get("issues", [])
            if not new_issues:
                print("WARNING: Cursor pagination returned empty page at %d issues, stopping" % len(issues), file=sys.stderr)
                break
            issues.extend(new_issues)
        return issues[:limit] if limit else issues

    total = data.get("total", len(issues))
    while len(issues) < total:
        if limit and len(issues) >= limit:
            break
        before = len(issues)
        remaining_page = min(50, limit - len(issues)) if limit else 50
        next_path = "/rest/api/3/search/jql?jql=%s&maxResults=%d&startAt=%d&fields=%s" % (
            encoded_jql, remaining_page, len(issues), fields
        )
        next_data = jira_get(base_url, next_path, auth)
        issues.extend(next_data.get("issues", []))
        if len(issues) == before:
            print("WARNING: Pagination stalled at %d/%d issues, stopping" % (len(issues), total), file=sys.stderr)
            break

    return issues[:limit] if limit else issues


def jira_get_changelog(base_url, auth, issue_key):
    """Fetch the full changelog for an issue (paginated)."""
    entries = []
    start_at = 0
    while True:
        path = "/rest/api/3/issue/%s/changelog?startAt=%d&maxResults=100" % (issue_key, start_at)
        data = jira_get(base_url, path, auth)
        values = data.get("values", [])
        if not values:
            break
        for entry in values:
            entries.append({
                "created": entry.get("created", ""),
                "author": entry.get("author", {}).get("displayName", ""),
                "items": [
                    {
                        "field": item.get("field", ""),
                        "from_string": item.get("fromString", ""),
                        "to_string": item.get("toString", ""),
                        "from": item.get("from", ""),
                        "to": item.get("to", ""),
                    }
                    for item in entry.get("items", [])
                ],
            })
        if start_at + len(values) >= data.get("total", 0):
            break
        start_at += len(values)
    return entries


def jira_get_dev_summary(base_url, auth, issue_numeric_id):
    """Fetch the Jira `Development` panel summary for an issue.

    Uses the undocumented but stable `/rest/dev-status/latest/issue/summary`
    endpoint that aggregates linked branches, PRs, builds, and deployments
    across all connected DVCS providers (GitLab / GitHub / Bitbucket).
    Requires the issue's NUMERIC id (not key).

    Returns a dict like {"pullrequest": {"count": N}, "repository": {"count": N},
    "build": {"count": N}}. Empty dict on any error so callers can fall back to
    URL-scan detection.
    """
    try:
        path = "/rest/dev-status/latest/issue/summary?issueId=%s" % str(issue_numeric_id)
        data = jira_get(base_url, path, auth)
    except Exception:
        return {}
    summary = (data or {}).get("summary") or {}
    out = {}
    for key in ("pullrequest", "repository", "build", "branch", "commit"):
        section = (summary.get(key) or {}).get("overall") or {}
        out[key] = {"count": section.get("count", 0) or 0}
    return out


def jira_get_comments(base_url, auth, issue_key):
    """Fetch comments for an issue.

    Returns a list of comment dicts with:
        - author: displayName
        - created: timestamp
        - body_text: plain text extracted from ADF body (via adf_to_text)
    """
    path = "/rest/api/3/issue/%s/comment?maxResults=100&orderBy=created" % issue_key
    data = jira_get(base_url, path, auth)
    comments = []
    for c in data.get("comments", []):
        body_text = adf_to_text(c.get("body", {}))
        comments.append({
            "author": c.get("author", {}).get("displayName", ""),
            "created": c.get("created", ""),
            "body_text": body_text,
        })
    return comments


def adf_to_text(adf):
    """Convert Atlassian Document Format JSON to plain text.

    Handles the common doc-root case used by Jira comment / description bodies.
    For richer structure-preserving conversion (headings, hardBreak, list-item
    bullets, trailing paragraph newlines), use `adf_to_text_rich` instead.
    """
    if not adf or not isinstance(adf, dict):
        return ""
    parts = []
    for node in adf.get("content", []):
        node_type = node.get("type", "")
        if node_type == "paragraph":
            para_parts = []
            for inline in node.get("content", []):
                if inline.get("type") == "text":
                    para_parts.append(inline.get("text", ""))
                elif inline.get("type") == "mention":
                    para_parts.append("@" + inline.get("attrs", {}).get("text", ""))
            parts.append("".join(para_parts))
        elif node_type in ("bulletList", "orderedList"):
            for item in node.get("content", []):
                item_text = adf_to_text(item)
                if item_text:
                    parts.append("- " + item_text)
        elif node_type == "blockquote":
            inner = adf_to_text(node)
            if inner:
                parts.append("> " + inner)
        elif node_type == "codeBlock":
            for inline in node.get("content", []):
                if inline.get("type") == "text":
                    parts.append(inline.get("text", ""))
        elif node_type == "listItem":
            parts.append(adf_to_text(node))
    return "\n".join(parts)


def adf_to_text_rich(node):
    """Recursively walk an ADF node tree and return plain text.

    Differs from `adf_to_text`: handles heading / hardBreak / rule, adds
    trailing newlines after block nodes, and prefixes list items with "- ".
    Used by incident-kb for Jira description rendering where the heading
    structure should survive the conversion.
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
        parts.append(adf_to_text_rich(child))

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


def ensure_tmp_dir(path):
    """Create a /tmp/ cache dir with 0o700, rejecting symlinks and repairing
    loose perms on a pre-existing dir. `exist_ok=True` alone doesn't repair perms.
    """
    if os.path.islink(path):
        print("ERROR: %s is a symlink; refusing to use it." % path, file=sys.stderr)
        sys.exit(1)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write_json(path, data):
    """Write JSON to path atomically via .tmp + os.replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)
