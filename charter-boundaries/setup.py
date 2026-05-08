#!/usr/bin/env python3
"""Charter-boundaries setup: validates env, resolves charter + examples paths,
selects focus teams (those with a SPRINT_TEAMS slot), and writes setup.json."""

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta

import _libpath  # noqa: F401
from charter_teams import parse_charter_teams  # noqa: F401  imported for setup.json + downstream consumers
from jira_client import load_env, init_auth, ensure_tmp_dir, atomic_write_json


CACHE_DIR = "/tmp/charter-boundaries"
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_WINDOW_RE = re.compile(r"\A(\d{1,3})d\Z", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def _resolve_under_roots(path, allowed_roots, label):
    """Reject symlinks pointing outside the allow-listed roots."""
    if not path or not os.path.isabs(path) or not os.path.exists(path):
        return None
    resolved = os.path.realpath(path)
    if os.path.islink(path) and resolved == path:
        print("ERROR: %s symlink could not be resolved: %s" % (label, path), file=sys.stderr)
        return None
    in_allowed = any(
        resolved == root or resolved.startswith(root + os.sep)
        for root in allowed_roots
    )
    if not in_allowed:
        print("ERROR: %s=%s is outside allowed roots; refusing." % (label, path), file=sys.stderr)
        return None
    return resolved


def _allowed_roots():
    roots = []
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if teams_path and os.path.isabs(teams_path):
        roots.append(os.path.realpath(teams_path))
    roots.append(os.path.realpath(SKILL_DIR))
    return roots


def _resolve_charters(arg_path):
    roots = _allowed_roots()
    if arg_path:
        return _resolve_under_roots(arg_path, roots, "--charters"), "arg"
    env = os.environ.get("CHARTERS_PATH", "").strip()
    if env:
        resolved = _resolve_under_roots(env, roots, "CHARTERS_PATH")
        if resolved:
            return resolved, "env(CHARTERS_PATH)"
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if teams_path:
        candidate = os.path.join(teams_path, ".charters.md")
        if os.path.exists(candidate):
            return os.path.realpath(candidate), "vault"
    scratch = os.path.join(SKILL_DIR, ".scratch", "charters.md")
    if os.path.exists(scratch):
        return os.path.realpath(scratch), "scratch"
    return None, None


def _resolve_examples(arg_path):
    roots = _allowed_roots()
    if arg_path:
        return _resolve_under_roots(arg_path, roots, "--examples"), "arg"
    scratch = os.path.join(SKILL_DIR, ".scratch", "examples.md")
    if os.path.exists(scratch):
        return os.path.realpath(scratch), "scratch"
    return None, None


def _resolve_focus_teams(allowed_teams, alias_map, sprint_teams_env, support_label_env, support_field_env):
    """Walk SPRINT_TEAMS slots and return the focus teams (those whose display_name
    or vault_dir matches a canonical or alias). Each entry includes the slot's
    label, Team-field value, and vault_dir for the per-team audit pipeline."""
    labels = [l.strip() for l in support_label_env.split("|")] if support_label_env else []
    field_values = [v.strip() for v in support_field_env.split("|")] if support_field_env else []
    slots = [t.strip() for t in sprint_teams_env.split(",") if t.strip()]
    out = []
    for idx, t in enumerate(slots):
        parts = t.split("|")
        if len(parts) != 4:
            continue
        vault_dir, _project_key, _board_id, display_name = [p.strip() for p in parts]
        canonical = (
            alias_map.get(display_name.lower())
            or alias_map.get(vault_dir.lower())
        )
        if not canonical:
            continue
        out.append({
            "canonical": canonical,
            "vault_dir": vault_dir,
            "label": labels[idx] if idx < len(labels) else "",
            "field_value": field_values[idx] if idx < len(field_values) else "",
            "slot_index": idx + 1,
            "display_name": display_name,
        })
    return out


def _parse_window(window_arg):
    m = _WINDOW_RE.match(window_arg)
    if not m:
        print("ERROR: --window malformed (expected e.g. 30d): %r" % window_arg, file=sys.stderr)
        sys.exit(2)
    days = int(m.group(1))
    if days < 1 or days > 365:
        print("ERROR: --window must be 1..365 days", file=sys.stderr)
        sys.exit(2)
    today = date.today()
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--charters", default="", help="Override path to charters.md")
    p.add_argument("--examples", default="", help="Override path to examples.md")
    p.add_argument("--window", default="30d", help="Audit window, e.g. 30d (default), 90d")
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
        sys.exit(1)

    sprint_teams = os.environ.get("SPRINT_TEAMS", "")
    support_team_label = os.environ.get("SUPPORT_TEAM_LABEL", "")
    support_team_field = os.environ.get("SUPPORT_TEAM_FIELD_VALUES", "")
    if not (support_team_label or support_team_field):
        print("ERROR: At least one of SUPPORT_TEAM_LABEL / SUPPORT_TEAM_FIELD_VALUES must be set.",
              file=sys.stderr)
        sys.exit(1)

    focus_teams = _resolve_focus_teams(
        allowed_teams, alias_map, sprint_teams, support_team_label, support_team_field)
    if not focus_teams:
        print("ERROR: No SPRINT_TEAMS slot resolved to a CHARTER_TEAMS canonical/alias.",
              file=sys.stderr)
        sys.exit(1)

    charters_path, charters_source = _resolve_charters(args.charters)
    if not charters_path:
        print("ERROR: charters.md not found.", file=sys.stderr)
        print("       Drop it at %s/.scratch/charters.md, or pass --charters." % SKILL_DIR,
              file=sys.stderr)
        sys.exit(1)

    examples_path, examples_source = _resolve_examples(args.examples)
    if not examples_path:
        print("ERROR: examples.md not found.", file=sys.stderr)
        print("       Drop it at %s/.scratch/examples.md, or pass --examples." % SKILL_DIR,
              file=sys.stderr)
        sys.exit(1)

    start, end = _parse_window(args.window)

    obsidian_teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if not obsidian_teams_path:
        print("ERROR: OBSIDIAN_TEAMS_PATH is required for vault output.", file=sys.stderr)
        sys.exit(1)

    print("=== ENV ===")
    print("JIRA: " + base_url)
    print("PROJECT: " + support_project_key)
    print("CHARTERS: %s (%s)" % (charters_path, charters_source))
    print("EXAMPLES: %s (%s)" % (examples_path, examples_source))
    print("WINDOW: %s → %s" % (start, end))
    print("FOCUS TEAMS (%d):" % len(focus_teams))
    for ft in focus_teams:
        print("  - %s  (slot %d, vault_dir=%s, label=%r, field_value=%r)" % (
            ft["canonical"], ft["slot_index"], ft["vault_dir"],
            ft["label"] or "(none)", ft["field_value"] or "(none)"))
    print()

    ensure_tmp_dir(CACHE_DIR)
    setup_data = {
        "env": {
            "base_url": base_url,
            "support_project_key": support_project_key,
            "obsidian_teams_path": obsidian_teams_path,
        },
        "allowed_teams": allowed_teams,
        "team_alias_map": alias_map,
        "focus_teams": focus_teams,
        "charters_path": charters_path,
        "charters_source": charters_source,
        "examples_path": examples_path,
        "examples_source": examples_source,
        "period": {"start": start, "end": end},
    }
    atomic_write_json(os.path.join(CACHE_DIR, "setup.json"), setup_data)
    print("Setup data saved to %s/setup.json" % CACHE_DIR)


if __name__ == "__main__":
    main()
