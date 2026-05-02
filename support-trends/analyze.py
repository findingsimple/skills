#!/usr/bin/env python3
"""Support trends v2 analyzer: deterministic L2 signals + crystallised
findings → /tmp/support_trends/analysis.json.

Findings are emitted at the top level of the JSON output. Each one has a
fixed schema (kind / claim / metric / evidence_keys / severity /
audience_hint) — see derive_findings(). All thresholds live in thresholds.py.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import concurrency
import thresholds
import _libpath  # noqa: F401
from jira_client import ensure_tmp_dir, atomic_write_json
from narrative_notes import derive_narrative_notes


CACHE_DIR = "/tmp/support_trends"

CLOSED_NAME_KEYWORDS = ("closed", "done", "resolved", "completed", "cancelled", "canceled")
NEVER_DO_RESOLUTIONS = {"won't do", "wont do", "won't fix", "wont fix", "cannot reproduce", "duplicate"}

# Resolved tickets with this resolution_category came from L2 but engineering
# determined they shouldn't have escalated. Unambiguous routing-miss signal.
L3_BOUNCED_CATEGORY = "L3 Bounced"


def parse_dt(dt_str):
    if not dt_str:
        return None
    # Strip sub-second fractions ONLY when they appear directly before a
    # timezone marker (Z, +HH, -HH) or end-of-string — `\.\d+` alone would
    # also strip a fractional inside e.g. an offset string. In practice Jira
    # never emits such, but the broader pattern is sloppy.
    s = re.sub(r"\.\d+(?=$|[Z+\-])", "", str(dt_str)).replace("Z", "+00:00")
    m = re.match(r"(.*[+-])(\d{2})(\d{2})$", s)
    if m:
        s = "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
    try:
        return datetime.fromisoformat(s)
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def adjusted_hours_between(a, b):
    """Calendar hours between two datetimes, scaled by 5/7 to approximate the
    "business hours" that fall inside an arbitrary calendar window. NOT a real
    working-calendar implementation — does not exclude weekends, holidays, or
    after-hours blocks. A ticket created Friday 5pm and assigned Monday 9am will
    be reported as ~46 adjusted hours, not ~1. The label "adjusted hours"
    everywhere downstream is a deliberate honest signal that this is an
    approximation."""
    if not a or not b:
        return None
    seconds = (b - a).total_seconds()
    if seconds < 0:
        return None
    return seconds / 3600.0 * (5.0 / 7.0)


def calendar_hours_between(a, b):
    """Plain wall-clock hours."""
    if not a or not b:
        return None
    seconds = (b - a).total_seconds()
    if seconds < 0:
        return None
    return seconds / 3600.0


def looks_closed(status_name):
    """Match a Jira status name as 'closed-shaped' on a word-boundary basis,
    NOT a substring basis — a status named e.g. 'Disclosed Bugs' would
    register as closed under a naive `'closed' in name` check.

    Split on any run of non-alphanumeric characters so trailing punctuation
    ('Resolved.', 'DONE!') and mixed separators all tokenise consistently.
    A status with no separator at all ('doneXX') does NOT match — that's the
    intentional word-boundary trade-off vs the old substring check."""
    n = (status_name or "").lower().strip()
    if not n:
        return False
    tokens = re.split(r"[^a-z0-9]+", n)
    return any(tok in CLOSED_NAME_KEYWORDS for tok in tokens)


def auto_bucket(start_date, end_date):
    days = (end_date - start_date).days + 1
    if days <= 14:
        return "daily"
    if days <= 56:
        return "weekly"
    return "monthly"


def build_buckets(start_date, end_date, bucket):
    """Return list of (bucket_label, bucket_start_date, bucket_end_date_exclusive)."""
    out = []
    if bucket == "daily":
        d = start_date
        while d <= end_date:
            out.append((d.isoformat(), d, d + timedelta(days=1)))
            d += timedelta(days=1)
    elif bucket == "weekly":
        # Anchor weeks to Monday for ISO weeks.
        d = start_date - timedelta(days=start_date.weekday())  # back to Monday
        if d > start_date:  # safety
            d = start_date
        while d <= end_date:
            wend = d + timedelta(days=7)
            label = "%s (W%02d)" % (d.isoformat(), d.isocalendar()[1])
            out.append((label, d, wend))
            d = wend
    elif bucket == "monthly":
        y, m = start_date.year, start_date.month
        while True:
            bstart = datetime(y, m, 1).date()
            if bstart > end_date:
                break
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
            bend = datetime(ny, nm, 1).date()
            label = "%04d-%02d" % (y, m)
            out.append((label, bstart, bend))
            y, m = ny, nm
    else:
        raise ValueError("Unknown bucket: %s" % bucket)
    return out


def to_date(dt):
    return dt.date() if hasattr(dt, "date") else dt


def compute_l1_signals(tickets, window_start_dt, window_end_dt, team_uuids, team_field_labels):
    """Compute the L2/triage quality signals over the in-window-created subset.

    `team_field_labels` is a set of acceptable display-name strings for the
    Team custom field — used as a fallback when UUID resolution failed for some
    of them. Pass an empty set if all resolution succeeded (UUIDs are then
    authoritative).

    Returns a dict ready for serialisation. All "hours" values are
    *adjusted hours* (calendar × 5/7), NOT real working calendar hours —
    see adjusted_hours_between() in this module.
    """
    in_window = [t for t in tickets if _created_in_window(t, window_start_dt, window_end_dt)]
    team_field_labels = set(team_field_labels or [])

    first_assignment_hours = []  # adjusted hours
    never_assigned = []
    reopen_keys = []
    ever_closed_keys = set()  # in-window tickets that transitioned INTO a closed
                              # status at any point — denominator for reopen rate.
                              # `resolutiondate` is unsuitable because Jira clears
                              # it on reopen; a closed-then-reopened-still-open
                              # ticket would otherwise be in the numerator but not
                              # the denominator, allowing rates > 1.0.
    quick_close = []
    reassign_out = []
    reassign_out_24h_calendar = []
    wont_do = []

    for t in in_window:
        created_dt = parse_dt(t.get("created"))
        if not created_dt:
            continue
        cl = t.get("changelog") or []

        # First assignee event (null → non-null)
        first_assign_dt = None
        first_assignee_name = None
        for entry in cl:
            entry_dt = parse_dt(entry.get("created"))
            for item in entry.get("items", []) or []:
                if item.get("field") == "assignee":
                    fr = (item.get("from_string") or "").strip()
                    to = (item.get("to_string") or "").strip()
                    if not fr and to:
                        first_assign_dt = entry_dt
                        first_assignee_name = to
                        break
            if first_assign_dt:
                break

        if first_assign_dt:
            ah = adjusted_hours_between(created_dt, first_assign_dt)
            if ah is not None:
                first_assignment_hours.append({"key": t["key"], "hours": ah, "engineer": first_assignee_name})
        else:
            never_assigned.append({"key": t["key"], "summary": t.get("summary", ""), "reporter": t.get("reporter", "")})

        # Reopen + ever-closed: walk every status transition in the changelog.
        # Reopen = closed → non-closed at least once (numerator).
        # Ever-closed = non-closed → closed at least once (denominator). A ticket
        # currently open with `resolutiondate=null` may still be ever-closed if it
        # was closed and reopened within the window — this is the case the old
        # `t.get("resolutiondate")` denominator missed. Tickets currently closed
        # are also detected here when their changelog records the closing
        # transition; for the rare case where the closing transition predates the
        # changelog (legacy data), `resolutiondate` is the fallback.
        reopened_this_ticket = False
        ever_closed_this_ticket = False
        for entry in cl:
            for item in entry.get("items", []) or []:
                if item.get("field") != "status":
                    continue
                fr = item.get("from_string", "")
                to = item.get("to_string", "")
                fr_closed = looks_closed(fr)
                to_closed = looks_closed(to)
                if fr_closed and not to_closed and not reopened_this_ticket:
                    reopen_keys.append(t["key"])
                    reopened_this_ticket = True
                if to_closed and not fr_closed:
                    ever_closed_this_ticket = True
        # Fallback: ticket currently has a resolutiondate but the closing
        # transition isn't in the (possibly truncated) changelog.
        if not ever_closed_this_ticket and t.get("resolutiondate"):
            ever_closed_this_ticket = True
        if ever_closed_this_ticket:
            ever_closed_keys.add(t["key"])

        # Quick-close: resolution exists, never assigned to anyone, < 4 adjusted hours.
        resolved_dt = parse_dt(t.get("resolutiondate"))
        if resolved_dt and not first_assign_dt:
            ah = adjusted_hours_between(created_dt, resolved_dt)
            if ah is not None and ah < 4.0:
                quick_close.append({
                    "key": t["key"],
                    "summary": t.get("summary", ""),
                    "reporter": t.get("reporter", ""),
                    "resolution": t.get("resolution", ""),
                    "hours": round(ah, 1),
                })

        # Reassigned out of team: Team field changes in changelog.
        # Jira changelog labels cf[10600] under field="Team".
        for entry in cl:
            entry_dt = parse_dt(entry.get("created"))
            for item in entry.get("items", []) or []:
                if (item.get("field") or "").lower() != "team":
                    continue
                from_id = item.get("from") or ""
                to_id = item.get("to") or ""
                from_name = item.get("from_string") or ""
                to_name = item.get("to_string") or ""
                # Was-in-team check: prefer UUID match (authoritative), fall back to display-name set.
                from_in_team = (from_id in team_uuids) or (from_name in team_field_labels)
                to_in_team = (to_id in team_uuids) or (to_name in team_field_labels)
                # Reassign-OUT requires the new value to be a real other team —
                # clearing the Team field (to "" / "(none)") is field bookkeeping,
                # not a team-to-team handoff.
                if from_in_team and not to_in_team and to_name:
                    ah = adjusted_hours_between(created_dt, entry_dt)
                    cal_h = calendar_hours_between(created_dt, entry_dt)
                    rec = {
                        "key": t["key"],
                        "summary": t.get("summary", ""),
                        "reporter": t.get("reporter", ""),
                        "from_team": from_name,
                        "to_team": to_name,
                        "hours_to_reassign": round(ah, 1) if ah is not None else None,
                        "calendar_hours_to_reassign": round(cal_h, 1) if cal_h is not None else None,
                    }
                    reassign_out.append(rec)
                    # Fast-bounce uses LITERAL 24 calendar hours, not adjusted.
                    if cal_h is not None and cal_h < 24.0:
                        reassign_out_24h_calendar.append(rec)
                    break  # only count first reassign-out per ticket
            else:
                continue
            break

        # Won't Do / Cannot Reproduce / Duplicate
        if (t.get("resolution") or "").lower().strip() in NEVER_DO_RESOLUTIONS:
            wont_do.append({
                "key": t["key"],
                "summary": t.get("summary", ""),
                "reporter": t.get("reporter", ""),
                "resolution": t.get("resolution", ""),
            })

    # Median + p90 over assignment hours.
    hours_sorted = sorted([h["hours"] for h in first_assignment_hours])
    def pctl(values, p):
        if not values:
            return None
        k = (len(values) - 1) * p
        f = int(k)
        c = min(f + 1, len(values) - 1)
        if f == c:
            return values[f]
        return values[f] + (values[c] - values[f]) * (k - f)

    median_h = pctl(hours_sorted, 0.5)
    p90_h = pctl(hours_sorted, 0.9)

    # Two closed-counts with different semantics:
    #   currently_closed_count = `resolutiondate` truthy at window-end. Used for
    #     `quick_close` rate, where the numerator is also "currently resolved".
    #   ever_closed_count = entered a closed status at any point during the
    #     window (regardless of whether they're still closed). Used for
    #     `reopen` rate, since a reopened ticket has its `resolutiondate`
    #     cleared and would otherwise be in numerator-but-not-denominator.
    currently_closed_count = sum(1 for t in in_window if t.get("resolutiondate"))
    ever_closed_count = len(ever_closed_keys)
    in_window_count = len(in_window)

    return {
        "in_window_count": in_window_count,
        "closed_in_window_count": currently_closed_count,
        "ever_closed_in_window_count": ever_closed_count,
        "first_assignment": {
            "median_adjusted_hours": round(median_h, 1) if median_h is not None else None,
            "p90_adjusted_hours": round(p90_h, 1) if p90_h is not None else None,
            "assigned_count": len(first_assignment_hours),
            "never_assigned_count": len(never_assigned),
        },
        "never_assigned_examples": never_assigned[:20],
        "reopen": {
            "count": len(reopen_keys),
            "rate": (len(reopen_keys) / ever_closed_count) if ever_closed_count else None,
            "keys": reopen_keys[:50],
        },
        "quick_close": {
            "count": len(quick_close),
            "rate": (len(quick_close) / currently_closed_count) if currently_closed_count else None,
            "items": quick_close,
        },
        "reassign_out": {
            "count": len(reassign_out),
            "fast_bounce_24h_calendar_count": len(reassign_out_24h_calendar),
            "items": reassign_out,
        },
        "wont_do": {
            "count": len(wont_do),
            "items": wont_do,
        },
    }


def _created_in_window(t, window_start_dt, window_end_dt):
    cdt = parse_dt(t.get("created"))
    return cdt is not None and window_start_dt <= cdt <= window_end_dt


def _safe_delta_pct(current, prior):
    """Percentage delta. Returns None when undefined (prior == 0 or either side None).

    Rounds to 1 decimal using Python 3's banker's rounding (round-half-to-
    even). Threshold checks elsewhere are `>=`-based with thresholds at
    coarse boundaries (5.0pp, 20.0pp, 50.0pp), so banker's-vs-half-up
    differences never bracket a threshold in practice. If you tighten a
    threshold to land near a 0.05 boundary, prefer comparing the raw
    quotient rather than the rounded value.
    """
    if current is None or prior is None:
        return None
    if prior == 0:
        return None
    return round(100.0 * (current - prior) / prior, 1)


def compute_buckets_view(in_window_all, backlog_open_at_start, start_date, end_date, bucket):
    """Compute per-bucket created/resolved counts and end-of-bucket backlog."""
    buckets = build_buckets(start_date, end_date, bucket)
    rows = []
    cum_created = 0
    cum_resolved = 0

    # Pre-extract creation/resolution dates
    created_dates = []
    resolved_dates = []
    for t in in_window_all:
        cdt = parse_dt(t.get("created"))
        rdt = parse_dt(t.get("resolutiondate"))
        if cdt:
            created_dates.append(to_date(cdt))
        if rdt:
            resolved_dates.append(to_date(rdt))
    created_dates.sort()
    resolved_dates.sort()

    # Clamp bucket boundaries to the user's window so weekly/monthly buckets
    # whose nominal range extends before window-start (e.g. Monday-before-start
    # for a window starting mid-week) don't pick up backlog tickets that were
    # created earlier and count them as "created in this bucket".
    window_end_inclusive = end_date + timedelta(days=1)
    ci = ri = 0
    for label, bstart, bend in buckets:
        bstart_eff = max(bstart, start_date)
        bend_eff = min(bend, window_end_inclusive)
        is_partial = (bstart < start_date) or (bend > window_end_inclusive)
        c = 0
        while ci < len(created_dates) and created_dates[ci] < bend_eff:
            if created_dates[ci] >= bstart_eff:
                c += 1
            ci += 1
        r = 0
        while ri < len(resolved_dates) and resolved_dates[ri] < bend_eff:
            if resolved_dates[ri] >= bstart_eff:
                r += 1
            ri += 1
        cum_created += c
        cum_resolved += r
        backlog_end = backlog_open_at_start + cum_created - cum_resolved
        # Annotate partial buckets in the label so a reader can't mistake
        # the count for a full-bucket figure when comparing front-loaded vs
        # back-loaded distributions.
        display_label = label
        if is_partial:
            display_label = "%s _(partial: %s → %s)_" % (
                label, bstart_eff.isoformat(),
                (bend_eff - timedelta(days=1)).isoformat())
        rows.append({
            "label": display_label,
            "start": bstart_eff.isoformat(),
            "end_exclusive": bend_eff.isoformat(),
            "is_partial": is_partial,
            "created": c,
            "resolved": r,
            "net": c - r,
            "backlog_end": backlog_end,
        })
    return rows


def compute_resolution_category_breakdown(tickets, window_start_dt, window_end_dt):
    """Group resolved-in-window tickets by `resolution_category` (cf 11695).

    Returns
    -------
    {
        "resolved_in_window_count": int,
        "blank_count": int,
        "blank_pct": float | None,
        "rows": [
            {"category": str | "(blank)", "count": int, "pct": float, "keys": [str, ...]},
            ...
        ],
        "l3_bounced_keys": [str, ...],
    }

    `rows` is sorted by count desc and includes a synthetic "(blank)" row when
    any resolved ticket has an empty/missing category. `pct` is share of
    resolved-in-window tickets, not of all in-window tickets.

    Used directly by the renderer (Numbers section) and by derive_findings()
    for the categorisation_blank + l3_bounced_back finding kinds.
    """
    by_cat = defaultdict(list)
    resolved_count = 0
    for t in tickets:
        rdt = parse_dt(t.get("resolutiondate"))
        if not (rdt and window_start_dt <= rdt <= window_end_dt):
            continue
        resolved_count += 1
        cat = (t.get("resolution_category") or "").strip()
        key = cat if cat else "(blank)"
        by_cat[key].append(t.get("key", ""))

    rows = []
    for cat, keys in by_cat.items():
        rows.append({
            "category": cat,
            "count": len(keys),
            "pct": (100.0 * len(keys) / resolved_count) if resolved_count else 0.0,
            "keys": keys,
        })
    rows.sort(key=lambda r: (-r["count"], r["category"]))

    blank_count = len(by_cat.get("(blank)", []))
    blank_pct = (100.0 * blank_count / resolved_count) if resolved_count else None

    return {
        "resolved_in_window_count": resolved_count,
        "blank_count": blank_count,
        "blank_pct": blank_pct,
        "rows": rows,
        "l3_bounced_keys": list(by_cat.get(L3_BOUNCED_CATEGORY, [])),
    }


def analyze_window(data, bucket_choice):
    """Analyze one window's `data.json`-shape dict. Returns the analysis dict:
    window/totals/buckets/l1_signals/resolution_categories."""
    fetch_args = data.get("args", {}) or {}
    try:
        start = datetime.strptime(fetch_args["start"], "%Y-%m-%d").date()
        end = datetime.strptime(fetch_args["end"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError) as e:
        raise ValueError(
            "analyze.py: data.json args.start/args.end missing or malformed (%s). "
            "data.json appears truncated — re-run fetch.py to regenerate it." % e
        )
    window_start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    window_end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

    bucket = bucket_choice if bucket_choice != "auto" else auto_bucket(start, end)

    tickets = data.get("tickets", []) or []
    backlog_open_at_start = len(data.get("backlog_open_keys", []) or [])
    team_uuids = data.get("team_uuids", []) or []
    raw_team_field = (fetch_args.get("support_team_field") or "")
    team_field_list = [v.strip() for v in raw_team_field.split(",") if v.strip()]
    team_field_labels = set(team_field_list)
    # Canonical = first entry in the configured support_team_field list. By
    # convention the active team name comes first; any later entries are
    # historical aliases (e.g. "TeamA,TeamA-legacy" — the second is the prior name).
    team_field_canonical = team_field_list[0] if team_field_list else None

    in_window = [t for t in tickets if _created_in_window(t, window_start_dt, window_end_dt)]

    bucket_rows = compute_buckets_view(tickets, backlog_open_at_start, start, end, bucket)
    l1 = compute_l1_signals(tickets, window_start_dt, window_end_dt, team_uuids, team_field_labels)
    resolution_categories = compute_resolution_category_breakdown(tickets, window_start_dt, window_end_dt)

    resolved_in_window = 0
    for t in tickets:
        rdt = parse_dt(t.get("resolutiondate"))
        if rdt and window_start_dt <= rdt <= window_end_dt:
            resolved_in_window += 1

    return {
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days + 1,
            "bucket": bucket,
        },
        "totals": {
            "created_in_window": len(in_window),
            "resolved_in_window": resolved_in_window,
            "net": len(in_window) - resolved_in_window,
            "backlog_open_at_start": backlog_open_at_start,
            "backlog_at_end": bucket_rows[-1]["backlog_end"] if bucket_rows else backlog_open_at_start,
        },
        "buckets": bucket_rows,
        "l1_signals": l1,
        "resolution_categories": resolution_categories,
        "team_field_labels": sorted(team_field_labels),
        "team_field_canonical": team_field_canonical,
    }


# ---------------------------------------------------------------------------
# Findings derivation — the v2 trust fix.
#
# Everything above this line is the same deterministic math v1 did. The v2
# difference is here: instead of leaving the synthesis agent to spot what
# crossed a threshold, we make those decisions in code with explicit
# thresholds, and emit one structured finding per crossed threshold. The
# synthesis agent then *picks and frames* findings — it does not decide what's
# true.
#
# Each finding has a fixed shape (kind / claim / metric / evidence_keys /
# severity / audience_hint). evidence_keys is mandatory; the synthesis agent
# is configured to reject any finding it tries to surface without one.
# ---------------------------------------------------------------------------


def _finding(kind, claim, metric, evidence_keys, severity, audience_hint, **extra):
    """Build one finding record. Centralised so schema drift is impossible."""
    rec = {
        "kind": kind,
        "claim": claim,
        "metric": metric,
        "evidence_keys": list(evidence_keys or []),
        "severity": severity,
        "audience_hint": audience_hint,
    }
    rec.update(extra)
    return rec


def _severity_from_pct(abs_pct, cfg):
    """High if |pct| crosses cfg['severity_high'] (when set), else medium."""
    high = cfg.get("severity_high")
    return "high" if (high is not None and abs_pct >= high) else "medium"


def _severity_from_count(count, cfg):
    """High if count crosses cfg['severity_high'] (when set), else medium."""
    high = cfg.get("severity_high")
    return "high" if (high is not None and count >= high) else "medium"


def _arrow(prior, current):
    return "%d → %d" % (prior, current)


def _evidence_from_items(items, limit=20):
    """Pull evidence keys from a list of l1-signal item dicts (each has a `key`)."""
    return [it.get("key", "") for it in (items or [])][:limit]


def derive_findings(current, prior, deltas):
    """Walk the deterministic outputs and emit a list of finding records.

    Findings are independent — every threshold gets its own check. Order is
    only used as the synthesis agent's starting hint; the agent re-prioritises.

    Skips kinds that need prior data when prior is None.
    """
    findings = []
    cur_totals = current.get("totals") or {}
    cur_l1 = current.get("l1_signals") or {}
    cur_cats = current.get("resolution_categories") or {}
    have_prior = prior is not None
    prior_totals = (prior or {}).get("totals") or {}
    prior_l1 = (prior or {}).get("l1_signals") or {}

    # --- volume_change ---
    cfg = thresholds.VOLUME_CHANGE
    cur_created = cur_totals.get("created_in_window") or 0
    if have_prior:
        prior_created = prior_totals.get("created_in_window") or 0
        delta_pct = (deltas or {}).get("totals", {}).get("created_pct")
        if delta_pct is not None and abs(delta_pct) >= cfg["pct"] and cur_created >= cfg["abs"]:
            # When the current and prior windows have different day counts
            # (typical: 30-day April vs 31-day March), the raw count Δ% silently
            # double-counts the calendar gap. Compute a per-day-rate-adjusted
            # Δ% as well so the synthesise agent can frame confidence and the
            # renderer can show "+92% (raw) / +98% (per-day)" when meaningful.
            cur_days = (current.get("window") or {}).get("days") or 0
            prior_days = ((prior or {}).get("window") or {}).get("days") or 0
            adjusted_pct = None
            if cur_days and prior_days and cur_days != prior_days and prior_created > 0:
                cur_rate = cur_created / cur_days
                prior_rate = prior_created / prior_days
                adjusted_pct = 100.0 * (cur_rate - prior_rate) / prior_rate
            extras = {"delta_pct": delta_pct}
            if adjusted_pct is not None:
                extras["delta_pct_per_day"] = adjusted_pct
                extras["cur_days"] = cur_days
                extras["prior_days"] = prior_days
            findings.append(_finding(
                kind="volume_change",
                claim="In-team ticket volume %s %.0f%% vs prior window" % (
                    "up" if delta_pct >= 0 else "down", abs(delta_pct)),
                metric=_arrow(prior_created, cur_created),
                evidence_keys=[],  # whole-window claim — no per-ticket evidence
                severity=_severity_from_pct(abs(delta_pct), cfg),
                audience_hint="exec",
                **extras,
            ))

    # --- time_to_engineer_regression ---
    if have_prior:
        cfg = thresholds.TIME_TO_ENGINEER
        cur_h = (cur_l1.get("first_assignment") or {}).get("median_adjusted_hours")
        prior_h = (prior_l1.get("first_assignment") or {}).get("median_adjusted_hours")
        if cur_h is not None and prior_h not in (None, 0) and cur_h >= cfg["abs_floor_hours"]:
            pct = 100.0 * (cur_h - prior_h) / prior_h
            if pct >= cfg["pct"]:
                # Evidence: tickets currently never-assigned (proxy for slowness drivers).
                never_assigned = cur_l1.get("never_assigned_examples") or []
                findings.append(_finding(
                    kind="time_to_engineer_regression",
                    claim="Median time-to-first-engineer up %.0f%% vs prior" % pct,
                    metric="%.1fh → %.1fh (adjusted)" % (prior_h, cur_h),
                    evidence_keys=[r.get("key", "") for r in never_assigned[:15] if r.get("key")],
                    severity="medium",
                    audience_hint="support",
                    pct=pct,
                ))

    # --- reopen_spike ---
    cfg = thresholds.REOPEN
    cur_reopen = cur_l1.get("reopen") or {}
    cur_count = cur_reopen.get("count") or 0
    if have_prior and cur_count >= cfg["abs"]:
        prior_reopen = prior_l1.get("reopen") or {}
        cur_rate = (cur_reopen.get("rate") or 0) * 100
        prior_rate = (prior_reopen.get("rate") or 0) * 100
        pp = cur_rate - prior_rate
        if pp >= cfg["pp"]:
            findings.append(_finding(
                kind="reopen_spike",
                claim="Reopen rate up %.1fpp vs prior" % pp,
                metric="%.0f%% → %.0f%%  (%d tickets)" % (prior_rate, cur_rate, cur_count),
                evidence_keys=cur_reopen.get("keys", [])[:20],
                severity="medium",
                audience_hint="support",
                pp=pp,
            ))

    # --- quick_close_pattern ---
    cfg = thresholds.QUICK_CLOSE
    cur_qc = cur_l1.get("quick_close") or {}
    cur_qc_count = cur_qc.get("count") or 0
    if have_prior and cur_qc_count >= cfg["abs"]:
        prior_qc = prior_l1.get("quick_close") or {}
        cur_qc_rate = (cur_qc.get("rate") or 0) * 100
        prior_qc_rate = (prior_qc.get("rate") or 0) * 100
        pp = cur_qc_rate - prior_qc_rate
        if pp >= cfg["pp"]:
            findings.append(_finding(
                kind="quick_close_pattern",
                claim="Quick-close rate up %.1fpp — tickets resolved <4h that never reached an engineer" % pp,
                metric="%.0f%% → %.0f%%  (%d tickets)" % (prior_qc_rate, cur_qc_rate, cur_qc_count),
                evidence_keys=_evidence_from_items(cur_qc.get("items")),
                severity="medium",
                audience_hint="support",
                pp=pp,
            ))

    # --- reassign_out_burst ---
    cfg = thresholds.REASSIGN_OUT
    ro = cur_l1.get("reassign_out") or {}
    ro_count = ro.get("count") or 0
    if ro_count >= cfg["abs"]:
        findings.append(_finding(
            kind="reassign_out_burst",
            claim="%d tickets routed *out* of the team after intake" % ro_count,
            metric="%d tickets (24h fast-bounce: %d)" % (
                ro_count, ro.get("fast_bounce_24h_calendar_count") or 0),
            evidence_keys=_evidence_from_items(ro.get("items")),
            severity=_severity_from_count(ro_count, cfg),
            audience_hint="support",
        ))

    # --- never_do_rate ---
    cfg = thresholds.NEVER_DO
    wd = cur_l1.get("wont_do") or {}
    wd_count = wd.get("count") or 0
    if have_prior and wd_count >= cfg["abs"]:
        prior_wd = prior_l1.get("wont_do") or {}
        prior_wd_count = prior_wd.get("count") or 0
        if prior_wd_count > 0 and (wd_count / prior_wd_count) >= cfg["ratio"]:
            findings.append(_finding(
                kind="never_do_rate",
                claim="Won't Do / Cannot Reproduce / Duplicate count %dx vs prior" % (
                    wd_count // max(prior_wd_count, 1)),
                metric=_arrow(prior_wd_count, wd_count),
                evidence_keys=_evidence_from_items(wd.get("items")),
                severity="medium",
                audience_hint="support",
            ))

    # --- categorisation_blank ---
    cfg = thresholds.CATEGORISATION_BLANK
    blank_pct = cur_cats.get("blank_pct")
    resolved_n = cur_cats.get("resolved_in_window_count") or 0
    blank_n = cur_cats.get("blank_count") or 0
    if (blank_pct is not None
            and blank_pct >= cfg["pct"]
            and resolved_n >= cfg["abs_resolved_floor"]):
        # Evidence: the blank-category tickets themselves.
        blank_keys = []
        for row in (cur_cats.get("rows") or []):
            if row.get("category") == "(blank)":
                blank_keys = row.get("keys") or []
                break
        findings.append(_finding(
            kind="categorisation_blank",
            claim="%.0f%% of resolved tickets have no Resolution Category set" % blank_pct,
            metric="%d of %d resolved (%.0f%%)" % (blank_n, resolved_n, blank_pct),
            evidence_keys=blank_keys[:20],
            severity=_severity_from_pct(blank_pct, cfg),
            audience_hint="support",
            blank_pct=blank_pct,
        ))

    # --- l3_bounced_back ---
    cfg = thresholds.L3_BOUNCED
    l3_keys = cur_cats.get("l3_bounced_keys") or []
    if len(l3_keys) >= cfg["abs"]:
        findings.append(_finding(
            kind="l3_bounced_back",
            claim="%d tickets engineering received but classified as L3 Bounced (sent back to L2)" % len(l3_keys),
            metric="%d of %d resolved" % (len(l3_keys), resolved_n) if resolved_n else "%d tickets" % len(l3_keys),
            evidence_keys=list(l3_keys)[:20],
            severity=_severity_from_count(len(l3_keys), cfg),
            audience_hint="support",
        ))

    return findings


def main():
    ok, msg = concurrency.verify_session()
    if not ok:
        print("ERROR: " + msg, file=sys.stderr)
        sys.exit(2)
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", default="auto", help="daily|weekly|monthly|auto (default auto)")
    args = p.parse_args()

    if args.bucket not in ("auto", "daily", "weekly", "monthly"):
        print("ERROR: --bucket must be one of auto|daily|weekly|monthly", file=sys.stderr)
        sys.exit(2)

    data_path = os.path.join(CACHE_DIR, "data.json")
    prior_path = os.path.join(CACHE_DIR, "data_prior.json")

    try:
        with open(data_path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: %s not found. Run fetch.py first." % data_path, file=sys.stderr)
        sys.exit(1)

    prior_data = None
    if os.path.exists(prior_path):
        try:
            with open(prior_path) as f:
                prior_data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print("WARNING: %s exists but could not be loaded (%s); proceeding without prior comparison." % (prior_path, e), file=sys.stderr)
            prior_data = None

    prior_analysis = analyze_window(prior_data, args.bucket) if prior_data is not None else None
    current_analysis = analyze_window(data, args.bucket)

    deltas = None
    if prior_analysis is not None:
        deltas = {
            "totals": {
                "created_pct": _safe_delta_pct(
                    current_analysis["totals"]["created_in_window"],
                    prior_analysis["totals"]["created_in_window"]),
                "resolved_pct": _safe_delta_pct(
                    current_analysis["totals"]["resolved_in_window"],
                    prior_analysis["totals"]["resolved_in_window"]),
                "net_abs": current_analysis["totals"]["net"] - prior_analysis["totals"]["net"],
            },
        }

    findings = derive_findings(current_analysis, prior_analysis, deltas)
    narrative_notes = derive_narrative_notes(current_analysis, prior_analysis, deltas)

    out = {
        "current": current_analysis,
        "prior": prior_analysis,
        "deltas": deltas,
        "findings": findings,
        "narrative_notes": narrative_notes,
    }
    ensure_tmp_dir(CACHE_DIR)
    atomic_write_json(os.path.join(CACHE_DIR, "analysis.json"), out)

    summary_bits = [
        "Created: %d" % current_analysis["totals"]["created_in_window"],
        "Resolved: %d" % current_analysis["totals"]["resolved_in_window"],
        "Net: %+d" % current_analysis["totals"]["net"],
        "Backlog (start→end): %d→%d" % (
            current_analysis["totals"]["backlog_open_at_start"],
            current_analysis["totals"]["backlog_at_end"]),
    ]
    if prior_analysis is not None:
        summary_bits.append("Prior created: %d (Δ %s%%)" % (
            prior_analysis["totals"]["created_in_window"],
            ("%+.1f" % deltas["totals"]["created_pct"]) if deltas["totals"]["created_pct"] is not None else "n/a",
        ))
    summary_bits.append("Findings: %d" % len(findings))
    summary_bits.append("Notes: %d" % len(narrative_notes))
    print(" | ".join(summary_bits))
    if findings:
        print()
        print("=== FINDINGS ===")
        for f in findings:
            print("[%s/%s] %s — %s" % (f["severity"], f["audience_hint"], f["claim"], f["metric"]))


if __name__ == "__main__":
    main()
