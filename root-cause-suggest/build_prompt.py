#!/usr/bin/env python3
"""Build the sub-agent bundle.

Reads /tmp/root-cause-suggest/data.json. For each kept support ticket,
computes a per-ticket shortlist of root-cause candidates (BM25-lite token
overlap, weighting components and labels above free-text). Writes
/tmp/root-cause-suggest/bundle.json (atomic). Bundle is capped at 256KB —
shortlists shrink before tickets do.

The bundle's `_untrusted` wrapping is already done in fetch.py; this step
just shapes the structure and adds shortlists.
"""

import json
import math
import os
import re
import sys

from jira_client import atomic_write_json


CACHE_DIR = "/tmp/root-cause-suggest"
DATA_PATH = os.path.join(CACHE_DIR, "data.json")
BUNDLE_PATH = os.path.join(CACHE_DIR, "bundle.json")

BUNDLE_BYTE_CAP = 256 * 1024

SHORTLIST_TARGET = 20
SHORTLIST_MIN = 5

WEIGHT_COMPONENT = 3.0
WEIGHT_LABEL = 2.5
WEIGHT_SUMMARY = 1.5
WEIGHT_DESCRIPTION = 0.7
WEIGHT_SUPPORT_RC = 3.0  # L2-authored Root Cause field — direct diagnosis, high signal
WEIGHT_RC_COMPONENT = 3.0
WEIGHT_RC_LABEL = 2.5
WEIGHT_RC_SUMMARY = 1.5
WEIGHT_RC_SNIPPET = 0.7
WEIGHT_RC_ENRICHED = 2.0  # autofilled root-cause analysis — high signal

STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "into", "your",
    "have", "has", "are", "was", "were", "but", "not", "all", "any", "can",
    "cannot", "could", "would", "should", "when", "where", "what", "which",
    "while", "does", "did", "will", "their", "them", "they", "than", "then",
    "issue", "ticket", "user", "users", "error", "page", "via", "also",
    "still", "more", "less", "some", "only",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}")


def _untext(field):
    """Unwrap a {_untrusted, text} field. Wrap was applied in fetch.py."""
    if isinstance(field, dict) and field.get("_untrusted"):
        return field.get("text", "") or ""
    if isinstance(field, str):
        return field
    return ""


def _tokenise(text):
    return [t.lower() for t in _TOKEN_RE.findall(text or "")
            if t.lower() not in STOPWORDS]


def _bag(tokens):
    bag = {}
    for t in tokens:
        bag[t] = bag.get(t, 0) + 1
    return bag


def _build_rc_index(rc_catalog):
    """Returns list of (rc_record, weighted_token_bag). When the RC has an
    enriched analysis (autofilled by /root-cause-triage), its tokens are
    added at WEIGHT_RC_ENRICHED — the highest-signal source we have."""
    index = []
    for rc in rc_catalog:
        bag = {}
        for c in (rc.get("components") or []):
            for tok in _tokenise(c):
                bag[tok] = bag.get(tok, 0) + WEIGHT_RC_COMPONENT
        for l in (rc.get("labels") or []):
            for tok in _tokenise(l):
                bag[tok] = bag.get(tok, 0) + WEIGHT_RC_LABEL
        for tok in _tokenise(_untext(rc.get("summary"))):
            bag[tok] = bag.get(tok, 0) + WEIGHT_RC_SUMMARY
        for tok in _tokenise(_untext(rc.get("description_snippet"))):
            bag[tok] = bag.get(tok, 0) + WEIGHT_RC_SNIPPET
        enriched = rc.get("enriched") or {}
        for field in ("root_cause_analysis", "background_context", "analysis"):
            text = enriched.get(field) or ""
            for tok in _tokenise(text):
                bag[tok] = bag.get(tok, 0) + WEIGHT_RC_ENRICHED
        index.append((rc, bag))
    return index


def _ticket_query_bag(ticket):
    bag = {}
    for c in (ticket.get("components") or []):
        for tok in _tokenise(c):
            bag[tok] = bag.get(tok, 0) + WEIGHT_COMPONENT
    for l in (ticket.get("labels") or []):
        for tok in _tokenise(l):
            bag[tok] = bag.get(tok, 0) + WEIGHT_LABEL
    for tok in _tokenise(_untext(ticket.get("summary"))):
        bag[tok] = bag.get(tok, 0) + WEIGHT_SUMMARY
    for tok in _tokenise(_untext(ticket.get("description"))):
        bag[tok] = bag.get(tok, 0) + WEIGHT_DESCRIPTION
    for tok in _tokenise(_untext(ticket.get("support_root_cause"))):
        bag[tok] = bag.get(tok, 0) + WEIGHT_SUPPORT_RC
    return bag


