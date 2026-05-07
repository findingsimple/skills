"""Shared helpers for skills that fan out per-batch agent prompts.

Used by `root-cause-triage`'s `enrich.py` and `autofill.py`. Both scripts:
  * write `batch_N.txt` prompts under a `/tmp/<skill>/` directory,
  * arrange for each agent to write its JSON result array to `results_batch_N.json`,
  * later `apply` step reads those results back, optionally merging with legacy
    per-key `result_{KEY}.json` files left by older runs (or by an orchestrator
    that materialised them out-of-band).

This module captures the four pieces of behaviour that were previously copy-pasted:

  * `PROMPT_FOOTER`        — the boilerplate that tells the agent where to write.
  * `existing_result_keys` — set of keys that already have a result, used by `prepare`
                             to skip already-processed issues. NOTE: keys for per-key
                             files are derived from the *filename*, not the payload —
                             a zero-byte/corrupt `result_PROJ-1.json` still counts as
                             "PROJ-1 has a result" for skip purposes. Pair this with
                             `load_results` (which keys by payload) to detect drift.
  * `load_results`         — read all results, with per-batch entries taking precedence
                             over legacy per-key entries. Per-batch files are sorted
                             *numerically* by batch number so `results_batch_10.json`
                             reliably wins over `results_batch_2.json` for shared keys.
  * `materialize_per_key_cache` — write per-key cache files for downstream consumers
                                  (e.g. `merge_results.py`, `build_prompts.py`) that
                                  still read `result_{KEY}.json`. Always overwrites
                                  with the latest data — per-batch is canonical.

Security: keys flowing into per-key filenames are validated against an anchored
issue-key regex before interpolation, matching the repo's CLAUDE.md Security Notes
convention. A malicious key like `../../../etc/passwd` is rejected — not silently
written outside `directory`.
"""

import glob
import json
import os
import re


PROMPT_FOOTER = """

---

**IMPORTANT:** Write ONLY the JSON to {output_path} using the Bash tool. No preamble, no commentary, no markdown fences — just the raw JSON starting with `[` and ending with `]`. After writing, confirm the file was saved.
"""

# Anchored issue-key regex (matches the repo convention used by collect.py /
# build_prompts.py). re.ASCII so unicode digit/letter classes can't bypass.
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)


def _read_json(path):
    """Read JSON from path, returning None on any read or parse error.

    A `JSONDecodeError` is logged to stderr so that silent corruption is
    visible (vs. yesterday's behaviour where a corrupted file was indistinguishable
    from a missing one). `OSError` (permission denied, transient I/O) is silent
    on the assumption the caller will retry.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        # Surface corruption so a bad file doesn't silently cause re-prepare loops.
        print("WARNING: malformed JSON in %s: %s" % (path, exc), file=__import__("sys").stderr)
        return None
    except OSError:
        return None


def _batch_num_from_path(path):
    """Extract the integer batch number from a `results_batch_N.json` path.

    Returns -1 if the filename doesn't match the expected pattern, which sorts
    such files first and keeps them harmless (any well-formed batch will overwrite
    them in load_results).
    """
    base = os.path.basename(path)
    m = re.match(r"\Aresults_batch_(\d+)\.json\Z", base, re.ASCII)
    return int(m.group(1)) if m else -1


def existing_result_keys(directory):
    """Return the set of issue keys that already have a result in `directory`.

    Per-key files contribute via filename (no read), per-batch files via payload.
    A zero-byte or corrupt `result_KEY.json` still counts as "KEY has a result"
    here — see module docstring for the rationale.
    """
    keys = set()

    for path in glob.glob(os.path.join(directory, "result_*.json")):
        base = os.path.basename(path)
        # Strip the "result_" prefix and ".json" suffix
        keys.add(base[len("result_"):-len(".json")])

    for path in glob.glob(os.path.join(directory, "results_batch_*.json")):
        data = _read_json(path)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("key"):
                    keys.add(item["key"])

    return keys


def load_results(directory):
    """Load all per-issue results from `directory`, keyed by issue key.

    Reads legacy per-key files first, then per-batch files. Per-batch entries
    overwrite per-key entries when both exist for the same key, on the basis
    that per-batch files are the new canonical output and a stale per-key file
    should not shadow them.

    Per-batch files are processed in *numeric* order of batch number — so
    `results_batch_10.json` wins over `results_batch_2.json` for shared keys
    (the higher batch number is later in the iteration order). This matters
    only when a key appears in multiple per-batch files, which shouldn't happen
    in normal use but can occur if `prepare` was re-run with a different
    batch-size.
    """
    results = {}

    for path in sorted(glob.glob(os.path.join(directory, "result_*.json"))):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("key"):
            results[data["key"]] = data

    batch_paths = glob.glob(os.path.join(directory, "results_batch_*.json"))
    for path in sorted(batch_paths, key=_batch_num_from_path):
        data = _read_json(path)
        # Per-batch files are *only* written as JSON arrays — see PROMPT_FOOTER.
        # If a file is malformed or unexpectedly shaped, skip rather than silently
        # accepting a single dict (which would mask a real bug in the agent output).
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict) and item.get("key"):
                results[item["key"]] = item

    return results


def materialize_per_key_cache(directory, results):
    """Write per-key `result_{KEY}.json` files for downstream consumers.

    Always overwrites — per-batch is canonical, so a stale per-key file from
    an older agent schema must not shadow the freshest data. Writes are atomic
    (write to `.tmp`, then `os.replace`). Returns the count of files written.

    Keys are validated against the anchored `_KEY_RE` pattern before
    interpolation into the filename. An invalid key (e.g. one containing path
    separators) is rejected with a stderr warning rather than written. This is
    the security boundary referenced in the module docstring.
    """
    import sys
    written = 0
    for key, item in results.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            print("WARNING: refusing to materialize cache for invalid key %r" % key,
                  file=sys.stderr)
            continue
        cache_path = os.path.join(directory, "result_%s.json" % key)
        tmp_path = cache_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(item, f, indent=2)
            os.replace(tmp_path, cache_path)
            written += 1
        except Exception:
            # Best-effort cleanup — never leave a half-written .tmp behind.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    return written
