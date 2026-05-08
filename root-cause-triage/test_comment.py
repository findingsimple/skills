#!/usr/bin/env python3
"""Tests for comment.py — vault Markdown extraction, ADF building, marker matching.

The Jira API boundary is mocked; no network. Run with:
  cd ~/.claude/skills/root-cause-triage && python3 -m unittest test_comment.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import comment
import _vault


class StripFrontmatterTests(unittest.TestCase):
    def test_strips_yaml_frontmatter(self):
        src = "---\nkey: PROJ-1\n---\n\n## Body\nHello\n"
        self.assertEqual(comment.strip_frontmatter(src), "\n## Body\nHello\n")

    def test_no_frontmatter_returns_original(self):
        src = "## Body\nHello\n"
        self.assertEqual(comment.strip_frontmatter(src), src)

    def test_unterminated_frontmatter_returns_original(self):
        src = "---\nkey: PROJ-1\n# never closes\n"
        self.assertEqual(comment.strip_frontmatter(src), src)


class ExtractRcaTests(unittest.TestCase):
    def test_extracts_content_until_next_h2(self):
        body = (
            "## Root Cause Analysis\n\n"
            "Race in the connection pool.\n\n"
            "Surfaced under load.\n\n"
            "## Description\n\nSomething else.\n"
        )
        self.assertEqual(
            comment.extract_rca(body),
            "Race in the connection pool.\n\nSurfaced under load.",
        )

    def test_extracts_content_at_end_of_file(self):
        body = "## Root Cause Analysis\n\nLast section in the file.\n"
        self.assertEqual(comment.extract_rca(body), "Last section in the file.")

    def test_missing_section_returns_empty(self):
        self.assertEqual(comment.extract_rca("## Description\n\nHi"), "")

    def test_empty_section_returns_empty(self):
        body = "## Root Cause Analysis\n\n\n## Description\n\nHi"
        self.assertEqual(comment.extract_rca(body), "")


class ExtractAutofillTests(unittest.TestCase):
    def _build(self, sections_block):
        return "## Auto-filled Template Sections\n\n" + sections_block + "\n## Description\n\nx\n"

    def test_all_confidence_levels(self):
        body = self._build(
            "### Background Context\n*Confidence: high*\n\nA.\n\n"
            "### Steps to reproduce\n*Confidence: medium*\n\nB.\n\n"
            "### Actual Results\n*Confidence: low*\n\nC.\n\n"
            "### Expected Results\n*Confidence: unknown*\n\nD.\n"
        )
        out = comment.extract_autofill(body)
        self.assertEqual(out["Background Context"]["confidence"], "high")
        self.assertEqual(out["Steps to reproduce"]["confidence"], "medium")
        self.assertEqual(out["Actual Results"]["confidence"], "low")
        self.assertEqual(out["Expected Results"]["confidence"], "unknown")
        self.assertEqual(out["Background Context"]["content"], "A.")

    def test_insufficient_evidence_section_skipped(self):
        body = self._build(
            "### Background Context\n*Confidence: low*\n\n*(insufficient evidence)*\n\n"
            "### Analysis\n*Confidence: high*\n\nReal content.\n"
        )
        out = comment.extract_autofill(body)
        self.assertNotIn("Background Context", out)
        self.assertIn("Analysis", out)

    def test_empty_section_skipped(self):
        body = self._build(
            "### Background Context\n*Confidence: high*\n\n\n"
            "### Analysis\n*Confidence: high*\n\nKept.\n"
        )
        out = comment.extract_autofill(body)
        self.assertNotIn("Background Context", out)
        self.assertIn("Analysis", out)

    def test_obsidian_callout_stripped(self):
        body = (
            "## Auto-filled Template Sections\n\n"
            "> [!note] Agent-generated from 5 linked tickets (3 with descriptions). Review before using.\n\n"
            "### Analysis\n*Confidence: high*\n\nKept.\n"
        )
        out = comment.extract_autofill(body)
        self.assertEqual(list(out.keys()), ["Analysis"])
        self.assertEqual(out["Analysis"]["content"], "Kept.")

    def test_missing_callout_still_parses(self):
        body = "## Auto-filled Template Sections\n\n### Analysis\n*Confidence: high*\n\nKept.\n"
        out = comment.extract_autofill(body)
        self.assertEqual(out["Analysis"]["content"], "Kept.")

    def test_missing_confidence_line_defaults_to_unknown(self):
        body = "## Auto-filled Template Sections\n\n### Analysis\n\nNo confidence stated.\n"
        out = comment.extract_autofill(body)
        self.assertEqual(out["Analysis"]["confidence"], "unknown")
        self.assertEqual(out["Analysis"]["content"], "No confidence stated.")

    def test_no_autofill_section_returns_empty(self):
        self.assertEqual(comment.extract_autofill("## Description\n\nHi"), {})


class ParagraphsFromTextTests(unittest.TestCase):
    def test_single_chunk_one_paragraph(self):
        out = comment.paragraphs_from_text("Hello world")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["content"][0]["text"], "Hello world")

    def test_blank_line_split(self):
        out = comment.paragraphs_from_text("Para one.\n\nPara two.\n\nPara three.")
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2]["content"][0]["text"], "Para three.")

    def test_single_newline_does_not_split(self):
        out = comment.paragraphs_from_text("Line one.\nLine two.")
        self.assertEqual(len(out), 1)
        self.assertIn("Line one.", out[0]["content"][0]["text"])

    def test_whitespace_only_returns_empty(self):
        # Critical: Jira's ADF schema rejects empty paragraph nodes.
        self.assertEqual(comment.paragraphs_from_text("   \n\n  "), [])
        self.assertEqual(comment.paragraphs_from_text(""), [])

    def test_trailing_blank_lines_dropped(self):
        out = comment.paragraphs_from_text("Hello\n\n\n\n")
        self.assertEqual(len(out), 1)


class FindAiCommentsTests(unittest.TestCase):
    def _ai(self, cid, account_id="acct-bot", body_first_line=None):
        return {
            "id": cid,
            "author_account_id": account_id,
            "body_text": (body_first_line or comment.COMMENT_HEADER) + "\n\nbody",
        }

    def test_matches_exact_first_line(self):
        comments = [
            {"id": "1", "author_account_id": "acct-x", "body_text": "Some other comment"},
            self._ai("42", account_id="acct-bot"),
        ]
        self.assertEqual(comment.find_ai_comments(comments), [("42", "acct-bot")])

    def test_quoted_reply_does_not_match(self):
        comments = [
            {"id": "99", "author_account_id": "acct-x",
             "body_text": "> " + comment.COMMENT_HEADER + "\n\nreply"},
        ]
        self.assertEqual(comment.find_ai_comments(comments), [])

    def test_header_in_body_not_first_line_does_not_match(self):
        comments = [
            {"id": "5", "author_account_id": "acct-x",
             "body_text": "Reply\n\n" + comment.COMMENT_HEADER},
        ]
        self.assertEqual(comment.find_ai_comments(comments), [])

    def test_trailing_whitespace_on_marker_still_matches(self):
        # Jira renderers occasionally trim differently; the .strip() is load-bearing.
        comments = [{"id": "7", "author_account_id": "acct-bot",
                     "body_text": comment.COMMENT_HEADER + "   \n\nbody"}]
        self.assertEqual(comment.find_ai_comments(comments), [("7", "acct-bot")])

    def test_empty_body_skipped(self):
        comments = [
            {"id": "1", "author_account_id": "acct-x", "body_text": ""},
            {"id": "2", "author_account_id": "acct-x", "body_text": None},
        ]
        self.assertEqual(comment.find_ai_comments(comments), [])

    def test_returns_all_matches_with_authors(self):
        comments = [
            self._ai("1", account_id="acct-bot"),
            {"id": "2", "author_account_id": "acct-x", "body_text": "other"},
            self._ai("3", account_id="acct-mallory"),
        ]
        self.assertEqual(
            comment.find_ai_comments(comments),
            [("1", "acct-bot"), ("3", "acct-mallory")],
        )

    def test_missing_author_account_id_falls_back_to_empty(self):
        comments = [{"id": "1", "body_text": comment.COMMENT_HEADER + "\nbody"}]
        self.assertEqual(comment.find_ai_comments(comments), [("1", "")])


class CommentIdRegexTests(unittest.TestCase):
    """The H1 fix: comment ids interpolated into the PUT URL must be numeric."""

    def test_numeric_id_accepted(self):
        self.assertIsNotNone(comment.COMMENT_ID_RE.match("12345"))

    def test_path_traversal_rejected(self):
        self.assertIsNone(comment.COMMENT_ID_RE.match("123/../456"))

    def test_alphabetic_rejected(self):
        self.assertIsNone(comment.COMMENT_ID_RE.match("abc"))

    def test_empty_rejected(self):
        self.assertIsNone(comment.COMMENT_ID_RE.match(""))

    def test_trailing_newline_rejected(self):
        # \A...\Z (not ^...$) closes the trailing-newline gap.
        self.assertIsNone(comment.COMMENT_ID_RE.match("123\n"))


class BuildAdfTests(unittest.TestCase):
    def test_marker_is_first_node_strong_paragraph(self):
        adf = comment.build_adf("Some RCA.", {})
        first = adf["content"][0]
        self.assertEqual(first["type"], "paragraph")
        self.assertEqual(first["content"][0]["text"], comment.COMMENT_HEADER)
        self.assertEqual(first["content"][0]["marks"], [{"type": "strong"}])

    def test_no_rca_no_autofill_produces_marker_only(self):
        adf = comment.build_adf("", {})
        self.assertEqual(len(adf["content"]), 1)
        self.assertEqual(adf["content"][0]["type"], "paragraph")

    def test_marker_survives_adf_to_text_round_trip(self):
        # Critical: the simple adf_to_text drops heading nodes. The marker must be
        # in a paragraph so the first line of body_text matches COMMENT_HEADER.
        import _libpath  # noqa: F401
        from jira_client import adf_to_text
        adf = comment.build_adf("Some RCA.", {})
        body_text = adf_to_text(adf)
        first_line = body_text.strip().splitlines()[0]
        self.assertEqual(first_line, comment.COMMENT_HEADER)

    def test_canonical_section_order_preserved(self):
        sections = {
            "Analysis": {"confidence": "high", "content": "A"},
            "Background Context": {"confidence": "low", "content": "B"},
            "Steps to reproduce": {"confidence": "medium", "content": "S"},
        }
        adf = comment.build_adf("", sections)
        # Pull heading-3 texts in order
        h3s = [
            n["content"][0]["text"]
            for n in adf["content"]
            if n["type"] == "heading" and n["attrs"]["level"] == 3
        ]
        self.assertEqual(h3s, ["Background Context", "Steps to reproduce", "Analysis"])

    def test_confidence_rendered_as_em(self):
        adf = comment.build_adf("", {"Analysis": {"confidence": "low", "content": "x"}})
        em_paras = [
            n for n in adf["content"]
            if n["type"] == "paragraph"
            and n["content"][0].get("marks") == [{"type": "em"}]
            and n["content"][0]["text"].startswith("Confidence:")
        ]
        self.assertEqual(len(em_paras), 1)
        self.assertEqual(em_paras[0]["content"][0]["text"], "Confidence: low")


class FindIssueMarkdownTests(unittest.TestCase):
    def test_finds_matching_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "PROJ-1 — Sample issue.md")
            open(path, "w").close()
            self.assertEqual(_vault.find_issue_markdown(d, "PROJ-1"), path)

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_vault.find_issue_markdown(d, "PROJ-99"))

    def test_partial_key_match_does_not_match(self):
        # Glob includes the literal " — " separator, so PROJ-1 must not match PROJ-10's file.
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "PROJ-10 — Other.md"), "w").close()
            self.assertIsNone(_vault.find_issue_markdown(d, "PROJ-1"))


class ResolveFromFileTests(unittest.TestCase):
    def test_reads_keys_from_allowed_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "keys.txt")
            with open(path, "w") as f:
                f.write("PROJ-1\nPROJ-2\n\n  PROJ-3  \n")
            out = comment._resolve_from_file(path, [d])
            self.assertEqual(out, ["PROJ-1", "PROJ-2", "PROJ-3"])

    def test_rejects_path_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
            path = os.path.join(outside, "keys.txt")
            with open(path, "w") as f:
                f.write("PROJ-1\n")
            with self.assertRaises(SystemExit):
                comment._resolve_from_file(path, [allowed])

    def test_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "real.txt")
            with open(target, "w") as f:
                f.write("PROJ-1\n")
            link = os.path.join(d, "link.txt")
            os.symlink(target, link)
            with self.assertRaises(SystemExit):
                comment._resolve_from_file(link, [d])


class AdfSizeCapTests(unittest.TestCase):
    """The size cap lives at process_key — exercised indirectly here by checking
    that build_adf produces a body whose serialized form is bounded for typical input."""

    def test_max_adf_bytes_constant_present(self):
        self.assertGreaterEqual(comment.MAX_ADF_BYTES, 16_000)
        self.assertLessEqual(comment.MAX_ADF_BYTES, 64_000)

    def test_typical_payload_well_under_cap(self):
        sections = {name: {"confidence": "high", "content": "Some content. " * 20}
                    for name in comment.AUTOFILL_SECTIONS}
        adf = comment.build_adf("RCA paragraph. " * 20, sections)
        import json
        size = len(json.dumps({"body": adf}).encode("utf-8"))
        self.assertLess(size, comment.MAX_ADF_BYTES)


class AcquireLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lock = os.path.join(self.tmp, "test.lock")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_lock_acquires_cleanly(self):
        comment.acquire_lock(self.lock)
        self.assertTrue(os.path.exists(self.lock))
        with open(self.lock) as f:
            content = f.read().strip()
        pid_str = content.split()[0]
        self.assertEqual(int(pid_str), os.getpid())

    def test_live_pid_lock_refuses(self):
        # Our own pid is alive — write it as the existing lock and re-acquire.
        with open(self.lock, "w") as f:
            f.write("%d 2026-01-01T00:00:00\n" % os.getpid())
        with self.assertRaises(SystemExit) as ctx:
            comment.acquire_lock(self.lock)
        self.assertEqual(ctx.exception.code, 1)

    def test_stale_pid_lock_overwrites(self):
        # Find a definitely-dead PID — try a high one.
        for candidate in (999999, 999998, 99999):
            try:
                os.kill(candidate, 0)
            except OSError:
                dead_pid = candidate
                break
        else:
            self.skipTest("no dead PID available")
        with open(self.lock, "w") as f:
            f.write("%d 2020-01-01T00:00:00\n" % dead_pid)
        comment.acquire_lock(self.lock)  # should not raise
        with open(self.lock) as f:
            self.assertIn(str(os.getpid()), f.read())

    def test_malformed_lock_overwrites(self):
        with open(self.lock, "w") as f:
            f.write("not a pid\n")
        comment.acquire_lock(self.lock)  # should not raise
        with open(self.lock) as f:
            self.assertIn(str(os.getpid()), f.read())

    def test_release_lock_removes_file(self):
        comment.acquire_lock(self.lock)
        self.assertTrue(os.path.exists(self.lock))
        comment.release_lock(self.lock)
        self.assertFalse(os.path.exists(self.lock))


class CollectKeysTests(unittest.TestCase):
    """Coverage for the new --from-jql + --limit branches."""

    class _Args:
        issue = None
        keys = None
        from_file = None
        from_jql = None
        limit = comment.DEFAULT_LIMIT

    def test_from_jql_resolves_keys(self):
        captured = {}

        def fake_search(base, auth, jql, fields, limit=None):
            captured["jql"] = jql
            return [{"key": "PROJ-1"}, {"key": "PROJ-2"}]

        original = comment.jira_search_all
        comment.jira_search_all = fake_search
        try:
            args = self._Args()
            args.from_jql = 'parentEpic = PROJ-99 AND status = "Backlog"'
            keys = comment.collect_keys(args, ["/tmp"], base_url="https://x", auth="auth")
            self.assertEqual(keys, ["PROJ-1", "PROJ-2"])
            self.assertEqual(captured["jql"], args.from_jql)
        finally:
            comment.jira_search_all = original

    def test_from_jql_empty_result_errors(self):
        comment_orig = comment.jira_search_all
        comment.jira_search_all = lambda *a, **k: []
        try:
            args = self._Args()
            args.from_jql = "project = NOPE"
            with self.assertRaises(SystemExit):
                comment.collect_keys(args, ["/tmp"], base_url="https://x", auth="auth")
        finally:
            comment.jira_search_all = comment_orig

    def test_from_jql_without_auth_errors(self):
        args = self._Args()
        args.from_jql = "project = X"
        with self.assertRaises(SystemExit):
            comment.collect_keys(args, ["/tmp"])

    def test_limit_cap_blocks_oversize_batch(self):
        args = self._Args()
        args.keys = ",".join(["PROJ-%d" % i for i in range(1, 12)])
        args.limit = 5
        with self.assertRaises(SystemExit):
            comment.collect_keys(args, ["/tmp"])

    def test_limit_cap_allows_at_threshold(self):
        args = self._Args()
        args.keys = "PROJ-1,PROJ-2,PROJ-3"
        args.limit = 3
        keys = comment.collect_keys(args, ["/tmp"])
        self.assertEqual(keys, ["PROJ-1", "PROJ-2", "PROJ-3"])

    def test_invalid_key_in_jql_result_rejected(self):
        comment_orig = comment.jira_search_all
        comment.jira_search_all = lambda *a, **k: [{"key": "lowercase-1"}]
        try:
            args = self._Args()
            args.from_jql = "x"
            with self.assertRaises(SystemExit):
                comment.collect_keys(args, ["/tmp"], base_url="https://x", auth="auth")
        finally:
            comment.jira_search_all = comment_orig


if __name__ == "__main__":
    unittest.main()
