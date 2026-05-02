#!/usr/bin/env python3
"""v2 report renderer — pure renderer over analysis.json + setup.json.

By v2 design, every claim in this output traces back to either:
  - a deterministic finding from analyze.derive_findings(), or
  - a synthesise-agent finding (which itself was selected from the above
    plus theme / support-feedback agent outputs and validated by
    apply_synthesise.py to have evidence_keys + valid audience).

This file emits NO new claims. If a number doesn't exist in the JSON, it
doesn't appear in the report. The four sections render in this order:

  # Findings              — synthesise selection (or fallback to raw findings)
  ## To Support           — sub-section: support_feedback agent output
  # Themes                — per-theme catalog from themes agent
  # Numbers               — direct renders of analysis tables

Window naming for the vault file:
  - month windows:    "Support Trends — {display} — {YYYY-MM}.md"
  - range windows:    "Support Trends — {display} — {start}_to_{end}.md"

Lands in {OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/{end_year}/.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import concurrency

CACHE_DIR = "/tmp/support_trends"
REPORT_TMP_PATH = os.path.join(CACHE_DIR, "report.md")

_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)
_PROJECT_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+\Z", re.ASCII)
_KEY_RE = re.compile(r"\A[A-Z][A-Z0-9_]+-\d+\Z", re.ASCII)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print("WARNING: %s unreadable (%s)" % (path, e), file=sys.stderr)
        return None


def _atomic_write(path, body):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(body)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _key_link(key, base_url):
    """Render `[[KEY]]` if it's a valid Jira key. Wiki-link form keeps Obsidian
    backlinks navigable; the actual Jira URL goes in the Numbers section once
    per ticket if needed."""
    if isinstance(key, str) and _KEY_RE.match(key):
        return "[[%s]]" % key
    return ""


def _key_links(keys, base_url, max_keys=8):
    parts = []
    for k in keys[:max_keys]:
        link = _key_link(k, base_url)
        if link:
            parts.append(link)
    if len(keys) > max_keys:
        parts.append("(+%d more)" % (len(keys) - max_keys))
    return " ".join(parts)


def _escape_table_cell(text):
    """Escape pipes in markdown table cells so cell content doesn't break the
    column layout. Wiki links with aliases use `\\|` already (per repo
    convention) — this catches everything else."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------------------
# Section: Findings + To Support
# ---------------------------------------------------------------------------

