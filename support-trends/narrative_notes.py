"""Narrative notes — the pressure-release valve.

The v2 architecture says report.py emits zero new claims. Every line traces
back to either a deterministic finding (analyze.derive_findings) or an agent
finding (synthesise / themes / support-feedback). That trust contract is
load-bearing: it's what lets a reader trust the report.

But not every useful sentence in a monthly report is finding-shaped. Sometimes
the right thing to add is a single line of *context* — "this window overlaps
the late-December office-closure period, expect lower volume" — which has no
metric, no evidence_keys, and doesn't fit the 11-kind finding taxonomy.

Without an escape hatch, the path of least resistance for adding such a line
is to invent a finding kind for it (heavyweight) or stand up a sub-agent
(very heavyweight). Either choice grows the system in a direction it
shouldn't grow. Six months later you have 20 finding kinds, half of which
are really one-line notes.

This module is the pressure-release. analyze.py calls derive_narrative_notes()
and ships the result alongside `findings` in analysis.json. Each note carries
explicit `derived_from` provenance so the renderer (and any reader) can see
*why* the note appeared. Notes render as italic prose under "## Context" at
the top of the Findings section.

Add a new note generator when you have a recurring kind of contextual line
the reader should see. Don't add a generator for one-off observations — those
belong in the synthesise agent's `so_what` for the relevant finding.
"""

from datetime import date


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def derive_narrative_notes(current, prior, deltas):
    """Walk the analysis output and emit a list of narrative-note records.

    Each note: {kind, text, derived_from: [str, ...]}.
    `derived_from` is the provenance — which inputs the note was computed
    from. The renderer doesn't display it directly, but it's there for
    auditability ("where did this sentence come from?") and so any future
    rule-tightening can reason about which generator produced what.

    Generators are independent — each one decides whether to fire from the
    inputs it cares about. Order is preserved so the same inputs always
    produce the same notes in the same order.
    """
    notes = []
    notes.extend(_calendar_context_notes(current, prior))
    # Future generators land here, e.g.:
    #   notes.extend(_multi_month_trend_notes(...))
    #   notes.extend(_seasonal_baseline_notes(...))
    return notes


# ---------------------------------------------------------------------------
# Generator: calendar context
# ---------------------------------------------------------------------------

def _calendar_context_notes(current, prior):
    """Notes about known calendar events inside the window or its prior.

    Currently emits one kind: late-December office-closure overlap. The
    Dec 22 – Jan 5 period is when many customer offices are closed and
    support volume is materially depressed across the board. Window
    comparisons that straddle Dec/Jan (either the current window OR the
    prior window) will skew, and the reader needs to know that *before*
    interpreting volume findings.

    Other calendar generators (Easter, Thanksgiving, end-of-financial-year)
    are deliberately not implemented yet — keep this surface small until
    we see real reports where we wished we'd had the note.
    """
    notes = []
    cur_window = (current or {}).get("window") or {}
    prior_window = (prior or {}).get("window") or {}

    cur_overlap = _holiday_overlap_summary(cur_window.get("start"), cur_window.get("end"))
    if cur_overlap:
        notes.append({
            "kind": "calendar_context",
            "text": (
                "Window overlaps the late-December / early-January "
                "office-closure period (%s). Expect depressed volume from "
                "US/AU customer offices being closed; comparisons against "
                "non-holiday months will look artificially low."
            ) % cur_overlap,
            "derived_from": ["window.start", "window.end"],
        })

    prior_overlap = _holiday_overlap_summary(prior_window.get("start"), prior_window.get("end"))
    if prior_overlap and not cur_overlap:
        # Only emit the "prior window is the depressed one" framing if the
        # CURRENT window is non-holiday — otherwise the cur_overlap note
        # already conveys that comparisons are unreliable in either direction.
        notes.append({
            "kind": "calendar_context",
            "text": (
                "Prior window overlaps the late-December / early-January "
                "office-closure period (%s). Volume growth vs prior may "
                "overstate the real shift — prior baseline was depressed "
                "by customer office closures."
            ) % prior_overlap,
            "derived_from": ["prior_window.start", "prior_window.end"],
        })

    return notes


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def _holiday_overlap_summary(start_iso, end_iso):
    """Return a short string describing the overlap with the late-Dec /
    early-Jan office-closure period, or None if no overlap.

    The closure period spans Dec 22 of year Y to Jan 5 of year Y+1. A window
    can overlap at the start (Dec end of year Y), the end (early Jan year Y+1),
    or fully contain the block.
    """
    if not (start_iso and end_iso):
        return None
    try:
        d_start = date.fromisoformat(start_iso)
        d_end = date.fromisoformat(end_iso)
    except ValueError:
        return None

    # Build the candidate closure periods that could touch this window: the
    # one starting Dec 22 of (start year - 1) (catches a Jan window) and the
    # one starting Dec 22 of start year (catches a Dec or Dec/Jan window).
    candidates = [
        (date(d_start.year - 1, 12, 22), date(d_start.year, 1, 5)),
        (date(d_start.year, 12, 22), date(d_start.year + 1, 1, 5)),
    ]
    for h_start, h_end in candidates:
        overlap_start = max(d_start, h_start)
        overlap_end = min(d_end, h_end)
        if overlap_start <= overlap_end:
            days = (overlap_end - overlap_start).days + 1
            return "%s → %s, %d day%s" % (
                overlap_start.isoformat(),
                overlap_end.isoformat(),
                days,
                "" if days == 1 else "s",
            )
    return None
