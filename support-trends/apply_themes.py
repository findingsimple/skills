#!/usr/bin/env python3
"""Merge sub-agent theme tags + vocabulary back into analysis.json under
the `themes` key. Also persist the vocabulary to themes_vocabulary.json so
subsequent runs can hint the sub-agent to reuse labels.

Output shape under analysis["themes"]:
  {
    "vocabulary": [
      {"id": "...", "definition": "...", "count_current": N, "count_prior": N,
       "delta_abs": N, "delta_pct": N, "is_new": bool}
    ],
    "current_records": [...],
    "prior_records": [...],
    "by_theme": {
      "theme-id": {
        "current_keys": [...], "prior_keys": [...],
        "current_customers": [...], "ticket_summaries": [{key, summary, customer}]
      }
    }
  }
"""

import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import concurrency
import _libpath  # noqa: F401
from jira_client import atomic_write_json
from analyze import _safe_delta_pct


CACHE_DIR = "/tmp/support_trends"
RESULTS_PATH = os.path.join(CACHE_DIR, "themes", "results.json")
ANALYSIS_PATH = os.path.join(CACHE_DIR, "analysis.json")
DATA_PATH = os.path.join(CACHE_DIR, "data.json")
# Fallback for first-time runs / missing OBSIDIAN_TEAMS_PATH; the canonical
# location is now in the team's vault dir so the hint survives reboots and
# is per-team rather than per-machine. See _vocab_hint_path() below.
#
# Legacy fallback is **team-scoped** to prevent cross-team vocabulary leakage:
# if team A runs without a vault, then team B runs without a vault, B would
# previously have read A's themes_vocabulary.json and tagged its tickets with
# A's vocabulary. Each team now gets its own legacy file. _legacy_vocab_path()
# below returns the team-scoped path; LEGACY_VOCAB_HINT_PATH (no team) is the
# single-team / pre-team-config fallback only.
LEGACY_VOCAB_HINT_PATH = os.path.join(CACHE_DIR, "themes_vocabulary.json")


def _legacy_vocab_path(vault_dir):
    """Team-scoped /tmp fallback. Returns the unscoped path when vault_dir is
    unknown or fails validation — single-team installs see the same path as
    before; multi-team installs see per-team isolation."""
    if vault_dir and _VAULT_DIR_RE.match(vault_dir):
        return os.path.join(CACHE_DIR, "themes_vocabulary_%s.json" % vault_dir)
    return LEGACY_VOCAB_HINT_PATH

VOCAB_HINT_RELATIVE = os.path.join("Support", "Trends", ".themes_vocabulary.json")
# Evict vocabulary entries that haven't been observed in any run for this
# many days. Long enough to span quarterly themes that briefly disappear
# during a quiet month; short enough to retire genuinely-extinct labels.
VOCAB_STALE_AFTER_DAYS = 90
# Cap the persisted vocabulary at this many entries. Prevents the hint file
# from ratcheting forever as themes accumulate (PMS variants, transient
# customer-specific themes, etc) and keeps the bundle the next themes
# sub-agent sees focused on themes that actually recur. Tail entries dropped
# when the cap is exceeded are the lowest by (last_seen, lifetime count).
VOCAB_HINT_CAP = 30

_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)

ALLOWED_THEME_RE = re.compile(r"\A[a-z][a-z0-9\-]{0,63}\Z")
MAX_THEME_DEFINITION = 200
MAX_MICRO_SUMMARY = 200
MAX_CUSTOMER = 80
MAX_KEY_LEN = 30
MAX_THEMES_PER_TICKET = 5
MAX_TOTAL_THEMES = 40


def _clean(s, maxlen):
    if not isinstance(s, str):
        return ""
    return s.strip()[:maxlen]


# `definition` flows downstream into build_synthesis_prompt.py where it lands
# inside a deterministic `observation` string the synthesis sub-agent treats as
# trusted. Strip newlines, control chars, backticks and angle brackets so a
# malicious tagger output (laundering an injection from a customer description)
# cannot inject instructions into the next sub-agent's prompt.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_definition(s, maxlen):
    if not isinstance(s, str):
        return ""
    s = _CONTROL_CHARS_RE.sub(" ", s)
    s = s.replace("`", "'").replace("<", "(").replace(">", ")")
    return " ".join(s.split())[:maxlen]


