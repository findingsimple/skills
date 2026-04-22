#!/usr/bin/env python3
"""Minimal pure-function tests for fetch.py.

Covers the three areas where a silent regression has real user impact:

1. Regex validators — bypasses would become JQL injection.
2. extract_keywords — empty result silently starves similar-search.
3. adf_to_text — Atlassian schema shifts silently drop ticket descriptions.

No mocks, no Jira, no network. Run with:

    python3 -m unittest test_fetch.py
"""

import unittest

from fetch import (
    EPICS_LIST_RE,
    EXTENSION_RE,
    ISSUE_KEY_RE,
    PROJECT_KEY_RE,
    SAFE_PATH_RE,
    extract_keywords,
    scrub_term,
)
from jira_client import adf_to_text


class RegexValidators(unittest.TestCase):
    def test_issue_key_accepts_valid(self):
        for key in ("PROJ-1", "PROJ-123", "A_B-99", "X1-7"):
            self.assertTrue(ISSUE_KEY_RE.match(key), key)

    def test_issue_key_rejects_invalid(self):
        bad = [
            "",
            "proj-1",          # lowercase
            "PROJ-1\n",        # trailing newline (regression: `$` used to allow)
            "PROJ-1,PROJ-2",   # list form, not single key
            "PROJ 1",          # space
            "-1",
            "PROJ-",
            "PROJ-abc",
            "PROJ-1; DROP TABLE",
            "PROJ‐1",     # unicode hyphen
            "PROJ-1\x00",      # null byte
            "\nPROJ-1",
        ]
        for key in bad:
            self.assertFalse(ISSUE_KEY_RE.match(key), "should reject: %r" % key)

    def test_epics_list_accepts_valid(self):
        # Jira project keys are always 2+ uppercase chars.
        for value in ("PROJ-1", "PROJ-1,PROJ-2", "AA-1,BB-2,C_D-3"):
            self.assertTrue(EPICS_LIST_RE.match(value), value)

    def test_epics_list_rejects_injection(self):
        bad = [
            "",
            "PROJ-1,",
            ",PROJ-1",
            "PROJ-1\n",               # trailing newline bypass
            "PROJ-1;DROP TABLE",
            "PROJ-1 OR 1=1",
            "PROJ-1,,PROJ-2",
            'PROJ-1") OR ("a"="a',
            "proj-1",
        ]
        for value in bad:
            self.assertFalse(EPICS_LIST_RE.match(value), "should reject: %r" % value)

    def test_project_key(self):
        self.assertTrue(PROJECT_KEY_RE.match("SUP"))
        self.assertTrue(PROJECT_KEY_RE.match("PROJ_X"))
        self.assertFalse(PROJECT_KEY_RE.match("sup"))
        self.assertFalse(PROJECT_KEY_RE.match("SUP\n"))
        self.assertFalse(PROJECT_KEY_RE.match("SUP; DROP"))

    def test_extension_regex(self):
        for ok in ("rb", "ts", "tsx", "py", "JAVA"):
            self.assertTrue(EXTENSION_RE.match(ok), ok)
        for bad in ("rb;rm", ".rb", "rb\n", "", "a" * 9, "rb/ts"):
            self.assertFalse(EXTENSION_RE.match(bad), "should reject: %r" % bad)

    def test_safe_path_regex(self):
        self.assertTrue(SAFE_PATH_RE.match("/Users/alex/code"))
        self.assertTrue(SAFE_PATH_RE.match("/repo with space/src"))
        self.assertFalse(SAFE_PATH_RE.match("/repo; rm -rf ~"))
        self.assertFalse(SAFE_PATH_RE.match("/repo$(evil)"))
        self.assertFalse(SAFE_PATH_RE.match("/repo\nmalicious"))


class ExtractKeywords(unittest.TestCase):
    def test_empty_ticket_returns_empty(self):
        self.assertEqual(extract_keywords({}), [])
        self.assertEqual(extract_keywords({"fields": {}}), [])

    def test_stopwords_only_returns_empty(self):
        ticket = {"fields": {"summary": "this issue cannot from when", "labels": [], "components": []}}
        self.assertEqual(extract_keywords(ticket), [])

    def test_collects_from_labels_components_summary(self):
        ticket = {"fields": {
            "summary": "Invoice generation fails for Stripe integration",
            "labels": ["Billing"],
            "components": [{"name": "Payments"}],
        }}
        kws = extract_keywords(ticket)
        # Lowercased, sorted, stopwords removed, short tokens (<4 chars) excluded.
        for expected in ("billing", "payments", "invoice", "generation", "fails", "stripe", "integration"):
            self.assertIn(expected, kws)
        for unexpected in ("for", "when", "this"):
            self.assertNotIn(unexpected, kws)

    def test_scrubs_jql_metachars_from_labels_and_components(self):
        ticket = {"fields": {
            "summary": "",
            "labels": ['foo") OR project = ADMIN OR text ~ ("bar'],
            "components": [{"name": "Payments*"}, {"name": "Billing~"}],
        }}
        kws = extract_keywords(ticket)
        joined = " ".join(kws)
        # Quote/paren/operator chars that could break JQL string quoting
        # must all be scrubbed. (Residual words like "or" inside the
        # resulting `text ~ "..."` match string are not dangerous — they
        # can't escape the enclosing quotes once the `"` itself is gone.)
        for forbidden in ('"', "(", ")", "*", "~", "="):
            self.assertNotIn(forbidden, joined, "should not contain %r in %r" % (forbidden, joined))


class ScrubTerm(unittest.TestCase):
    def test_strips_shell_and_lucene_metas(self):
        # scrub_term collapses runs of whitespace into a single space.
        self.assertEqual(scrub_term('foo"; rm -rf'), "foo rm -rf")
        self.assertEqual(scrub_term("bar*?"), "bar")
        self.assertEqual(scrub_term("   "), "")
        self.assertEqual(scrub_term(None), "")
        # Dangerous chars individually:
        for meta in ('"', ";", "|", "`", "$", "(", ")", "*", "?", "~", "\\", "<", ">", "\n"):
            self.assertNotIn(meta, scrub_term("x%sy" % meta))


class ADFToText(unittest.TestCase):
    def test_empty_adf(self):
        self.assertEqual(adf_to_text({}), "")
        self.assertEqual(adf_to_text(None), "")

    def test_paragraph_with_text(self):
        adf = {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hello world"}]},
        ]}
        self.assertEqual(adf_to_text(adf), "Hello world")

    def test_bullet_list(self):
        adf = {"type": "doc", "content": [
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                ]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                ]},
            ]},
        ]}
        self.assertIn("- one", adf_to_text(adf))
        self.assertIn("- two", adf_to_text(adf))

    def test_unknown_node_does_not_crash(self):
        adf = {"type": "doc", "content": [
            {"type": "somethingNew", "content": [{"type": "text", "text": "ignored"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "kept"}]},
        ]}
        self.assertEqual(adf_to_text(adf).strip(), "kept")


if __name__ == "__main__":
    unittest.main()
