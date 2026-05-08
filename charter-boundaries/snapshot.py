#!/usr/bin/env python3
"""Copy /tmp/support-routing-audit/audit.json into
/tmp/charter-boundaries/audits/<team>.json after a per-team audit run, before
the next team's audit overwrites the upstream file."""

import argparse
import json
import os
import re
import shutil
import sys

import _libpath  # noqa: F401
from charter_teams import TEAM_NAME_RE as _TEAM_NAME_RE, slugify_team
from jira_client import ensure_tmp_dir


CACHE_DIR = "/tmp/charter-boundaries"
AUDIT_DIR = os.path.join(CACHE_DIR, "audits")
UPSTREAM_AUDIT = "/tmp/support-routing-audit/audit.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--team", required=True, help="Canonical team name (must match setup.json focus_teams)")
    args = p.parse_args()

    if not _TEAM_NAME_RE.match(args.team):
        print("ERROR: --team malformed: %r" % args.team, file=sys.stderr)
        sys.exit(2)

    setup_path = os.path.join(CACHE_DIR, "setup.json")
    if not os.path.exists(setup_path):
        print("ERROR: %s not found. Run setup.py first." % setup_path, file=sys.stderr)
        sys.exit(1)
    with open(setup_path, "r", encoding="utf-8") as f:
        setup = json.load(f)
    focus_canonicals = {ft["canonical"] for ft in setup.get("focus_teams", [])}
    if args.team not in focus_canonicals:
        print("ERROR: --team %r not in setup focus_teams: %s" % (
            args.team, sorted(focus_canonicals)), file=sys.stderr)
        sys.exit(2)

    if not os.path.exists(UPSTREAM_AUDIT):
        print("ERROR: %s not found — run the support-routing-audit pipeline for %s first." % (
            UPSTREAM_AUDIT, args.team), file=sys.stderr)
        sys.exit(1)
    with open(UPSTREAM_AUDIT, "r", encoding="utf-8") as f:
        audit = json.load(f)
    upstream_team = audit.get("focus_team", "")
    if upstream_team != args.team:
        print("ERROR: upstream audit.json focus_team=%r does not match --team=%r. "
              "Re-run support-routing-audit with --team %s before snapshotting." % (
                  upstream_team, args.team, args.team), file=sys.stderr)
        sys.exit(1)

    ensure_tmp_dir(AUDIT_DIR)
    slug = slugify_team(args.team)
    out_path = os.path.join(AUDIT_DIR, slug + ".json")
    tmp_path = out_path + ".tmp"
    shutil.copyfile(UPSTREAM_AUDIT, tmp_path)
    os.replace(tmp_path, out_path)
    n_tickets = len(audit.get("tickets", []))
    print("Snapshotted %s audit (%d tickets) → %s" % (args.team, n_tickets, out_path))


if __name__ == "__main__":
    main()
