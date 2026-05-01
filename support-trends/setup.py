#!/usr/bin/env python3
"""Support trends v2 setup: validates env, parses team config, resolves the
window arg (default = previous calendar month), and writes setup.json that the
rest of the pipeline reads."""

import argparse
import calendar
import json
import os
import re
import sys
from datetime import date

import concurrency
from jira_client import load_env, init_auth, jira_get, ensure_tmp_dir, atomic_write_json


CACHE_DIR = "/tmp/support_trends"

_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_NUMERIC_ID_RE = re.compile(r"\A\d+\Z", re.ASCII)
_TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-]{0,63}\Z", re.ASCII)
_WINDOW_MONTH_RE = re.compile(r"\A\d{4}-\d{2}\Z", re.ASCII)
_WINDOW_RANGE_RE = re.compile(r"\A(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})\Z", re.ASCII)


def _require_match(pattern, value, name):
    if not pattern.match(value or ""):
        print("ERROR: %s is malformed: %r" % (name, value), file=sys.stderr)
        sys.exit(2)


def _previous_month(today=None):
    """Return (year, month) for the calendar month before `today` (default: today())."""
    today = today or date.today()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def _month_bounds(year, month):
    """Return (start, end, label) for a calendar month."""
    last_day = calendar.monthrange(year, month)[1]
    start = "%04d-%02d-01" % (year, month)
    end = "%04d-%02d-%02d" % (year, month, last_day)
    label = "%04d-%02d" % (year, month)
    return start, end, label


def _resolve_window(value):
    """Resolve `--window` arg to (start, end, label, kind).

    Accepts:
      - None / "" / "month" → previous calendar month
      - "YYYY-MM"           → that calendar month
      - "YYYY-MM-DD..YYYY-MM-DD" → explicit range (label = "{start}_to_{end}")
    """
    if not value or value == "month":
        y, m = _previous_month()
        s, e, label = _month_bounds(y, m)
        return s, e, label, "month"

    if _WINDOW_MONTH_RE.match(value):
        y, m = int(value[:4]), int(value[5:7])
        if not (1 <= m <= 12):
            print("ERROR: --window month component out of range: %r" % value, file=sys.stderr)
            sys.exit(2)
        s, e, label = _month_bounds(y, m)
        return s, e, label, "month"

    m_range = _WINDOW_RANGE_RE.match(value)
    if m_range:
        start, end = m_range.group(1), m_range.group(2)
        # Sanity: parseable + start <= end.
        try:
            from datetime import datetime
            d_start = datetime.strptime(start, "%Y-%m-%d").date()
            d_end = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError:
            print("ERROR: --window contains invalid date: %r" % value, file=sys.stderr)
            sys.exit(2)
        if d_start > d_end:
            print("ERROR: --window start (%s) is after end (%s)" % (start, end), file=sys.stderr)
            sys.exit(2)
        return start, end, "%s_to_%s" % (start, end), "range"

    print("ERROR: --window must be 'month', 'YYYY-MM', or 'YYYY-MM-DD..YYYY-MM-DD'; got %r" % value, file=sys.stderr)
    sys.exit(2)


def _resolve_prior_window(start, end, kind):
    """Return (prior_start, prior_end) for the immediately preceding window of
    the same shape, or (None, None) if we can't sensibly derive one."""
    from datetime import datetime, timedelta
    d_start = datetime.strptime(start, "%Y-%m-%d").date()
    d_end = datetime.strptime(end, "%Y-%m-%d").date()
    if kind == "month":
        py, pm = (d_start.year - 1, 12) if d_start.month == 1 else (d_start.year, d_start.month - 1)
        ps, pe, _ = _month_bounds(py, pm)
        return ps, pe
    # Range: prior window of equal length, ending the day before start.
    span = (d_end - d_start).days  # inclusive count = span + 1
    prior_end_d = d_start - timedelta(days=1)
    prior_start_d = prior_end_d - timedelta(days=span)
    return prior_start_d.isoformat(), prior_end_d.isoformat()


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--team", default="", help="Match a vault_dir or display_name from SPRINT_TEAMS; if omitted, all configured teams are written and the orchestrator picks one.")
    p.add_argument("--window", default="", help="month (default: previous calendar month) | YYYY-MM | YYYY-MM-DD..YYYY-MM-DD")
    p.add_argument("--no-prior", action="store_true", help="Skip prior-window resolution.")
    return p.parse_args()


