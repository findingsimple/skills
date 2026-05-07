"""Unit tests for _lib/batch_results.py — no network, no external state.

Run with:
    cd _lib && python3 -m unittest test_batch_results -v
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import batch_results


class BatchResultsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="batch_results_test_")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def _write(self, name, payload):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(payload, f)

    # --- existing_result_keys --------------------------------------------------

    def test_existing_keys_empty_dir(self):
        self.assertEqual(batch_results.existing_result_keys(self.dir), set())

    def test_existing_keys_per_key_files(self):
        self._write("result_PROJ-1.json", {"key": "PROJ-1"})
        self._write("result_PROJ-2.json", {"key": "PROJ-2"})
        self.assertEqual(
            batch_results.existing_result_keys(self.dir),
            {"PROJ-1", "PROJ-2"},
        )

    def test_existing_keys_per_batch_files(self):
        self._write(
            "results_batch_0.json",
            [{"key": "PROJ-1"}, {"key": "PROJ-2"}],
        )
        self._write(
            "results_batch_1.json",
            [{"key": "PROJ-3"}],
        )
        self.assertEqual(
            batch_results.existing_result_keys(self.dir),
            {"PROJ-1", "PROJ-2", "PROJ-3"},
        )

    def test_existing_keys_mixed(self):
        self._write("result_PROJ-1.json", {"key": "PROJ-1"})
        self._write("results_batch_0.json", [{"key": "PROJ-1"}, {"key": "PROJ-2"}])
        # Set semantics — duplicates collapse.
        self.assertEqual(
            batch_results.existing_result_keys(self.dir),
            {"PROJ-1", "PROJ-2"},
        )

    def test_existing_keys_skips_corrupt_batch_file(self):
        with open(os.path.join(self.dir, "results_batch_0.json"), "w") as f:
            f.write("{not json")
        self._write("result_PROJ-9.json", {"key": "PROJ-9"})
        # Corrupt batch file is silently skipped; per-key result still found.
        self.assertEqual(
            batch_results.existing_result_keys(self.dir),
            {"PROJ-9"},
        )

    def test_existing_keys_skips_items_without_key(self):
        self._write("results_batch_0.json", [{"no_key": "x"}, {"key": "PROJ-1"}, "string-not-dict"])
        self.assertEqual(
            batch_results.existing_result_keys(self.dir),
            {"PROJ-1"},
        )

    def test_existing_keys_empty_per_key_file_diverges_from_load(self):
        # An empty (zero-byte) result_KEY.json counts as "KEY has a result" via
        # filename, but contributes nothing to load_results (no payload). Pin
        # this divergence so a future refactor doesn't accidentally couple them.
        path = os.path.join(self.dir, "result_PROJ-1.json")
        open(path, "w").close()  # zero bytes
        self.assertEqual(batch_results.existing_result_keys(self.dir), {"PROJ-1"})
        self.assertEqual(batch_results.load_results(self.dir), {})

    # --- load_results ----------------------------------------------------------

    def test_load_results_empty(self):
        self.assertEqual(batch_results.load_results(self.dir), {})

    def test_load_results_per_key_only(self):
        self._write("result_PROJ-1.json", {"key": "PROJ-1", "value": "from-per-key"})
        results = batch_results.load_results(self.dir)
        self.assertEqual(set(results), {"PROJ-1"})
        self.assertEqual(results["PROJ-1"]["value"], "from-per-key")

    def test_load_results_per_batch_only(self):
        self._write(
            "results_batch_0.json",
            [{"key": "PROJ-1", "value": "a"}, {"key": "PROJ-2", "value": "b"}],
        )
        results = batch_results.load_results(self.dir)
        self.assertEqual(set(results), {"PROJ-1", "PROJ-2"})

    def test_load_results_per_batch_overrides_per_key(self):
        # Stale per-key file should NOT shadow the newer per-batch entry.
        self._write("result_PROJ-1.json", {"key": "PROJ-1", "value": "stale"})
        self._write("results_batch_0.json", [{"key": "PROJ-1", "value": "fresh"}])
        results = batch_results.load_results(self.dir)
        self.assertEqual(results["PROJ-1"]["value"], "fresh")

    def test_load_results_numeric_batch_order(self):
        # batch_10 must win over batch_2 for shared keys — lexical sort would
        # invert this and silently use the older payload.
        self._write("results_batch_2.json", [{"key": "PROJ-1", "value": "from-batch-2"}])
        self._write("results_batch_10.json", [{"key": "PROJ-1", "value": "from-batch-10"}])
        results = batch_results.load_results(self.dir)
        self.assertEqual(results["PROJ-1"]["value"], "from-batch-10")

    def test_load_results_skips_non_list(self):
        # A bare dict (or any non-list) at the top of a per-batch file is not
        # the documented schema — skip rather than silently accept.
        self._write("results_batch_0.json", {"key": "PROJ-1", "value": "x"})
        self._write("result_PROJ-2.json", {"key": "PROJ-2"})
        results = batch_results.load_results(self.dir)
        self.assertEqual(set(results), {"PROJ-2"})

    def test_load_results_skips_string_per_batch(self):
        with open(os.path.join(self.dir, "results_batch_0.json"), "w") as f:
            json.dump("just-a-string", f)
        self._write("result_PROJ-1.json", {"key": "PROJ-1"})
        results = batch_results.load_results(self.dir)
        self.assertEqual(set(results), {"PROJ-1"})

    def test_load_results_skips_per_key_file_without_key(self):
        # `result_X.json` filename does not guarantee a "key" field — defensive.
        self._write("result_BAD.json", {"value": "no key field"})
        self.assertEqual(batch_results.load_results(self.dir), {})

    # --- materialize_per_key_cache --------------------------------------------

    def test_materialize_writes_per_key_files(self):
        results = {
            "PROJ-1": {"key": "PROJ-1", "v": 1},
            "PROJ-2": {"key": "PROJ-2", "v": 2},
        }
        written = batch_results.materialize_per_key_cache(self.dir, results)
        self.assertEqual(written, 2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "result_PROJ-1.json")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "result_PROJ-2.json")))
        with open(os.path.join(self.dir, "result_PROJ-1.json")) as f:
            self.assertEqual(json.load(f)["v"], 1)

    def test_materialize_overwrites_existing(self):
        # Per-batch is canonical — a stale per-key file MUST be overwritten,
        # not preserved (otherwise downstream consumers read outdated data).
        self._write("result_PROJ-1.json", {"key": "PROJ-1", "v": "stale"})
        results = {"PROJ-1": {"key": "PROJ-1", "v": "fresh"}}
        written = batch_results.materialize_per_key_cache(self.dir, results)
        self.assertEqual(written, 1)
        with open(os.path.join(self.dir, "result_PROJ-1.json")) as f:
            self.assertEqual(json.load(f)["v"], "fresh")

    def test_materialize_atomic_no_tmp_residue(self):
        # After write, the .tmp shadow should not exist.
        results = {"PROJ-1": {"key": "PROJ-1"}}
        batch_results.materialize_per_key_cache(self.dir, results)
        residue = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(residue, [])

    def test_materialize_atomic_on_replace_failure(self):
        # If os.replace fails mid-write, the .tmp must be cleaned up AND the
        # final cache file must remain untouched (atomicity guarantee).
        self._write("result_PROJ-1.json", {"key": "PROJ-1", "v": "original"})
        results = {"PROJ-1": {"key": "PROJ-1", "v": "would-be-new"}}

        with mock.patch("batch_results.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                batch_results.materialize_per_key_cache(self.dir, results)

        # Final file must still hold the original payload (atomicity).
        with open(os.path.join(self.dir, "result_PROJ-1.json")) as f:
            self.assertEqual(json.load(f)["v"], "original")
        # No .tmp residue.
        residue = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(residue, [])

    def test_materialize_rejects_path_traversal_key(self):
        # Path-traversal payload — anchored _KEY_RE must reject before any
        # filesystem call. No file is written, no exception raised, returned
        # count is 0, and a warning hits stderr.
        results = {"../../etc/passwd": {"key": "../../etc/passwd", "v": "evil"}}
        import io
        import sys

        captured = io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = captured
            written = batch_results.materialize_per_key_cache(self.dir, results)
        finally:
            sys.stderr = original_stderr

        self.assertEqual(written, 0)
        self.assertIn("invalid key", captured.getvalue())
        # Confirm nothing escaped the directory.
        self.assertEqual(os.listdir(self.dir), [])

    def test_materialize_rejects_non_string_key(self):
        results = {42: {"key": 42, "v": "x"}}
        import io
        import sys

        captured = io.StringIO()
        original_stderr = sys.stderr
        try:
            sys.stderr = captured
            written = batch_results.materialize_per_key_cache(self.dir, results)
        finally:
            sys.stderr = original_stderr

        self.assertEqual(written, 0)
        self.assertIn("invalid key", captured.getvalue())

    def test_materialize_accepts_standard_jira_key(self):
        # Sanity — make sure the regex isn't so tight it rejects real keys.
        results = {"PDE-13499": {"key": "PDE-13499", "v": "ok"}}
        written = batch_results.materialize_per_key_cache(self.dir, results)
        self.assertEqual(written, 1)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "result_PDE-13499.json")))

    # --- PROMPT_FOOTER ---------------------------------------------------------

    def test_prompt_footer_interpolates_output_path(self):
        rendered = batch_results.PROMPT_FOOTER.format(output_path="/tmp/x/results_batch_0.json")
        self.assertIn("/tmp/x/results_batch_0.json", rendered)
        self.assertIn("Write ONLY the JSON", rendered)

    def test_prompt_footer_has_output_path_placeholder(self):
        # The constant must contain a `{output_path}` placeholder — if a future
        # refactor accidentally hardcodes a path, callers would silently write
        # all batches to the same file.
        self.assertIn("{output_path}", batch_results.PROMPT_FOOTER)


if __name__ == "__main__":
    unittest.main()
