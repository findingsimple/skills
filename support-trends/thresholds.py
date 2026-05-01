"""Shared thresholds — single source of truth for cross-file constants.

Two classes of constant live here:

1. **Pair-locked thresholds** — `report.discussion_prompts` and
   `build_synthesis_prompt` MUST agree on these. The synthesis sub-agent only
   produces grounded recommendations for signals that cross a threshold; if
   the synthesis-side constant says "fire at +20% volume" but the report-side
   says "fire at +25%", the synthesis bundle ships evidence for a signal the
   report won't render (or the report renders a question-form bullet for a
   signal the synthesis agent didn't see, which falls back to the un-helpful
   default). Drift here is silent and material.

2. **Single-call-site display thresholds** — used only by `report.py` for
   per-row formatting (e.g. small-base markers). Lifted here for visibility
   so future readers know they exist and where to tune them.

Threshold meanings are documented inline. When changing a value, search for
all readers across `report.py` and `build_synthesis_prompt.py`.
"""

# ---- Pair-locked: must agree across report.py + build_synthesis_prompt.py ----

# Volume acceleration: fires when current.created / prior.created exceeds the
# percentage threshold AND current.created is at least the absolute floor.
# Absolute floor prevents tiny windows from triggering on noise.
VOLUME_PCT_THRESHOLD = 20.0
VOLUME_ABS_THRESHOLD = 20

# Per-component spike: fires when a single component's count grew by ≥ pct
# AND has at least the absolute floor of tickets in current.
COMPONENT_SPIKE_PCT = 50.0
COMPONENT_SPIKE_ABS = 5

# L2 quality regression: rate-shaped signals fire when the percentage-point
# delta vs prior reaches this. Applies to reopen rate and quick-close rate.
L2_PP_THRESHOLD = 5.0

# Won't Do / Cannot Reproduce / Duplicate concentration: count must have at
# least doubled vs prior AND reached the absolute floor.
WONT_DO_RATIO = 2.0
WONT_DO_ABS = 3

# Cross-team routing signals — count thresholds (apply identically in report
# and synthesis). Reassign-out, engineer-unassigned, bouncing, returned-to-
# focus all use the same "≥3 events" bar.
REASSIGN_OUT_THRESHOLD = 3
ENG_UNASSIGNED_THRESHOLD = 3
BOUNCING_THRESHOLD = 3
RETURNED_TO_FOCUS_THRESHOLD = 3

# Theme volume floor for coverage prompts (uncovered / backlog-only high-volume
# themes): only surface a "no triage coverage" or "stuck in backlog" prompt for
# a theme if its in-window count is at least this large. Keeps singletons and
# tiny themes out of the leadership conversation.
THEME_HIGH_VOLUME_FLOOR = 5

# Backlog-widening: net/created exceeds this fraction.
BACKLOG_PCT_THRESHOLD = 0.05

# Theme spike: a recurring theme is "spiking" vs prior if both:
#   - delta percentage reaches THEME_SPIKE_DELTA_PCT, AND
#   - prior count is at least THEME_SPIKE_PRIOR_FLOOR (avoid 1→3 noise).
# A second floor on absolute delta (THEME_SPIKE_DELTA_ABS) keeps the report
# from naming spikes that are "real" by % but trivial by count.
THEME_SPIKE_DELTA_PCT = 50.0
THEME_SPIKE_PRIOR_FLOOR = 3
THEME_SPIKE_DELTA_ABS = 4

# New-theme floor: a brand-new theme (no prior occurrences) needs at least
# this many tickets in-window before it earns a discussion prompt.
NEW_THEME_FLOOR = 5

# Bug-share rising: fires when bug-share pp delta reaches BUG_SHARE_PP, OR
# absolute bug-count delta reaches BUG_SHARE_ABS_DELTA AND current bug count
# reaches BUG_SHARE_ABS_FLOOR. The OR semantics handle two distinct shapes:
# share-shift (the pp), and absolute volume (the abs pair).
BUG_SHARE_PP = 5.0
BUG_SHARE_ABS_DELTA = 5
BUG_SHARE_ABS_FLOOR = 10

# ---- Display / formatting thresholds (report.py only) ----

# Δ% suffix "(small base)" attaches when prior < this. Cuts visual noise from
# 1→3 = +200% style cells.
SMALL_BASE_PRIOR_CUTOFF = 5

# Routing-share movement at the *focus team* level — confound flag in TL;DR.
ROUTING_SHIFT_CONFOUND_PP = 3.0
# Per-other-team intake drift used in routing-drift discussion prompts.
ROUTING_DRIFT_PP = 5.0

# Reporter concentration: name a reporter as "driving the growth" only when
# their delta meets the absolute floor AND owns at least the share fraction
# of the total volume delta.
REPORTER_CONCENTRATION_FLOOR = 5
REPORTER_CONCENTRATION_SHARE = 0.30

# Cross-surface customers: name customers in the TL;DR who appeared in this
# many distinct themes during the window.
REPEAT_CUSTOMER_THEME_FLOOR = 3

# Component "concentration" callout in priority/component breakdowns: a row
# is highlighted when count ≥ floor AND its share ≥ share floor.
COMPONENT_CONCENTRATION_COUNT_FLOOR = 5
COMPONENT_CONCENTRATION_SHARE_PCT = 25.0

# Charter "partial coverage" callout: only call out a partially-charter-owned
# theme if it has at least this many in-window tickets (avoids highlighting a
# 1-ticket theme as a routing problem).
CHARTER_PARTIAL_TICKET_FLOOR = 3
