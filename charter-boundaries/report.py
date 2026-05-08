#!/usr/bin/env python3
"""Render per-team Charter Boundaries draft Markdown into the Obsidian vault.

Reads /tmp/charter-boundaries/draft.json and writes one file per team that has
a SPRINT_TEAMS slot (i.e. a vault_dir):

  {OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Charter Clarification/{end_year}/Charter Boundaries — {team} — {YYYY-MM-DD}.md

External Jira keys render as markdown hyperlinks; team references in
frontmatter use [[wiki links]] (team hubs are vault-resident)."""

import argparse
import json
import os
import re
import sys
from datetime import date

import _libpath  # noqa: F401


CACHE_DIR = "/tmp/charter-boundaries"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
DRAFT_PATH = os.path.join(CACHE_DIR, "draft.json")
REPORT_TMP_PATH = os.path.join(CACHE_DIR, "report.md")

_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _atomic_write(path, body):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)


def _key_link(key, base_url):
    """[KEY](https://.../browse/KEY) — markdown link, NOT a wiki link
    (Jira keys are external, not vault-resident)."""
    if not _KEY_RE.match(key):
        return key
    if base_url:
        return "[%s](%s/browse/%s)" % (key, base_url.rstrip("/"), key)
    return key


def _render_team(team_record, base_url, charters_source, examples_source, period, today):
    """Return Markdown body for one team's draft doc."""
    team = team_record["team"]
    owns = team_record.get("owns_seed") or []
    rules = team_record.get("boundary_rules_seed") or []
    clusters = team_record.get("does_not_own_clusters") or []
    edge_cases = team_record.get("edge_cases_seed") or []

    lines = []
    lines.append("---")
    lines.append("team: \"[[%s]]\"" % team)
    lines.append("period_start: %s" % period.get("start", ""))
    lines.append("period_end: %s" % period.get("end", ""))
    lines.append("generated_at: %s" % today.isoformat())
    if charters_source:
        lines.append("charters_source: %s" % json.dumps(charters_source))
    if examples_source:
        lines.append("examples_source: %s" % json.dumps(examples_source))
    lines.append("tags: [charter, draft]")
    lines.append("---")
    lines.append("")
    lines.append("# Charter Boundaries — %s (DRAFT)" % team)
    lines.append("")
    lines.append("> Auto-generated draft. **Owns** and **Boundary rules** are seeded from the existing charter blurb. **Does not own (common mistakes)** is populated from routing-audit evidence over the period above. Review and edit each section in place.")
    lines.append("")

    # Owns
    lines.append("## Owns")
    lines.append("")
    if owns:
        lines.append("_Seeded from the existing charter — review, refine, anchor on system names where possible._")
        lines.append("")
        for item in owns:
            lines.append("- %s" % item)
    else:
        lines.append("_No items seeded from the charter for %s. Add the team's owned features here._" % team)
    lines.append("")

    # Does not own
    lines.append("## Does not own (common mistakes)")
    lines.append("")
    if clusters:
        lines.append("> Populated from `should_be_elsewhere` verdicts in routing audit, clustered by theme. Each cluster groups tickets that landed at %s but belong to a different team." % team)
        lines.append("")
        for c in clusters:
            title = c.get("title") or c.get("theme_id")
            lines.append("### %s" % title)
            lines.append("")
            if c.get("description"):
                lines.append(c["description"])
                lines.append("")
            if c.get("boundary_rule"):
                lines.append("**Boundary rule:** %s" % c["boundary_rule"])
                lines.append("")
            evidence = c.get("evidence_keys") or []
            if evidence:
                links = ", ".join(_key_link(k, base_url) for k in evidence)
                lines.append("**Evidence:** %s" % links)
                lines.append("")
            anchored = c.get("anchored_by_curated") or []
            if anchored:
                links = ", ".join(_key_link(k, base_url) for k in anchored)
                lines.append("**Anchored by curated example:** %s" % links)
                lines.append("")
    else:
        lines.append("_No misroute clusters for %s in this window. This means either no out-of-charter tickets landed here, or the audit window was too short to see patterns._" % team)
        lines.append("")

    # Boundary rules
    lines.append("## Boundary rules")
    lines.append("")
    if rules:
        lines.append("_Seeded from any if/then language already in the existing charter. Add more as the team agrees them._")
        lines.append("")
        for r in rules:
            lines.append("- %s" % r)
    else:
        lines.append("_No explicit boundary rules in the existing charter. Add if/then rules here as the team agrees them — especially for the cases hinted at by the clusters above._")
    lines.append("")

    # Edge case registry
    lines.append("## Edge case registry")
    lines.append("")
    lines.append("_When a ticket triggers a debate, record the agreed call here with the date so the next ambiguous ticket has a precedent._")
    lines.append("")
    lines.append("| Date | Question | Decision | Decided by |")
    lines.append("|------|----------|----------|------------|")
    if edge_cases:
        for ec in edge_cases:
            lines.append("| _TBD_ | %s | _%s_ | _TBD_ |" % (
                ec["question"], ec.get("current_understanding", "")))
    else:
        lines.append("| _YYYY-MM-DD_ | _…_ | _…_ | _…_ |")
    lines.append("")

    return "\n".join(lines) + "\n"


