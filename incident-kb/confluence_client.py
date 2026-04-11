#!/usr/bin/env python3
"""Confluence Cloud REST API client for incident-kb skill.

Uses the same Atlassian auth (email + API token) as jira_client.py.
Wiki API lives at {JIRA_BASE_URL}/wiki/...
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser


def load_env(keys):
    """Load specific environment variables from the shell environment."""
    return {k: os.environ.get(k, "") for k in keys}


def init_auth(env):
    """Create base64 auth string and return (base_url, auth).

    Reuses JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN since Confluence Cloud
    shares the same Atlassian instance.
    """
    for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        if not env.get(key):
            print("ERROR: Missing env var: %s" % key, file=sys.stderr)
            sys.exit(1)
    base_url = env["JIRA_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        (env["JIRA_EMAIL"] + ":" + env["JIRA_API_TOKEN"]).encode()
    ).decode()
    return base_url, auth


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
                print(
                    "HTTP %d, retrying in %.1fs... (%s)" % (e.code, delay, req.full_url),
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print(
                    "Network error: %s, retrying in %.1fs... (%s)" % (e, delay, req.full_url),
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise


def confluence_get(base_url, path, auth):
    """GET a JSON response from the Confluence API.

    Args:
        base_url: Atlassian instance URL (e.g. https://example.atlassian.net)
        path: API path WITHOUT /wiki prefix (e.g. /api/v2/pages/123)
        auth: base64 auth string
    """
    url = base_url + "/wiki" + path
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    try:
        with _urlopen_with_retry(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("Confluence API %d on %s: %s" % (e.code, path, body)) from None


def confluence_get_children(base_url, auth, page_id, limit=25):
    """Fetch all child pages under a parent page (v2 API, cursor-based pagination).

    Returns list of page objects (id, title, status, _links, etc).
    """
    all_children = []
    path = "/api/v2/pages/%s/children?limit=%d" % (page_id, limit)

    while path:
        data = confluence_get(base_url, path, auth)
        results = data.get("results", [])
        all_children.extend(results)

        # Cursor-based pagination: _links.next is a relative path including /wiki
        next_link = data.get("_links", {}).get("next", "")
        if next_link:
            # Strip /wiki prefix if present since confluence_get adds it
            if next_link.startswith("/wiki"):
                next_link = next_link[len("/wiki"):]
            path = next_link
        else:
            path = None

    return all_children


def confluence_get_page(base_url, auth, page_id, body_format="atlas_doc_format"):
    """Fetch a single page with its body content.

    Args:
        body_format: "atlas_doc_format" (ADF JSON) or "storage" (XHTML)

    Returns page object including body field.
    """
    path = "/api/v2/pages/%s?body-format=%s" % (page_id, body_format)
    return confluence_get(base_url, path, auth)


def confluence_get_page_labels(base_url, auth, page_id):
    """Fetch labels for a page (v2 API)."""
    path = "/api/v2/pages/%s/labels" % page_id
    data = confluence_get(base_url, path, auth)
    return [label.get("name", "") for label in data.get("results", [])]


def confluence_search_cql(base_url, auth, cql, limit=25):
    """CQL search with auto-pagination (v1 API, offset-based).

    Returns list of content objects.
    """
    all_results = []
    start = 0
    encoded_cql = urllib.parse.quote(cql, safe="")

    while True:
        path = "/rest/api/content/search?cql=%s&start=%d&limit=%d" % (
            encoded_cql, start, limit,
        )
        data = confluence_get(base_url, path, auth)
        results = data.get("results", [])
        all_results.extend(results)

        total = data.get("totalSize", 0)
        start += len(results)
        if not results or start >= total:
            break

    return all_results


def adf_to_text(node):
    """Convert an Atlassian Document Format (ADF) node to plain text.

    ADF is the JSON document format used by Confluence and Jira for rich text.
    Recursively walks the node tree and extracts text content.
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

    if node_type in ("paragraph", "blockquote", "rule"):
        return joined.strip() + "\n\n"
    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        return "#" * level + " " + joined.strip() + "\n\n"
    if node_type == "hardBreak":
        return "\n"
    if node_type == "listItem":
        return "- " + joined.strip() + "\n"
    if node_type in ("bulletList", "orderedList"):
        return joined + "\n"
    if node_type == "table":
        return joined + "\n"
    if node_type == "tableRow":
        return joined + "\n"
    if node_type == "tableCell" or node_type == "tableHeader":
        return joined.strip() + " | "

    return joined


class _StorageParser(HTMLParser):
    """Simple HTML parser to extract text from Confluence storage format (XHTML)."""

    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "div", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self._tag_stack = []

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag)
        if tag in ("br",):
            self.parts.append("\n")
        if tag in ("li",):
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        self.parts.append(data)


def storage_to_text(html):
    """Convert Confluence storage format (XHTML) to plain text.

    Fallback for pages where ADF body is unavailable.
    """
    if not html:
        return ""
    parser = _StorageParser()
    parser.feed(html)
    text = "".join(parser.parts)
    # Collapse multiple blank lines
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()
