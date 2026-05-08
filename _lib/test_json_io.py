#!/usr/bin/env python3
"""Unit tests for _lib/json_io.py."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr

from json_io import load_json


class LoadJsonTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="json-io-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_loads_valid_json(self):
        path = self._write("data.json", json.dumps({"key": "value", "n": 42}))
        self.assertEqual(load_json(path), {"key": "value", "n": 42})

    def test_missing_file_returns_none_silently(self):
        # The "file not yet written by upstream stage" case — no warning.
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = load_json(os.path.join(self.tmp, "does-not-exist.json"))
        self.assertIsNone(result)
        self.assertEqual(buf.getvalue(), "")

    def test_corrupt_json_warns_and_returns_none(self):
        path = self._write("corrupt.json", "{not valid json")
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = load_json(path)
        self.assertIsNone(result)
        self.assertIn("WARNING", buf.getvalue())
        self.assertIn("unreadable", buf.getvalue())
        self.assertIn(path, buf.getvalue())

    def test_empty_file_warns_and_returns_none(self):
        # Truncated write — file exists but is empty. JSONDecodeError, not
        # FileNotFoundError, so it must warn.
        path = self._write("empty.json", "")
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = load_json(path)
        self.assertIsNone(result)
        self.assertIn("WARNING", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