def _vault_path(teams_path, vault_dir, team, end_date):
    if not _VAULT_DIR_RE.match(vault_dir):
        print("WARNING: vault_dir %r fails regex; skipping vault write" % vault_dir, file=sys.stderr)
        return None
    end_year = end_date[:4]
    if not re.match(r"\A\d{4}\Z", end_year):
        return None
    safe_team = re.sub(r"[^\w &\-]", "_", team)
    filename = "Charter Boundaries — %s — %s.md" % (safe_team, end_date)
    return os.path.join(teams_path, vault_dir, "Support", "Charter Clarification", end_year, filename)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Skip vault write; print to stdout + /tmp.")
    p.add_argument("--from-cache", action="store_true",
                   help="Render from existing draft.json without re-running the pipeline.")
    args = p.parse_args()

    setup = _load(SETUP_PATH)
    draft = _load(DRAFT_PATH)
    if not setup or not draft:
        print("ERROR: setup.json or draft.json missing.", file=sys.stderr)
        sys.exit(1)

    if args.from_cache:
        print("Rendering from cache (%s, %s)…" % (SETUP_PATH, DRAFT_PATH))

    base_url = (setup.get("env") or {}).get("base_url", "")
    teams_path = (setup.get("env") or {}).get("obsidian_teams_path", "")
    period = setup.get("period", {})
    end_date = period.get("end") or date.today().isoformat()
    today = date.today()

    # Build a vault_dir lookup from setup.focus_teams (only those teams get vault writes).
    focus_by_canonical = {ft["canonical"]: ft for ft in setup.get("focus_teams", [])}

    combined_lines = []
    written = []
    skipped = []
    for tr in draft.get("teams", []):
        team = tr["team"]
        if team not in focus_by_canonical:
            skipped.append(team)
            continue
        vault_dir = focus_by_canonical[team]["vault_dir"]
        body = _render_team(
            tr, base_url,
            draft.get("charters_source", ""),
            draft.get("examples_source", ""),
            period, today,
        )
        combined_lines.append("\n\n" + ("=" * 60) + "\n%s\n" % team + ("=" * 60) + "\n" + body)
        if args.dry_run or not teams_path:
            print("\n" + ("=" * 60))
            print("DRY-RUN: %s" % team)
            print("=" * 60)
            print(body)
            continue
        out_path = _vault_path(teams_path, vault_dir, team, end_date)
        if not out_path:
            skipped.append(team)
            continue
        _atomic_write(out_path, body)
        written.append(out_path)

    _atomic_write(REPORT_TMP_PATH, "\n".join(combined_lines) or "(no team drafts rendered)\n")

    if written:
        print("\n=== VAULT WRITES ===")
        for p in written:
            print("  %s" % p)
    if skipped:
        print("\n=== SKIPPED (no SPRINT_TEAMS slot or invalid vault_dir) ===")
        for t in skipped:
            print("  %s" % t)
    print("\nReport tmp copy: %s" % REPORT_TMP_PATH)


if __name__ == "__main__":
    main()