def render_findings_section(analysis, base_url):
    """Emit the # Findings section.

    Source priority:
      1. analysis['synthesise']['findings'] — the schema-locked, agent-selected list
      2. fallback: analysis['findings'] grouped by audience_hint=exec

    A `## Context` sub-section sits above the bullets when narrative_notes
    are present — calendar / baseline framing the reader needs *before*
    interpreting the findings. Notes have explicit provenance via the
    `derived_from` field even though we don't render that.
    """
    out = ["# Findings\n"]

    notes = analysis.get("narrative_notes") or []
    if notes:
        out.append("## Context\n")
        for note in notes:
            text = (note.get("text") or "").strip()
            if text:
                out.append("- _%s_\n" % _escape_table_cell(text))
        out.append("")

    syn = (analysis.get("synthesise") or {}).get("findings") or []
    if syn:
        # Exec-audience findings render under Findings; support-audience under
        # To Support. A finding tagged ["exec","support"] appears in both.
        exec_items = [f for f in syn if "exec" in f.get("audience", [])]
        if exec_items:
            for f in exec_items:
                out.append(_render_finding_bullet(f, base_url))
        else:
            out.append("_No exec-audience findings selected this window._\n")
    else:
        # Fallback: render raw deterministic findings with audience_hint=exec.
        raw = [f for f in (analysis.get("findings") or [])
               if f.get("audience_hint") == "exec"]
        if raw:
            out.append("_(Synthesise sub-agent unavailable — rendering raw deterministic findings.)_\n")
            for f in raw:
                out.append(_render_raw_finding_bullet(f, base_url))
        else:
            out.append("_No exec-audience findings this window._\n")

    # ## To Support sub-section
    out.append("\n## To Support\n")
    support_items = []
    if syn:
        support_items = [f for f in syn if "support" in f.get("audience", [])]

    sf = analysis.get("support_feedback") or {}
    cd = sf.get("charter_drift") or []
    cont = sf.get("l2_containment_signals") or []
    cat = sf.get("categorisation_quality") or []

    # Suppress synthesise support-only findings whose evidence_keys are fully
    # contained in the dedicated charter_drift section — otherwise the same two
    # tickets render twice (once as a To Support bullet, once under
    # ### Charter drift candidates) with overlapping prose. A finding tagged
    # ["exec","support"] keeps its exec bullet regardless; we only deduplicate
    # the ["support"]-only restatement.
    if cd and support_items:
        cd_keys = set()
        for rec in cd:
            for k in (rec.get("ticket_keys") or []):
                if isinstance(k, str):
                    cd_keys.add(k)
        kept = []
        for f in support_items:
            audience = f.get("audience") or []
            if audience == ["support"]:
                ev = [k for k in (f.get("evidence_keys") or []) if isinstance(k, str)]
                if ev and all(k in cd_keys for k in ev):
                    continue  # restates charter drift; subsection will carry it
            kept.append(f)
        support_items = kept

    rendered_any = False
    if support_items:
        for f in support_items:
            out.append(_render_finding_bullet(f, base_url))
        rendered_any = True

    if cd:
        out.append("\n### Charter drift candidates\n")
        for rec in cd:
            keys = _key_links(rec.get("ticket_keys", []), base_url)
            suggested = rec.get("suggested_team") or "(team unclear)"
            reason = rec.get("reason") or ""
            confidence = rec.get("confidence") or "medium"
            out.append("- %s — suggested: **%s** _(%s)_\n  → %s\n" % (
                keys, _escape_table_cell(suggested), confidence, _escape_table_cell(reason)))
        rendered_any = True

    if cont:
        out.append("\n### L2 containment signals\n")
        for rec in cont:
            keys = _key_links(rec.get("ticket_keys", []), base_url)
            pattern = rec.get("pattern") or ""
            gap = rec.get("gap") or ""
            confidence = rec.get("confidence") or "medium"
            out.append("- **%s** _(%s)_ — %s\n  → %s\n" % (
                _escape_table_cell(pattern), confidence,
                keys, _escape_table_cell(gap)))
        rendered_any = True

    if cat:
        out.append("\n### Categorisation quality\n")
        for rec in cat:
            keys = _key_links(rec.get("ticket_keys", []), base_url)
            issue = rec.get("issue") or ""
            suggested = rec.get("suggested_category") or ""
            confidence = rec.get("confidence") or "medium"
            line = "- %s _(%s)_ — %s" % (keys, confidence, _escape_table_cell(issue))
            if suggested:
                line += "\n  → suggested: `%s`" % _escape_table_cell(suggested)
            out.append(line + "\n")
        rendered_any = True

    if not rendered_any:
        out.append("_No support-feedback signals this window._\n")

    return "\n".join(out)


def _finding_preamble(f, base_url):
    """Shared shape for both synthesise and raw-fallback finding bullets:
    claim, optional metric, key-link list. Returns (claim, metric_part, keys)."""
    claim = _escape_table_cell(f.get("claim", ""))
    metric = f.get("metric", "")
    metric_part = " (`%s`)" % metric if metric else ""
    keys = _key_links(f.get("evidence_keys", []), base_url)
    return claim, metric_part, keys


def _render_finding_bullet(f, base_url):
    claim, metric_part, keys = _finding_preamble(f, base_url)
    confidence = f.get("confidence", "medium")
    so_what = (f.get("so_what") or "").strip()
    line = "- **%s**%s _(%s)_ — %s" % (claim, metric_part, confidence, keys)
    if so_what:
        line += "\n  → %s" % _escape_table_cell(so_what)
    return line + "\n"


