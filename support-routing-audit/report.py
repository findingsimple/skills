#!/usr/bin/env python3
"""Render the support routing audit as terminal-friendly Markdown."""

import argparse
import contextlib
import datetime
import io
import json
import os
import re
import sys


CACHE_DIR = "/tmp/support-routing-audit"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
AUDIT_PATH = os.path.join(CACHE_DIR, "audit.json")
REPORT_PATH = os.path.join(CACHE_DIR, "report.md")

_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)
_YEAR_RE = re.compile(r"\A\d{4}\Z", re.ASCII)

PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4, "": 5}


def _browse_url(base_url, key):
    return "%s/browse/%s" % (base_url.rstrip("/"), key)


def _md_cell(s):
    """Escape markdown table cells: pipes break columns, backticks create code spans
    that can hide content, and `[`/`]` can form fake links from sub-agent reasoning.
    Whitespace is already flattened upstream in apply.py:_clean."""
    if not s:
        return ""
    return (s
            .replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("`", "\\`")
            .replace("[", "\\[")
            .replace("]", "\\]"))


def _sort_misroutes(rows):
    """Stable sort: priority asc, then created desc within each priority bucket."""
    rows.sort(key=lambda t: t.get("created", ""), reverse=True)
    rows.sort(key=lambda t: PRIORITY_ORDER.get(t.get("priority", ""), 5))
    return rows


def _summarise_path(transitions):
    """Render a transitions list as 'A → B → C', collapsing empty 'from' to
    '(unrouted)' on the first hop only."""
    if not transitions:
        return ""
    nodes = []
    for tr in transitions:
        fr = (tr.get("from") or "").strip()
        to = (tr.get("to") or "").strip()
        if not nodes:
            nodes.append(fr or "(unrouted)")
        if to and nodes[-1] != to:
            nodes.append(to)
    return " → ".join(nodes)


