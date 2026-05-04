#!/usr/bin/env python3
"""Tests for gitlab_client pagination, MR/issue-key matching, and notes shape.

Run with `python3 _lib/test_gitlab_client.py`. No network — urlopen and
the inner urlopen_with_retry are stubbed via a scripted handler.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitlab_client


class _FakeResp:
    """Context-manager response with optional next-page header."""

    def __init__(self, body, next_page=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._next_page = next_page

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getheader(self, name):
        if name.lower() == "x-next-page":
            return self._next_page
        return None


class _ScriptedRetry:
    """Stand-in for _http.urlopen_with_retry that returns successive responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, req, timeout=30, max_retries=3, base_delay=1.0):
        self.calls.append(req.full_url)
        if not self.responses:
            raise AssertionError("urlopen_with_retry called more times than scripted")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class GitlabGetTests(unittest.TestCase):
    def setUp(self):
        self._orig = gitlab_client.urlopen_with_retry

    def tearDown(self):
        gitlab_client.urlopen_with_retry = self._orig

    def test_http_error_wrap_includes_status_path_and_truncated_body(self):
        body = b'{"message":"forbidden"}' + b"x" * 1000
        err = urllib.error.HTTPError(
            url="https://gl.test/api/v4/x", code=403, msg="forbidden",
            hdrs={}, fp=io.BytesIO(body))
        gitlab_client.urlopen_with_retry = _ScriptedRetry([err])
        with self.assertRaises(Exception) as ctx:
            gitlab_client.gitlab_get("https://gl.test", "/x", "tok")
        msg = str(ctx.exception)
        self.assertIn("403", msg)
        self.assertIn("/x", msg)
        # Body capped at 500 bytes per gitlab_client.gitlab_get implementation
        self.assertLess(len(msg), 700)


class GitlabGetAllTests(unittest.TestCase):
    def setUp(self):
        self._orig = gitlab_client.urlopen_with_retry

    def tearDown(self):
        gitlab_client.urlopen_with_retry = self._orig

    def test_single_page_no_next_header(self):
        gitlab_client.urlopen_with_retry = _ScriptedRetry([
            _FakeResp([{"id": 1}, {"id": 2}], next_page=None),
        ])
        out = gitlab_client.gitlab_get_all("https://gl.test", "/projects/1/mrs", "tok")
        self.assertEqual(out, [{"id": 1}, {"id": 2}])

    def test_paginates_via_x_next_page_header(self):
        scripted = _ScriptedRetry([
            _FakeResp([{"id": 1}], next_page="2"),
            _FakeResp([{"id": 2}], next_page="3"),
            _FakeResp([{"id": 3}], next_page=None),
        ])
        gitlab_client.urlopen_with_retry = scripted
        out = gitlab_client.gitlab_get_all("https://gl.test", "/x", "tok")
        self.assertEqual(out, [{"id": 1}, {"id": 2}, {"id": 3}])
        # Each follow-up request must add ?page= to the same base path
        self.assertEqual(scripted.calls[0], "https://gl.test/api/v4/x")
        self.assertEqual(scripted.calls[1], "https://gl.test/api/v4/x?page=2")
        self.assertEqual(scripted.calls[2], "https://gl.test/api/v4/x?page=3")

    def test_paginates_preserves_existing_query_string(self):
        scripted = _ScriptedRetry([
            _FakeResp([{"id": 1}], next_page="2"),
            _FakeResp([{"id": 2}], next_page=None),
        ])
        gitlab_client.urlopen_with_retry = scripted
        gitlab_client.gitlab_get_all("https://gl.test", "/x?per_page=20", "tok")
        self.assertEqual(scripted.calls[1], "https://gl.test/api/v4/x?per_page=20&page=2")

    def test_max_pages_caps_iteration(self):
        # Always say there's a next page; max_pages=2 must stop after 2.
        scripted = _ScriptedRetry([
            _FakeResp([{"id": 1}], next_page="2"),
            _FakeResp([{"id": 2}], next_page="3"),
        ])
        gitlab_client.urlopen_with_retry = scripted
        out = gitlab_client.gitlab_get_all("https://gl.test", "/x", "tok", max_pages=2)
        self.assertEqual(out, [{"id": 1}, {"id": 2}])
        self.assertEqual(len(scripted.calls), 2)

    def test_dict_response_appended_not_extended(self):
        gitlab_client.urlopen_with_retry = _ScriptedRetry([
            _FakeResp({"id": 99}, next_page=None),
        ])
        out = gitlab_client.gitlab_get_all("https://gl.test", "/single", "tok")
        self.assertEqual(out, [{"id": 99}])

    def test_http_error_wrapped_with_status_and_body(self):
        err = urllib.error.HTTPError(
            url="https://gl.test/api/v4/x", code=403, msg="forbidden",
            hdrs={}, fp=io.BytesIO(b'{"message":"403 Forbidden"}'))
        gitlab_client.urlopen_with_retry = _ScriptedRetry([err])
        with self.assertRaises(Exception) as ctx:
            gitlab_client.gitlab_get_all("https://gl.test", "/x", "tok")
        self.assertIn("403", str(ctx.exception))
        self.assertIn("/x", str(ctx.exception))


