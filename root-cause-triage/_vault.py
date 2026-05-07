"""Locate the Obsidian Markdown file for a triage issue.

Centralises the `{KEY} — {summary}.md` filename convention so a future rename
is a one-line change instead of three drifting copies in comment.py / enrich.py /
autofill.py.
"""

import glob
import os


def find_issue_markdown(issues_dir, key):
    """Return the absolute path to the vault Markdown file for the given Jira key,
    or None if no match exists. Caller is responsible for validating `key` against
    the Jira-key regex before passing it in.
    """
    matches = glob.glob(os.path.join(issues_dir, "%s — *.md" % key))
    return matches[0] if matches else None