def _render_raw_finding_bullet(f, base_url):
    """Rendering for the synthesise-fallback path. Shows the raw deterministic
    finding without `so_what` (since no agent ran to write one)."""
    claim, metric_part, keys = _finding_preamble(f, base_url)
    severity = f.get("severity", "medium")
    line = "- **%s**%s _(severity %s)_" % (claim, metric_part, severity)
    if keys:
        line += " — %s" % keys
    return line + "\n"


# ---------------------------------------------------------------------------
# Section: Themes
# ---------------------------------------------------------------------------

def render_themes_section(analysis, base_url):
    out = ["# Themes\n"]
    themes = analysis.get("themes") or {}
    vocab = themes.get("vocabulary") or []
    by_theme = themes.get("by_theme") or {}

    if not vocab:
        out.append("_[unavailable — themes sub-agent did not run or produced no themes]_\n")
        return "\n".join(out)

    out.append("| Theme | Current | Prior | Δ | Tickets |")
    out.append("|---|---:|---:|---:|---|")
    for entry in sorted(vocab, key=lambda v: -((v.get("count_current") or 0) + (v.get("count_prior") or 0))):
        tid = entry.get("id", "")
        cur = entry.get("count_current") or 0
        prior = entry.get("count_prior") or 0
        delta = cur - prior
        delta_cell = "+%d" % delta if delta > 0 else (str(delta) if delta < 0 else "0")
        sample = (by_theme.get(tid, {}).get("current_keys") or [])[:5]
        sample_links = " ".join(_key_link(k, base_url) for k in sample if _key_link(k, base_url))
        out.append("| `%s` | %d | %d | %s | %s |" % (
            _escape_table_cell(tid), cur, prior, delta_cell, sample_links))

    family_rows = _theme_family_rollups(vocab)
    if family_rows:
        out.append("")
        out.append("**Theme families** _(rollup of related theme IDs sharing a prefix)_")
        out.append("")
        out.append("| Family | Current | Prior | Δ | Members |")
        out.append("|---|---:|---:|---:|---|")
        for row in family_rows:
            delta = row["current"] - row["prior"]
            delta_cell = "+%d" % delta if delta > 0 else (str(delta) if delta < 0 else "0")
            members = ", ".join("`%s`" % _escape_table_cell(m) for m in row["members"])
            out.append("| `%s-*` | %d | %d | %s | %s |" % (
                _escape_table_cell(row["prefix"]),
                row["current"], row["prior"], delta_cell, members))
    return "\n".join(out) + "\n"


def _theme_family_rollups(vocab):
    """Aggregate vocab entries by their 2-segment prefix (e.g. `pms-sync` from
    `pms-sync-yardi`). A family rolls up when 2+ themes share the prefix AND
    the prefix has at least 3 segments total in at least one member (so we
    don't produce trivial families like `login-access` rolling up just one
    theme). Returns a list of rollup dicts sorted by combined volume.
    """
    buckets = {}
    for entry in vocab:
        tid = entry.get("id") or ""
        parts = tid.split("-")
        if len(parts) < 3:
            continue  # need at least prefix-prefix-leaf to form a family
        prefix = "-".join(parts[:2])
        bucket = buckets.setdefault(prefix, {"prefix": prefix, "current": 0,
                                              "prior": 0, "members": []})
        bucket["current"] += entry.get("count_current") or 0
        bucket["prior"] += entry.get("count_prior") or 0
        bucket["members"].append(tid)
    rollups = [b for b in buckets.values() if len(b["members"]) >= 2]
    rollups.sort(key=lambda b: -(b["current"] + b["prior"]))
    for b in rollups:
        b["members"].sort()
    return rollups


# ---------------------------------------------------------------------------
# Section: Numbers
# ---------------------------------------------------------------------------

