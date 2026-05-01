#!/usr/bin/env python3
"""Render the root-cause link suggestions as Markdown and write to /tmp + vault.

Output sections:
  Header → Summary → Link existing (high/med/low) → Proposed new clusters →
  Skip → Insufficient evidence → Already linked → Next steps.
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import re
import sys


CACHE_DIR = "/tmp/root-cause-suggest"
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
AUDIT_PATH = os.path.join(CACHE_DIR, "audit.json")
REPORT_PATH = os.path.join(CACHE_DIR, "report.md")

_VAULT_DIR_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)

PRIORITY_ORDER = {"Highest": 0, "High": 1, "Medium": 2, "Low": 3, "Lowest": 4, "": 5}

CLOSED_RC_STATUSES = frozenset({"done", "closed", "resolved", "released", "deployed"})


def _is_closed_status(status):
    return (status or "").strip().lower() in CLOSED_RC_STATUSES

SKIP_REASON_LABEL = {
    "not_a_root_cause": "Not a root cause (one-off)",
    "data_export": "Data export / SOW",
    "config_question": "Config / how-to question",
    "wont_do": "Won't do",
    "user_error": "User error",
    "noise": "Noise / spam / test",
}


def _browse_url(base_url, key):
    return "%s/browse/%s" % (base_url.rstrip("/"), key)


def _md_cell(s):
    """Escape markdown table cells: pipes break columns, backticks create code
    spans, [/] form fake links, backslashes need escaping. Whitespace already
    flattened upstream in apply.py:_clean."""
    if not s:
        return ""
    return (s
            .replace("\\", "\\\\")
            .replace("|", "\\|")
            .replace("`", "\\`")
            .replace("[", "\\[")
            .replace("]", "\\]"))


def _sort_rows(rows):
    rows.sort(key=lambda r: r.get("created", ""), reverse=True)
    rows.sort(key=lambda r: PRIORITY_ORDER.get(r.get("priority", ""), 5))
    return rows


def _atomic_write(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(content)
    os.replace(tmp, path)


def _resolve_vault_path(audit):
    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    if not teams_path:
        return None, "OBSIDIAN_TEAMS_PATH not set"
    vault_dir = (audit.get("vault_dir") or "").strip()
    if not vault_dir:
        return None, "vault_dir missing in audit.json"
    if not _VAULT_DIR_RE.match(vault_dir):
        return None, "vault_dir %r does not match safe-path regex" % vault_dir

    mode = audit.get("mode") or "auto_discover"
    today = datetime.date.today().isoformat()
    end_year = today.split("-")[0]
    period = audit.get("period") or {}
    if mode == "auto_discover" and period:
        since_days = period.get("since_days")
        if not (isinstance(since_days, int) and 1 <= since_days <= 90):
            return None, "period.since_days malformed: %r" % since_days
        end = datetime.date.today()
        start = end - datetime.timedelta(days=since_days)
        end_year = end.strftime("%Y")
        out_name = "Root Cause Suggestions %s to %s.md" % (start.isoformat(), end.isoformat())
    else:
        out_name = "Root Cause Suggestions %s (manual).md" % today

    out_dir = os.path.join(teams_path, vault_dir, "Support", "Root Cause Links", end_year)
    return os.path.join(out_dir, out_name), None


def _frontmatter(audit, generated_at, period_start, period_end):
    summary = audit.get("summary") or {}
    vault_dir = audit.get("vault_dir") or ""
    lines = [
        "---",
        "type: support-root-cause-suggest",
        'team: "[[%s]]"' % vault_dir if vault_dir else 'team: ""',
        "focus_team: %s" % audit.get("focus_team", ""),
        "mode: %s" % audit.get("mode", ""),
    ]
    if period_start and period_end:
        lines.append("period_start: %s" % period_start)
        lines.append("period_end: %s" % period_end)
    lines.extend([
        "generated_at: %s" % generated_at,
        "tickets_total: %d" % summary.get("tickets_total", 0),
        "link_existing: %d" % summary.get("link_existing", 0),
        "link_existing_high: %d" % summary.get("link_existing_high", 0),
        "link_existing_medium: %d" % summary.get("link_existing_medium", 0),
        "link_existing_low: %d" % summary.get("link_existing_low", 0),
        "propose_new: %d" % summary.get("propose_new", 0),
        "propose_new_clusters: %d" % summary.get("propose_new_clusters", 0),
        "skip: %d" % summary.get("skip", 0),
        "insufficient_evidence: %d" % summary.get("insufficient_evidence", 0),
        "already_linked: %d" % summary.get("already_linked", 0),
        "open_skipped: %d" % summary.get("open_skipped", 0),
        "pre_decided: %d" % summary.get("pre_decided", 0),
        "pre_decided_out_of_catalog: %d" % summary.get("pre_decided_out_of_catalog", 0),
        "tags: [support-root-cause-suggest]",
        "---",
        "",
    ])
    return "\n".join(lines)


def _render(audit, setup, period_start, period_end):
    base_url = setup["env"]["base_url"]
    project_key = setup["env"]["support_project_key"]
    focus_team = audit.get("focus_team", "")
    summary = audit.get("summary") or {}
    tickets = audit.get("tickets") or []
    already_linked = audit.get("already_linked") or []
    rc_catalog_count = audit.get("rc_catalog_count", 0)
    candidates_count = audit.get("candidates_count", 0)
    kept_count = audit.get("kept_count", len(tickets))
    truncated = audit.get("truncated", False)
    mode = audit.get("mode", "")

    if mode == "auto_discover" and period_start and period_end:
        period_str = "%s → %s" % (period_start, period_end)
    else:
        period_str = "manual run (%d explicit ticket(s))" % kept_count

    print("# Root Cause Link Suggestions — %s — %s" % (focus_team, period_str))
    print()
    audited_str = "%d" % kept_count
    if candidates_count and candidates_count != kept_count:
        audited_str = "%d kept (of %d candidates after already-linked filter)" % (kept_count, candidates_count)
    print("**Project:** %s · **Focus team:** %s · **Tickets evaluated:** %s · **RC catalog:** %d entries" % (
        project_key, focus_team, audited_str, rc_catalog_count))
    if truncated:
        print()
        print("> ⚠️ Result truncated by `--max-tickets`. Narrow the period or run with `--keys`.")
    print()

    # ----- Summary -----
    print("## Summary")
    print()
    print("| Decision | Count |")
    print("|---|---|")
    print("| Link to existing root cause | %d |" % summary.get("link_existing", 0))
    print("| Propose new root cause | %d (in %d cluster%s) |" % (
        summary.get("propose_new", 0),
        summary.get("propose_new_clusters", 0),
        "" if summary.get("propose_new_clusters", 0) == 1 else "s"))
    print("| Skip (not a root cause) | %d |" % summary.get("skip", 0))
    print("| Insufficient evidence | %d |" % summary.get("insufficient_evidence", 0))
    print()
    if summary.get("pre_decided"):
        n = summary.get("pre_decided", 0)
        print("**Pre-decided links from L2 Root Cause field:** %d ticket%s — L2 named the catalog RC key directly; rendered at the top of the report (apply 1:1, no review needed)." % (
            n, "" if n == 1 else "s"))
        print()
    if summary.get("pre_decided_out_of_catalog"):
        n = summary.get("pre_decided_out_of_catalog", 0)
        print("**L2 nominated an out-of-catalog RC:** %d ticket%s — L2 typed a Jira key not under the configured `ROOT_CAUSE_EPICS`. Listed in a dedicated section below." % (
            n, "" if n == 1 else "s"))
        print()
    if summary.get("already_linked"):
        print("**Already linked (skipped during fetch):** %d ticket%s." % (
            summary.get("already_linked", 0),
            "" if summary.get("already_linked", 0) == 1 else "s"))
        print()
    if summary.get("open_skipped"):
        n = summary.get("open_skipped", 0)
        print("**Still-open tickets (skipped during fetch):** %d ticket%s — only closed/resolved tickets are evaluated; engineers may still link a root cause themselves." % (
            n, "" if n == 1 else "s"))
        print()
    le_high = summary.get("link_existing_high", 0)
    le_med = summary.get("link_existing_medium", 0)
    le_low = summary.get("link_existing_low", 0)
    if le_high or le_med or le_low:
        print("**Link confidence breakdown:** high %d · medium %d · low %d" % (le_high, le_med, le_low))
        print()
    skip_by = summary.get("skip_by_reason") or {}
    skip_chunks = ["%s: %d" % (SKIP_REASON_LABEL.get(k, k), v) for k, v in skip_by.items() if v]
    if skip_chunks:
        print("**Skip reasons:** " + ", ".join(skip_chunks))
        print()

    print("> **Confidence legend:** **High** = symptom + affected surface both unambiguous; safe to link.  ")
    print("> **Medium** = strong directional signal but check the ticket before linking.  **Low** = judgement call; review carefully.")
    print()

    # ----- Pre-decided links (L2 named the RC key directly) -----
    pre_decided_rows = [t for t in tickets if t.get("source") == "support_root_cause_field"]
    if pre_decided_rows:
        pre_decided_rows = _sort_rows(pre_decided_rows)
        print("## Pre-decided links — L2 named the root cause directly")
        print()
        print("> L2 typed the catalog RC key into the support ticket's Root Cause field. These are 1:1 — apply the link without further review.")
        print()
        any_closed = any(_is_closed_status(r.get("existing_root_cause_status")) for r in pre_decided_rows)
        if any_closed:
            print("> ⚠ One or more named RCs are **closed** — fix already shipped. The ticket may need re-classification rather than a direct link; spot-check before applying.")
            print()
        print("| Ticket | Existing root cause | Link type | L2 Root Cause field | Priority |")
        print("|---|---|---|---|---|")
        for r in pre_decided_rows:
            rc_key = r.get("existing_root_cause_key") or ""
            rc_summary = r.get("existing_root_cause_summary") or ""
            rc_status = r.get("existing_root_cause_status") or ""
            rc_cell = "[%s](%s)" % (rc_key, _browse_url(base_url, rc_key))
            if _is_closed_status(rc_status):
                rc_cell = "%s ⚠ closed (%s)" % (rc_cell, _md_cell(rc_status))
            if rc_summary:
                rc_cell = "%s — %s" % (rc_cell, _md_cell(rc_summary))
            print("| [%s](%s) — %s | %s | %s | %s | %s |" % (
                r["key"],
                _browse_url(base_url, r["key"]),
                _md_cell(r.get("summary") or ""),
                rc_cell,
                _md_cell(r.get("link_type") or ""),
                _md_cell(r.get("reasoning") or ""),
                _md_cell(r.get("priority") or ""),
            ))
        print()

    # ----- Link existing, by confidence (model-suggested only) -----
    link_rows = [t for t in tickets
                 if t.get("decision") == "link_existing"
                 and t.get("source") != "support_root_cause_field"]
    by_conf = {"high": [], "medium": [], "low": []}
    for t in link_rows:
        by_conf.setdefault(t.get("confidence", "low"), []).append(t)
    for level in ("high", "medium", "low"):
        rows = _sort_rows(by_conf.get(level) or [])
        if not rows:
            continue
        print("## Link to existing root cause — %s confidence" % level)
        print()
        print("| Ticket | Existing root cause | Link type | Why | Priority |")
        print("|---|---|---|---|---|")
        any_closed = any(_is_closed_status(r.get("existing_root_cause_status")) for r in rows)
        if any_closed:
            print("> ⚠ One or more matches point to a **closed** root-cause ticket — the engineering fix already shipped. Treat these as a signal that the recurring symptom may need a follow-up RC (regression / incomplete fix / unrelated configuration cause). Review before linking.")
            print()
        for r in rows:
            rc_key = r.get("existing_root_cause_key") or ""
            rc_summary = r.get("existing_root_cause_summary") or ""
            rc_status = r.get("existing_root_cause_status") or ""
            rc_cell = "[%s](%s)" % (rc_key, _browse_url(base_url, rc_key))
            if _is_closed_status(rc_status):
                rc_cell = "%s ⚠ closed (%s)" % (rc_cell, _md_cell(rc_status))
            if rc_summary:
                rc_cell = "%s — %s" % (rc_cell, _md_cell(rc_summary))
            print("| [%s](%s) — %s | %s | %s | %s | %s |" % (
                r["key"],
                _browse_url(base_url, r["key"]),
                _md_cell(r.get("summary") or ""),
                rc_cell,
                _md_cell(r.get("link_type") or ""),
                _md_cell(r.get("reasoning") or ""),
                _md_cell(r.get("priority") or ""),
            ))
        print()

    # ----- Propose new — grouped by proposed_root_cause_id -----
    propose_rows = [t for t in tickets if t.get("decision") == "propose_new" and t.get("proposed")]
    if propose_rows:
        clusters = {}
        for r in propose_rows:
            pid = r["proposed"]["proposed_root_cause_id"]
            clusters.setdefault(pid, {
                "title": r["proposed"]["proposed_title"],
                "summary": r["proposed"]["proposed_summary"],
                "components": r["proposed"]["proposed_components"],
                "labels": r["proposed"]["proposed_labels"],
                "rows": [],
                "max_priority": 5,
            })
            cluster = clusters[pid]
            cluster["rows"].append(r)
            cluster["max_priority"] = min(
                cluster["max_priority"],
                PRIORITY_ORDER.get(r.get("priority", ""), 5),
            )
        sorted_clusters = sorted(clusters.items(), key=lambda kv: (kv[1]["max_priority"], -len(kv[1]["rows"])))

        print("## Proposed new root causes")
        print()
        print("> %d ticket%s did not match any catalog entry. The sub-agent grouped them into %d proposal%s — review each block, decide whether to file a new root-cause ticket, then bulk-link the source tickets to it." % (
            len(propose_rows), "" if len(propose_rows) == 1 else "s",
            len(clusters), "" if len(clusters) == 1 else "s"))
        print()
        for pid, cluster in sorted_clusters:
            cluster["rows"] = _sort_rows(cluster["rows"])
            print("### %s" % _md_cell(cluster["title"]))
            print()
            print("**Slug:** `%s`" % pid)
            print()
            print("**Summary:** %s" % cluster["summary"])
            print()
            if cluster.get("components"):
                print("**Suggested components:** " + ", ".join("`%s`" % c for c in cluster["components"]))
                print()
            if cluster.get("labels"):
                print("**Suggested labels:** " + ", ".join("`%s`" % l for l in cluster["labels"]))
                print()
            print("**Source tickets (%d):**" % len(cluster["rows"]))
            print()
            print("| Ticket | Summary | Confidence | Why | Priority |")
            print("|---|---|---|---|---|")
            for r in cluster["rows"]:
                print("| [%s](%s) | %s | %s | %s | %s |" % (
                    r["key"],
                    _browse_url(base_url, r["key"]),
                    _md_cell(r.get("summary") or ""),
                    _md_cell(r.get("confidence") or ""),
                    _md_cell(r.get("reasoning") or ""),
                    _md_cell(r.get("priority") or ""),
                ))
            print()

    # ----- Skip -----
    skip_rows = [t for t in tickets if t.get("decision") == "skip"]
    if skip_rows:
        skip_rows = _sort_rows(skip_rows)
        print("## Skip (not a root cause)")
        print()
        print("| Ticket | Summary | Reason | Why | Priority |")
        print("|---|---|---|---|---|")
        for r in skip_rows:
            reason = SKIP_REASON_LABEL.get(r.get("skip_reason") or "", r.get("skip_reason") or "")
            print("| [%s](%s) | %s | %s | %s | %s |" % (
                r["key"],
                _browse_url(base_url, r["key"]),
                _md_cell(r.get("summary") or ""),
                _md_cell(reason),
                _md_cell(r.get("reasoning") or ""),
                _md_cell(r.get("priority") or ""),
            ))
        print()

    # ----- Insufficient evidence -----
    insuf = [t["key"] for t in tickets if t.get("decision") == "insufficient_evidence"]
    if insuf:
        print("## Insufficient evidence")
        print()
        print(", ".join("`%s`" % k for k in insuf) + " (%d tickets)" % len(insuf))
        print()
        print("> For these tickets, run `/support-ticket-triage <KEY>` for deeper code-level investigation before deciding.")
        print()

    # ----- L2 nominated a key outside the configured RC catalog -----
    out_of_catalog = audit.get("pre_decided_out_of_catalog") or []
    if out_of_catalog:
        print("## L2 nominated an RC outside the configured catalog")
        print()
        print("> L2 typed a Jira key into the support ticket's Root Cause field, but that key is not a child of `ROOT_CAUSE_EPICS`. Either expand `ROOT_CAUSE_EPICS` to cover its parent epic, or apply the link manually in Jira.")
        print()
        print("| Ticket | L2 nominated | L2 wrote |")
        print("|---|---|---|")
        for entry in out_of_catalog[:50]:
            key = entry.get("key", "")
            sum_obj = entry.get("summary")
            sum_text = sum_obj.get("text", "") if isinstance(sum_obj, dict) else (sum_obj or "")
            named = entry.get("support_rc_named_keys_out_of_catalog") or []
            named_links = ", ".join("[%s](%s)" % (k, _browse_url(base_url, k)) for k in named)
            sr_obj = entry.get("support_root_cause") or {}
            sr_text = sr_obj.get("text", "") if isinstance(sr_obj, dict) else (sr_obj or "")
            print("| [%s](%s) — %s | %s | %s |" % (
                key, _browse_url(base_url, key), _md_cell(sum_text),
                named_links,
                _md_cell(sr_text),
            ))
        if len(out_of_catalog) > 50:
            print("|  |  | …and %d more |" % (len(out_of_catalog) - 50))
        print()

    # ----- Already linked -----
    if already_linked:
        print("## Already linked (skipped during fetch)")
        print()
        print("> These tickets already have a link to a root cause in the catalog and were not sent to the sub-agent.")
        print()
        print("| Ticket | Existing link |")
        print("|---|---|")
        for entry in already_linked[:50]:
            key = entry.get("key", "")
            link_strs = []
            for link in entry.get("rc_links") or []:
                phrase = link.get("phrase") or link.get("type_name") or "linked"
                link_strs.append("%s [%s](%s)" % (phrase, link["target_key"], _browse_url(base_url, link["target_key"])))
            sum_obj = entry.get("summary")
            sum_text = sum_obj.get("text", "") if isinstance(sum_obj, dict) else (sum_obj or "")
            print("| [%s](%s) — %s | %s |" % (
                key,
                _browse_url(base_url, key),
                _md_cell(sum_text),
                _md_cell("; ".join(link_strs)),
            ))
        if len(already_linked) > 50:
            print("|  | …and %d more |" % (len(already_linked) - 50))
        print()

    # ----- Still-open tickets (skipped) -----
    open_skipped = audit.get("open_skipped") or []
    if open_skipped:
        print("## Still-open tickets (skipped during fetch)")
        print()
        print("> Only closed/resolved tickets are evaluated. The engineer working an in-progress ticket may still link a root cause themselves; re-run after they close out.")
        print()
        print("| Ticket | Status | Summary |")
        print("|---|---|---|")
        for entry in open_skipped[:50]:
            key = entry.get("key", "")
            sum_obj = entry.get("summary")
            sum_text = sum_obj.get("text", "") if isinstance(sum_obj, dict) else (sum_obj or "")
            print("| [%s](%s) | %s | %s |" % (
                key, _browse_url(base_url, key),
                _md_cell(entry.get("status") or ""),
                _md_cell(sum_text),
            ))
        if len(open_skipped) > 50:
            print("|  |  | …and %d more |" % (len(open_skipped) - 50))
        print()

    # ----- Next steps -----
    high_count = summary.get("link_existing_high", 0)
    propose_count = summary.get("propose_new_clusters", 0)
    if high_count or propose_count or summary.get("link_existing", 0) or summary.get("insufficient_evidence", 0):
        print("---")
        print()
        print("**Next steps:**")
        if high_count:
            print("- **Bulk-create the high-confidence \"is caused by\" links** — these are safe to apply without per-ticket review.")
        if summary.get("link_existing_medium", 0) or summary.get("link_existing_low", 0):
            print("- For **medium / low confidence** links, spot-check each ticket before applying.")
        if propose_count:
            print("- For each **proposed new root cause** cluster, decide whether to file a new RC ticket; then link all source tickets to it.")
        if summary.get("insufficient_evidence", 0):
            print("- For **insufficient evidence** rows, run `/support-ticket-triage <KEY>` for a deep classification before deciding.")
        print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Write only to /tmp/root-cause-suggest/report.md; skip the vault.")
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

    # Compute period strings for header + frontmatter consistency.
    mode = audit.get("mode") or ""
    period = audit.get("period") or {}
    period_start = ""
    period_end = ""
    if mode == "auto_discover":
        since_days = period.get("since_days")
        if isinstance(since_days, int) and 1 <= since_days <= 90:
            end = datetime.date.today()
            start = end - datetime.timedelta(days=since_days)
            period_start = start.isoformat()
            period_end = end.isoformat()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _render(audit, setup, period_start, period_end)
    body = buf.getvalue()
    generated_at = datetime.datetime.now().replace(microsecond=0).isoformat()
    markdown = _frontmatter(audit, generated_at, period_start, period_end) + body

    _atomic_write(REPORT_PATH, markdown)
    print("OK report written to %s (%d bytes)" % (REPORT_PATH, len(markdown)))

    if args.dry_run:
        print("(--dry-run: skipping vault write)")
        return 0

    vault_path, err = _resolve_vault_path(audit)
    if not vault_path:
        print("WARNING: skipping vault write — %s" % err, file=sys.stderr)
        return 0

    teams_path = os.environ.get("OBSIDIAN_TEAMS_PATH", "").strip()
    vault_dir = (audit.get("vault_dir") or "").strip()
    if teams_path and vault_dir:
        hub_candidates = [
            os.path.join(teams_path, "%s.md" % vault_dir),
            os.path.join(teams_path, vault_dir, "%s.md" % vault_dir),
        ]
        if not any(os.path.exists(p) for p in hub_candidates):
            print("WARNING: team hub page [[%s]] not found at any of: %s — "
                  "the frontmatter link will be unresolved in Obsidian." % (
                      vault_dir, ", ".join(hub_candidates)), file=sys.stderr)

    os.makedirs(os.path.dirname(vault_path), exist_ok=True)
    _atomic_write(vault_path, markdown)
    print("OK vault file written to %s" % vault_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