def _validate_theme_id(t):
    if not isinstance(t, str):
        return None
    t = t.strip().lower()
    if ALLOWED_THEME_RE.match(t):
        return t
    return None


def _validate_record(r):
    key = _clean(r.get("key", ""), MAX_KEY_LEN)
    if not key:
        return None
    themes = []
    for t in (r.get("themes") or [])[:MAX_THEMES_PER_TICKET]:
        v = _validate_theme_id(t)
        if v:
            themes.append(v)
    return {
        "key": key,
        "themes": themes,
        "customer": _clean(r.get("customer", ""), MAX_CUSTOMER) or "(unknown)",
        "micro_summary": _clean(r.get("micro_summary", ""), MAX_MICRO_SUMMARY),
    }


def _resolve_vocab_hint_path():
    """Where to read/write the persistent themes vocabulary hint.

    Returns (canonical_path, legacy_path).

    Canonical location is the team's vault dir so the hint survives reboots
    and is per-team. Legacy is the /tmp fallback for first-time runs / missing
    OBSIDIAN_TEAMS_PATH; team-scoped to prevent cross-team vocabulary leakage.
    """
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    vault_dir = ""
    try:
        with open(DATA_PATH) as f:
            vault_dir = ((json.load(f).get("args") or {}).get("team_vault_dir") or "").strip()
    except (OSError, json.JSONDecodeError):
        pass
    legacy = _legacy_vocab_path(vault_dir)
    if teams_path and vault_dir and _VAULT_DIR_RE.match(vault_dir):
        return os.path.join(teams_path, vault_dir, VOCAB_HINT_RELATIVE), legacy
    return legacy, legacy


def _load_existing_hint(path, legacy_path):
    """Load prior hint entries keyed by id. Tolerates missing file or schema drift.

    `legacy_path` is the team-scoped /tmp fallback; reading it lets the first
    vault-write absorb whatever was previously persisted in /tmp for this team.
    Cross-team data is never read because `legacy_path` is now team-scoped at
    the caller (see `_legacy_vocab_path`)."""
    out = {}
    for candidate in (path, legacy_path):
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            with open(candidate) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for entry in (data.get("themes") or []):
            tid = entry.get("id")
            if not isinstance(tid, str) or tid in out:
                continue
            out[tid] = {
                "id": tid,
                "definition": entry.get("definition", "") or "",
                "last_seen_run": entry.get("last_seen_run") or "",
                "count_total_lifetime": int(entry.get("count_total_lifetime") or 0),
            }
        if out:
            break
    return out


