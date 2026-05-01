# Support Trends — Output Template

`report.py` builds the markdown deterministically from `analysis.json`. This file documents the structure so future edits stay aligned.

## File path

```
{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/{end_year}/Support Trends {start} to {end}.md
```

## Frontmatter (YAML)

| Key | Source |
|-----|--------|
| `type` | constant `support-trends` |
| `team` | `[[{vault_dir}]]` (Obsidian wiki link) |
| `project_key` | `data.json#args.support_project_key` |
| `period_start` / `period_end` | `analysis.json#window.start` / `.end` |
| `window_days` | `.days` |
| `bucket` | `daily` / `weekly` / `monthly` |
| `total_created` / `total_resolved` / `net` | `analysis.json#totals.*` |
| `backlog_at_start` / `backlog_at_end` | `analysis.json#totals.backlog_*` |
| `top_components` | first 5 component names from `breakdowns.component` |
| `top_themes` | first 5 theme IDs (highest `count_current` first) when themes ran |
| `theme_count` | size of the theme vocabulary (when themes ran) |
| `l1_first_assignment_median_adjusted_h` | `l1_signals.first_assignment.median_adjusted_hours` |
| `l1_never_assigned_count` | `l1_signals.first_assignment.never_assigned_count` |
| `l1_reopen_rate` | 0.0–1.0 |
| `l1_quick_close_rate` | 0.0–1.0 |
| `l1_reassign_out_count` / `l1_reassign_out_24h_calendar_count` | counts (the 24h subset uses **calendar** hours, not adjusted) |
| `l1_bouncing_count` / `l1_returned_to_focus_count` | counts of multi-hop tickets and returned handoffs |
| `l1_engineer_unassigned_count` | count |
| `l1_wont_do_count` | count |
| `bug_with_code_count` / `bug_no_code_count` / `non_bug_eng_count` / `external_or_other_count` | classification of each in-window ticket by linked engineering issuetype + presence of a code-change link |
| `current_bug_share_pct` / `prior_bug_share_pct` / `bug_share_pp_delta` | bug-share fraction of tickets-with-engineering-links + percentage-point shift vs prior |
| `intake_total_current` / `intake_total_prior` | whole-support-project ticket counts (denominator for intake share) |
| `theme_coverage_total` / `theme_coverage_triage_in_flight` / `theme_coverage_partially_covered` / `theme_coverage_adjacent_only` / `theme_coverage_uncovered` | counts from the themes ↔ root-cause-triage cross-reference |
| `pms_sync_unique_count` / `pms_sync_unique_share_pct` | unique tickets touching any `pms-sync-*` theme (used for the PMS sync headline) |
| `prior_window_start` / `prior_window_end` | only present when prior comparison is enabled (`--no-prior` omitted) |
| `prior_total_created` / `prior_total_resolved` / `prior_net` | counts from the prior window; omitted when `--no-prior` |
| `created_delta_pct` / `resolved_delta_pct` | percentage delta vs prior window (float, signed); omitted when prior data is absent or prior count was 0 |
| `generated` | ISO 8601 UTC at render time |
| `tags` | `[support, trends]` |

## Sections (in order)