def _render_misroute_table(rows, base_url, focus_team):
    out = []
    out.append("| Ticket | Should be at | Currently at | Why | Priority |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        cur = (r.get("current_team") or "").strip() or "(unrouted)"
        sb = (r.get("should_be_at") or "").strip()
        if cur == focus_team:
            cur_disp = cur  # still sitting at focus — the actionable case
        elif sb and cur == sb:
            cur_disp = "%s (already rerouted ✓)" % cur
        else:
            cur_disp = "%s (rerouted, still wrong)" % cur
        out.append("| [%s](%s) | %s | %s | %s | %s |" % (
            r["key"],
            _browse_url(base_url, r["key"]),
            _md_cell(sb),
            _md_cell(cur_disp),
            _md_cell(r.get("reasoning") or ""),
            _md_cell(r.get("priority") or ""),
        ))
    return "\n".join(out)


def _frontmatter(audit, setup, generated_at):
    summary = audit.get("summary") or {}
    period = audit.get("period") or {}
    vault_dir = setup.get("vault_dir") or ""
    # vault_dir is regex-validated in _resolve_vault_path; the regex blocks all
    # YAML-breaking and wiki-link-breaking characters. focus_team comes from setup.json
    # (validated in setup.py) — if its regex is ever loosened to allow `:` or `"`,
    # this line will need quoting added.
    lines = [
        "---",
        "type: support-routing-audit",
        'team: "[[%s]]"' % vault_dir if vault_dir else 'team: ""',
        "project_key: %s" % setup.get("env", {}).get("support_project_key", ""),
        "focus_team: %s" % audit.get("focus_team", ""),
        "period_start: %s" % period.get("start", ""),
        "period_end: %s" % period.get("end", ""),
        "generated_at: %s" % generated_at,
        "tickets_audited: %d" % audit.get("kept_count", 0),
        "belongs_at_focus: %d" % summary.get("belongs_at_focus", 0),
        "should_be_elsewhere: %d" % summary.get("should_be_elsewhere", 0),
        "split_charter: %d" % summary.get("split_charter", 0),
        "insufficient_evidence: %d" % summary.get("insufficient_evidence", 0),
        "out_of_charter_work: %d" % summary.get("out_of_charter_work", 0),
        "out_of_charter_l2_actionable: %d" % summary.get("out_of_charter_l2_actionable", 0),
        "tags: [support-routing-audit]",
        "---",
        "",
    ]
    return "\n".join(lines)


def _render(audit, setup):
    base_url = setup["env"]["base_url"]
    project_key = setup["env"]["support_project_key"]
    focus_team = audit.get("focus_team", "")
    period = audit.get("period") or {}
    summary = audit.get("summary") or {}
    tickets = audit.get("tickets") or []
    truncated = audit.get("truncated", False)
    candidates = audit.get("candidates_count", 0)
    kept = audit.get("kept_count", len(tickets))
    charters_source = setup.get("charters_source", "")

    charters_label = {
        "vault": "vault (canonical)",
        "env(CHARTERS_PATH)": "env override",
        "scratch": "scratch (local fallback)",
    }.get(charters_source, charters_source or "unknown")

    audited_str = "%d" % kept if kept == candidates else "%d (of %d candidates after filtering)" % (kept, candidates)

    print("# Support Routing Audit — Misrouted to %s — %s → %s" % (
        focus_team, period.get("start", ""), period.get("end", "")))
    print()
    print("**Project:** %s · **Focus team:** %s · **Tickets audited:** %s · **Charters:** %s" % (
        project_key, focus_team, audited_str, charters_label))
    if truncated:
        print()
        print("> ⚠️ Result truncated by `--max-tickets`. Narrow the period to see all in-scope tickets.")
    if summary.get("headline"):
        print()
        print("> " + summary["headline"])
    print()

    # ----- Summary table -----
    print("## Summary")
    print()
    print("| Verdict | Count |")
    print("|---|---|")
    print("| Belongs at %s | %d |" % (focus_team, summary.get("belongs_at_focus", 0)))
    print("| Should be elsewhere | %d |" % summary.get("should_be_elsewhere", 0))
    print("| Split charter | %d |" % summary.get("split_charter", 0))
    print("| Insufficient evidence | %d |" % summary.get("insufficient_evidence", 0))
    print()
    ooc = summary.get("out_of_charter_work", 0)
    if ooc:
        l2_actionable = summary.get("out_of_charter_l2_actionable", 0)
        by_cause = summary.get("out_of_charter_by_cause") or {}
        cause_chunks = []
        for cause in ("l2_misroute", "accepted_by_focus", "redirected_back", "unclear"):
            n = by_cause.get(cause, 0)
            if n:
                cause_chunks.append("%s: %d" % (cause.replace("_", " "), n))
        causes_str = ("; " + ", ".join(cause_chunks)) if cause_chunks else ""
        print("**Out-of-charter work done by %s:** %d ticket%s (%d L2-actionable%s) — see breakdown below." % (
            focus_team, ooc, "" if ooc == 1 else "s", l2_actionable, causes_str))
        print()
    by_target = summary.get("by_target_team") or []
    if by_target:
        chunks = ["%s (%d)" % (b["should_be_at"], b["count"]) for b in by_target]
        print("**Misroutes by target team:** " + ", ".join(chunks))
        print()

    print("> **Confidence legend:** **High** = explicit charter clause + unambiguous content; safe to post as-is.  ")
    print("> **Medium** = strong signal but check the ticket before posting.  **Low** = judgment call; don't post without review.")
    print()

    # ----- Misrouted: high then medium then low. -----
    misrouted = _sort_misroutes([t for t in tickets if t["verdict"] == "should_be_elsewhere"])
    by_conf = {"high": [], "medium": [], "low": []}
    for t in misrouted:
        by_conf.setdefault(t.get("confidence", "low"), []).append(t)

    # ----- Out-of-charter work done by focus team -----
    # Grouped by routing_cause: l2_misroute is the support-actionable bucket;
    # accepted_by_focus / redirected_back / unclear are listed separately so a
    # leadership reader doesn't accuse L2 of misroutes L2 didn't cause.
    out_of_charter = _sort_misroutes([t for t in tickets if t.get("out_of_charter_work")])
    if out_of_charter:
        cause_buckets = {"l2_misroute": [], "accepted_by_focus": [], "redirected_back": [], "unclear": []}
        for t in out_of_charter:
            cause_buckets.setdefault(t.get("routing_cause") or "unclear", []).append(t)

        cause_titles = {
            "l2_misroute": "L2 misroute (support-actionable feedback)",
            "accepted_by_focus": "Accepted by %s (other team declined or punted)" % focus_team,
            "redirected_back": "Internally redirected to %s (lead/manager request)" % focus_team,
            "unclear": "Cause unclear from comments",
        }
        cause_blurbs = {
            "l2_misroute": "L2 named %s in the initial routing, no internal request asked us to take it. **This is the bucket worth raising with support — wasted engineering effort that better routing would have avoided.**" % focus_team,
            "accepted_by_focus": "Another team declined the work or bounced it back without doing engineering. The misroute exists, but the cause is the *other* team refusing scope, not L2 mis-categorising. Raise with the other team's lead, not L2.",
            "redirected_back": "An internal lead, manager, or %s engineer asked %s to investigate even though the charter says elsewhere. Not a routing failure — but worth tracking if the pattern repeats." % (focus_team, focus_team),
            "unclear": "Comments don't clearly show the routing cause. Spot-check before drawing conclusions.",
        }

        print("## Out-of-charter work done by %s" % focus_team)
        print()
        print("> %d ticket%s where the **charter says elsewhere** AND %s engineers actually did substantive work, grouped by *why* %s ended up doing it. The other misroute tables below include tickets that were rerouted before %s spent real effort." % (
            len(out_of_charter), "" if len(out_of_charter) == 1 else "s", focus_team, focus_team, focus_team))
        print()
        for cause in ("l2_misroute", "accepted_by_focus", "redirected_back", "unclear"):
            rows = cause_buckets.get(cause) or []
            if not rows:
                continue
            print("### %s" % cause_titles[cause])
            print()
            print("> " + cause_blurbs[cause])
            print()
            print("| Ticket | Should be at | Charter clause | What %s did | Why this cause | Confidence | Priority |" % focus_team)
            print("|---|---|---|---|---|---|---|")
            for t in rows:
                sb = (t.get("should_be_at") or "").strip() or "(split charter)"
                print("| [%s](%s) | %s | %s | %s | %s | %s | %s |" % (
                    t["key"],
                    _browse_url(base_url, t["key"]),
                    _md_cell(sb),
                    _md_cell(t.get("reasoning") or ""),
                    _md_cell(t.get("focus_team_contribution_reasoning") or ""),
                    _md_cell(t.get("routing_cause_reasoning") or ""),
                    _md_cell(t.get("confidence") or ""),
                    _md_cell(t.get("priority") or ""),
                ))
            print()

    for level in ("high", "medium", "low"):
        rows = by_conf.get(level) or []
        if not rows:
            continue
        print("## Misrouted to %s — %s confidence" % (focus_team, level))
        print()
        print(_render_misroute_table(rows, base_url, focus_team))
        print()

    # ----- Bounced (>= 2 transitions, focus team appears) -----
    focus_values = {(focus_team or "").upper()}
    for v in audit.get("focus_team_field_values") or []:
        focus_values.add(v.upper())

    def _focus_in_path(t):
        for tr in t.get("team_transitions") or []:
            if (tr.get("from") or "").upper() in focus_values:
                return True
            if (tr.get("to") or "").upper() in focus_values:
                return True
        if (t.get("current_team") or "").upper() in focus_values:
            return True
        return False

    bounced = _sort_misroutes([t for t in tickets
                               if t.get("transition_count", 0) >= 2 and _focus_in_path(t)])
    if bounced:
        ownership_label = {
            "single_team": "single team",
            "multi_team": "multi-team",
            "unclear": "unclear",
            "not_applicable": "",
        }
        single_count = sum(1 for t in bounced if t.get("ownership") == "single_team")
        multi_count = sum(1 for t in bounced if t.get("ownership") == "multi_team")
        unclear_count = sum(1 for t in bounced if t.get("ownership") == "unclear")

        print("## Bounced (≥2 team transitions, %s involved)" % focus_team)
        print()
        print("**Ownership breakdown:** %d single-team · %d multi-team · %d unclear  " % (
            single_count, multi_count, unclear_count))
        print("> *Ownership* = whether resolution actually required substantive work from more than one team (based on comments and transitions). Distinct from charter verdict, which is about where the ticket *should* sit.")
        print()
        print("| Ticket | Path | Current | Verdict | Ownership | Why (ownership) | Priority |")
        print("|---|---|---|---|---|---|---|")
        for t in bounced:
            path = _summarise_path(t.get("team_transitions") or [])
            v = t.get("verdict", "")
            sb = t.get("should_be_at") or ""
            verdict_disp = v if not sb else "%s → %s" % (v, sb)
            own = ownership_label.get(t.get("ownership", ""), "")
            print("| [%s](%s) | %s | %s | %s | %s | %s | %s |" % (
                t["key"],
                _browse_url(base_url, t["key"]),
                _md_cell(path),
                _md_cell(t.get("current_team") or "(unrouted)"),
                _md_cell(verdict_disp),
                _md_cell(own),
                _md_cell(t.get("ownership_reasoning") or ""),
                _md_cell(t.get("priority") or ""),
            ))
        print()

    # ----- Insufficient evidence (compact key list) -----
    insufficient = [t["key"] for t in tickets if t["verdict"] == "insufficient_evidence"]
    if insufficient:
        print("## Insufficient evidence")
        print()
        print(", ".join("`%s`" % k for k in insufficient) + " (%d tickets)" % len(insufficient))
        print()

    # ----- Belongs at focus (compact key list) -----
    belongs = [t["key"] for t in tickets if t["verdict"] == "belongs_at_focus"]
    if belongs:
        print("## Belongs at %s (correctly routed)" % focus_team)
        print()
        print(", ".join("`%s`" % k for k in belongs) + " (%d tickets)" % len(belongs))
        print()

    # ----- Next steps -----
    high_count = sum(1 for t in tickets
                     if t["verdict"] == "should_be_elsewhere" and t.get("confidence") == "high")
    ooc_count = summary.get("out_of_charter_work", 0)
    l2_actionable = summary.get("out_of_charter_l2_actionable", 0)
    if high_count or ooc_count:
        print("---")
        print()
        print("**Next steps:**")
        if l2_actionable:
            print("- **Share the *L2 misroute* sub-table with support** — %d ticket%s where L2 routing went wrong and %s did the work anyway. Skip the *Accepted by %s* / *Redirected to %s* rows: those aren't L2 feedback." % (
                l2_actionable, "" if l2_actionable == 1 else "s", focus_team, focus_team, focus_team))
        elif ooc_count:
            print("- Out-of-charter work happened (%d ticket%s) but **none was caused by L2 misrouting** — the cause was the other team declining or an internal redirect. No support feedback to give from this window." % (
                ooc_count, "" if ooc_count == 1 else "s"))
        if high_count:
            print("- Paste the **high-confidence** misroute table into the leadership channel and mention the L2 leads.")
            print("- For medium-confidence rows, spot-check the ticket before posting.")
        print("- For recurring miscategorisation patterns, run `/support-trends --team %s` to see whether they show up as themes." % focus_team)
        print()


def _atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


def _resolve_vault_path(setup, audit):
    """Resolve {OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Routing Audit/{end_year}/Routing Audit {start} to {end}.md.

    Returns (path, error_message). path is None if vault output isn't possible.
    Mirrors support-trends — vault_dir is validated and joined under the teams root."""
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if not teams_path:
        return None, "OBSIDIAN_TEAMS_PATH not set"
    vault_dir = (setup.get("vault_dir") or "").strip()
    if not vault_dir:
        return None, "vault_dir missing in setup.json (re-run setup.py)"
    if not _VAULT_DIR_RE.match(vault_dir):
        return None, "vault_dir %r does not match the safe-path regex" % vault_dir

    period = audit.get("period") or {}
    start = (period.get("start") or "").strip()
    end = (period.get("end") or "").strip()
    if not (start and end):
        return None, "period.start / period.end missing"
    # Defence-in-depth: dates are validated upstream in setup.py, but they're being
    # interpolated into a path here so re-check before joining.
    if not (_DATE_RE.match(start) and _DATE_RE.match(end)):
        return None, "period.start / period.end malformed"
    end_year = end.split("-", 1)[0]
    if not _YEAR_RE.match(end_year):
        return None, "end_year malformed"

    out_dir = os.path.join(teams_path, vault_dir, "Support", "Routing Audit", end_year)
    out_name = "Routing Audit %s to %s.md" % (start, end)
    return os.path.join(out_dir, out_name), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Write only to /tmp/support-routing-audit/report.md; skip the vault.")
    args = parser.parse_args()

    try:
        with open(AUDIT_PATH) as f:
            audit = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run apply.py first." % AUDIT_PATH, file=sys.stderr)
        return 1
    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run setup.py first." % SETUP_PATH, file=sys.stderr)
        return 1

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _render(audit, setup)
    body = buf.getvalue()
    generated_at = datetime.datetime.now().replace(microsecond=0).isoformat()
    markdown = _frontmatter(audit, setup, generated_at) + body

    _atomic_write(REPORT_PATH, markdown)
    print("OK report written to %s (%d bytes)" % (REPORT_PATH, len(markdown)))

    if args.dry_run:
        print("(--dry-run: skipping vault write)")
        return 0

    vault_path, err = _resolve_vault_path(setup, audit)
    if not vault_path:
        print("WARNING: skipping vault write — %s" % err, file=sys.stderr)
        return 0

    # Probe the team-hub page the frontmatter `team: "[[<vault_dir>]]"` link points
    # to. We don't fail on a miss — Obsidian will create an unresolved link and
    # graph view shows it as orphan. But warn so a renamed hub page (e.g. "Team ACE"
    # vs "ACE") gets noticed before every audit creates a stale link.
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    vault_dir = (setup.get("vault_dir") or "").strip()
    if teams_path and vault_dir:
        hub_candidates = [
            os.path.join(teams_path, "%s.md" % vault_dir),
            os.path.join(teams_path, vault_dir, "%s.md" % vault_dir),
        ]
        if not any(os.path.exists(p) for p in hub_candidates):
            print("WARNING: team hub page [[%s]] not found at any of: %s — "
                  "the frontmatter link will be unresolved in Obsidian until "
                  "the hub page exists or vault_dir is renamed to match." % (
                      vault_dir, ", ".join(hub_candidates)),
                  file=sys.stderr)

    os.makedirs(os.path.dirname(vault_path), exist_ok=True)
    _atomic_write(vault_path, markdown)
    print("OK vault file written to %s" % vault_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