def render_numbers_section(analysis, base_url):
    out = ["# Numbers\n"]
    cur = analysis.get("current") or {}

    # ## Volume by bucket
    out.append("## Volume by bucket\n")
    buckets = cur.get("buckets") or []
    if buckets:
        out.append("| Bucket | Created | Resolved | Net | Backlog end |")
        out.append("|---|---:|---:|---:|---:|")
        for b in buckets:
            out.append("| %s | %d | %d | %+d | %d |" % (
                _escape_table_cell(b.get("label", "")),
                b.get("created", 0),
                b.get("resolved", 0),
                b.get("net", 0),
                b.get("backlog_end", 0),
            ))
        out.append("")

    totals = cur.get("totals") or {}
    out.append("**Totals:** created %d, resolved %d, net %+d, backlog %d → %d\n" % (
        totals.get("created_in_window", 0),
        totals.get("resolved_in_window", 0),
        totals.get("net", 0),
        totals.get("backlog_open_at_start", 0),
        totals.get("backlog_at_end", 0),
    ))

    # ## Resolution Category
    cats = cur.get("resolution_categories") or {}
    out.append("\n## Resolution Category breakdown\n")
    rows = cats.get("rows") or []
    resolved_n = cats.get("resolved_in_window_count") or 0
    if rows:
        out.append("_(of %d resolved in window)_\n" % resolved_n)
        out.append("| Category | Count | Share |")
        out.append("|---|---:|---:|")
        for r in rows:
            out.append("| %s | %d | %.0f%% |" % (
                _escape_table_cell(r.get("category", "")),
                r.get("count", 0),
                r.get("pct", 0.0),
            ))
        out.append("")
    else:
        out.append("_No resolved tickets in window._\n")

    # ## L2 / triage signals
    l1 = cur.get("l1_signals") or {}
    if l1:
        out.append("\n## L2 / triage signals\n")
        fa = l1.get("first_assignment") or {}
        reopen = l1.get("reopen") or {}
        qc = l1.get("quick_close") or {}
        ro = l1.get("reassign_out") or {}
        wd = l1.get("wont_do") or {}

        def hours(v):
            return "%.1fh" % v if v is not None else "—"

        def rate_pct(r):
            return "%.0f%%" % (r * 100) if r is not None else "—"

        out.append("| Signal | Value |")
        out.append("|---|---|")
        out.append("| Median time-to-first-engineer (adjusted hours) | %s |"
                   % hours(fa.get("median_adjusted_hours")))
        out.append("| p90 time-to-first-engineer (adjusted hours) | %s |"
                   % hours(fa.get("p90_adjusted_hours")))
        out.append("| Tickets never assigned to an engineer | %d |" % fa.get("never_assigned_count", 0))
        out.append("| Reopen count / rate | %d / %s |" % (reopen.get("count", 0), rate_pct(reopen.get("rate"))))
        out.append("| Quick-close count / rate | %d / %s |" % (qc.get("count", 0), rate_pct(qc.get("rate"))))
        out.append("| Reassigned out of team | %d (24h fast-bounce: %d) |" % (
            ro.get("count", 0), ro.get("fast_bounce_24h_calendar_count", 0)))
        out.append("| Won't Do / Cannot Reproduce / Duplicate | %d |" % wd.get("count", 0))
        out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Top-level render
# ---------------------------------------------------------------------------

