#!/usr/bin/env python3
"""root-cause-suggest setup: validates env, parses input source (--keys, --from-file,
or auto-discover defaults), resolves focus team's label / Team-field / vault_dir
slot, computes the auto-discover window, and writes /tmp/root-cause-suggest/setup.json."""

import argparse
import json
import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import load_env, init_auth, ensure_tmp_dir, atomic_write_json


CACHE_DIR = "/tmp/root-cause-suggest"
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-&]{0,63}\Z", re.ASCII)
_ISSUE_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)
_SINCE_RE = re.compile(r"\A\d{1,3}d\Z", re.ASCII)

MAX_SINCE_DAYS = 90
DEFAULT_SINCE_DAYS = 30
MAX_MAX_TICKETS = 200
DEFAULT_MAX_TICKETS = 50


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def parse_charter_teams(env_value):
    """Parse CHARTER_TEAMS into (canonical_names, alias_map). Same shape and rules
    as support-routing-audit/setup.py:parse_charter_teams."""
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
    if not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    return alias_map.get(raw.lower())


def _resolve_focus_team_slot(focus_team, sprint_teams_env, support_label_env, support_field_env):
    """Returns (label, field_value, vault_dir, slot_index, total_slots). slot_index 1-based."""
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


def parse_root_cause_epics(env_value):
    """Parse ROOT_CAUSE_EPICS env into a list of validated keys. Empty / malformed
    entries are dropped with a WARNING."""
    out = []
    if not env_value:
        return out
    for item in env_value.split(","):
        key = item.strip()
        if not key:
            continue
        if not _ISSUE_KEY_RE.match(key):
            print("WARNING: ROOT_CAUSE_EPICS: skipping malformed key %r" % key, file=sys.stderr)
            continue
        out.append(key)
    return out


def _resolve_keys_file(path):
    """Return absolute path to a --from-file argument, or abort. Path must
    resolve under SKILL_DIR or OBSIDIAN_TEAMS_PATH and must not be a symlink
    that escapes those roots."""
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not os.path.exists(path):
        print("ERROR: --from-file path does not exist: %s" % path, file=sys.stderr)
        sys.exit(2)
    if os.path.islink(path):
        print("ERROR: --from-file path is a symlink (refusing): %s" % path, file=sys.stderr)
        sys.exit(2)
    resolved = os.path.realpath(path)
    allowed_roots = [os.path.realpath(SKILL_DIR)]
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if teams_path and os.path.isabs(teams_path):
        allowed_roots.append(os.path.realpath(teams_path))
    if not any(resolved == r or resolved.startswith(r + os.sep) for r in allowed_roots):
        print("ERROR: --from-file %s resolves outside SKILL_DIR / OBSIDIAN_TEAMS_PATH" % path, file=sys.stderr)
        sys.exit(2)
    return path


