#!/usr/bin/env python3
"""Tests for confluence_client: pagination (children + CQL), ADF/storage parsers, auth.

Run with `python3 _lib/test_confluence_client.py`. No network — confluence_get
is monkey-patched.
"""

import base64
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import confluence_client


class _ScriptedGet:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, base_url, path, auth):
        self.calls.append(path)
        if not self.pages:
            raise AssertionError("confluence_get called more times than scripted")
        return self.pages.pop(0)


class GetChildrenTests(unittest.TestCase):
    def setUp(self):
        self._orig = confluence_client.confluence_get

    def tearDown(self):
        confluence_client.confluence_get = self._orig

    def test_single_page_no_next_link(self):
        confluence_client.confluence_get = _ScriptedGet([
            {"results": [{"id": "1"}, {"id": "2"}], "_links": {}},
        ])
        out = confluence_client.confluence_get_children("https://x.test", "auth", "PARENT")
        self.assertEqual([c["id"] for c in out], ["1", "2"])

    def test_paginates_strips_wiki_prefix_from_next_link(self):
        scripted = _ScriptedGet([
            {"results": [{"id": "1"}], "_links": {"next": "/wiki/api/v2/pages/PARENT/children?cursor=abc"}},
            {"results": [{"id": "2"}], "_links": {}},
        ])
        confluence_client.confluence_get = scripted
        out = confluence_client.confluence_get_children("https://x.test", "auth", "PARENT")
        self.assertEqual([c["id"] for c in out], ["1", "2"])
        # Second call must use the next link with /wiki stripped (confluence_get re-adds it).
        self.assertEqual(scripted.calls[1], "/api/v2/pages/PARENT/children?cursor=abc")

    def test_next_link_without_wiki_prefix_passed_through(self):
        scripted = _ScriptedGet([
            {"results": [{"id": "1"}], "_links": {"next": "/api/v2/pages/PARENT/children?cursor=xyz"}},
            {"results": [], "_links": {}},
        ])
        confluence_client.confluence_get = scripted
        confluence_client.confluence_get_children("https://x.test", "auth", "PARENT")
        self.assertEqual(scripted.calls[1], "/api/v2/pages/PARENT/children?cursor=xyz")


class CQLSearchTests(unittest.TestCase):
    def setUp(self):
        self._orig = confluence_client.confluence_get

    def tearDown(self):
        confluence_client.confluence_get = self._orig

    def test_offset_pagination_until_total_reached(self):
        scripted = _ScriptedGet([
            {"results": [{"id": "a"}, {"id": "b"}], "totalSize": 5},
            {"results": [{"id": "c"}, {"id": "d"}], "totalSize": 5},
            {"results": [{"id": "e"}], "totalSize": 5},
        ])
        confluence_client.confluence_get = scripted
        out = confluence_client.confluence_search_cql(
            "https://x.test", "auth", 'space = "ENG"', limit=2)
        self.assertEqual([r["id"] for r in out], ["a", "b", "c", "d", "e"])
        # Each path encodes the CQL safely and increments start.
        self.assertIn("start=0", scripted.calls[0])
        self.assertIn("start=2", scripted.calls[1])
        self.assertIn("start=4", scripted.calls[2])
        # Pin the per-page limit so a regression dropping it from the URL fails.
        for call in scripted.calls:
            self.assertIn("limit=2", call)
        self.assertIn("space%20%3D%20%22ENG%22", scripted.calls[0])

    def test_empty_results_breaks_loop(self):
        scripted = _ScriptedGet([{"results": [], "totalSize": 100}])
        confluence_client.confluence_get = scripted
        out = confluence_client.confluence_search_cql("https://x.test", "auth", "type=page")
        self.assertEqual(out, [])
        self.assertEqual(len(scripted.calls), 1)

    def test_cql_with_special_chars_url_encoded(self):
        scripted = _ScriptedGet([{"results": [], "totalSize": 0}])
        confluence_client.confluence_get = scripted
        confluence_client.confluence_search_cql("https://x.test", "auth", "title ~ \"foo & bar\"")
        # Ampersand and quote escaped; not raw in path.
        self.assertNotIn("foo & bar", scripted.calls[0])
        self.assertIn("%22", scripted.calls[0])
        self.assertIn("%26", scripted.calls[0])


