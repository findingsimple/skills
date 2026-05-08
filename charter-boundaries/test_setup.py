#!/usr/bin/env python3
"""Unit tests for setup.py — _resolve_under_roots containment.

These pin the path-traversal and symlink-rejection semantics. _resolve_under_roots
is the ONLY guard between user-supplied --charters / --examples paths and the
filesystem boundary, so it gets explicit coverage."""

import os
import shutil
import tempfile
import unittest

import _libpath  # noqa: F401
from setup import _resolve_under_roots


class ResolveUnderRootsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resolve-roots-test-")
        self.allowed = os.path.realpath(os.path.join(self.tmp, "allowed"))
        self.outside = os.path.realpath(os.path.join(self.tmp, "outside"))
        os.makedirs(self.allowed)
        os.makedirs(self.outside)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, parent, name, content="hello"):
        path = os.path.join(parent, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_path_inside_allowed_root_returns_realpath(self):
        path = self._write(self.allowed, "charters.md")
        result = _resolve_under_roots(path, [self.allowed], "--charters")
        self.assertEqual(result, os.path.realpath(path))

    def test_path_outside_allowed_root_rejected(self):
        path = self._write(self.outside, "charters.md")
        result = _resolve_under_roots(path, [self.allowed], "--charters")
        self.assertIsNone(result)

    def test_relative_path_rejected(self):
        # Whatever directory the test runs from, a relative path should fail.
        result = _resolve_under_roots("charters.md", [self.allowed], "--charters")
        self.assertIsNone(result)

    def test_missing_file_rejected(self):
        path = os.path.join(self.allowed, "does-not-exist.md")
        result = _resolve_under_roots(path, [self.allowed], "--charters")
        self.assertIsNone(result)

    def test_symlink_pointing_outside_rejected(self):
        # Create a symlink inside the allowed root that points outside.
        target = self._write(self.outside, "secret.md", "secret content")
        link = os.path.join(self.allowed, "charters.md")
        os.symlink(target, link)
        result = _resolve_under_roots(link, [self.allowed], "--charters")
        self.assertIsNone(result, "symlink to outside-root must be rejected")

    def test_symlink_pointing_inside_allowed(self):
        # A symlink inside the root pointing to another file inside the root is fine.
        target = self._write(self.allowed, "real.md")
        link = os.path.join(self.allowed, "charters.md")
        os.symlink(target, link)
        result = _resolve_under_roots(link, [self.allowed], "--charters")
        self.assertEqual(result, os.path.realpath(target))

    def test_multiple_allowed_roots(self):
        # If the path resolves under any of the allowed roots, it's accepted.
        other = os.path.realpath(os.path.join(self.tmp, "second"))
        os.makedirs(other)
        path = self._write(other, "charters.md")
        result = _resolve_under_roots(path, [self.allowed, other], "--charters")
        self.assertEqual(result, os.path.realpath(path))

    def test_empty_path_rejected(self):
        self.assertIsNone(_resolve_under_roots("", [self.allowed], "--charters"))
        self.assertIsNone(_resolve_under_roots(None, [self.allowed], "--charters"))


if __name__ == "__main__":
    unittest.main()