class SearchMRsForIssueTests(unittest.TestCase):
    def setUp(self):
        self._orig = gitlab_client.urlopen_with_retry

    def tearDown(self):
        gitlab_client.urlopen_with_retry = self._orig

    def _stub_with(self, mrs):
        gitlab_client.urlopen_with_retry = _ScriptedRetry([_FakeResp(mrs)])

    def test_match_in_title(self):
        self._stub_with([{"title": "Fix PROJ-123 bug", "description": "", "source_branch": "x"}])
        out = gitlab_client.search_mrs_for_issue("https://gl.test", "tok", "1", "PROJ-123")
        self.assertEqual(len(out), 1)

    def test_match_in_branch(self):
        self._stub_with([{"title": "x", "description": "", "source_branch": "feature/PROJ-123-thing"}])
        out = gitlab_client.search_mrs_for_issue("https://gl.test", "tok", "1", "PROJ-123")
        self.assertEqual(len(out), 1)

    def test_no_substring_false_positive(self):
        # PROJ-1234 must NOT match a search for PROJ-123 (the (?!\d) lookahead).
        self._stub_with([{"title": "PROJ-1234 only", "description": "", "source_branch": ""}])
        out = gitlab_client.search_mrs_for_issue("https://gl.test", "tok", "1", "PROJ-123")
        self.assertEqual(out, [])

    def test_case_insensitive_match(self):
        self._stub_with([{"title": "fix proj-123", "description": "", "source_branch": ""}])
        out = gitlab_client.search_mrs_for_issue("https://gl.test", "tok", "1", "PROJ-123")
        self.assertEqual(len(out), 1)

    def test_search_failure_returns_empty_and_warns_to_stderr(self):
        err = urllib.error.HTTPError("https://gl.test/api/v4/x", 500, "boom", {}, io.BytesIO(b""))
        gitlab_client.urlopen_with_retry = _ScriptedRetry([err])
        captured = io.StringIO()
        orig = sys.stderr
        sys.stderr = captured
        try:
            out = gitlab_client.search_mrs_for_issue("https://gl.test", "tok", "1", "PROJ-123")
        finally:
            sys.stderr = orig
        self.assertEqual(out, [])
        # Production silently dropping search failures is the dangerous case;
        # require a warning so a caller monitoring stderr can detect it.
        self.assertIn("PROJ-123", captured.getvalue())
        self.assertIn("Warning", captured.getvalue())


class GetMRNotesTests(unittest.TestCase):
    def setUp(self):
        self._orig = gitlab_client.urlopen_with_retry

    def tearDown(self):
        gitlab_client.urlopen_with_retry = self._orig

    def test_normalises_note_shape(self):
        gitlab_client.urlopen_with_retry = _ScriptedRetry([_FakeResp([
            {
                "author": {"username": "alice", "name": "Alice A"},
                "created_at": "2026-04-01T00:00:00Z",
                "body": "lgtm",
                "system": False,
            },
            {
                # missing author dict — should not crash
                "created_at": "2026-04-02T00:00:00Z",
                "body": "merged",
                "system": True,
            },
        ])])
        out = gitlab_client.get_mr_notes("https://gl.test", "tok", "1", "42")
        self.assertEqual(out[0]["author"], "alice")
        self.assertEqual(out[0]["author_name"], "Alice A")
        self.assertFalse(out[0]["system"])
        self.assertEqual(out[1]["author"], "")
        self.assertTrue(out[1]["system"])

    def test_handles_author_explicitly_none(self):
        # Real GitLab notes from system events (e.g. "Marked as merged") have
        # been seen with `author: null`. The previous implementation called
        # `n.get("author", {}).get(...)` which crashes on None.
        gitlab_client.urlopen_with_retry = _ScriptedRetry([_FakeResp([
            {"author": None, "created_at": "2026-04-03T00:00:00Z", "body": "auto", "system": True},
        ])])
        out = gitlab_client.get_mr_notes("https://gl.test", "tok", "1", "42")
        self.assertEqual(out[0]["author"], "")
        self.assertEqual(out[0]["author_name"], "")

    def test_failure_returns_empty_and_warns_to_stderr(self):
        err = urllib.error.HTTPError("https://gl.test", 404, "nope", {}, io.BytesIO(b""))
        gitlab_client.urlopen_with_retry = _ScriptedRetry([err])
        captured = io.StringIO()
        orig = sys.stderr
        sys.stderr = captured
        try:
            out = gitlab_client.get_mr_notes("https://gl.test", "tok", "1", "42")
        finally:
            sys.stderr = orig
        self.assertEqual(out, [])
        self.assertIn("42", captured.getvalue())
        self.assertIn("Warning", captured.getvalue())


class LoadEnvTests(unittest.TestCase):
    def test_missing_vars_exits_with_message(self):
        saved = {k: os.environ.pop(k, None) for k in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID")}
        captured = io.StringIO()
        orig_stderr = sys.stderr
        sys.stderr = captured
        try:
            with self.assertRaises(SystemExit):
                gitlab_client.load_gitlab_env()
        finally:
            sys.stderr = orig_stderr
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
        self.assertIn("GITLAB_URL", captured.getvalue())
        self.assertIn("GITLAB_TOKEN", captured.getvalue())
        self.assertIn("GITLAB_PROJECT_ID", captured.getvalue())

    def test_all_present_returns_tuple(self):
        saved = {k: os.environ.get(k) for k in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT_ID")}
        os.environ["GITLAB_URL"] = "https://gl.test"
        os.environ["GITLAB_TOKEN"] = "tok"
        os.environ["GITLAB_PROJECT_ID"] = "42"
        try:
            url, tok, pid = gitlab_client.load_gitlab_env()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual((url, tok, pid), ("https://gl.test", "tok", "42"))


if __name__ == "__main__":
    unittest.main()
