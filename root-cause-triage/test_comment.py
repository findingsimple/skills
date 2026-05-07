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
    def test_marker_header_is_first_node(self):
        adf = comment.build_adf("Some RCA.", {})
        self.assertEqual(adf["content"][0]["type"], "heading")
        self.assertEqual(adf["content"][0]["content"][0]["text"], comment.COMMENT_HEADER)

    def test_no_rca_no_autofill_still_produces_header_and_intro(self):
        adf = comment.build_adf("", {})
        # heading + intro paragraph only
        self.assertEqual(len(adf["content"]), 2)

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


if __name__ == "__main__":
    unittest.main()
