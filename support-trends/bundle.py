#!/usr/bin/env python3
"""Build the single shared bundle consumed by the themes and support-feedback
sub-agents.

v2 design: one bundle, one place ticket records are assembled. The themes
agent uses summary + description_snippet + light metadata to tag tickets;
the support-feedback agent uses the same per-ticket records plus the
findings + resolution_categories + charters context blocks for its three
classes (charter drift, L2 containment, categorisation quality).

Output: /tmp/support_trends/bundle.json
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import concurrency
import _libpath  # noqa: F401
from jira_client import ensure_tmp_dir, atomic_write_json
from ticket_record import ticket_record, untrusted

CACHE_DIR = "/tmp/support_trends"
BUNDLE_PATH = os.path.join(CACHE_DIR, "bundle.json")

# Themes agent uses a shorter description snippet than the support-feedback
# agent (which needs more context to assess charter drift). Bundle ships the
# full ticket_record (1500 chars) — themes agent samples the first 300 itself
# via `description_snippet`. Both reference the same `description` field.
MAX_TICKETS_TOTAL = 200  # safety cap — typical windows are 30–100

# Persistent theme vocabulary. Canonical location is in the team's vault dir
# so it survives reboots and stays per-team. Falls back to the legacy /tmp
# path on first run.
LEGACY_VOCAB_HINT_PATH = os.path.join(CACHE_DIR, "themes_vocabulary.json")
VOCAB_HINT_RELATIVE = os.path.join("Support", "Trends", ".themes_vocabulary.json")
_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)
# CHARTER_TEAMS values land in bundle.json as trusted strings (the
# support-feedback prompt instructs the agent to "prefer naming a
# suggested_team from that list"). Anchor + ASCII-only to keep newlines /
# null bytes / shell metachars out of the agent context.
_CHARTER_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}\Z", re.ASCII)


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("WARNING: %s exists but unreadable (%s)" % (path, e), file=sys.stderr)
        return None


def _resolve_vocab_hint_path(setup):
    """Return the most recent vocabulary file path, or None if neither exists.

    Canonical: {OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/.themes_vocabulary.json
    Legacy fallback: /tmp/support_trends/themes_vocabulary.json
    """
    teams_path = (setup.get("env") or {}).get("teams_path", "")
    teams = setup.get("teams") or []
    if teams_path and teams:
        vault_dir = teams[0].get("vault_dir", "")
        if vault_dir and _VAULT_DIR_RE.match(vault_dir):
            canonical = os.path.join(teams_path, vault_dir, VOCAB_HINT_RELATIVE)
            if os.path.exists(canonical):
                return canonical
    if os.path.exists(LEGACY_VOCAB_HINT_PATH):
        return LEGACY_VOCAB_HINT_PATH
    return None


def _vocab_hint(setup):
    """Returns {"themes": [{"id": ..., "definition": ...}, ...]} or None."""
    path = _resolve_vocab_hint_path(setup)
    if not path:
        return None
    raw = _load_json(path)
    if not raw:
        return None
    themes = []
    for entry in (raw.get("vocabulary") or raw.get("themes") or []):
        tid = entry.get("id") or entry.get("theme_id")
        defn = entry.get("definition") or entry.get("def") or ""
        if tid:
            themes.append({"id": tid, "definition": defn})
    return {"themes": themes} if themes else None


def _charters(setup):
    """Loads CHARTER_TEAMS env (pipe-delimited) into a list. Used by the
    support-feedback agent for charter-drift assessment.

    Format: `team1|team2|team3` — each entry can be `name` or `name,alias1,alias2`.
    Returns [{"canonical": str, "aliases": [str, ...]}, ...].
    """
    raw = os.environ.get("CHARTER_TEAMS", "")
    if not raw:
        return []
    out = []
    for slot in raw.split("|"):
        parts = [p.strip() for p in slot.split(",") if p.strip()]
        if not parts:
            continue
        validated = [p for p in parts if _CHARTER_NAME_RE.match(p)]
        dropped = [p for p in parts if p not in validated]
        if dropped:
            print("WARNING: CHARTER_TEAMS dropped malformed name(s) %r" % dropped, file=sys.stderr)
        if not validated:
            continue
        out.append({"canonical": validated[0], "aliases": validated[1:]})
    return out


def _in_window_tickets(data, start, end):
    """Filter normalised tickets to those created in the [start, end] inclusive
    window. Date strings are 'YYYY-MM-DD'."""
    start_dt = datetime.fromisoformat(start + "T00:00:00").replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end + "T23:59:59").replace(tzinfo=timezone.utc)
    out = []
    for t in (data.get("tickets") or []):
        s = (t.get("created") or "")
        # Lenient parse — Jira already serialises ISO; trim subseconds.
        s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
        m = re.match(r"(.*[+-])(\d{2})(\d{2})$", s)
        if m:
            s = "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
        try:
            cdt = datetime.fromisoformat(s)
        except ValueError:
            continue
        if start_dt <= cdt <= end_dt:
            out.append(t)
    return out[:MAX_TICKETS_TOTAL]


def _window_days(start, end):
    """Inclusive day count between two YYYY-MM-DD strings."""
    return (
        datetime.fromisoformat(end).toordinal()
        - datetime.fromisoformat(start).toordinal()
        + 1
    )


def _build_ticket_records(raw_tickets):
    """Build per-ticket records for the bundle. Themes agent reads
    `description_snippet` (300 chars) — derive it from the already-trimmed
    `description.text`. Single source of truth: the full description lives in
    `rec["description"]`; the snippet is a presentation slice for the themes
    agent. Both are wrapped untrusted."""
    records = []
    for t in raw_tickets:
        rec = ticket_record(t)
        desc_full = (rec.get("description") or {}).get("text", "")
        rec["description_snippet"] = untrusted(desc_full[:300])
        records.append(rec)
    return records


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    setup = _load_json(os.path.join(CACHE_DIR, "setup.json"))
    if setup is None:
        print("ERROR: setup.json missing — run setup.py first.", file=sys.stderr)
        sys.exit(1)
    data = _load_json(os.path.join(CACHE_DIR, "data.json"))
    if data is None:
        print("ERROR: data.json missing — run fetch.py first.", file=sys.stderr)
        sys.exit(1)
    analysis = _load_json(os.path.join(CACHE_DIR, "analysis.json"))
    if analysis is None:
        print("ERROR: analysis.json missing — run analyze.py first.", file=sys.stderr)
        sys.exit(1)

    window = setup.get("window") or {}
    start = window.get("start")
    end = window.get("end")
    if not (start and end):
        print("ERROR: setup.json window missing start/end.", file=sys.stderr)
        sys.exit(2)

    current_tickets = _build_ticket_records(_in_window_tickets(data, start, end))

    # Prior tickets (themes vocabulary continuity). Keep the ticket_record
    # contract identical so the agent doesn't see a different shape per window.
    prior_tickets = []
    prior_window = None
    prior_data = _load_json(os.path.join(CACHE_DIR, "data_prior.json"))
    if prior_data is not None and window.get("prior_start"):
        prior_start = window["prior_start"]
        prior_end = window["prior_end"]
        prior_tickets = _build_ticket_records(
            _in_window_tickets(prior_data, prior_start, prior_end))
        prior_window = {
            "start": prior_start,
            "end": prior_end,
            "days": _window_days(prior_start, prior_end),
        }

    teams = setup.get("teams") or []
    team_meta = teams[0] if teams else {}

    bundle = {
        "team_vault_dir": team_meta.get("vault_dir", ""),
        "team_display_name": team_meta.get("display_name", ""),
        "current_window": {
            "start": start,
            "end": end,
            "days": _window_days(start, end),
        },
        "prior_window": prior_window,
        "current_tickets": current_tickets,
        "prior_tickets": prior_tickets,
        "vocabulary_hint": _vocab_hint(setup),
        # Support-feedback context blocks (themes agent ignores these).
        "findings": (analysis.get("findings") or []),
        "resolution_categories": (analysis.get("current") or {}).get("resolution_categories"),
        "charters": _charters(setup),
        "team_field_canonical": (analysis.get("current") or {}).get("team_field_canonical"),
    }

    ensure_tmp_dir(CACHE_DIR)
    atomic_write_json(BUNDLE_PATH, bundle)
    n_cur = len(current_tickets)
    n_prior = len(prior_tickets)
    n_findings = len(bundle["findings"])
    print("Bundle written: %s (%d current + %d prior tickets, %d findings, %d charters, vocab_hint=%s)" % (
        BUNDLE_PATH, n_cur, n_prior, n_findings,
        len(bundle["charters"]),
        "yes" if bundle["vocabulary_hint"] else "no"))


if __name__ == "__main__":
    main()
