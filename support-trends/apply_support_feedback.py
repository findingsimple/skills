#!/usr/bin/env python3
"""Validate + merge the support-feedback sub-agent's results into analysis.json
under the `support_feedback` key.

Validation rules (rejected records are dropped with a WARNING; the run
continues so a partially-bad sub-agent output doesn't block the whole report):

- Each record MUST have a non-empty `ticket_keys` list (referenced by the
  renderer to make `[[...]]` evidence links).
- Every key in `ticket_keys` MUST exist in the in-window ticket set we
  fetched. Hallucinated keys are dropped silently.
- `confidence` is normalised to one of {high, medium, low}; anything else
  becomes `medium`.
- `summary.*_count` is recomputed from the validated arrays — never trust
  the agent's own count.
"""

import json
import os
import re
import sys

import concurrency
from jira_client import atomic_write_json


CACHE_DIR = "/tmp/support_trends"
RESULTS_PATH = os.path.join(CACHE_DIR, "support_feedback", "results.json")
ANALYSIS_PATH = os.path.join(CACHE_DIR, "analysis.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")

_VALID_CONFIDENCE = {"high", "medium", "low"}
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("WARNING: %s unreadable (%s)" % (path, e), file=sys.stderr)
        return None


def _valid_keys_set(data):
    """Return the set of in-window ticket keys we actually fetched. Any agent
    output referencing keys outside this set is hallucinated → drop."""
    keys = set()
    for t in (data.get("tickets") or []):
        k = t.get("key", "")
        if _KEY_RE.match(k):
            keys.add(k)
    return keys


def _normalise_confidence(value):
    v = (value or "").strip().lower()
    return v if v in _VALID_CONFIDENCE else "medium"


def _filter_keys(raw_keys, valid_keys, label):
    """Keep only keys matching the issue-key regex AND present in the
    fetched-ticket set. Returns the filtered list, dropping silently."""
    out = []
    for k in (raw_keys or []):
        if not isinstance(k, str):
            continue
        k = k.strip()
        if not _KEY_RE.match(k):
            continue
        if k in valid_keys:
            out.append(k)
    if not out and raw_keys:
        print("WARNING: support_feedback %s record dropped — none of %r are valid in-window keys" % (
            label, raw_keys), file=sys.stderr)
    return out


def _validate_charter_drift(rec, valid_keys):
    keys = _filter_keys(rec.get("ticket_keys"), valid_keys, "charter_drift")
    if not keys:
        return None
    return {
        "ticket_keys": keys,
        "current_team": str(rec.get("current_team") or ""),
        "suggested_team": str(rec.get("suggested_team") or "") or None,
        "reason": str(rec.get("reason") or "")[:1000],
        "confidence": _normalise_confidence(rec.get("confidence")),
    }


def _validate_containment(rec, valid_keys):
    keys = _filter_keys(rec.get("ticket_keys"), valid_keys, "l2_containment_signals")
    if not keys:
        return None
    return {
        "ticket_keys": keys,
        "pattern": str(rec.get("pattern") or "")[:500],
        "gap": str(rec.get("gap") or "")[:1000],
        "confidence": _normalise_confidence(rec.get("confidence")),
    }


def _validate_categorisation(rec, valid_keys):
    keys = _filter_keys(rec.get("ticket_keys"), valid_keys, "categorisation_quality")
    if not keys:
        return None
    return {
        "ticket_keys": keys,
        "issue": str(rec.get("issue") or "")[:1000],
        "suggested_category": str(rec.get("suggested_category") or "") or None,
        "confidence": _normalise_confidence(rec.get("confidence")),
    }


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    results = _load_json(RESULTS_PATH)
    if results is None:
        print("WARNING: %s missing — sub-agent likely failed; report will render without support-feedback section." % RESULTS_PATH, file=sys.stderr)
        sys.exit(0)
    if not isinstance(results, dict):
        print("WARNING: %s top level is %s, not a JSON object — sub-agent produced malformed output; report will render without support-feedback section." % (
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

    valid_keys = _valid_keys_set(data)

    charter_drift = []
    for rec in (results.get("charter_drift") or []):
        validated = _validate_charter_drift(rec, valid_keys)
        if validated:
            charter_drift.append(validated)

    containment = []
    for rec in (results.get("l2_containment_signals") or []):
        validated = _validate_containment(rec, valid_keys)
        if validated:
            containment.append(validated)

    categorisation = []
    for rec in (results.get("categorisation_quality") or []):
        validated = _validate_categorisation(rec, valid_keys)
        if validated:
            categorisation.append(validated)

    analysis["support_feedback"] = {
        "charter_drift": charter_drift,
        "l2_containment_signals": containment,
        "categorisation_quality": categorisation,
        "summary": {
            "charter_drift_count": len(charter_drift),
            "l2_containment_signal_count": len(containment),
            "categorisation_quality_count": len(categorisation),
        },
    }

    atomic_write_json(ANALYSIS_PATH, analysis)
    print("Support-feedback merged: %d charter_drift / %d containment / %d categorisation" % (
        len(charter_drift), len(containment), len(categorisation)))


if __name__ == "__main__":
    main()