class GetPageLabelsTests(unittest.TestCase):
    def setUp(self):
        self._orig = confluence_client.confluence_get

    def tearDown(self):
        confluence_client.confluence_get = self._orig

    def test_extracts_label_names(self):
        confluence_client.confluence_get = _ScriptedGet([
            {"results": [{"name": "incident"}, {"name": "retro"}, {}]}
        ])
        out = confluence_client.confluence_get_page_labels("https://x.test", "auth", "PAGE-1")
        self.assertEqual(out, ["incident", "retro", ""])


class ADFToTextTests(unittest.TestCase):
    def test_text_node(self):
        self.assertEqual(confluence_client.adf_to_text({"type": "text", "text": "hello"}), "hello")

    def test_none_returns_empty(self):
        self.assertEqual(confluence_client.adf_to_text(None), "")

    def test_paragraph_appends_double_newline(self):
        node = {"type": "paragraph", "content": [{"type": "text", "text": "Hi"}]}
        self.assertEqual(confluence_client.adf_to_text(node), "Hi\n\n")

    def test_heading_uses_level(self):
        node = {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "Section"}],
        }
        self.assertEqual(confluence_client.adf_to_text(node), "### Section\n\n")

    def test_bullet_list_with_items(self):
        node = {
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [{"type": "text", "text": "one"}]},
                {"type": "listItem", "content": [{"type": "text", "text": "two"}]},
            ],
        }
        out = confluence_client.adf_to_text(node)
        self.assertIn("- one\n", out)
        self.assertIn("- two\n", out)

    def test_hard_break_emits_newline(self):
        self.assertEqual(confluence_client.adf_to_text({"type": "hardBreak"}), "\n")

    def test_table_cell_pipe_separator(self):
        cell = {"type": "tableCell", "content": [{"type": "text", "text": "x"}]}
        self.assertEqual(confluence_client.adf_to_text(cell), "x | ")

    def test_unknown_type_falls_through_to_children(self):
        node = {"type": "mystery", "content": [{"type": "text", "text": "z"}]}
        self.assertEqual(confluence_client.adf_to_text(node), "z")

    def test_invalid_input_returns_empty(self):
        self.assertEqual(confluence_client.adf_to_text(42), "")


class StorageToTextTests(unittest.TestCase):
    def test_basic_paragraph(self):
        out = confluence_client.storage_to_text("<p>hello world</p>")
        self.assertEqual(out, "hello world")

    def test_list_items_get_dash(self):
        out = confluence_client.storage_to_text("<ul><li>one</li><li>two</li></ul>")
        self.assertIn("- one", out)
        self.assertIn("- two", out)

    def test_br_emits_newline(self):
        out = confluence_client.storage_to_text("<p>a<br/>b</p>")
        self.assertIn("a\nb", out)

    def test_blank_line_collapsing(self):
        # Many blocks should not produce 3+ consecutive newlines.
        out = confluence_client.storage_to_text("<p>a</p><p>b</p><p>c</p>")
        self.assertNotIn("\n\n\n", out)

    def test_empty_string(self):
        self.assertEqual(confluence_client.storage_to_text(""), "")
        self.assertEqual(confluence_client.storage_to_text(None), "")


class InitAuthTests(unittest.TestCase):
    def test_missing_var_exits_with_stderr_message(self):
        env = {"JIRA_BASE_URL": "https://x.test", "JIRA_EMAIL": "u@e", "JIRA_API_TOKEN": ""}
        captured = io.StringIO()
        orig = sys.stderr
        sys.stderr = captured
        try:
            with self.assertRaises(SystemExit):
                confluence_client.init_auth(env)
        finally:
            sys.stderr = orig
        self.assertIn("JIRA_API_TOKEN", captured.getvalue())

    def test_strips_trailing_slash_and_returns_b64_auth(self):
        env = {
            "JIRA_BASE_URL": "https://x.test/",
            "JIRA_EMAIL": "u@e",
            "JIRA_API_TOKEN": "tok",
        }
        base, auth = confluence_client.init_auth(env)
        self.assertEqual(base, "https://x.test")
        self.assertEqual(base64.b64decode(auth).decode(), "u@e:tok")


if __name__ == "__main__":
    unittest.main()