def render(setup, analysis):
    env = setup.get("env") or {}
    base_url = env.get("base_url", "")
    teams = setup.get("teams") or []
    if not teams:
        raise ValueError("No teams in setup.json — cannot render report")
    team = teams[0]
    vault_dir = team.get("vault_dir", "")
    display = team.get("display_name", "") or vault_dir

    window = setup.get("window") or {}
    start = window.get("start", "")
    end = window.get("end", "")
    label = window.get("label", "%s_to_%s" % (start, end))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        d_start = datetime.strptime(start, "%Y-%m-%d").date()
        d_end = datetime.strptime(end, "%Y-%m-%d").date()
        days = (d_end - d_start).days + 1
    except (ValueError, TypeError) as e:
        # setup.py validates window dates with anchored regex + datetime
        # round-trip before writing setup.json, so reaching here means the
        # cache file is corrupted. Better to fail loud than render "0 days".
        raise ValueError(
            "report.py: window dates failed to parse (start=%r end=%r): %s. "
            "Re-run setup.py to regenerate setup.json." % (start, end, e))

    body_parts = [
        "---",
        "title: Support Trends — %s — %s" % (display, label),
        'team: "[[%s]]"' % vault_dir,
        "window: %s → %s" % (start, end),
        "created: %s" % today,
        "tags: [support-trends]",
        "---",
        "",
        "# Support Trends — %s — %s" % (display, label),
        "",
        "_Window: %s → %s (%d days). Generated %s._" % (start, end, days, today),
        "",
        render_findings_section(analysis, base_url),
        "",
        render_themes_section(analysis, base_url),
        "",
        render_numbers_section(analysis, base_url),
    ]
    return "\n".join(body_parts) + "\n"


# ---------------------------------------------------------------------------
# Filename + path resolution
# ---------------------------------------------------------------------------

def _vault_filename(display, label):
    safe_display = re.sub(r"[/\\:]", "-", display).strip()
    return "Support Trends — %s — %s.md" % (safe_display, label)


def _vault_path(setup):
    """Resolve the destination path under OBSIDIAN_TEAMS_PATH, or None when
    invalid (caller falls back to /tmp + dry-run print)."""
    env = setup.get("env") or {}
    teams_path = env.get("teams_path", "")
    if not teams_path or not os.path.isdir(teams_path):
        return None
    teams = setup.get("teams") or []
    if not teams:
        return None
    team = teams[0]
    vault_dir = team.get("vault_dir", "")
    if not _VAULT_DIR_RE.match(vault_dir):
        print("WARNING: vault_dir %r failed regex; skipping vault write" % vault_dir, file=sys.stderr)
        return None
    display = team.get("display_name", "") or vault_dir
    window = setup.get("window") or {}
    end = window.get("end", "")
    label = window.get("label", "")
    if not end or not label:
        return None
    end_year = end[:4]
    if not re.match(r"\A\d{4}\Z", end_year):
        return None
    out_dir = os.path.join(teams_path, vault_dir, "Support", "Trends", end_year)
    return os.path.join(out_dir, _vault_filename(display, label))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print to stdout + /tmp; do not write to the vault.")
    args = p.parse_args()

    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)

    setup = _load(os.path.join(CACHE_DIR, "setup.json"))
    if setup is None:
        print("ERROR: setup.json missing — run setup.py first.", file=sys.stderr)
        sys.exit(1)
    analysis = _load(os.path.join(CACHE_DIR, "analysis.json"))
    if analysis is None:
        print("ERROR: analysis.json missing — run analyze.py first.", file=sys.stderr)
        sys.exit(1)

    body = render(setup, analysis)
    _atomic_write(REPORT_TMP_PATH, body)
    print("Wrote %s (%d bytes)" % (REPORT_TMP_PATH, len(body)))

    try:
        if args.dry_run:
            print()
            print("=" * 60)
            print(body)
            print("=" * 60)
            return

        vault_path = _vault_path(setup)
        if vault_path is None:
            print("WARNING: vault path could not be resolved; report not written to vault. Re-run with --dry-run to see output.", file=sys.stderr)
            return

        out_dir = os.path.dirname(vault_path)
        os.makedirs(out_dir, exist_ok=True)
        _atomic_write(vault_path, body)
        print("Wrote vault file: %s" % vault_path)
    finally:
        # Release the pipeline lock on every exit path from the post-render
        # phase, including --dry-run and the vault-path-unresolved early
        # return. Without this finally, a dry-run preview or a missing
        # OBSIDIAN_TEAMS_PATH leaves the lock held until the 4h staleness
        # cutoff, blocking every subsequent pipeline run.
        try:
            concurrency.release()
        except Exception as e:
            print("WARNING: lock release failed (%s) — manual cleanup may be needed." % e, file=sys.stderr)


if __name__ == "__main__":
    main()
