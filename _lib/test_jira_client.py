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


class _FakeUrlopenContext:
    """Minimal context manager stand-in for urllib.request.urlopen / urlopen_with_retry."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


class JiraPutTests(unittest.TestCase):
    """jira_put round-trips the request body and returns the parsed response."""

    def setUp(self):
        self._real_retry = jira_client.urlopen_with_retry

    def tearDown(self):
        jira_client.urlopen_with_retry = self._real_retry

    def test_put_returns_parsed_json(self):
        captured = {}

        def fake_retry(req, **kwargs):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _FakeUrlopenContext(b'{"id":"42","ok":true}')

        jira_client.urlopen_with_retry = fake_retry
        out = jira_client.jira_put("https://x", "/rest/api/3/issue/PROJ-1/comment/42", "auth", {"body": "hi"})
        self.assertEqual(out, {"id": "42", "ok": True})
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["content_type"], "application/json")
        self.assertEqual(captured["body"], b'{"body": "hi"}')

    def test_put_returns_none_for_empty_body(self):
        jira_client.urlopen_with_retry = lambda req, **kw: _FakeUrlopenContext(b"")
        self.assertIsNone(jira_client.jira_put("https://x", "/p", "auth", {}))

    def test_put_raises_on_http_error(self):
        import urllib.error
        import io

        def fake_retry(req, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"denied"))

        jira_client.urlopen_with_retry = fake_retry
        with self.assertRaises(Exception) as ctx:
            jira_client.jira_put("https://x", "/p", "auth", {})
        self.assertIn("403", str(ctx.exception))
        self.assertIn("PUT /p", str(ctx.exception))


class JiraGetCommentsIdTests(unittest.TestCase):
    """Regression: jira_get_comments must include the comment id so callers can update or
    delete a specific comment by ID."""

    def setUp(self):
        self._real_jira_get = jira_client.jira_get

    def tearDown(self):
        jira_client.jira_get = self._real_jira_get

    def test_id_field_is_populated(self):
        jira_client.jira_get = lambda base_url, path, auth: {
            "comments": [
                {
                    "id": "10042",
                    "author": {"displayName": "Alex Chen"},
                    "created": "2026-04-01T10:00:00.000+0000",
                    "body": {"type": "doc", "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Hello"}]},
                    ]},
                },
            ],
        }
        out = jira_client.jira_get_comments("https://x", "auth", "PROJ-1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "10042")
        self.assertEqual(out[0]["author"], "Alex Chen")
        self.assertEqual(out[0]["body_text"], "Hello")

    def test_missing_id_falls_back_to_empty_string(self):
        jira_client.jira_get = lambda base_url, path, auth: {
            "comments": [
                {
                    "author": {"displayName": "Bot"},
                    "created": "",
                    "body": {},
                },
            ],
        }
        out = jira_client.jira_get_comments("https://x", "auth", "PROJ-2")
        self.assertEqual(out[0]["id"], "")

    def test_author_account_id_populated(self):
        jira_client.jira_get = lambda base_url, path, auth: {
            "comments": [
                {
                    "id": "1",
                    "author": {"displayName": "Alex", "accountId": "acct-abc-123"},
                    "body": {},
                },
            ],
        }
        out = jira_client.jira_get_comments("https://x", "auth", "PROJ-1")
        self.assertEqual(out[0]["author_account_id"], "acct-abc-123")

    def test_author_explicitly_none_does_not_crash(self):
        # Real production shape: Jira sometimes returns author=null on system events.
        jira_client.jira_get = lambda base_url, path, auth: {
            "comments": [
                {"id": "1", "author": None, "body": {}},
            ],
        }
        out = jira_client.jira_get_comments("https://x", "auth", "PROJ-1")
        self.assertEqual(out[0]["author"], "")
        self.assertEqual(out[0]["author_account_id"], "")


class JiraGetMyselfTests(unittest.TestCase):
    """The author check in comment.py depends on /myself returning a stable accountId."""

    def setUp(self):
        self._real_jira_get = jira_client.jira_get

    def tearDown(self):
        jira_client.jira_get = self._real_jira_get

    def test_returns_account_id_and_name(self):
        captured = {}

        def fake_get(base_url, path, auth):
            captured["path"] = path
            return {"accountId": "acct-bot", "displayName": "Skills Bot"}

        jira_client.jira_get = fake_get
        out = jira_client.jira_get_myself("https://x", "auth")
        self.assertEqual(captured["path"], "/rest/api/3/myself")
        self.assertEqual(out, {"account_id": "acct-bot", "display_name": "Skills Bot"})

    def test_missing_fields_fall_back_to_empty_string(self):
        jira_client.jira_get = lambda base_url, path, auth: {}
        out = jira_client.jira_get_myself("https://x", "auth")
        self.assertEqual(out, {"account_id": "", "display_name": ""})

    def test_none_response_does_not_crash(self):
        jira_client.jira_get = lambda base_url, path, auth: None
        out = jira_client.jira_get_myself("https://x", "auth")
        self.assertEqual(out, {"account_id": "", "display_name": ""})


if __name__ == "__main__":
    unittest.main()