def main():
    args = parse_args()
    if args.team:
        _require_match(_TEAM_NAME_RE, args.team, "--team")

    required = [
        "OBSIDIAN_TEAMS_PATH",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "SPRINT_TEAMS",
        "SUPPORT_PROJECT_KEY",
    ]
    env = load_env(required)
    missing = [v for v in required if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    # Acquire the pipeline lock as the very first thing once env is valid.
    # Subsequent steps (fetch, analyze, bundle, apply_*, report) all run as
    # separate Python processes against the shared /tmp/support_trends/ cache;
    # the lock prevents two pipelines from clobbering each other's intermediate
    # state. report.py releases on success; otherwise the 4h staleness cutoff
    # in concurrency.py reclaims a crashed run.
    acquired, lock_msg = concurrency.acquire(None)
    if not acquired:
        print("ERROR: " + lock_msg, file=sys.stderr)
        sys.exit(1)
    print(lock_msg, file=sys.stderr)

    base_url, auth = init_auth(env)
    teams_path = env["OBSIDIAN_TEAMS_PATH"]

    support_project_key = env["SUPPORT_PROJECT_KEY"]
    _require_match(_PROJECT_KEY_RE, support_project_key, "SUPPORT_PROJECT_KEY")
    support_board_id = os.environ.get("SUPPORT_BOARD_ID", "")
    if support_board_id:
        _require_match(_NUMERIC_ID_RE, support_board_id, "SUPPORT_BOARD_ID")
    support_team_label = os.environ.get("SUPPORT_TEAM_LABEL", "")
    support_team_field = os.environ.get("SUPPORT_TEAM_FIELD_VALUES", "")

    if not (support_team_label or support_team_field):
        print("ERROR: At least one of SUPPORT_TEAM_LABEL / SUPPORT_TEAM_FIELD_VALUES must be set", file=sys.stderr)
        print("       (otherwise we'd query the entire support project across all teams)", file=sys.stderr)
        sys.exit(1)

    # Window resolution.
    start, end, window_label, window_kind = _resolve_window(args.window)
    prior_start, prior_end = (None, None)
    if not args.no_prior:
        prior_start, prior_end = _resolve_prior_window(start, end, window_kind)

    print("=== ENV ===")
    print("TEAMS: " + teams_path)
    print("JIRA: " + base_url)
    print("AUTH: " + env["JIRA_EMAIL"])
    print("SUPPORT: project=%s board=%s (labels: %s) (team field: %s)" % (
        support_project_key, support_board_id or "none", support_team_label or "none", support_team_field or "none"))
    print("WINDOW: %s → %s  (%s, %s)" % (start, end, window_label, window_kind))
    if prior_start:
        print("PRIOR: %s → %s" % (prior_start, prior_end))
    print()

    # Parse teams: SPRINT_TEAMS is "vault_dir|project_key|board_id|display_name" (comma-separated).
    # SUPPORT_TEAM_LABEL / SUPPORT_TEAM_FIELD_VALUES are pipe-delimited per-team-position;
    # each slot can have comma-separated values internally (OR logic).
    support_labels = [l.strip() for l in support_team_label.split("|")] if support_team_label else []
    support_field_values = [v.strip() for v in support_team_field.split("|")] if support_team_field else []

    teams = []
    for idx, t in enumerate(env["SPRINT_TEAMS"].split(",")):
        parts = t.strip().split("|")
        if len(parts) != 4:
            continue
        vault_dir, project_key, board_id, display_name = parts
        team_path = os.path.join(teams_path, vault_dir)
        team = {
            "vault_dir": vault_dir,
            "project_key": project_key,
            "display_name": display_name,
            "path_exists": os.path.isdir(team_path),
            "support_label": "",
            "support_team_field": "",
        }
        if idx < len(support_labels) and support_labels[idx]:
            team["support_label"] = support_labels[idx]
        if idx < len(support_field_values) and support_field_values[idx]:
            team["support_team_field"] = support_field_values[idx]
        if team["support_label"] or team["support_team_field"]:
            teams.append(team)

    if not teams:
        print("ERROR: No teams in SPRINT_TEAMS have a corresponding SUPPORT_TEAM_LABEL or SUPPORT_TEAM_FIELD_VALUES entry", file=sys.stderr)
        sys.exit(1)

    # Filter to a single team if --team specified.
    if args.team:
        wanted = args.team.lower()
        matched = [t for t in teams if t["vault_dir"].lower() == wanted or t["display_name"].lower() == wanted]
        if not matched:
            print("ERROR: --team %r matched no configured team. Available: %s" % (
                args.team, ", ".join("%s (%s)" % (t["vault_dir"], t["display_name"]) for t in teams)),
                file=sys.stderr)
            sys.exit(2)
        if len(matched) > 1:
            print("ERROR: --team %r matched multiple teams: %s" % (
                args.team, ", ".join(t["vault_dir"] for t in matched)), file=sys.stderr)
            sys.exit(2)
        teams = matched

    print("=== TEAMS ===")
    for t in teams:
        status = "OK" if t["path_exists"] else "MISSING"
        bits = []
        if t["support_label"]:
            bits.append("labels=%s" % t["support_label"])
        if t["support_team_field"]:
            bits.append("team_field=%s" % t["support_team_field"])
        print("%s|%s|%s [%s]  %s" % (
            t["vault_dir"], t["project_key"], t["display_name"], status, "  ".join(bits)))
    print()

    # Fetch support board column config (used by analyze.py to know which statuses are closed).
    support_board_config = []
    if support_board_id:
        try:
            path = "/rest/agile/1.0/board/%s/configuration" % support_board_id
            config = jira_get(base_url, path, auth)
            for col in config.get("columnConfig", {}).get("columns", []):
                statuses = [s.get("id", "") for s in col.get("statuses", [])]
                support_board_config.append({
                    "name": col.get("name", ""),
                    "statuses": statuses,
                })
            col_names = [c["name"] for c in support_board_config]
            print("=== SUPPORT BOARD COLUMNS ===")
            print(" → ".join(col_names))
            print()
        except Exception as e:
            print("WARNING: Could not fetch support board config (board %s): %s" % (support_board_id, e), file=sys.stderr)
    else:
        print("INFO: SUPPORT_BOARD_ID not set; reopen and quick-close detection will fall back to resolution presence.", file=sys.stderr)

    ensure_tmp_dir(CACHE_DIR)
    setup_data = {
        "env": {
            "teams_path": teams_path,
            "base_url": base_url,
            "email": env["JIRA_EMAIL"],
            "support_project_key": support_project_key,
            "support_board_id": support_board_id,
        },
        "window": {
            "start": start,
            "end": end,
            "label": window_label,
            "kind": window_kind,
            "prior_start": prior_start,
            "prior_end": prior_end,
        },
        "teams": teams,
        "support_board_config": support_board_config,
    }
    atomic_write_json(os.path.join(CACHE_DIR, "setup.json"), setup_data)
    print("Setup data saved to %s/setup.json" % CACHE_DIR)


if __name__ == "__main__":
    main()
