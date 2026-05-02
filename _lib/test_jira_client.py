#!/usr/bin/env python3
"""Tests for the pagination contract of jira_search_all and the two adf_to_text variants.

Run with `python3 _lib/test_jira_client.py` from the skills repo root, or from
inside _lib/. No network access — `jira_get` is monkey-patched to return canned pages.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import jira_client


def _make_cursor_pages(total):
    """Return cursor-based pages, 50 issues per page until total reached."""
    pages = []
    issued = 0
    page_index = 0
    while issued < total:
        n = min(50, total - issued)
        is_last = issued + n >= total
        pages.append({
            "issues": [{"key": "PROJ-%d" % (issued + i + 1)} for i in range(n)],
            "nextPageToken": None if is_last else "tok-%d" % page_index,
            "isLast": is_last,
        })
        issued += n
        page_index += 1
    return pages


def _make_offset_pages(total):
    """Return offset-based pages (no nextPageToken). 50 per page."""
    pages = []
    issued = 0
    while issued < total:
        n = min(50, total - issued)
        pages.append({
            "issues": [{"key": "PROJ-%d" % (issued + i + 1)} for i in range(n)],
            "total": total,
        })
        issued += n
    return pages


class _PagedFakeJiraGet:
    """Records each call's path and returns successive canned pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, base_url, path, auth):
        self.calls.append(path)
        if not self.pages:
            raise AssertionError("jira_get called more times than canned pages")
        return self.pages.pop(0)


class JiraSearchAllPaginationTests(unittest.TestCase):
    """Pagination math for jira_search_all — limit=None (the 8-skill default) and limit=N."""

    def setUp(self):
        self._real_jira_get = jira_client.jira_get

    def tearDown(self):
        jira_client.jira_get = self._real_jira_get

    # --- cursor pagination ---

    def test_cursor_no_limit_returns_all(self):
        """limit=None must return every issue (matches prior 8-copy behaviour)."""
        jira_client.jira_get = _PagedFakeJiraGet(_make_cursor_pages(127))
        out = jira_client.jira_search_all("https://x", "auth", "project = X", "summary")
        self.assertEqual(len(out), 127)
        self.assertEqual(out[0]["key"], "PROJ-1")
        self.assertEqual(out[-1]["key"], "PROJ-127")

    def test_cursor_limit_exact_page_boundary(self):
        """limit=50 must stop at first page without a second fetch."""
        fake = _PagedFakeJiraGet(_make_cursor_pages(127))
        jira_client.jira_get = fake
        out = jira_client.jira_search_all("https://x", "auth", "p", "f", limit=50)
        self.assertEqual(len(out), 50)
        self.assertEqual(len(fake.calls), 1)

    def test_cursor_limit_mid_second_page(self):
        """limit=75 must take 50 from page 1 + 25 from page 2."""
        fake = _PagedFakeJiraGet(_make_cursor_pages(200))
        jira_client.jira_get = fake
        out = jira_client.jira_search_all("https://x", "auth", "p", "f", limit=75)
        self.assertEqual(len(out), 75)
        self.assertEqual(out[-1]["key"], "PROJ-75")

    def test_cursor_limit_smaller_than_page_size(self):
        """limit=10 must request maxResults=10 on the first page."""
        fake = _PagedFakeJiraGet(_make_cursor_pages(200))
        jira_client.jira_get = fake
        # Adjust the canned first page to honour the smaller maxResults
        fake.pages[0] = {
            "issues": [{"key": "PROJ-%d" % (i + 1)} for i in range(10)],
            "nextPageToken": "tok-0",
            "isLast": False,
        }
        out = jira_client.jira_search_all("https://x", "auth", "p", "f", limit=10)
        self.assertEqual(len(out), 10)
        self.assertIn("maxResults=10", fake.calls[0])

    # --- offset pagination ---

    def test_offset_no_limit_returns_all(self):
        jira_client.jira_get = _PagedFakeJiraGet(_make_offset_pages(123))
        out = jira_client.jira_search_all("https://x", "auth", "p", "f")
        self.assertEqual(len(out), 123)

    def test_offset_limit_mid_page(self):
        fake = _PagedFakeJiraGet(_make_offset_pages(200))
        jira_client.jira_get = fake
        out = jira_client.jira_search_all("https://x", "auth", "p", "f", limit=75)
        self.assertEqual(len(out), 75)
        self.assertEqual(out[-1]["key"], "PROJ-75")

    # --- contract: limit=0 is currently treated as "no limit" (truthy check). ---
    # Pinning current behaviour so a future change is a deliberate choice, not a silent regression.

    def test_limit_zero_currently_fetches_all(self):
        """limit=0 is currently truthy-false → behaves like limit=None.

        Documented quirk: the implementation uses `if limit and ...`. Callers
        wanting a no-op should pass an empty list, not limit=0. Change this
        contract deliberately if it ever causes confusion.
        """
        jira_client.jira_get = _PagedFakeJiraGet(_make_cursor_pages(60))
        out = jira_client.jira_search_all("https://x", "auth", "p", "f", limit=0)
        self.assertEqual(len(out), 60)


class AdfToTextVariantsTests(unittest.TestCase):
    """The simple `adf_to_text` (used by 7 skills) and the recursive
    `adf_to_text_rich` (used by incident-kb only) emit different output for
    the same input. Pin both shapes."""

    HEADING_DOC = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2},
             "content": [{"type": "text", "text": "Outage summary"}]},
            {"type": "paragraph",
             "content": [{"type": "text", "text": "Service was down for 12 minutes."}]},
        ],
    }

    def test_simple_adf_drops_headings(self):
        """The simple variant only knows paragraph/list/blockquote/codeBlock — it
        silently drops heading nodes. This is the prior 7-skill behaviour."""
        out = jira_client.adf_to_text(self.HEADING_DOC)
        self.assertNotIn("Outage summary", out)
        self.assertIn("Service was down", out)

    def test_rich_adf_preserves_headings(self):
        """The rich variant walks recursively and keeps heading text. This is
        what incident-kb relies on for Jira description rendering."""
        out = jira_client.adf_to_text_rich(self.HEADING_DOC)
        self.assertIn("Outage summary", out)
        self.assertIn("Service was down", out)

    def test_simple_adf_handles_bullet_list(self):
        doc = {"content": [
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "First"}]},
                ]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Second"}]},
                ]},
            ]},
        ]}
        out = jira_client.adf_to_text(doc)
        self.assertIn("- First", out)
        self.assertIn("- Second", out)


if __name__ == "__main__":
    unittest.main()
