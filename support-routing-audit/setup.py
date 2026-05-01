#!/usr/bin/env python3
"""Support routing audit setup: validates env, resolves focus team to its label
and Team-field slot, resolves charters file, computes default date window."""

import argparse
import calendar
import json
import os
import re
import sys
from datetime import date

from jira_client import load_env, init_auth, ensure_tmp_dir, atomic_write_json


CACHE_DIR = "/tmp/support-routing-audit"
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-&]{0,63}\Z", re.ASCII)
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def parse_charter_teams(env_value):
    """Parse the CHARTER_TEAMS env var into (canonical_names, alias_map).

    Format (pipe-delimited per slot, optional comma-separated aliases):
      "TeamA|TeamB|TeamC"
      "TeamA:alpha,team alpha|TeamB|TeamC:gamma,team-c"

    Returns ([canonical names in declared order], {lowercased_alias_or_canonical: canonical}).
    Invalid names/aliases are dropped with a WARNING."""
    canonicals = []
    aliases = {}
    if not env_value:
        return canonicals, aliases
    for slot in env_value.split("|"):
        slot = slot.strip()
        if not slot:
            continue
        if ":" in slot:
            canonical, alias_csv = slot.split(":", 1)
            canonical = canonical.strip()
            alias_list = [a.strip() for a in alias_csv.split(",") if a.strip()]
        else:
            canonical = slot
            alias_list = []
        if not _TEAM_NAME_RE.match(canonical):
            print("WARNING: CHARTER_TEAMS: skipping invalid canonical name %r" % canonical, file=sys.stderr)
            continue
        canonicals.append(canonical)
        aliases[canonical.lower()] = canonical
        for a in alias_list:
            if not _TEAM_NAME_RE.match(a):
                print("WARNING: CHARTER_TEAMS: skipping invalid alias %r for %s" % (a, canonical), file=sys.stderr)
                continue
            aliases[a.lower()] = canonical
    return canonicals, aliases


def norm_team(s, alias_map):
    """Resolve a free-form team name against the alias map. Returns the canonical
    name on hit, None on miss."""
    if not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    return alias_map.get(raw.lower())


def _resolve_charters_path():
    """Where to read the charters markdown from. Same priority + symlink-safe
    allow-list as support-trends/build_charter_prompt.py:_resolve_charters_path."""
    env = os.environ.get("CHARTERS_PATH", "").strip()
    if env and os.path.isabs(env) and os.path.exists(env):
        resolved = os.path.realpath(env)
        allowed_roots = []
        teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
        if teams_path and os.path.isabs(teams_path):
            allowed_roots.append(os.path.realpath(teams_path))
        allowed_roots.append(os.path.realpath(SKILL_DIR))
        in_allowed = any(
            resolved == root or resolved.startswith(root + os.sep)
            for root in allowed_roots
        )
        if in_allowed:
            return env, "env(CHARTERS_PATH)"
        print("WARNING: CHARTERS_PATH=%s is outside OBSIDIAN_TEAMS_PATH and "
              "SKILL_DIR — refusing to ship its content as TRUSTED. Falling "
              "back to vault / scratch lookup." % env, file=sys.stderr)
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if teams_path:
        candidate = os.path.join(teams_path, ".charters.md")
        if os.path.exists(candidate):
            return candidate, "vault"
    scratch = os.path.join(SKILL_DIR, ".scratch", "charters.md")
    if os.path.exists(scratch):
        return scratch, "scratch"
    return None, None


def _resolve_focus_team_slot(focus_team, sprint_teams_env, support_label_env, support_field_env):
    """Look up the focus team's label, Team-field display value, and vault_dir
    by matching the team name against SPRINT_TEAMS slots. Returns
    (label, field_value, vault_dir, slot_index, total_slots). slot_index is 1-based.

    Either label or field_value may be empty — we fall back to that channel-only
    filter in fetch.py. If both are empty, returns (None, None, None, None, total)
    and the caller aborts."""
    labels = [l.strip() for l in support_label_env.split("|")] if support_label_env else []
    field_values = [v.strip() for v in support_field_env.split("|")] if support_field_env else []

    slots = [t.strip() for t in sprint_teams_env.split(",") if t.strip()]
    total_slots = len(slots)
    target = focus_team.lower()
    for idx, t in enumerate(slots):
        parts = t.split("|")
        if len(parts) != 4:
            continue
        vault_dir, _project_key, _board_id, display_name = [p.strip() for p in parts]
        if target in (display_name.lower(), vault_dir.lower()):
            label = labels[idx] if idx < len(labels) else ""
            field_value = field_values[idx] if idx < len(field_values) else ""
            return label, field_value, vault_dir, idx + 1, total_slots
    return None, None, None, None, total_slots


def _default_window():
    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return (
        date(today.year, today.month, 1).isoformat(),
        date(today.year, today.month, last_day).isoformat(),
    )


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--team", default="",
        help=("Focus team to audit. Must match a canonical name (or alias) "
              "declared in the CHARTER_TEAMS env var. Defaults to the first "
              "team listed in CHARTER_TEAMS if omitted."),
    )
    p.add_argument("--start", default="", help="YYYY-MM-DD inclusive (default: first of current month)")
    p.add_argument("--end", default="", help="YYYY-MM-DD inclusive (default: last of current month)")
    return p.parse_args()