def _score(query_bag, rc_bag):
    """Cosine-ish on weighted bags. We don't bother with IDF — the catalog is
    small enough (≤500) that simple weighted overlap beats false precision."""
    if not query_bag or not rc_bag:
        return 0.0
    common = 0.0
    for tok, w in query_bag.items():
        if tok in rc_bag:
            common += w * rc_bag[tok]
    if common == 0:
        return 0.0
    qn = math.sqrt(sum(w * w for w in query_bag.values()))
    rn = math.sqrt(sum(w * w for w in rc_bag.values()))
    return common / (qn * rn)


def _shortlist(ticket, rc_index, target):
    query = _ticket_query_bag(ticket)
    if not query:
        return []
    scored = [(_score(query, bag), rc["key"]) for rc, bag in rc_index]
    scored = [s for s in scored if s[0] > 0]
    scored.sort(reverse=True)
    return [k for _, k in scored[:target]]


def _bundle_size(bundle):
    return len(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))


def main():
    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run fetch.py first." % DATA_PATH, file=sys.stderr)
        sys.exit(1)

    rc_catalog = data.get("rc_catalog") or []
    rc_index = _build_rc_index(rc_catalog)
    rc_keys = sorted({rc.get("key") for rc, _ in rc_index if rc.get("key")})

    tickets = data.get("tickets") or []
    enriched_per_ticket = []
    union_shortlist = set()
    for t in tickets:
        shortlist = _shortlist(t, rc_index, SHORTLIST_TARGET)
        union_shortlist.update(shortlist)
        out = dict(t)
        out["shortlist"] = shortlist
        enriched_per_ticket.append(out)

    # Carry the autofilled enriched analysis ONLY for shortlist members. The full
    # catalog (~140 entries × ~1500 chars enriched) would blow the bundle cap.
    # Non-shortlisted entries keep their summary + components + labels for the
    # rare case the agent needs to reach into the full catalog.
    catalog_for_bundle = []
    for rc in rc_catalog:
        rc_copy = dict(rc)
        if rc_copy.get("key") not in union_shortlist:
            rc_copy.pop("enriched", None)
        catalog_for_bundle.append(rc_copy)
    enriched_count_in_bundle = sum(1 for rc in catalog_for_bundle if rc.get("enriched"))

    bundle = {
        "focus_team": data.get("focus_team", ""),
        "mode": data.get("mode", ""),
        "period": data.get("period"),
        "rc_epics": data.get("rc_epics") or [],
        "rc_catalog": catalog_for_bundle,
        "rc_catalog_keys": rc_keys,
        "tickets": enriched_per_ticket,
    }

    # Trim if we blow the cap. Strategy: shrink each ticket's shortlist toward
    # SHORTLIST_MIN before dropping any ticket; only drop the rc_catalog
    # description_snippet as a last resort. Tickets themselves are never
    # dropped — every input must reach the sub-agent.
    size = _bundle_size(bundle)
    if size > BUNDLE_BYTE_CAP:
        # Stage 1: shrink shortlists.
        for length in range(SHORTLIST_TARGET - 1, SHORTLIST_MIN - 1, -1):
            for t in bundle["tickets"]:
                if len(t.get("shortlist", [])) > length:
                    t["shortlist"] = t["shortlist"][:length]
            size = _bundle_size(bundle)
            if size <= BUNDLE_BYTE_CAP:
                break

    if size > BUNDLE_BYTE_CAP:
        # Stage 2: drop enriched analyses for non-shortlist catalog entries
        # (they were already stripped above) — re-trim the kept ones to half-length.
        for rc in bundle["rc_catalog"]:
            enr = rc.get("enriched")
            if enr:
                for k in list(enr.keys()):
                    enr[k] = (enr[k] or "")[:300]
        size = _bundle_size(bundle)
        if size > BUNDLE_BYTE_CAP:
            print("WARNING: bundle still over cap after trimming enriched; dropping enriched entirely", file=sys.stderr)
            for rc in bundle["rc_catalog"]:
                rc.pop("enriched", None)
            size = _bundle_size(bundle)

    if size > BUNDLE_BYTE_CAP:
        # Stage 3: drop rc description snippets.
        for rc in bundle["rc_catalog"]:
            rc["description_snippet"] = {"_untrusted": True, "text": ""}
        size = _bundle_size(bundle)
        print("WARNING: bundle hit byte cap; dropped rc_catalog description snippets", file=sys.stderr)

    if size > BUNDLE_BYTE_CAP:
        print("WARNING: bundle still over cap (%d > %d); sub-agent may struggle." % (
            size, BUNDLE_BYTE_CAP), file=sys.stderr)

    atomic_write_json(BUNDLE_PATH, bundle)
    print("Wrote bundle: %d tickets, %d rc_catalog entries (%d with enriched), %d bytes" % (
        len(bundle["tickets"]), len(bundle["rc_catalog"]),
        sum(1 for rc in bundle["rc_catalog"] if rc.get("enriched")), size))


if __name__ == "__main__":
    main()
