#!/usr/bin/env python3
"""Unit tests for snapshot.py — focus_team mismatch guard, missing upstream,
valid happy-path."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PY = os.path.join(SKILL_DIR, "snapshot.py")


class SnapshotTests(unittest.TestCase):
    """End-to-end tests via subprocess — snapshot.py is a thin script and the
    most useful failure modes (missing upstream, mismatched focus_team) are
    only meaningful at the script-execution boundary."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="snapshot-test-")
        self.cache_dir = os.path.join(self.tmp, "charter-boundaries")
        self.upstream_dir = os.path.join(self.tmp, "support-routing-audit")
        os.makedirs(self.cache_dir, mode=0o700)
        os.makedirs(self.upstream_dir, mode=0o700)
        # Write a minimal setup.json with one focus team.
        with open(os.path.join(self.cache_dir, "setup.json"), "w") as f:
            json.dump({"focus_teams": [{"canonical": "ACE"}, {"canonical": "COPS"}]}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, team, env_overrides=None):
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        # We patch CACHE_DIR + UPSTREAM_AUDIT via a shim because snapshot.py
        # uses module-level constants. Instead, run snapshot.py directly with
        # a side script that imports it and rebinds.
        script = (
            "import sys, os, json\n"
            "sys.path.insert(0, %r)\n"
            "import snapshot\n"
            "snapshot.CACHE_DIR = %r\n"
            "snapshot.AUDIT_DIR = os.path.join(%r, 'audits')\n"
            "snapshot.UPSTREAM_AUDIT = %r\n"
            "sys.argv = ['snapshot.py', '--team', %r]\n"
            "try:\n"
            "    snapshot.main()\n"
            "    print('EXIT:0')\n"
            "except SystemExit as e:\n"
            "    print('EXIT:' + str(e.code))\n"
        ) % (
            SKILL_DIR,
            self.cache_dir,
            self.cache_dir,
            os.path.join(self.upstream_dir, "audit.json"),
            team,
        )
        return subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, env=env,
        )

    def _write_upstream(self, focus_team):
        path = os.path.join(self.upstream_dir, "audit.json")
        with open(path, "w") as f:
            json.dump({"focus_team": focus_team, "tickets": [{"key": "ECS-1"}]}, f)

    def test_happy_path_writes_snapshot(self):
        self._write_upstream("ACE")
        result = self._run("ACE")
        self.assertIn("EXIT:0", result.stdout, msg=result.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.cache_dir, "audits", "ACE.json")))

    def test_focus_team_mismatch_fails(self):
        self._write_upstream("ACE")
        result = self._run("COPS")
        self.assertIn("EXIT:1", result.stdout, msg=result.stderr)
        self.assertIn("does not match", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, "audits", "COPS.json")))

    def test_unknown_team_fails(self):
        self._write_upstream("Echo")
        result = self._run("Echo")
        self.assertIn("EXIT:2", result.stdout, msg=result.stderr)
        self.assertIn("not in setup focus_teams", result.stderr)

    def test_missing_upstream_fails(self):
        # Don't write upstream audit.json.
        result = self._run("ACE")
        self.assertIn("EXIT:1", result.stdout, msg=result.stderr)
        self.assertIn("not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
