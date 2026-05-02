#!/usr/bin/env python3
"""Validate + merge the synthesise sub-agent's results into analysis.json
under the `synthesise` key.

Validation rules — strict (the renderer renders this verbatim):

- `evidence_keys` MUST be non-empty for every finding. Records without are
  dropped with WARNING.
- All `evidence_keys` MUST exist in the in-window ticket set we fetched.
  Per-key filtering happens silently; if 0 keys remain, the record is dropped.
- `audience` MUST be a non-empty subset of {"exec", "support"}.
- `confidence` MUST be one of {high, medium, low}; anything else → "medium".
- `claim` is truncated to 140 chars.
- `so_what` is truncated to 200 chars.
- If validation drops EVERY synthesise finding, the report falls back to
  rendering raw analysis.findings grouped by audience_hint (handled by
  report.py — this file just emits an empty list in that case).
"""

import json
import os
import re
import sys

import concurrency
from jira_client import atomic_write_json


CACHE_DIR = "/tmp/support_trends"
RESULTS_PATH = os.path.join(CACHE_DIR, "synthesise", "results.json")
ANALYSIS_PATH = os.path.join(CACHE_DIR, "analysis.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")

_VALID_CONFIDENCE = {"high", "medium", "low"}
_VALID_AUDIENCE = {"exec", "support"}
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
MAX_CLAIM_CHARS = 140
# Raised from 200 to 320: prior runs truncated `so_what` mid-word (e.g.
# "...give L2 a CLI re-grant ru") because the agent prompt allows up to 200
# chars of useful prose and the action clause often runs into a recommendation
# tail. 320 covers the long tail without inviting paragraphs.
MAX_SO_WHAT_CHARS = 320


def _smart_truncate(text, limit):
    """Truncate `text` to at most `limit` chars without splitting a word.

    If the raw string is already within limit, return it unchanged. Otherwise
    cut to the last whitespace before `limit - 1` and append `…`. Falls back to
    a hard cut when the string has no whitespace before the limit (e.g. a giant
    URL): one ugly truncation is still better than a chopped-mid-word claim.
    """
    if text is None:
        return ""
    s = str(text)
    if len(s) <= limit:
        return s
    cut = s.rfind(" ", 0, limit - 1)
    if cut <= 0:
        return s[: limit - 1].rstrip() + "…"
    return s[:cut].rstrip(" ,;:") + "…"


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("WARNING: %s unreadable (%s)" % (path, e), file=sys.stderr)
        return None


def _valid_keys(data):
    return {t.get("key", "") for t in (data.get("tickets") or [])
            if _KEY_RE.match(t.get("key", ""))}


def _filter_keys(raw, valid):
    return [k for k in (raw or [])
            if isinstance(k, str) and _KEY_RE.match(k.strip()) and k.strip() in valid]


def _validate_audience(raw):
    if not isinstance(raw, list):
        return None
    out = [a for a in raw if a in _VALID_AUDIENCE]
    return out or None


def _validate_finding(rec, valid_keys):
    keys = _filter_keys(rec.get("evidence_keys"), valid_keys)
    if not keys:
        print("WARNING: synthesise finding dropped — no valid evidence_keys: %r" % (
            rec.get("claim", "")[:80]), file=sys.stderr)
        return None
    audience = _validate_audience(rec.get("audience"))
    if not audience:
        print("WARNING: synthesise finding dropped — invalid audience: %r" % rec.get("audience"),
              file=sys.stderr)
        return None
    claim = str(rec.get("claim") or "").strip()
    if not claim:
        print("WARNING: synthesise finding dropped — empty claim", file=sys.stderr)
        return None
    # Coerce non-string confidence (int, bool, None) to string before normalising —
    # an int from the agent would crash .strip() and drop every finding.
    confidence = str(rec.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"
    return {
        "claim": _smart_truncate(claim, MAX_CLAIM_CHARS),
        "metric": str(rec.get("metric") or "")[:80],
        "evidence_keys": keys[:20],
        "audience": audience,
        "so_what": _smart_truncate(rec.get("so_what") or "", MAX_SO_WHAT_CHARS),
        "confidence": confidence,
    }


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    results = _load_json(RESULTS_PATH)
    if results is None:
        print("WARNING: %s missing — synthesise sub-agent likely failed; report will fall back to raw analysis findings." % RESULTS_PATH, file=sys.stderr)
        # Write empty findings list so report.py knows the section ran but
        # produced nothing. Distinguishes "agent ran and found nothing" from
        # "agent didn't run at all" — both render the same way (raw fallback).
        sys.exit(0)
    if not isinstance(results, dict):
        print("WARNING: %s top level is %s, not a JSON object — sub-agent produced malformed output; falling back." % (
            RESULTS_PATH, type(results).__name__), file=sys.stderr)
        sys.exit(0)
    analysis = _load_json(ANALYSIS_PATH)
    if analysis is None:
        print("ERROR: analysis.json missing.", file=sys.stderr)
        sys.exit(1)
    data = _load_json(DATA_PATH)
    if data is None:
        print("ERROR: data.json missing.", file=sys.stderr)
        sys.exit(1)

    valid_keys = _valid_keys(data)

    findings = []
    for rec in (results.get("findings") or []):
        validated = _validate_finding(rec, valid_keys)
        if validated:
            findings.append(validated)

    exec_count = sum(1 for f in findings if "exec" in f["audience"])
    support_count = sum(1 for f in findings if "support" in f["audience"])

    analysis["synthesise"] = {
        "findings": findings,
        "summary": {
            "total": len(findings),
            "exec": exec_count,
            "support": support_count,
        },
    }

    atomic_write_json(ANALYSIS_PATH, analysis)
    print("Synthesise merged: %d findings (%d exec / %d support)" % (
        len(findings), exec_count, support_count))


if __name__ == "__main__":
    main()