1. **Window** — period, bucket, backlog start/end, totals. When prior is enabled, includes a one-line "Vs prior N days (start → end): Created X (vs Y, ±Z%) · Resolved … · Net widened/narrowed from … to …" line.
2. **Volume by {bucket}** — table of (label, created, resolved, net, backlog_end). **Bucket-level prior comparison is intentionally out of scope** — aligning prior-window buckets with current-window buckets is awkward for monthly windows of unequal calendar lengths. The window summary one-liner carries the prior comparison instead.
3. **Priority breakdown** — count + share. When prior is enabled, adds **Prior** + **Δ%** columns. Names appearing in current but not prior render `Prior=0`, `Δ=new`. None-safe — un-computable deltas render `—`.
4. **Component breakdown (top 10)** — count + share, plus Prior + Δ% when prior enabled.
5. **Label breakdown (top 10)** — count + share, plus Prior + Δ% when prior enabled.
6. **Status at end of window** — count + share, plus Prior + Δ% when prior enabled.
3. **Component breakdown (top 10)** — count + share, plus Prior + Δ% when prior enabled. Kept as a standalone section because component is the most leadership-actionable cut.
4. **Recurring themes** — sub-agent-tagged kebab-case theme IDs with definitions, current count + Δ vs prior, micro-summaries, ticket samples, and the themes ↔ root-cause-triage coverage state per theme. **Themes are LLM-tagged**; vocabulary persists across runs via `.themes_vocabulary.json` in the team's vault.
8. **Where support tickets are routed (intake share)** — top-10 teams by share of *whole-support-project* intake in the window, using the FIRST non-null Team-field value from the changelog (i.e. where the ticket landed at intake, not where it ended up). With prior, adds **Prior count**, **Prior share**, **Δpp**. The focus team's row is bolded. Footer line states whole-project intake totals so the denominator is visible.
9. **Routing flow for {team}** — handoffs OUT (focus team → other team) and handoffs IN (other team → focus team) computed from Team-field changelog transitions on tickets that touched the focus team. Two stacked tables; with prior each row gains **Prior** + **Δ**. The `(none)` row in the IN table is filtered out — it represents tickets created without a Team field, not real handoffs.
10. **Bug vs other engineering outcomes** — each in-window support ticket classified by whether issues linked from it include any `Bug` (counted as `Bug`), any non-bug engineering work like Story / Task / Tech Story / Spike / Documentation (counted as `Non-bug engineering`), or only external/support links / no engineering link (counted as `External / other`). With prior, adds **Prior count**, **Prior share**, **Δpp share** (percentage-point shift on share-of-tickets-with-links). Footer line states the linked / unlinked split + the bug-share Δ. Demoted to appendix unless `bug_share_pp_delta` crosses a threshold.
11. **L2 / triage quality signals** — single signal table. When prior is enabled, each row gains **Prior** + **Δ** cells. Rate fields render Δ as percentage points (`+4.5pp`); count fields render Δ as integer + percent; hour fields render Δ as absolute hours + percent. None-safe.
12. Per-signal lists (only rendered if non-empty):
    - Reassigned out of team
    - Engineer un-assigned
    - Quick-closed by L2 (no engineering touch)
    - Closed as Won't Do / Cannot Reproduce / Duplicate
13. **Discussion prompts for engineering leadership** — emitted **immediately after the Window summary** (not at the bottom of the report). 1–8 bullets generated by `discussion_prompts()` in `report.py`. Each bullet starts with a deterministic *Observation* (cites a number visible elsewhere in the report). When the synthesis sub-agent ran (default; opt out via `--no-prompt-synthesis`), enriched bullets carry nested *Likely driver* (with Jira evidence links), *Suggested action*, *Confidence*, and *Ask the team* lines grounded in the description + comments of evidence tickets. Without synthesis, bullets fall back to question-form. Bullets whose synthesis returned `confidence: low` AND no grounded evidence are suppressed rather than printed as "absence-of-evidence" prompts. A blockquote caveat above the list flags the AI-generated lines as starting hypotheses, not verdicts.
14. **Reference tables (appendix)** — Priority, Label, Status, and Top L2 staff breakdowns. These are status mirrors / deep cuts; they live below the Discussion Prompts so they don't push the conversation payload off-screen.
15. **Limits of this report** — fixed list of caveats covering adjusted-hour semantics, quick-close/Won't-Do ambiguity, theme tagging brittleness, missing engineering-side metrics, reconstructed (not snapshotted) backlog-at-start, and (when prior is enabled) the assumption that the team's `SUPPORT_TEAM_LABEL` / `SUPPORT_TEAM_FIELD_VALUES` config was unchanged across the prior window. When `--no-prior` was used, a fallback bullet notes that no period-over-period comparison was performed. This section is rendered every time so a leader scanning the report can't miss it.

## Discussion-prompt thresholds