def _merge_and_age_hint(existing, this_run_vocab, today_iso):
    """Merge this run's themes into the persistent hint and evict stale entries.

    `existing` is keyed by id; `this_run_vocab` is the validated final_vocab
    list. Entries seen this run get last_seen_run bumped + lifetime count
    incremented; entries unseen for VOCAB_STALE_AFTER_DAYS are dropped.
    """
    merged = dict(existing)
    for v in this_run_vocab:
        tid = v["id"]
        prior = merged.get(tid) or {}
        merged[tid] = {
            "id": tid,
            "definition": v.get("definition", "") or prior.get("definition", ""),
            "last_seen_run": today_iso,
            "count_total_lifetime": int(prior.get("count_total_lifetime") or 0) + int(v.get("count_current") or 0),
        }
    today = date.fromisoformat(today_iso)
    fresh = {}
    for tid, entry in merged.items():
        try:
            seen = date.fromisoformat(entry.get("last_seen_run") or today_iso)
        except ValueError:
            seen = today
        if (today - seen).days <= VOCAB_STALE_AFTER_DAYS:
            fresh[tid] = entry
    # Sort by last_seen_run desc, then lifetime count desc — the sub-agent
    # sees most-recent-and-most-frequent first when the hint is presented.
    ordered = sorted(
        fresh.values(),
        key=lambda r: (r["last_seen_run"], r["count_total_lifetime"]),
        reverse=True,
    )
    # Cap at VOCAB_HINT_CAP so the file doesn't ratchet forever. Anything
    # not seen in this run *and* below the cap by recency/lifetime drops
    # out. Themes seen in the current run are protected — they're at the
    # top by last_seen_run.
    return ordered[:VOCAB_HINT_CAP]


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    try:
        with open(RESULTS_PATH) as f:
            results = json.load(f)
    except FileNotFoundError:
        # Match apply_support_feedback / apply_synthesise: missing results = WARN
        # + rc=0 so a slow / timed-out themes agent doesn't block the rest of the
        # pipeline. report.py renders "_[unavailable]_" in the Themes section.
        print("WARNING: %s missing — themes sub-agent likely failed; report will render without themes." % RESULTS_PATH, file=sys.stderr)
        sys.exit(0)
    except json.JSONDecodeError as e:
        print("WARNING: %s is not valid JSON (%s) — themes sub-agent produced truncated or malformed output; report will render without themes." % (RESULTS_PATH, e), file=sys.stderr)
        sys.exit(0)
    if not isinstance(results, dict):
        print("WARNING: %s top level is %s, not a JSON object — themes sub-agent produced malformed output; report will render without themes." % (
            RESULTS_PATH, type(results).__name__), file=sys.stderr)
        sys.exit(0)
    try:
        with open(ANALYSIS_PATH) as f:
            analysis = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found." % ANALYSIS_PATH, file=sys.stderr)
        sys.exit(1)

    # Validate vocabulary
    vocab_raw = (results.get("theme_vocabulary") or [])[:MAX_TOTAL_THEMES]
    vocab = []
    seen = set()
    for v in vocab_raw:
        tid = _validate_theme_id(v.get("id"))
        if not tid or tid in seen:
            continue
        seen.add(tid)
        vocab.append({
            "id": tid,
            "definition": _clean_definition(v.get("definition", ""), MAX_THEME_DEFINITION),
            "count_current": int(v.get("count_current") or 0),
            "count_prior": int(v.get("count_prior") or 0),
        })

    cur_records = [r for r in (_validate_record(r) for r in results.get("current_records") or []) if r]
    prior_records = [r for r in (_validate_record(r) for r in results.get("prior_records") or []) if r]

    # Restrict every record's themes to ones that survived vocab validation,
    # so the report doesn't render a theme that has no definition.
    valid_ids = {v["id"] for v in vocab}
    for r in cur_records + prior_records:
        r["themes"] = [t for t in r["themes"] if t in valid_ids]

    # Recompute counts from records (sub-agent counts may not match validated set).
    cur_count_by_theme = defaultdict(int)
    prior_count_by_theme = defaultdict(int)
    for r in cur_records:
        for t in r["themes"]:
            cur_count_by_theme[t] += 1
    for r in prior_records:
        for t in r["themes"]:
            prior_count_by_theme[t] += 1

    # Customers + ticket lists per theme (current window only — prior is just
    # for the count comparison).
    by_theme = {}
    for r in cur_records:
        for t in r["themes"]:
            entry = by_theme.setdefault(t, {
                "current_keys": [],
                "current_customers": [],
                "ticket_summaries": [],
            })
            entry["current_keys"].append(r["key"])
            entry["current_customers"].append(r["customer"])
            entry["ticket_summaries"].append({
                "key": r["key"],
                "customer": r["customer"],
                "micro_summary": r["micro_summary"],
            })
    for theme_data in by_theme.values():
        # Dedupe customers, preserve order.
        seen_c = []
        for c in theme_data["current_customers"]:
            if c not in seen_c:
                seen_c.append(c)
        theme_data["current_customers"] = seen_c

    # Final vocabulary rows with deltas. Drop ghost rows where the sub-agent
    # invented a vocabulary entry with no record references in either window —
    # they render as empty 0/0/0 rows that mislead the reader.
    final_vocab = []
    for v in vocab:
        c = cur_count_by_theme.get(v["id"], 0)
        p = prior_count_by_theme.get(v["id"], 0)
        if c == 0 and p == 0:
            continue
        final_vocab.append({
            "id": v["id"],
            "definition": v["definition"],
            "count_current": c,
            "count_prior": p,
            "delta_abs": c - p,
            "delta_pct": _safe_delta_pct(c, p),
            "is_new": (p == 0 and c > 0),
        })
    final_vocab.sort(key=lambda r: r["count_current"], reverse=True)

    # Persist vocabulary hint for next run. Lives in the team's vault dir
    # (so it survives reboots and is per-team), with last_seen aging so
    # genuinely-extinct themes drop out after VOCAB_STALE_AFTER_DAYS rather
    # than ratcheting forever.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    hint_path, legacy_path = _resolve_vocab_hint_path()
    existing_hint = _load_existing_hint(hint_path, legacy_path)
    aged = _merge_and_age_hint(existing_hint, final_vocab, today_iso)
    try:
        # Vault path needs makedirs; /tmp legacy paths already live in CACHE_DIR
        # which is created by setup. Compare against both legacy variants so the
        # team-scoped legacy file doesn't accidentally trigger makedirs.
        if hint_path != legacy_path and hint_path != LEGACY_VOCAB_HINT_PATH:
            os.makedirs(os.path.dirname(hint_path), exist_ok=True)
        atomic_write_json(hint_path, {
            "schema_version": 2,
            "evicted_after_days": VOCAB_STALE_AFTER_DAYS,
            "cap": VOCAB_HINT_CAP,
            "last_run": today_iso,
            "themes": aged,
        })
        merged_total = len(set(existing_hint.keys()) | {v["id"] for v in final_vocab})
        evicted = max(0, merged_total - len(aged))
        print("Persisted themes vocabulary hint to %s (%d entries; %d dropped: stale >%dd or below top-%d cap)" % (
            hint_path, len(aged), evicted, VOCAB_STALE_AFTER_DAYS, VOCAB_HINT_CAP))
        # Once the canonical vault path is the writer, the legacy /tmp file
        # is a duplicate that builds up confusion (and a tempting wrong
        # source of truth). Quietly remove it. Skip when the canonical path
        # IS the legacy path (single-team installs that haven't moved yet).
        if (hint_path != legacy_path and hint_path != LEGACY_VOCAB_HINT_PATH
                and legacy_path and os.path.exists(legacy_path)):
            try:
                os.unlink(legacy_path)
                print("Pruned legacy /tmp vocabulary hint at %s (canonical now at %s)" % (
                    legacy_path, hint_path), file=sys.stderr)
            except OSError:
                pass
        if (hint_path != LEGACY_VOCAB_HINT_PATH and os.path.exists(LEGACY_VOCAB_HINT_PATH)):
            try:
                os.unlink(LEGACY_VOCAB_HINT_PATH)
                print("Pruned legacy unscoped /tmp vocabulary hint at %s." % LEGACY_VOCAB_HINT_PATH,
                      file=sys.stderr)
            except OSError:
                pass
    except OSError as e:
        print("WARNING: failed to persist themes vocabulary hint at %s: %s" % (hint_path, e), file=sys.stderr)

    # Cross-theme customer aggregate: customers tagged across multiple themes
    # (cross-surface signal vs heavy filer in one theme).
    aggregates = {"customer_theme_counts": []}
    customer_to_themes = defaultdict(set)
    for tid, td in by_theme.items():
        for c in (td or {}).get("current_customers") or []:
            if c and c not in ("(unknown)", "(internal)"):
                customer_to_themes[c].add(tid)
    aggregates["customer_theme_counts"] = sorted(
        ({"customer": c, "theme_count": len(t), "themes": sorted(t)}
         for c, t in customer_to_themes.items()),
        key=lambda r: (r["theme_count"], r["customer"]),
        reverse=True,
    )

    analysis["themes"] = {
        "vocabulary": final_vocab,
        "current_records": cur_records,
        "prior_records": prior_records,
        "by_theme": by_theme,
        "aggregates": aggregates,
    }
    atomic_write_json(ANALYSIS_PATH, analysis)
    print("Merged themes: %d distinct themes across %d current + %d prior tickets" % (
        len(final_vocab), len(cur_records), len(prior_records)))


if __name__ == "__main__":
    main()
