#!/usr/bin/env python3
"""Support trends v2 analyzer: deterministic breakdowns + L2 signals + crystallised
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
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import concurrency
import thresholds
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


def compute_l1_signals(tickets, window_start_dt, window_end_dt, team_uuids, team_field_labels, team_field_canonical=None):
    """Compute the L2/triage quality signals over the in-window-created subset.

    `team_field_labels` is a set of acceptable display-name strings for the
    Team custom field — used as a fallback when UUID resolution failed for some
    of them. Pass an empty set if all resolution succeeded (UUIDs are then
    authoritative). `team_field_canonical` is the canonical display name to
    render in transition paths when an alias matches (e.g. SECO → ACE rename).

    Returns a dict ready for serialisation. All "hours" values are
    *adjusted hours* (calendar × 5/7), NOT real working calendar hours —
    see adjusted_hours_between() in this module.
    """
    in_window = [t for t in tickets if _created_in_window(t, window_start_dt, window_end_dt)]
    team_field_labels = set(team_field_labels or [])
    focus_aliases_lower = {x.lower() for x in team_field_labels}

    def _canon(name):
        if not name:
            return "(none)"
        if name.lower() in focus_aliases_lower and team_field_canonical:
            return team_field_canonical
        return name

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
    engineer_unassigned = []
    wont_do = []
    bouncing = []          # tickets with >= 3 Team-field transitions
    returned_to_focus = [] # tickets where focus → other → ... → focus
    transitions_count_by_key = {}  # key -> int, used to enrich reassign_out items

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

        # ---- Full Team-field transition walk (independent of reassign_out) ----
        # The reassign_out block above intentionally captures only the FIRST
        # focus→non-focus move per ticket. Here we walk every Team transition
        # to spot tickets that bounce between teams (≥3 hops) and tickets that
        # were handed off and returned to the focus team. Both are stronger
        # "charter ambiguity" signals than a one-shot handoff.
        all_team_transitions = []
        for entry in cl:
            entry_dt = parse_dt(entry.get("created"))
            for item in entry.get("items", []) or []:
                if (item.get("field") or "").lower() != "team":
                    continue
                fr_id = item.get("from") or ""
                to_id = item.get("to") or ""
                fr_str = (item.get("from_string") or "").strip()
                to_str = (item.get("to_string") or "").strip()
                fr_focus = (fr_id in team_uuids) or (fr_str in team_field_labels)
                to_focus = (to_id in team_uuids) or (to_str in team_field_labels)
                # Skip alias-to-alias renames (e.g. SECO → ACE) — same team.
                if fr_focus and to_focus:
                    continue
                all_team_transitions.append({
                    "dt": entry_dt,
                    "from": fr_str,
                    "to": to_str,
                    "from_focus": fr_focus,
                    "to_focus": to_focus,
                    # A transition to/from "(none)" — i.e. the Team field was
                    # cleared or set from null — is not a *team-to-team* hop.
                    # Counted toward path/display history but excluded from
                    # the bouncing threshold below so a path like
                    # ACE → (none) → ACE doesn't register as a 3-team bounce.
                    "is_real_team_hop": bool(fr_str) and bool(to_str),
                })
        # Real-hop count: same exclusion as the bouncing threshold below.
        transitions_count_by_key[t["key"]] = sum(
            1 for s in all_team_transitions if bool(s["from"]) and bool(s["to"]))

        if all_team_transitions:
            # Build a path string starting from the FIRST transition's "from"
            # so the reader can see where the ticket originated. Append "to"
            # for each step, plus a "(currently)" marker on the final node.
            path_nodes = [_canon(all_team_transitions[0]["from"])]
            for step in all_team_transitions:
                path_nodes.append(_canon(step["to"]))
            current_team = (t.get("team_field_name") or "").strip()
            if current_team:
                path_nodes[-1] = path_nodes[-1] + " (currently)" if path_nodes[-1] != "(none)" else _canon(current_team) + " (currently)"
            path_str = " → ".join(path_nodes)

            first_dt = all_team_transitions[0]["dt"]
            last_dt = all_team_transitions[-1]["dt"]
            span_h = calendar_hours_between(first_dt, last_dt) if (first_dt and last_dt) else None

            # Returned to focus: any focus→other-real-team event followed by
            # an other-real-team→focus event. Excludes "(none)" both ways —
            # clearing the Team field then setting it back is bookkeeping,
            # not another team explicitly handing the ticket back.
            saw_focus_exit = False
            came_back = False
            for step in all_team_transitions:
                if not step.get("is_real_team_hop"):
                    continue
                if step["from_focus"] and not step["to_focus"]:
                    saw_focus_exit = True
                elif saw_focus_exit and step["to_focus"] and not step["from_focus"]:
                    came_back = True
                    break

            # Count only real team-to-team hops for the bouncing threshold —
            # transitions where the Team field was cleared (to "(none)") or
            # set from null are bookkeeping, not cross-team movement.
            real_hop_count = sum(1 for s in all_team_transitions if s.get("is_real_team_hop"))
            base = {
                "key": t["key"],
                "summary": t.get("summary", ""),
                "reporter": t.get("reporter", ""),
                "transitions_count": real_hop_count,
                "raw_transitions_count": len(all_team_transitions),
                "path": path_str,
                "current_team": current_team or "(unrouted)",
                "total_bounce_span_calendar_hours": round(span_h, 1) if span_h is not None else None,
                "returned_to_focus": came_back,
            }
            if real_hop_count >= 3:
                bouncing.append(base)
            if came_back:
                returned_to_focus.append(base)

        # Engineer un-assigned: assignee → null after being non-null.
        seen_assigned = False
        for entry in cl:
            entry_dt = parse_dt(entry.get("created"))
            for item in entry.get("items", []) or []:
                if item.get("field") != "assignee":
                    continue
                fr = (item.get("from_string") or "").strip()
                to = (item.get("to_string") or "").strip()
                if fr and not to:
                    if seen_assigned or first_assign_dt:  # only count if we know they were assigned at some point
                        ah = adjusted_hours_between(created_dt, entry_dt)
                        engineer_unassigned.append({
                            "key": t["key"],
                            "summary": t.get("summary", ""),
                            "engineer": fr,
                            "hours": round(ah, 1) if ah is not None else None,
                        })
                        break
                if not fr and to:
                    seen_assigned = True
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

    # Enrich each reassign_out item with the ticket's total Team-transition
    # count so the report can flag "moved 1 time" vs "moved 4 times" inline
    # without re-walking the changelog.
    for rec in reassign_out:
        rec["transitions_count"] = transitions_count_by_key.get(rec["key"], 1)
    # Sort bouncing tickets by transitions desc so the noisiest land at the top.
    bouncing.sort(key=lambda r: r["transitions_count"], reverse=True)
    returned_to_focus.sort(key=lambda r: r["transitions_count"], reverse=True)

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
        "engineer_unassigned": {
            "count": len(engineer_unassigned),
            "items": engineer_unassigned,
        },
        "wont_do": {
            "count": len(wont_do),
            "items": wont_do,
        },
        "bouncing": {
            "count": len(bouncing),
            "items": bouncing,
        },
        "returned_to_focus": {
            "count": len(returned_to_focus),
            "items": returned_to_focus,
        },
    }


def _created_in_window(t, window_start_dt, window_end_dt):
    cdt = parse_dt(t.get("created"))
    return cdt is not None and window_start_dt <= cdt <= window_end_dt


def compute_breakdowns(in_window, prior_window=None):
    """Count tickets by priority/component/label/reporter/status. If
    `prior_window` is provided, each row also carries `prior_count`,
    `delta_pct`, and `is_new` so report.py can render a Δ-vs-prior column.

    Delta semantics:
      - prior_count > 0   → percentage delta vs prior, is_new=False
      - prior_count == 0  → delta_pct=None, is_new=True (renders as 'new')
      - prior_window None → fields omitted entirely (snapshot mode, v2 shape)
    """
    def counts(tickets, key_extractor):
        c = Counter()
        for t in tickets:
            for k in key_extractor(t):
                c[k] += 1
        return c

    def by_priority(t):
        return [t.get("priority") or "None"]

    def by_component(t):
        comps = t.get("components") or []
        return comps if comps else ["(no component)"]

    def by_label(t):
        lbls = t.get("labels") or []
        return lbls if lbls else []

    def by_reporter(t):
        return [t.get("reporter") or "(unknown)"]

    def by_status(t):
        return [t.get("status") or "(unknown)"]

    has_prior = prior_window is not None

    def make_breakdown(extractor, top=10):
        cur = counts(in_window, extractor)
        prior = counts(prior_window, extractor) if has_prior else Counter()
        rows = []
        for name, count in cur.most_common(top):
            row = {
                "name": name,
                "count": count,
                "share_pct": round(100.0 * count / max(len(in_window), 1), 1),
            }
            if has_prior:
                prior_count = prior.get(name, 0)
                row["prior_count"] = prior_count
                if prior_count > 0:
                    row["delta_pct"] = round(100.0 * (count - prior_count) / prior_count, 1)
                    row["is_new"] = False
                else:
                    row["delta_pct"] = None
                    row["is_new"] = True
            rows.append(row)
        return rows

    return {
        "priority": make_breakdown(by_priority),
        "component": make_breakdown(by_component),
        "label": make_breakdown(by_label),
        "reporter": make_breakdown(by_reporter),
        "status_at_end": make_breakdown(by_status),
    }


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


def _safe_pp_delta(current_rate, prior_rate):
    """Percentage-point delta for a 0.0–1.0 rate. None-safe."""
    if current_rate is None or prior_rate is None:
        return None
    return round(100.0 * (current_rate - prior_rate), 1)


def compute_l1_deltas(current_l1, prior_l1):
    """Compute structured deltas between two L2-signal dicts.

    Returns None if `prior_l1` is None. Every cell is None-safe so report.py
    can render '—' for unmeasurable values. Rate fields produce
    percentage-point deltas (`pp_delta`); count and hour fields produce
    absolute (`abs_delta`) and percentage (`pct_delta`) deltas.
    """
    if prior_l1 is None:
        return None

    cf = (current_l1 or {}).get("first_assignment") or {}
    pf = (prior_l1 or {}).get("first_assignment") or {}

    def hours_delta(cur_h, prior_h):
        if cur_h is None or prior_h is None:
            return {"current": cur_h, "prior": prior_h, "abs_delta": None, "pct_delta": None}
        return {
            "current": cur_h,
            "prior": prior_h,
            "abs_delta": round(cur_h - prior_h, 1),
            "pct_delta": _safe_delta_pct(cur_h, prior_h),
        }

    def _walk(d, path):
        cur = d
        for k in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur

    def count_delta(path):
        # Distinguish "key absent on prior" (signal didn't exist yet — abs_delta
        # is meaningless) from "measured zero" (signal existed and was 0 —
        # abs_delta is the absolute count). The old code coerced both to 0,
        # making a current=5 / missing-prior look like "+5" rather than
        # "no comparable prior".
        c_raw = _walk(current_l1, path)
        p_raw = _walk(prior_l1, path)
        c = c_raw or 0
        p = p_raw or 0
        return {
            "current": c,
            "prior": p,
            "prior_present": p_raw is not None,
            "abs_delta": (c - p) if p_raw is not None else None,
            "pct_delta": _safe_delta_pct(c, p) if p_raw is not None else None,
        }

    def rate_delta(path):
        c = _walk(current_l1, path)
        p = _walk(prior_l1, path)
        return {
            "current": c,
            "prior": p,
            "pp_delta": _safe_pp_delta(c, p),
        }

    return {
        "median_adjusted_hours": hours_delta(cf.get("median_adjusted_hours"), pf.get("median_adjusted_hours")),
        "p90_adjusted_hours": hours_delta(cf.get("p90_adjusted_hours"), pf.get("p90_adjusted_hours")),
        "never_assigned_count": count_delta(["first_assignment", "never_assigned_count"]),
        "reopen_rate": rate_delta(["reopen", "rate"]),
        "reopen_count": count_delta(["reopen", "count"]),
        "quick_close_rate": rate_delta(["quick_close", "rate"]),
        "quick_close_count": count_delta(["quick_close", "count"]),
        "reassign_out_count": count_delta(["reassign_out", "count"]),
        "reassign_out_24h_calendar_count": count_delta(["reassign_out", "fast_bounce_24h_calendar_count"]),
        "engineer_unassigned_count": count_delta(["engineer_unassigned", "count"]),
        "wont_do_count": count_delta(["wont_do", "count"]),
    }


def _canonical_team_name(name, focus_aliases, canonical):
    """If `name` (case-insensitive) matches any focus alias, return the
    canonical focus-team name; otherwise return `name` unchanged.

    Used to merge renamed-team aliases (e.g. SECO + ACE → ACE) so historical
    intake rows don't double-count the same logical team across windows.
    """
    if not name:
        return name
    if name.lower() in focus_aliases and canonical:
        return canonical
    return name


def compute_intake_share(intake_records, focus_team_labels=None, focus_canonical=None, top=10):
    """Aggregate the whole-support-project intake set by FIRST routed team.

    Returns a list of rows sorted by count desc:
      [{"team": "...", "count": N, "share_pct": ..., "is_focus": bool}]

    `focus_team_labels` (set of strings) is matched case-insensitively to:
      (a) merge any matching first-team values into `focus_canonical` so a
          renamed/aliased team is counted as one row, and
      (b) flag the resulting row as `is_focus` for table highlighting.
    """
    focus = {x.lower() for x in (focus_team_labels or set())}
    counts = Counter()
    for r in (intake_records or []):
        team = (r.get("first_team") or "(unrouted)").strip() or "(unrouted)"
        team = _canonical_team_name(team, focus, focus_canonical)
        counts[team] += 1
    total = sum(counts.values())
    canonical_lower = (focus_canonical or "").lower()
    rows = []
    for name, count in counts.most_common(top):
        rows.append({
            "team": name,
            "count": count,
            "share_pct": round(100.0 * count / max(total, 1), 1),
            "is_focus": name.lower() == canonical_lower or name.lower() in focus,
        })
    return rows, total


def compute_routing_share_with_prior(current_intake, prior_intake, focus_team_labels=None, focus_canonical=None, top=10):
    """Compute routing-share rows with prior_count + delta_pp + is_new.

    Uses percentage-point delta on share (not absolute count) since denominators
    differ across windows. A row is `is_new` when the team had 0 prior tickets.
    """
    cur_rows, cur_total = compute_intake_share(current_intake, focus_team_labels, focus_canonical, top=top)
    if not prior_intake:
        return {"rows": cur_rows, "current_total": cur_total, "prior_total": None}
    prior_rows, prior_total = compute_intake_share(prior_intake, focus_team_labels, focus_canonical, top=999)  # all teams for lookup
    prior_share_by_team = {r["team"]: r["share_pct"] for r in prior_rows}
    prior_count_by_team = {r["team"]: r["count"] for r in prior_rows}
    for row in cur_rows:
        prior_share = prior_share_by_team.get(row["team"])
        prior_count = prior_count_by_team.get(row["team"], 0)
        row["prior_count"] = prior_count
        row["prior_share_pct"] = prior_share
        if prior_share is None:
            row["pp_delta"] = None
            row["is_new"] = True
        else:
            row["pp_delta"] = round(row["share_pct"] - prior_share, 1)
            row["is_new"] = False
    return {"rows": cur_rows, "current_total": cur_total, "prior_total": prior_total}


def compute_routing_flow(in_team_tickets, focus_team_labels, focus_canonical=None):
    """Compute from-team→to-team flows for tickets where the team field
    transitioned, restricted to tickets that touched the focus team.

    Returns:
      {
        "from_focus": {to_team: count, ...},  # focus → other (handed off)
        "to_focus":   {from_team: count, ...} # other → focus (received)
      }

    `focus_team_labels` aliases (e.g. SECO + ACE for the same team) are
    treated as the focus team — transitions BETWEEN aliases are skipped (they
    represent renames, not real handoffs). Other-team values are also
    canonicalised so a transition to `Seco` is reported as a transition to
    `ACE` if those are aliases.
    """
    focus = {x.lower() for x in (focus_team_labels or set())}
    from_focus = Counter()
    to_focus = Counter()
    for t in (in_team_tickets or []):
        for entry in (t.get("changelog") or []):
            for item in entry.get("items", []) or []:
                if (item.get("field") or "").lower() != "team":
                    continue
                fr = (item.get("from_string") or "").strip() or "(none)"
                to = (item.get("to_string") or "").strip() or "(none)"
                fr_focus = fr.lower() in focus
                to_focus_match = to.lower() in focus
                if fr_focus and to_focus_match:
                    # Alias-to-alias transition (e.g. SECO → ACE) — same team,
                    # not a real handoff. Skip.
                    continue
                if fr_focus and not to_focus_match:
                    from_focus[_canonical_team_name(to, focus, focus_canonical)] += 1
                elif to_focus_match and not fr_focus:
                    to_focus[_canonical_team_name(fr, focus, focus_canonical)] += 1
    return {
        "from_focus": dict(from_focus.most_common()),
        "to_focus": dict(to_focus.most_common()),
    }


# Issuetypes that count as "engineering bug" outcomes vs "non-bug" outcomes
# when a support ticket is converted to engineering work. Keep this grouping
# coarse — leadership cares about bug-vs-not-bug, not Story-vs-Task minutiae.
_BUG_ISSUETYPES = {"bug"}
_NONBUG_ENG_ISSUETYPES = {"story", "task", "technical story", "spike", "documentation", "code", "sub-task"}


def compute_bug_vs_other(in_window_tickets, linked_issue_types):
    """For each in-window support ticket, classify the engineering-side
    outcome by looking at issues linked from it.

    Returns a per-ticket aggregate dict:
      {
        "ticket_count": N,                       # in-window support tickets
        "tickets_with_links": N,                 # have ≥1 linked engineering issue
        "tickets_without_links": N,
        "by_classification": {
          "bug_with_code": N,    # ≥1 Bug-typed link with code-change evidence
          "bug_no_code": N,      # ≥1 Bug-typed link, none with code evidence
          "non_bug": N,          # only non-bug eng links
          "external_or_other": N,
        },
        "issuetype_counts": {issuetype: count, ...},  # raw distribution
      }

    Per-ticket classification rule:
      - `bug_with_code` if any Bug-typed link has has_code_change=True
      - `bug_no_code`   if a Bug-typed link exists but none have code-change evidence
      - `non_bug`       otherwise, if any non-bug eng link exists
      - `external_or_other` otherwise (linked support tickets, no eng link)
    """
    issuetype_counts = Counter()
    classification_counts = Counter()
    tickets_with_links = 0
    for t in (in_window_tickets or []):
        links = t.get("linked_issues") or []
        if not links:
            continue
        eng_types_for_ticket = []
        bug_link_has_code = []  # one bool per Bug-typed link
        for ln in links:
            type_info = linked_issue_types.get(ln["key"]) or {}
            it = type_info.get("issuetype") or ln.get("type", "")
            if not it:
                continue
            it_low = it.lower().strip()
            issuetype_counts[it] += 1
            eng_types_for_ticket.append(it_low)
            if it_low in _BUG_ISSUETYPES:
                # has_code_change may be None for old data shapes; treat as
                # False (no evidence found) rather than crash.
                bug_link_has_code.append(bool(type_info.get("has_code_change")))
        if not eng_types_for_ticket:
            continue
        tickets_with_links += 1
        if bug_link_has_code:
            if any(bug_link_has_code):
                classification_counts["bug_with_code"] += 1
            else:
                classification_counts["bug_no_code"] += 1
        elif any(it in _NONBUG_ENG_ISSUETYPES for it in eng_types_for_ticket):
            classification_counts["non_bug"] += 1
        else:
            classification_counts["external_or_other"] += 1
    total = len(in_window_tickets or [])
    return {
        "ticket_count": total,
        "tickets_with_links": tickets_with_links,
        "tickets_without_links": total - tickets_with_links,
        "by_classification": {
            "bug_with_code": classification_counts.get("bug_with_code", 0),
            "bug_no_code": classification_counts.get("bug_no_code", 0),
            "non_bug": classification_counts.get("non_bug", 0),
            "external_or_other": classification_counts.get("external_or_other", 0),
        },
        "issuetype_counts": dict(issuetype_counts.most_common()),
    }


def compute_bug_vs_other_with_prior(current_bvo, prior_bvo):
    """Add prior + delta to the bug-vs-other dict. Δ is percentage-point on
    bug-share-of-tickets-with-links (so windows of different size compare).

    `bug_share_pp_delta` reports the cleaned signal: share of `bug_with_code`
    only, NOT total Bug-typed links — the latter is sensitive to intake
    labelling habits (config requests / feature gaps logged as Bug). The raw
    "Bug ticket-type share" is still available via `current_bug_typed_share_pct`
    for cross-checking.
    """
    if prior_bvo is None:
        return {"current": current_bvo, "prior": None, "deltas": None}

    def _share_of_with_links(b, key):
        n = b.get("tickets_with_links", 0)
        if not n:
            return None
        return 100.0 * b.get("by_classification", {}).get(key, 0) / n

    def _bug_typed_share(b):
        # Combined bug_with_code + bug_no_code — what the previous report
        # showed. Kept for cross-reference only.
        n = b.get("tickets_with_links", 0)
        if not n:
            return None
        cls = b.get("by_classification") or {}
        return 100.0 * (cls.get("bug_with_code", 0) + cls.get("bug_no_code", 0)) / n

    cur_share = _share_of_with_links(current_bvo, "bug_with_code")
    prior_share = _share_of_with_links(prior_bvo, "bug_with_code")
    cur_typed = _bug_typed_share(current_bvo)
    prior_typed = _bug_typed_share(prior_bvo)

    deltas = {
        "bug_count_abs_delta": current_bvo["by_classification"].get("bug_with_code", 0)
                               - prior_bvo["by_classification"].get("bug_with_code", 0),
        "bug_share_pp_delta": (round(cur_share - prior_share, 1)
                               if (cur_share is not None and prior_share is not None) else None),
        "current_bug_share_pct": round(cur_share, 1) if cur_share is not None else None,
        "prior_bug_share_pct": round(prior_share, 1) if prior_share is not None else None,
        # Cross-reference: the noisier "any Bug-typed link" share.
        "current_bug_typed_share_pct": round(cur_typed, 1) if cur_typed is not None else None,
        "prior_bug_typed_share_pct": round(prior_typed, 1) if prior_typed is not None else None,
    }
    return {"current": current_bvo, "prior": prior_bvo, "deltas": deltas}


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


def analyze_window(data, bucket_choice, prior_in_window_tickets=None):
    """Analyze one window's `data.json`-shape dict. Returns the analysis dict:
    window/totals/buckets/breakdowns/l1_signals/routing_flow/bug_vs_other.

    `prior_in_window_tickets` (optional) is a list of normalized prior-window
    in-window tickets; when supplied, breakdown rows carry prior_count + delta_pct."""
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
    # historical aliases (e.g. "ACE,SECO" — SECO is the prior name).
    team_field_canonical = team_field_list[0] if team_field_list else None

    in_window = [t for t in tickets if _created_in_window(t, window_start_dt, window_end_dt)]

    bucket_rows = compute_buckets_view(tickets, backlog_open_at_start, start, end, bucket)
    breakdowns = compute_breakdowns(in_window, prior_window=prior_in_window_tickets)
    l1 = compute_l1_signals(tickets, window_start_dt, window_end_dt, team_uuids, team_field_labels, team_field_canonical=team_field_canonical)
    resolution_categories = compute_resolution_category_breakdown(tickets, window_start_dt, window_end_dt)

    # Routing-flow (per-ticket from→to / to→from for the focus team).
    routing_flow = compute_routing_flow(tickets, team_field_labels, focus_canonical=team_field_canonical)

    # Bug-vs-other classification of in-window support tickets via linked issuetypes.
    linked_issue_types = data.get("linked_issue_types") or {}
    bug_vs_other = compute_bug_vs_other(in_window, linked_issue_types)

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
        "breakdowns": breakdowns,
        "l1_signals": l1,
        "routing_flow": routing_flow,
        "bug_vs_other": bug_vs_other,
        "resolution_categories": resolution_categories,
        "team_field_labels": sorted(team_field_labels),
        "team_field_canonical": team_field_canonical,
        "_in_window_tickets": in_window,  # internal — used to wire prior into compute_breakdowns
        "_intake_records": data.get("intake_all_teams") or [],  # internal — wired into routing-share at top level
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
    cur_breakdowns = current.get("breakdowns") or {}
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
            findings.append(_finding(
                kind="volume_change",
                claim="In-team ticket volume %s %.0f%% vs prior window" % (
                    "up" if delta_pct >= 0 else "down", abs(delta_pct)),
                metric=_arrow(prior_created, cur_created),
                evidence_keys=[],  # whole-window claim — no per-ticket evidence
                severity=_severity_from_pct(abs(delta_pct), cfg),
                audience_hint="exec",
                delta_pct=delta_pct,
            ))

    # --- volume_spike_by_component ---
    if have_prior:
        cfg = thresholds.COMPONENT_SPIKE
        for row in (cur_breakdowns.get("components") or []):
            cur_n = row.get("count") or 0
            prior_n = row.get("prior_count")
            delta_pct = row.get("delta_pct")
            if (
                cur_n >= cfg["abs"]
                and (prior_n or 0) >= cfg["prior_floor"]
                and delta_pct is not None
                and abs(delta_pct) >= cfg["pct"]
            ):
                findings.append(_finding(
                    kind="volume_spike_by_component",
                    claim="Component '%s' volume %s %.0f%% vs prior" % (
                        row.get("name", "(unknown)"),
                        "up" if delta_pct >= 0 else "down",
                        abs(delta_pct)),
                    metric=_arrow(prior_n or 0, cur_n),
                    evidence_keys=row.get("keys", [])[:20],
                    severity="medium",
                    audience_hint="exec",
                    delta_pct=delta_pct,
                ))

    # --- defect_rate_change ---
    if have_prior:
        cfg = thresholds.DEFECT_RATE
        bvo_combined = current.get("bug_vs_other_combined") or {}
        bvo_deltas = bvo_combined.get("deltas") or {}
        bug_pp = bvo_deltas.get("bug_share_pp")
        bug_abs_delta = bvo_deltas.get("bug_count_abs_delta")
        cur_bug = bvo_combined.get("current", {}).get("bug_count", 0)
        prior_bug = bvo_combined.get("prior", {}).get("bug_count", 0)
        triggers_pp = bug_pp is not None and abs(bug_pp) >= cfg["pp"]
        triggers_abs = (bug_abs_delta is not None and abs(bug_abs_delta) >= cfg["abs_delta"]
                        and cur_bug >= cfg["abs_floor"])
        if triggers_pp or triggers_abs:
            findings.append(_finding(
                kind="defect_rate_change",
                claim="Bug-share of in-window tickets %s vs prior" % (
                    "up" if (bug_pp or 0) >= 0 else "down"),
                metric=_arrow(prior_bug, cur_bug),
                evidence_keys=(bvo_combined.get("current") or {}).get("bug_keys", [])[:20],
                severity="medium",
                audience_hint="exec",
                bug_share_pp=bug_pp,
            ))

    # --- priority_mix_shift ---
    if have_prior:
        cfg = thresholds.PRIORITY_MIX
        # Priority breakdowns expose per-row prior counts; sum highest+high
        # share both windows and compare.
        def _hi_share(rows):
            total = sum((r.get("count") or 0) for r in rows)
            hi = sum((r.get("count") or 0) for r in rows
                     if r.get("name", "").lower() in ("highest", "high"))
            return (hi, total, (100.0 * hi / total) if total else 0.0)
        cur_pri = cur_breakdowns.get("priorities") or []
        prior_pri = ((prior or {}).get("breakdowns") or {}).get("priorities") or []
        cur_hi, cur_total, cur_share = _hi_share(cur_pri)
        prior_hi, prior_total, prior_share = _hi_share(prior_pri)
        pp_delta = cur_share - prior_share
        if cur_hi >= cfg["abs_floor"] and abs(pp_delta) >= cfg["pp"]:
            cur_keys = [k for r in cur_pri
                        if r.get("name", "").lower() in ("highest", "high")
                        for k in (r.get("keys") or [])][:20]
            findings.append(_finding(
                kind="priority_mix_shift",
                claim="Highest+High priority share %s %.1fpp vs prior" % (
                    "up" if pp_delta >= 0 else "down", abs(pp_delta)),
                metric="%.0f%% → %.0f%%" % (prior_share, cur_share),
                evidence_keys=cur_keys,
                severity="medium",
                audience_hint="exec",
                pp_delta=pp_delta,
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

    # --- Cross-finding merge: component spike subsumes team-level volume_change ---
    # A team-level volume_change finding plus a parallel volume_spike_by_component
    # finding for the same window are usually two angles on the same underlying
    # signal — the synthesise agent is supposed to merge them, but agent
    # judgement is inconsistent run-to-run. Do the merge in code: if any single
    # component's absolute delta accounts for at least the threshold share of
    # the team-level absolute delta, suppress volume_change and tag the
    # component finding with `also_explains_team_volume: true`.
    if have_prior:
        share_cfg = thresholds.COMPONENT_EXPLAINS_TEAM_VOLUME
        team_abs_delta = abs(
            (cur_totals.get("created_in_window") or 0)
            - (prior_totals.get("created_in_window") or 0)
        )
        if team_abs_delta > 0:
            comp_findings = [f for f in findings if f["kind"] == "volume_spike_by_component"]
            for cf in comp_findings:
                # The component finding's metric is "{prior} → {current}" — derive
                # absolute delta from the component breakdown rows we already
                # walked, by re-matching the component name out of the claim.
                # Cheap and avoids stashing extra state on the finding.
                m = re.search(r"Component '([^']+)'", cf.get("claim", ""))
                if not m:
                    continue
                name = m.group(1)
                row = next((r for r in (cur_breakdowns.get("components") or [])
                            if r.get("name") == name), None)
                if row is None:
                    continue
                comp_abs_delta = abs(
                    (row.get("count") or 0) - (row.get("prior_count") or 0))
                if comp_abs_delta / team_abs_delta >= share_cfg["share"]:
                    cf["also_explains_team_volume"] = True
                    cf["explains_team_volume_share"] = round(
                        comp_abs_delta / team_abs_delta, 2)
                    # Drop the team-level volume_change finding — its story is
                    # now told by the component spike, with provenance.
                    findings = [f for f in findings if f["kind"] != "volume_change"]
                    # Keep the loop running so multiple co-firing component
                    # spikes get tagged, but only one removal of volume_change
                    # is needed.
            # If any tag fired, re-emit so subsequent additions to derive_findings
            # don't accidentally re-add volume_change.

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

    # Compute prior first (if present) so we can pass its in-window ticket set
    # into compute_breakdowns for the current window.
    prior_analysis = None
    prior_in_window = None
    if prior_data is not None:
        prior_analysis = analyze_window(prior_data, args.bucket)
        prior_in_window = prior_analysis.pop("_in_window_tickets", None)

    current_analysis = analyze_window(
        data, args.bucket,
        prior_in_window_tickets=prior_in_window,
    )
    current_analysis.pop("_in_window_tickets", None)

    # Routing-share + bug-vs-other Δ are wired here at the top level so they
    # can compare current vs prior intake/linked-issue datasets.
    cur_intake = current_analysis.pop("_intake_records", [])
    prior_intake = (prior_analysis or {}).pop("_intake_records", []) if prior_analysis else []
    focus_labels = set(current_analysis.get("team_field_labels") or [])
    focus_canonical = current_analysis.get("team_field_canonical")

    routing_share = compute_routing_share_with_prior(
        cur_intake, prior_intake if prior_analysis is not None else None,
        focus_team_labels=focus_labels,
        focus_canonical=focus_canonical)
    current_analysis["routing_share"] = routing_share

    if prior_analysis is not None:
        # Stash the prior routing-flow + bug_vs_other so report.py can render Δ
        # without re-running the analysis.
        prior_routing_flow = prior_analysis.get("routing_flow")
        prior_bug_vs_other = prior_analysis.get("bug_vs_other")
    else:
        prior_routing_flow = None
        prior_bug_vs_other = None

    bvo_combined = compute_bug_vs_other_with_prior(
        current_analysis.get("bug_vs_other"), prior_bug_vs_other)
    current_analysis["bug_vs_other_combined"] = bvo_combined

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
            "l1_signals": compute_l1_deltas(
                current_analysis.get("l1_signals"),
                prior_analysis.get("l1_signals")),
            "routing_flow_prior": prior_routing_flow,
            "bug_vs_other": bvo_combined.get("deltas"),
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
