"""Tunable thresholds for v2 deterministic findings.

Every finding kind emitted by `analyze.derive_findings()` reads its trigger
constants from the matching dict here. To make a finding fire more / less
often: edit the dict, no code changes elsewhere.

Conventions
-----------
- `pct`     — percentage change vs prior window (signed compare uses `abs()`)
- `pp`      — percentage-point change vs prior window
- `abs`     — absolute floor on the current-window count
- `prior_floor` — minimum prior-window count to consider the comparison meaningful
- `severity_high` / `severity_medium` — break thresholds for severity assignment;
  if absent, all triggered findings get `medium`

A finding kind with no prior-window data available (e.g. when --no-prior was
passed) silently skips its check rather than firing on incomplete data.

Display-only thresholds (small-base markers, concentration call-outs) live in
`DISPLAY` at the bottom — they affect the renderer, not whether a finding fires.
"""

# === Finding: volume_change ===
# Total in-team ticket volume vs prior window (created in window).
# Tune `pct` lower to be more sensitive; raise `abs` to suppress noise on tiny
# windows. `severity_high` triggers when the % change exceeds that threshold.
VOLUME_CHANGE = {
    "pct": 20.0,
    "abs": 20,
    "severity_high": 50.0,
}

# === Finding: volume_spike_by_component ===
# A single component's count grows or shrinks by `pct` AND has at least `abs`
# tickets in the current window AND `prior_floor` tickets in prior. Multiple
# components can fire simultaneously.
COMPONENT_SPIKE = {
    "pct": 50.0,
    "abs": 5,
    "prior_floor": 2,
}

# === Cross-finding merge: component spike explains team-level volume change ===
# When a single component's absolute delta accounts for at least `share` of the
# team-level absolute volume delta, suppress the parallel volume_change finding
# and mark the component spike with `also_explains_team_volume: true`. Stops
# the synthesise agent (and the report) from restating the same underlying
# signal twice. Lower the share to merge more aggressively; raise to merge
# only when a component is clearly the sole driver.
COMPONENT_EXPLAINS_TEAM_VOLUME = {
    "share": 0.60,
}

# === Finding: defect_rate_change ===
# Bug-share of in-window tickets shifts by `pp` percentage points OR absolute
# bug-count delta reaches `abs_delta` AND current bug count reaches `abs_floor`.
# OR semantics handle two distinct shapes — a share shift and a raw volume
# spike — that both warrant a finding.
DEFECT_RATE = {
    "pp": 5.0,
    "abs_delta": 5,
    "abs_floor": 10,
}

# === Finding: priority_mix_shift ===
# High+Highest priority share moves by `pp` percentage points vs prior. Only
# fires when the current count of high+highest is at least `abs_floor`.
PRIORITY_MIX = {
    "pp": 10.0,
    "abs_floor": 5,
}

# === Finding: time_to_engineer_regression ===
# Median adjusted-hours from ticket creation to first engineer assignment is
# up by `pct` vs prior AND now exceeds `abs_floor_hours`. Approximate calendar
# math (weekend-discounted, not real business hours) — see
# `analyze.adjusted_hours_between` for the caveat.
TIME_TO_ENGINEER = {
    "pct": 30.0,
    "abs_floor_hours": 24,
}

# === Finding: reopen_spike ===
# Reopened-ticket count or rate above the prior window. `pp` triggers on rate
# change; `abs` is the absolute count floor in current window.
REOPEN = {
    "pp": 5.0,
    "abs": 3,
}

# === Finding: quick_close_pattern ===
# Quick-closes (resolved < 4h, never assigned to anyone) — a "shouldn't have
# escalated" signal. Fires when current count is at least `abs` AND grew by
# `pp` percentage points of resolved-volume vs prior.
QUICK_CLOSE = {
    "pp": 5.0,
    "abs": 3,
}

# === Finding: reassign_out_burst ===
# Tickets that left this team via the cf[10600] / labels custom-field changelog
# during the window. `abs` is a flat count threshold (no prior comparison —
# even one bounce-out can warrant attention).
REASSIGN_OUT = {
    "abs": 3,
    "severity_high": 8,
}

# === Finding: never_do_rate ===
# Won't Do / Cannot Reproduce / Duplicate concentration vs prior. Fires when
# count grew by `ratio` AND the current count reaches `abs`. Distinct from
# l3_bounced_back: never-do covers the Jira `resolution` field; l3_bounced_back
# covers the resolution_category custom field.
NEVER_DO = {
    "ratio": 2.0,
    "abs": 3,
}

# === Finding: categorisation_blank ===
# Share of resolved-in-window tickets where resolution_category is blank.
# Unambiguous quality signal: L2 / engineers aren't filling the field. `pct`
# is the share of resolved tickets; `abs_resolved_floor` keeps tiny windows
# from triggering on a single missing entry.
CATEGORISATION_BLANK = {
    "pct": 20.0,
    "abs_resolved_floor": 10,
    "severity_high": 40.0,
}

# === Finding: l3_bounced_back ===
# Tickets engineering received but classified as "L3 Bounced" (i.e. sent back
# to L2 because not engineering's job). Pure routing-miss signal; no prior
# comparison needed — even a few of these is interesting.
L3_BOUNCED = {
    "abs": 3,
    "severity_high": 8,
}

# === Display thresholds (renderer only — do NOT affect findings) ===
DISPLAY = {
    # Δ% suffix "(small base)" attaches to a per-row delta when prior < this.
    # Cuts visual noise from "1→3 = +200%" cells.
    "small_base_prior_cutoff": 5,
    # In Numbers tables, a row gets a 'concentration' marker when count ≥ floor
    # AND its share of total ≥ share_pct.
    "concentration_count_floor": 5,
    "concentration_share_pct": 25.0,
}
