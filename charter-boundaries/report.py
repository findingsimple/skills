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
    individuals = team_record.get("individual_reroutings") or []
    should_own = team_record.get("should_own_examples") or []
    disputes = team_record.get("boundary_disputes") or []
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
    lines.append("> Auto-generated draft. **Owns** and **Boundary rules** are seeded from the existing charter blurb. **Should own (frequently mis-routed away)** comes from curated `examples.md` (inbound drift). **Does not own (common mistakes)** is populated from routing-audit clusters of misrouted tickets (outbound drift, patterns). **Re-routings (one-off cases)** lists individual misroutes for L2 learning — including ones that were already correctly re-routed. **Boundary disputes** lists `split_charter` cases for cross-team conversations. Review and edit each section in place.")
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

    # Should own (frequently mis-routed away)
    lines.append("## Should own (frequently mis-routed away)")
    lines.append("")
    if should_own:
        lines.append("> Tickets that landed at another team but belong to %s. Sourced from your curated `examples.md`. Use these to write boundary rules and update L2 routing guidance." % team)
        lines.append("")
        # Group by from_team — usually the same team for several examples.
        by_from = {}
        for ex in should_own:
            by_from.setdefault(ex["from_team"], []).append(ex)
        for from_team in sorted(by_from):
            examples = by_from[from_team]
            lines.append("### From %s" % from_team)
            lines.append("")
            links = ", ".join(_key_link(ex["ticket_key"], base_url) for ex in examples)
            lines.append("**Tickets:** %s" % links)
            lines.append("")
    else:
        lines.append("_No curated mis-allocation examples target %s in `examples.md` yet. When you re-route a ticket that should have come here, add a line like `Assigned to <X>, but should belong to %s (we rerouted): <URL>` and re-run the skill._" % (team, team))
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

    # Individual re-routings — single-ticket misroutes (no pattern) for L2 learning
    lines.append("## Re-routings — one-off cases for learning")
    lines.append("")
    if individuals:
        lines.append("> Tickets the audit flagged as `should_be_elsewhere` that didn't form a pattern with other tickets. Each is a learning example for L2 — *next time you see this kind of ticket, route it to <team>*. The `currently at` column shows whether the routing was already corrected.")
        lines.append("")
        # Group by should_be_at (the team the ticket should have gone to).
        by_target = {}
        for it in individuals:
            by_target.setdefault(it["should_be_at"], []).append(it)
        for target in sorted(by_target, key=lambda c: -len(by_target[c])):
            entries = by_target[target]
            lines.append("### Should have gone to: %s (%d ticket%s)" % (
                target, len(entries), "" if len(entries) == 1 else "s"))
            lines.append("")
            for it in entries:
                link = _key_link(it["key"], base_url)
                summary = it.get("summary", "")
                reasoning = it.get("reasoning", "")
                priority = it.get("priority", "")
                priority_str = " *(%s)*" % priority if priority else ""
                current = it.get("current_team", "")
                if it.get("re_routed"):
                    status_str = "✅ re-routed to **%s**" % current if current else "✅ re-routed"
                else:
                    status_str = "⚠ still at %s" % team
                lines.append("- %s%s — *%s* — %s — %s" % (
                    link, priority_str, summary, status_str, reasoning))
            lines.append("")
    else:
        lines.append("_No one-off `should_be_elsewhere` cases in this window. (Either no tickets were re-routed away from %s, or all misroutes clustered into patterns above.)_" % team)
        lines.append("")

    # Boundary disputes — split_charter cases needing other-team input
    lines.append("## Boundary disputes")
    lines.append("")
    if disputes:
        lines.append("> Tickets the routing audit flagged as `split_charter` — work that touches both %s and another team's charter. Take this list to the candidate team's standup or PM to clarify ownership; agreed calls belong in the *Edge case registry* below." % team)
        lines.append("")
        # Group by candidate team — one section per team to ask.
        by_candidate = {}
        for d in disputes:
            by_candidate.setdefault(d["candidate_team"], []).append(d)
        # Sort candidate teams by dispute count descending; within each, sort by priority.
        for candidate in sorted(by_candidate, key=lambda c: -len(by_candidate[c])):
            entries = by_candidate[candidate]
            lines.append("### Candidate: %s (%d ticket%s)" % (
                candidate, len(entries), "" if len(entries) == 1 else "s"))
            lines.append("")
            for d in entries:
                link = _key_link(d["key"], base_url)
                summary = d.get("summary", "")
                reasoning = d.get("reasoning", "")
                priority = d.get("priority", "")
                priority_str = " *(%s)*" % priority if priority else ""
                # One ticket per bullet: link — summary — reasoning.
                lines.append("- %s%s — *%s* — %s" % (link, priority_str, summary, reasoning))
            lines.append("")
    else:
        lines.append("_No `split_charter` cases flagged for %s in this window. The audit didn't find tickets ambiguously straddling charters with another team — either the work was clean, or the audit window was short._" % team)
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