All thresholds live in `thresholds.py` and are pair-locked between `report.py:discussion_prompts` and `build_synthesis_prompt.py` so the synthesis sub-agent only prepares evidence for signals the report will actually render.

| Signal | Threshold to emit | Constant(s) |
|---|---|---|
| Backlog widening | `net > 0` AND `net / created > BACKLOG_PCT_THRESHOLD` | `BACKLOG_PCT_THRESHOLD` |
| Component dominance | first component with `count >= COMPONENT_CONCENTRATION_COUNT_FLOOR` AND `share >= COMPONENT_CONCENTRATION_SHARE_PCT` | `COMPONENT_CONCENTRATION_*` |
| Won't-Do escalation | `wont_do_count / closed > WONT_DO_RATIO` AND absolute count `>= WONT_DO_ABS` | `WONT_DO_RATIO`, `WONT_DO_ABS` |
| Quick-close by L2 | `quick_close_rate > L2_PP_THRESHOLD` | `L2_PP_THRESHOLD` |
| Reassign out of team | `count >= REASSIGN_OUT_THRESHOLD` | `REASSIGN_OUT_THRESHOLD` |
| Engineer un-assigned | `count >= ENG_UNASSIGNED_THRESHOLD` | `ENG_UNASSIGNED_THRESHOLD` |
| Bouncing tickets | `count >= BOUNCING_THRESHOLD` (≥3 Team-field transitions) | `BOUNCING_THRESHOLD` |
| Returned to focus | `count >= RETURNED_TO_FOCUS_THRESHOLD` (handed off and came back) | `RETURNED_TO_FOCUS_THRESHOLD` |
| Theme spike vs prior | `count_current >= THEME_HIGH_VOLUME_FLOOR` AND `delta_pct >= THEME_SPIKE_DELTA_PCT` AND `count_prior >= THEME_SPIKE_PRIOR_FLOOR` AND `\|delta_abs\| >= THEME_SPIKE_DELTA_ABS` | `THEME_*` |
| New theme | `is_new` AND `count_current >= NEW_THEME_FLOOR` | `NEW_THEME_FLOOR` |
| Theme has no triage coverage | volume-floored uncovered themes | `THEME_HIGH_VOLUME_FLOOR` |
| Theme stuck in Backlog | volume-floored themes with all triage matches in Backlog | `THEME_HIGH_VOLUME_FLOOR` |
| Volume acceleration vs prior | `created_in_window >= VOLUME_ABS_THRESHOLD` AND `created_pct_delta > VOLUME_PCT_THRESHOLD` (only when prior data present) | `VOLUME_*` |
| Component spike vs prior | any component with `current.count >= COMPONENT_SPIKE_ABS` AND grew `>= COMPONENT_SPIKE_PCT` vs prior | `COMPONENT_SPIKE_*` |
| L2 signal regression vs prior | `reopen_rate` or `quick_close_rate` worsened by `>= L2_PP_THRESHOLD`, OR `wont_do.count` more than doubled with absolute current count `>= WONT_DO_ABS` | `L2_PP_THRESHOLD`, `WONT_DO_*` |
| Routing drift vs prior | any team's intake share moved by `>= ROUTING_DRIFT_PP`, OR a team is new in window with `count >= ROUTING_SHIFT_CONFOUND_PP` worth of share | `ROUTING_*` |
| Bug share rising vs prior | `bug_share_pp_delta >= BUG_SHARE_PP`, OR absolute bug count up `>= BUG_SHARE_ABS_DELTA` with current bug count `>= BUG_SHARE_ABS_FLOOR` | `BUG_SHARE_*` |

If none cross thresholds, a single "no threshold-crossing signals" line is emitted so the leader sees the absence of evidence explicitly.

## Role-mapping note (do not remove from the report)

The "Top L2 staff by ticket volume" section MUST include the inline note that the reporter field is the staff member, not the customer. This prevents misreading the table as customer-side noise. See the project memory `project_happyco_support_ticket_roles.md` for the full mapping.
