#!/usr/bin/env python3
"""Validate the sub-agent's results.json and merge with data.json into a single
audit.json that report.py reads.

Validation is strict — every interpolated value (existing_root_cause_key,
link_type, decision, skip_reason, confidence) is allow-listed; reasoning
fields are whitespace-flattened and length-capped. Tickets the sub-agent
omitted gain a fallback `insufficient_evidence` row so totals always match.
"""

import json
import os
import re
import sys

from jira_client import atomic_write_json


CACHE_DIR = "/tmp/root-cause-suggest"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")
RESULTS_PATH = os.path.join(CACHE_DIR, "results.json")
AUDIT_PATH = os.path.join(CACHE_DIR, "audit.json")

ALLOWED_DECISIONS = {"link_existing", "propose_new", "skip", "insufficient_evidence"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_LINK_TYPES = {"is caused by", "causes", "relates"}
ALLOWED_SKIP_REASONS = {
    "not_a_root_cause", "data_export", "config_question", "wont_do",
    "user_error", "noise",
}

_ISSUE_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9\-_]{0,63}\Z", re.ASCII)
_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}\Z", re.ASCII)
_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _\-/&.]{0,63}\Z", re.ASCII)

MAX_REASONING = 600
MAX_PROPOSED_TITLE = 120
MAX_PROPOSED_SUMMARY = 500
MAX_PROPOSED_LIST = 10

_WS_RE = re.compile(r"\s+")


def _clean(s, n):
    if not isinstance(s, str):
        return ""
    return _WS_RE.sub(" ", s).strip()[:n]


def _filter_match(pattern, values, name, key):
    out = []
    for v in values or []:
        if not isinstance(v, str):
            continue
        if pattern.match(v):
            out.append(v)
        else:
            print("WARNING: %s on %s: dropping malformed entry %r" % (name, key, v), file=sys.stderr)
    return out[:MAX_PROPOSED_LIST]


def _validate_link_existing(entry, key, rc_catalog_keys):
    rc_key = (entry.get("existing_root_cause_key") or "").strip()
    if not _ISSUE_KEY_RE.match(rc_key):
        print("WARNING: %s link_existing: malformed existing_root_cause_key %r" % (key, rc_key), file=sys.stderr)
        return None
    if rc_key not in rc_catalog_keys:
        print("WARNING: %s link_existing: existing_root_cause_key %s not in catalog" % (key, rc_key), file=sys.stderr)
        return None
    raw_link_type = (entry.get("link_type") or "").strip().lower()
    if raw_link_type not in ALLOWED_LINK_TYPES:
        print("WARNING: %s link_existing: unknown link_type %r → defaulting to 'is caused by'"
              % (key, raw_link_type), file=sys.stderr)
        link_type = "is caused by"
    else:
        link_type = raw_link_type
    return {"existing_root_cause_key": rc_key, "link_type": link_type}


def _validate_propose_new(entry, key):
    slug = (entry.get("proposed_root_cause_id") or "").strip().lower()
    if not _SLUG_RE.match(slug):
        print("WARNING: %s propose_new: malformed proposed_root_cause_id %r" % (key, slug), file=sys.stderr)
        return None
    title = _clean(entry.get("proposed_title", ""), MAX_PROPOSED_TITLE)
    summary = _clean(entry.get("proposed_summary", ""), MAX_PROPOSED_SUMMARY)
    if not (title and summary):
        print("WARNING: %s propose_new: missing title/summary" % key, file=sys.stderr)
        return None
    components = _filter_match(_COMPONENT_RE, entry.get("proposed_components"), "proposed_components", key)
    labels = _filter_match(_LABEL_RE, entry.get("proposed_labels"), "proposed_labels", key)
    return {
        "proposed_root_cause_id": slug,
        "proposed_title": title,
        "proposed_summary": summary,
        "proposed_components": components,
        "proposed_labels": labels,
    }