def main():
    args = parse_args()

    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "SUPPORT_PROJECT_KEY"]
    env = load_env(required)
    missing = [v for v in required if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    base_url, _auth = init_auth(env)
    support_project_key = env["SUPPORT_PROJECT_KEY"]
    _require_match(_PROJECT_KEY_RE, support_project_key, "SUPPORT_PROJECT_KEY")

    charter_teams_env = os.environ.get("CHARTER_TEAMS", "")
    allowed_teams, alias_map = parse_charter_teams(charter_teams_env)
    if not allowed_teams:
        print("ERROR: CHARTER_TEAMS env var is missing or empty.", file=sys.stderr)
        print("       Set it to a pipe-delimited list of canonical team names, e.g.", file=sys.stderr)
        print("       export CHARTER_TEAMS=\"TeamA|TeamB|TeamC\"", file=sys.stderr)
        print("       Aliases per slot are optional, separated by colon then commas:", file=sys.stderr)
        print("       export CHARTER_TEAMS=\"TeamA:alpha,team alpha|TeamB|TeamC:gamma\"", file=sys.stderr)
        sys.exit(1)

    requested_team = args.team or allowed_teams[0]
    _require_match(_TEAM_NAME_RE, requested_team, "--team")
    focus_team = norm_team(requested_team, alias_map)
    if not focus_team:
        print("ERROR: --team %r is not a recognised charter team. Allowed: %s" % (
            requested_team, ", ".join(allowed_teams)), file=sys.stderr)
        sys.exit(2)

    if args.start or args.end:
        if not (args.start and args.end):
            print("ERROR: --start and --end must be supplied together", file=sys.stderr)
            sys.exit(2)
        _require_match(_DATE_RE, args.start, "--start")
        _require_match(_DATE_RE, args.end, "--end")
        start, end = args.start, args.end
    else:
        start, end = _default_window()

    sprint_teams = os.environ.get("SPRINT_TEAMS", "")
    support_team_label = os.environ.get("SUPPORT_TEAM_LABEL", "")
    support_team_field = os.environ.get("SUPPORT_TEAM_FIELD_VALUES", "")
    if not (support_team_label or support_team_field):
        print("ERROR: At least one of SUPPORT_TEAM_LABEL / SUPPORT_TEAM_FIELD_VALUES must be set "
              "(otherwise we cannot find tickets that have ever been routed to %s)." % focus_team,
              file=sys.stderr)
        sys.exit(1)

    label, field_value, vault_dir, slot_index, total_slots = _resolve_focus_team_slot(
        focus_team, sprint_teams, support_team_label, support_team_field)
    if not (label or field_value):
        print("ERROR: Could not resolve focus team %r to a SUPPORT_TEAM_LABEL or "
              "SUPPORT_TEAM_FIELD_VALUES slot. Check SPRINT_TEAMS slot order matches "
              "your support env vars." % focus_team, file=sys.stderr)
        sys.exit(1)

    charters_path, charters_source = _resolve_charters_path()
    if not charters_path:
        print("ERROR: No charters file found.", file=sys.stderr)
        print("       Set CHARTERS_PATH (must resolve under OBSIDIAN_TEAMS_PATH or this skill dir),", file=sys.stderr)
        print("       or place at $OBSIDIAN_TEAMS_PATH/.charters.md,", file=sys.stderr)
        print("       or place at %s/.scratch/charters.md" % SKILL_DIR, file=sys.stderr)
        sys.exit(1)

    print("=== ENV ===")
    print("JIRA: " + base_url)
    print("AUTH: " + env["JIRA_EMAIL"])
    print("PROJECT: " + support_project_key)
    print("FOCUS TEAM: %s  (slot %d of %d in SPRINT_TEAMS, label=%r, team_field=%r)" % (
        focus_team, slot_index, total_slots, label or "(none)", field_value or "(none)"))
    print("  ↑ verify the slot above matches the team you intended to audit")
    print("PERIOD: %s → %s" % (start, end))
    print("CHARTERS: %s (%s)" % (charters_path, charters_source))
    print()

    ensure_tmp_dir(CACHE_DIR)
    setup_data = {
        "env": {
            "base_url": base_url,
            "email": env["JIRA_EMAIL"],
            "support_project_key": support_project_key,
        },
        "focus_team": focus_team,
        "vault_dir": vault_dir or "",
        "focus_label": label,
        "focus_team_field_value": field_value,
        "allowed_teams": allowed_teams,
        "team_alias_map": alias_map,
        "period": {"start": start, "end": end},
        "charters_path": charters_path,
        "charters_source": charters_source,
    }
    atomic_write_json(os.path.join(CACHE_DIR, "setup.json"), setup_data)
    print("Setup data saved to %s/setup.json" % CACHE_DIR)


if __name__ == "__main__":
    main()