def _read_keys_file(path):
    keys = []
    with open(path) as f:
        for line in f:
            for token in re.split(r"[,\s]+", line.strip()):
                token = token.strip()
                if not token or token.startswith("#"):
                    continue
                keys.append(token)
    return keys


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--team", default="",
                   help="Focus team. Must match a CHARTER_TEAMS canonical / alias. "
                        "Defaults to the first CHARTER_TEAMS slot.")
    p.add_argument("--keys", default="",
                   help="Comma-separated support ticket keys to evaluate. "
                        "Mutually exclusive with --from-file. If both omitted, "
                        "auto-discover unlinked tickets in the period.")
    p.add_argument("--from-file", default="",
                   help="Path to a file containing one ticket key per line "
                        "(comma/space separated also accepted, '#' lines ignored). "
                        "Path must resolve under the skill dir or OBSIDIAN_TEAMS_PATH.")
    p.add_argument("--since", default="%dd" % DEFAULT_SINCE_DAYS,
                   help="Lookback window for auto-discover, e.g. 30d. Capped at %dd." % MAX_SINCE_DAYS)
    p.add_argument("--max-tickets", type=int, default=DEFAULT_MAX_TICKETS,
                   help="Max tickets to evaluate. Capped at %d." % MAX_MAX_TICKETS)
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

    rc_epics_env = os.environ.get("ROOT_CAUSE_EPICS", "")
    rc_epics = parse_root_cause_epics(rc_epics_env)
    if not rc_epics:
        print("ERROR: ROOT_CAUSE_EPICS is missing or empty.", file=sys.stderr)
        print("       Set it to a comma-separated list of root-cause epic keys, e.g.", file=sys.stderr)
        print("       export ROOT_CAUSE_EPICS=\"PROJ-1234,PROJ-5678\"", file=sys.stderr)
        sys.exit(1)

    charter_teams_env = os.environ.get("CHARTER_TEAMS", "")
    allowed_teams, alias_map = parse_charter_teams(charter_teams_env)
    if not allowed_teams:
        print("ERROR: CHARTER_TEAMS env var is missing or empty.", file=sys.stderr)
        sys.exit(1)

    requested_team = args.team or allowed_teams[0]
    _require_match(_TEAM_NAME_RE, requested_team, "--team")
    focus_team = norm_team(requested_team, alias_map)
    if not focus_team:
        print("ERROR: --team %r is not a recognised charter team. Allowed: %s" % (
            requested_team, ", ".join(allowed_teams)), file=sys.stderr)
        sys.exit(2)

    # --keys / --from-file are mutually exclusive
    if args.keys and args.from_file:
        print("ERROR: --keys and --from-file are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    explicit_keys = []
    if args.keys:
        for raw in args.keys.split(","):
            k = raw.strip()
            if not k:
                continue
            _require_match(_ISSUE_KEY_RE, k, "--keys entry")
            explicit_keys.append(k)
    elif args.from_file:
        keys_path = _resolve_keys_file(args.from_file)
        for k in _read_keys_file(keys_path):
            _require_match(_ISSUE_KEY_RE, k, "--from-file entry")
            explicit_keys.append(k)
        if not explicit_keys:
            print("ERROR: --from-file %s contained no ticket keys" % keys_path, file=sys.stderr)
            sys.exit(2)
    # Deduplicate, preserve order
    seen = set()
    explicit_keys_dedup = []
    for k in explicit_keys:
        if k not in seen:
            seen.add(k)
            explicit_keys_dedup.append(k)
    explicit_keys = explicit_keys_dedup
    mode = "explicit_keys" if explicit_keys else "auto_discover"

    _require_match(_SINCE_RE, args.since, "--since")
    since_days = int(args.since[:-1])
    if since_days < 1 or since_days > MAX_SINCE_DAYS:
        print("ERROR: --since must be 1d..%dd" % MAX_SINCE_DAYS, file=sys.stderr)
        sys.exit(2)

    if args.max_tickets < 1 or args.max_tickets > MAX_MAX_TICKETS:
        print("ERROR: --max-tickets must be 1..%d" % MAX_MAX_TICKETS, file=sys.stderr)
        sys.exit(2)

    sprint_teams = os.environ.get("SPRINT_TEAMS", "")
    support_team_label = os.environ.get("SUPPORT_TEAM_LABEL", "")
    support_team_field = os.environ.get("SUPPORT_TEAM_FIELD_VALUES", "")
    label, field_value, vault_dir, slot_index, total_slots = _resolve_focus_team_slot(
        focus_team, sprint_teams, support_team_label, support_team_field)

    if mode == "auto_discover" and not (label or field_value):
        print("ERROR: auto-discover mode needs SUPPORT_TEAM_LABEL or SUPPORT_TEAM_FIELD_VALUES "
              "for focus team %r (no slot found in SPRINT_TEAMS).\n"
              "       Pass --keys or --from-file to skip the auto-discover JQL." % focus_team,
              file=sys.stderr)
        sys.exit(1)

    if not vault_dir:
        # Explicit-keys mode without a SPRINT_TEAMS slot is allowed; vault_dir
        # falls back to the canonical name so the report has somewhere to land.
        vault_dir = focus_team

    print("=== ENV ===")
    print("JIRA: " + base_url)
    print("AUTH: " + env["JIRA_EMAIL"])
    print("PROJECT: " + support_project_key)
    if slot_index:
        print("FOCUS TEAM: %s  (slot %d of %d in SPRINT_TEAMS, label=%r, team_field=%r, vault_dir=%r)" % (
            focus_team, slot_index, total_slots, label or "(none)", field_value or "(none)", vault_dir))
    else:
        print("FOCUS TEAM: %s  (no SPRINT_TEAMS slot; vault_dir=%r)" % (focus_team, vault_dir))
    print("MODE: %s" % mode)
    if mode == "explicit_keys":
        print("KEYS: %d ticket(s) — %s" % (len(explicit_keys), ", ".join(explicit_keys[:10]) + (" ..." if len(explicit_keys) > 10 else "")))
    else:
        print("WINDOW: last %d days" % since_days)
    print("ROOT_CAUSE_EPICS: %s" % ", ".join(rc_epics))
    print()

    ensure_tmp_dir(CACHE_DIR)
    support_rc_field = os.environ.get("SUPPORT_ROOT_CAUSE_FIELD", "").strip()
    if support_rc_field and not re.match(r"\Acustomfield_\d{4,8}\Z", support_rc_field):
        print("WARNING: SUPPORT_ROOT_CAUSE_FIELD %r does not match `customfield_NNNN` — ignoring." % support_rc_field, file=sys.stderr)
        support_rc_field = ""

    setup_data = {
        "env": {
            "base_url": base_url,
            "email": env["JIRA_EMAIL"],
            "support_project_key": support_project_key,
            "support_root_cause_field": support_rc_field,
        },
        "focus_team": focus_team,
        "vault_dir": vault_dir,
        "focus_label": label or "",
        "focus_team_field_value": field_value or "",
        "allowed_teams": allowed_teams,
        "team_alias_map": alias_map,
        "rc_epics": rc_epics,
        "mode": mode,
        "explicit_keys": explicit_keys,
        "since_days": since_days,
        "max_tickets": args.max_tickets,
    }
    atomic_write_json(os.path.join(CACHE_DIR, "setup.json"), setup_data)
    print("Setup data saved to %s/setup.json" % CACHE_DIR)


if __name__ == "__main__":
    main()