def main():
    try:
        with open(RESULTS_PATH) as f:
            results = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found — sub-agent did not write results." % RESULTS_PATH, file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print("ERROR: %s is not valid JSON (%s) — sub-agent likely truncated output."
              % (RESULTS_PATH, e), file=sys.stderr)
        return 1

    try:
        with open(DATA_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run fetch.py first." % DATA_PATH, file=sys.stderr)
        return 1

    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        return 1

    rc_catalog = data.get("rc_catalog") or []
    rc_catalog_keys = {rc.get("key") for rc in rc_catalog if rc.get("key")}
    rc_summary_by_key = {rc.get("key"): rc for rc in rc_catalog if rc.get("key")}
    by_key = {t.get("key"): t for t in (data.get("tickets") or []) if t.get("key")}

    rows_out = []
    seen_keys = set()
    dropped_invalid = 0
    dropped_unknown_key = 0
    dropped_duplicate = 0

    for entry in (results.get("tickets") or []):
        key = (entry.get("key") or "").strip()
        if not _ISSUE_KEY_RE.match(key):
            dropped_invalid += 1
            continue
        if key not in by_key:
            dropped_unknown_key += 1
            continue
        if key in seen_keys:
            dropped_duplicate += 1
            continue
        seen_keys.add(key)

        raw_decision = entry.get("decision")
        if raw_decision not in ALLOWED_DECISIONS:
            print("WARNING: %s unknown decision %r → defaulting to insufficient_evidence"
                  % (key, raw_decision), file=sys.stderr)
            decision = "insufficient_evidence"
        else:
            decision = raw_decision

        raw_confidence = entry.get("confidence")
        if raw_confidence not in ALLOWED_CONFIDENCE:
            print("WARNING: %s unknown confidence %r → defaulting to low"
                  % (key, raw_confidence), file=sys.stderr)
            confidence = "low"
        else:
            confidence = raw_confidence

        reasoning = _clean(entry.get("reasoning", ""), MAX_REASONING)

        existing_root_cause_key = None
        link_type = None
        existing_root_cause_summary = ""
        existing_root_cause_status = ""
        proposed = None
        skip_reason = None

        if decision == "link_existing":
            v = _validate_link_existing(entry, key, rc_catalog_keys)
            if not v:
                # Demote to insufficient_evidence
                decision = "insufficient_evidence"
                if not reasoning:
                    reasoning = "(invalid existing_root_cause_key from sub-agent — demoted)"
            else:
                existing_root_cause_key = v["existing_root_cause_key"]
                link_type = v["link_type"]
                rc = rc_summary_by_key.get(existing_root_cause_key) or {}
                rc_sum = rc.get("summary")
                if isinstance(rc_sum, dict):
                    existing_root_cause_summary = rc_sum.get("text", "") or ""
                else:
                    existing_root_cause_summary = rc_sum or ""
                existing_root_cause_status = rc.get("status", "") or ""

        if decision == "propose_new":
            v = _validate_propose_new(entry, key)
            if not v:
                decision = "insufficient_evidence"
                if not reasoning:
                    reasoning = "(invalid propose_new payload from sub-agent — demoted)"
            else:
                proposed = v

        if decision == "skip":
            raw_reason = (entry.get("skip_reason") or "").strip()
            if raw_reason in ALLOWED_SKIP_REASONS:
                skip_reason = raw_reason
            else:
                print("WARNING: %s skip: unknown skip_reason %r → defaulting to not_a_root_cause"
                      % (key, raw_reason), file=sys.stderr)
                skip_reason = "not_a_root_cause"

        d = by_key[key]
        # Unwrap summary for table rendering downstream.
        ticket_summary_obj = d.get("summary")
        if isinstance(ticket_summary_obj, dict):
            ticket_summary = ticket_summary_obj.get("text", "") or ""
        else:
            ticket_summary = ticket_summary_obj or ""
        rows_out.append({
            "key": key,
            "decision": decision,
            "confidence": confidence,
            "reasoning": reasoning,
            "existing_root_cause_key": existing_root_cause_key,
            "existing_root_cause_summary": existing_root_cause_summary,
            "existing_root_cause_status": existing_root_cause_status,
            "link_type": link_type,
            "proposed": proposed,
            "skip_reason": skip_reason,
            "summary": ticket_summary,
            "components": d.get("components") or [],
            "labels": d.get("labels") or [],
            "priority": d.get("priority", "Medium"),
            "status": d.get("status", ""),
            "created": d.get("created", ""),
            "issuetype": d.get("issuetype", ""),
        })

    # Materialise pre-decided rows: tickets where L2 wrote an RC catalog key
    # directly into the support_root_cause field. These bypass the sub-agent.
    pre_decided = data.get("pre_decided") or []
    for d in pre_decided:
        key = d.get("key", "")
        if key in seen_keys:
            continue  # extreme defence: shouldn't happen — pre_decided is disjoint from results
        named_keys = [k for k in (d.get("support_rc_named_keys") or []) if k in rc_catalog_keys]
        if not named_keys:
            continue
        rc_key = named_keys[0]  # 1:1 — first catalog key wins
        rc = rc_summary_by_key.get(rc_key) or {}
        rc_sum = rc.get("summary")
        if isinstance(rc_sum, dict):
            rc_summary_text = rc_sum.get("text", "") or ""
        else:
            rc_summary_text = rc_sum or ""
        rc_status = rc.get("status", "") or ""
        sr_text_obj = d.get("support_root_cause") or {}
        sr_text = sr_text_obj.get("text", "") if isinstance(sr_text_obj, dict) else (sr_text_obj or "")
        extra = ""
        if len(named_keys) > 1:
            extra = " (L2 also named: %s)" % ", ".join(named_keys[1:])
        ticket_summary_obj = d.get("summary")
        ticket_summary = ticket_summary_obj.get("text", "") if isinstance(ticket_summary_obj, dict) else (ticket_summary_obj or "")
        seen_keys.add(key)
        rows_out.append({
            "key": key,
            "decision": "link_existing",
            "confidence": "high",
            "reasoning": _clean(
                "L2 named %s in the support ticket's Root Cause field: \"%s\".%s" % (
                    rc_key, sr_text, extra), MAX_REASONING),
            "existing_root_cause_key": rc_key,
            "existing_root_cause_summary": rc_summary_text,
            "existing_root_cause_status": rc_status,
            "link_type": "is caused by",
            "proposed": None,
            "skip_reason": None,
            "summary": ticket_summary,
            "components": d.get("components") or [],
            "labels": d.get("labels") or [],
            "priority": d.get("priority", "Medium"),
            "status": d.get("status", ""),
            "created": d.get("created", ""),
            "issuetype": d.get("issuetype", ""),
            "source": "support_root_cause_field",
        })

    # Fallback rows for any input ticket the sub-agent dropped.
    for key, d in by_key.items():
        if key in seen_keys:
            continue
        ticket_summary_obj = d.get("summary")
        ticket_summary = ticket_summary_obj.get("text", "") if isinstance(ticket_summary_obj, dict) else (ticket_summary_obj or "")
        rows_out.append({
            "key": key,
            "decision": "insufficient_evidence",
            "confidence": "low",
            "reasoning": "(sub-agent did not return a decision for this ticket)",
            "existing_root_cause_key": None,
            "existing_root_cause_summary": "",
            "existing_root_cause_status": "",
            "link_type": None,
            "proposed": None,
            "skip_reason": None,
            "summary": ticket_summary,
            "components": d.get("components") or [],
            "labels": d.get("labels") or [],
            "priority": d.get("priority", "Medium"),
            "status": d.get("status", ""),
            "created": d.get("created", ""),
            "issuetype": d.get("issuetype", ""),
        })

    # Cross-row dedupe: every ticket key appears at most once across propose_new
    # rows is automatically guaranteed by the seen_keys check above (a ticket
    # has one decision). What we still need to validate is that no two rows
    # share the same proposed_root_cause_id with conflicting titles.
    proposal_titles = {}
    for r in rows_out:
        if r["decision"] != "propose_new" or not r.get("proposed"):
            continue
        pid = r["proposed"]["proposed_root_cause_id"]
        title = r["proposed"]["proposed_title"]
        if pid in proposal_titles and proposal_titles[pid] != title:
            print("WARNING: proposed_root_cause_id %r has conflicting titles: %r vs %r — keeping first"
                  % (pid, proposal_titles[pid], title), file=sys.stderr)
            r["proposed"]["proposed_title"] = proposal_titles[pid]
        else:
            proposal_titles[pid] = title

    # Re-derive summary counts.
    counts = {k: 0 for k in ALLOWED_DECISIONS}
    confidence_counts = {"high": 0, "medium": 0, "low": 0}
    skip_counts = {k: 0 for k in ALLOWED_SKIP_REASONS}
    propose_clusters = {}
    for r in rows_out:
        counts[r["decision"]] += 1
        if r["decision"] == "link_existing":
            confidence_counts[r["confidence"]] = confidence_counts.get(r["confidence"], 0) + 1
        if r["decision"] == "skip" and r.get("skip_reason"):
            skip_counts[r["skip_reason"]] = skip_counts.get(r["skip_reason"], 0) + 1
        if r["decision"] == "propose_new" and r.get("proposed"):
            pid = r["proposed"]["proposed_root_cause_id"]
            propose_clusters.setdefault(pid, []).append(r["key"])

    summary = {
        "focus_team": data.get("focus_team", ""),
        "tickets_total": len(rows_out),
        "link_existing": counts["link_existing"],
        "link_existing_high": confidence_counts.get("high", 0),
        "link_existing_medium": confidence_counts.get("medium", 0),
        "link_existing_low": confidence_counts.get("low", 0),
        "propose_new": counts["propose_new"],
        "propose_new_clusters": len(propose_clusters),
        "skip": counts["skip"],
        "skip_by_reason": skip_counts,
        "insufficient_evidence": counts["insufficient_evidence"],
        "already_linked": len(data.get("already_linked") or []),
        "open_skipped": len(data.get("open_skipped") or []),
        "pre_decided": sum(1 for r in rows_out if r.get("source") == "support_root_cause_field"),
        "pre_decided_out_of_catalog": len(data.get("pre_decided_out_of_catalog") or []),
    }

    audit = {
        "focus_team": data.get("focus_team", ""),
        "vault_dir": data.get("vault_dir") or "",
        "mode": data.get("mode", ""),
        "period": data.get("period"),
        "rc_epics": data.get("rc_epics") or [],
        "rc_catalog_count": data.get("rc_catalog_count", len(rc_catalog)),
        "candidates_count": data.get("candidates_count", 0),
        "kept_count": data.get("kept_count", len(rows_out)),
        "truncated": data.get("truncated", False),
        "tickets": rows_out,
        "already_linked": data.get("already_linked") or [],
        "open_skipped": data.get("open_skipped") or [],
        "pre_decided_out_of_catalog": data.get("pre_decided_out_of_catalog") or [],
        "summary": summary,
    }
    atomic_write_json(AUDIT_PATH, audit)

    if dropped_invalid or dropped_unknown_key or dropped_duplicate:
        print("WARNING: dropped sub-agent entries — invalid_key=%d, unknown_key=%d, duplicate=%d"
              % (dropped_invalid, dropped_unknown_key, dropped_duplicate), file=sys.stderr)
    print("Wrote audit.json: %d tickets — %d link / %d propose / %d skip / %d insufficient (%d clusters)" % (
        len(rows_out), counts["link_existing"], counts["propose_new"],
        counts["skip"], counts["insufficient_evidence"], len(propose_clusters)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
