---
name: support-trends
description: >-
  Generates a monthly (or custom-window) support-ticket trends report for one
  team — a Findings section the engineering manager can take to exec, a
  To Support sub-section with charter / containment / categorisation feedback,
  and an evidence-linked themes catalog backed by the underlying numbers. Use
  when the user asks for support trends, monthly support analysis, or wants
  evidence to take into a leadership conversation about support volume or L2
  performance.
disable-model-invocation: true
argument-hint: "[--team <name>] [--window <month|YYYY-MM|YYYY-MM-DD..YYYY-MM-DD>] [--no-prior] [--no-themes] [--dry-run]"
allowed-tools: Bash Read Glob AskUserQuestion Agent
---

# Support Trends (v2)

Builds a deterministic markdown report that aggregates support tickets for one team over a chosen window, enriched by three sub-agents running in parallel-then-merge:

1. **themes** — tags each in-window ticket with 1–3 kebab-case theme IDs; vocabulary persists across runs
2. **support-feedback** — surfaces what L2 should do differently: charter drift, containment opportunities, categorisation quality
3. **synthesise** — final pass that picks the top findings, groups by audience (exec / support), and writes a one-line `so_what` per finding. Schema-locked output: every claim has a metric and `evidence_keys`; the report renderer rejects records without them.

The report lands in the team's Obsidian vault under `Support/Trends/{year}/`.

**Role mapping** (don't get this wrong): `reporter` is the L2 support staff member who logged the ticket on the customer's behalf (tickets typically only reach engineering via L2); `assignee` is the engineer; `cf[10600]` is the team the ticket is currently routed to.

## Why v2 (vs v1, preserved on `support-trends-v1`)

- **Faster**: themes and support-feedback agents run in parallel, not sequentially.
- **Less drift**: one shared bundle (`bundle.py` → `bundle.json`) instead of three near-duplicate per-stage builders.
- **More trustworthy report**: deterministic step crystallises findings; sub-agents enrich; synthesise picks + frames; renderer renders. No agent invents claims at render time.
- **Smaller surface**: triage cross-ref dropped; charter agent rebranded to "support-feedback" with three explicit classes.

## Environment variables

Reuses sprint-pulse's variables — no new vars beyond what v1 already used.

| Variable | Required | Purpose |
|----------|----------|---------|
| `JIRA_BASE_URL` | Yes | Jira instance URL |
| `JIRA_EMAIL` | Yes | Atlassian email for Basic auth |
| `JIRA_API_TOKEN` | Yes | Atlassian API token |
| `OBSIDIAN_TEAMS_PATH` | Yes | Teams subdirectory in Obsidian vault |
| `SPRINT_TEAMS` | Yes | Pipe-delimited team config: `vault_dir\|project_key\|board_id\|display_name` |
| `SUPPORT_PROJECT_KEY` | Yes | Jira project key for the support project |
| `SUPPORT_BOARD_ID` | No | Used to detect closed-status set; falls back to keyword detection |
| `SUPPORT_TEAM_LABEL` | One required | Per-team labels (matches `SPRINT_TEAMS` order); CSV-OR per slot |
| `SUPPORT_TEAM_FIELD_VALUES` | One required | Per-team values for the Team custom field `cf[10600]` |
| `CHARTER_TEAMS` | No | Canonical team names for charter-drift detection (reuses `support-routing-audit` format) |
| `CHARTERS_PATH` | No | Optional charter doc override; must resolve under `OBSIDIAN_TEAMS_PATH` or the skill dir |

If neither support team filter is set the skill aborts to avoid querying the entire support project across all teams.

## Argument allow-lists

For JQL/URL safety, every interpolated argument is validated against an anchored regex (`\A...\Z` + `re.ASCII`) before it reaches a JQL string.

`setup.py` (window resolution):

| Arg | Pattern | Resolves to |
|-----|---------|-------------|
| `--window month` | literal | previous calendar month |
| `--window YYYY-MM` | `\A\d{4}-\d{2}\Z` | that calendar month |
| `--window YYYY-MM-DD..YYYY-MM-DD` | `\A\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}\Z` | explicit start/end |
| `--team` | `\A[A-Za-z][A-Za-z0-9 _\-]{0,63}\Z` | one of `SPRINT_TEAMS` slots |

`fetch.py`: same validators as v1 (`--support-project-key`, `--support-label`, `--support-team-field`, `--start`, `--end`, `--prior-start`, `--prior-end`).

## Pipeline

```
setup ─► fetch ─► analyze ─► bundle ─┬─► themes-agent ─────────┐
                                     └─► support-feedback-agent ─┴─► synthesise-agent ─► report
                                         (these two run in parallel)
```

All sub-agent stages read `/tmp/support_trends/bundle.json` via `cat` (not the Read tool) and write results back to `/tmp/support_trends/{themes,support_feedback,synthesise}/results.json`.

### Step 1 — Setup

```bash
python3 ~/.claude/skills/support-trends/setup.py --team {TEAM} --window {WINDOW}
```

Validates env, resolves the team slot, resolves the window. Default window when `--window` is omitted: previous calendar month. Writes `/tmp/support_trends/setup.json`.

### Step 2 — Fetch

```bash
python3 ~/.claude/skills/support-trends/fetch.py
```

Reads `setup.json`, runs the three JQLs (created-in-window, resolved-in-window-but-created-earlier, open-backlog-at-window-start), per-ticket changelog enrichment, and writes the merged ticket bundle to `/tmp/support_trends/data.json`. Includes `resolution_category` (customfield_11695).

### Step 3 — Analyze (deterministic findings)

```bash
python3 ~/.claude/skills/support-trends/analyze.py
```

Runs all deterministic checks and emits **crystallised findings**, not raw counters. Each finding has the shape:

```json
{
  "kind": "volume_spike|defect_rate|reopens|quick_close|reassign_out|ack_sla|never_do|charter_drift_candidate|categorisation_blank|...",
  "claim": "Ingest defects up 38% MoM (16 → 22)",
  "metric": "16 → 22",
  "delta_pct": 37.5,
  "evidence_keys": ["ECS-123", ...],
  "severity": "high|medium|low",
  "audience_hint": "exec|support|both"
}
```

Writes `/tmp/support_trends/analysis.json` (findings + raw windows for the renderer's number tables).

### Step 4 — Bundle

```bash
python3 ~/.claude/skills/support-trends/bundle.py
```

Builds the **single shared bundle** consumed by all three sub-agents. Per-ticket records use `ticket_record.ticket_record()`. Wraps free-text fields as `_untrusted`. Writes `/tmp/support_trends/bundle.json`.

### Step 5 — Themes + support-feedback agents (parallel)

Spawn **both sub-agents in a single message with two Agent tool calls** so they run concurrently.

**Themes sub-agent** — `subagent_type: general-purpose`. Prompt: read `~/.claude/skills/support-trends/THEMES_PROMPT.md` (full file contents), then append:
```
Read /tmp/support_trends/bundle.json with cat, tag each ticket per the rules,
and write /tmp/support_trends/themes/results.json. The bundle has {N} in-window tickets.
```

**Support-feedback sub-agent** — `subagent_type: general-purpose`. Prompt: read `~/.claude/skills/support-trends/SUPPORT_FEEDBACK_PROMPT.md`, then append:
```
Read /tmp/support_trends/bundle.json with cat, evaluate each ticket against the
three feedback classes (charter drift, L2 containment, categorisation quality),
and write /tmp/support_trends/support_feedback/results.json.
```

Wait for both to complete, then merge:

```bash
python3 ~/.claude/skills/support-trends/apply_themes.py
python3 ~/.claude/skills/support-trends/apply_support_feedback.py
```

Both apply scripts validate IDs, recompute counts from validated records, and persist vocabulary back to `/tmp/support_trends/themes_vocabulary.json`. If either sub-agent fails or its `results.json` is missing, the renderer continues without that section and prints a `[section unavailable]` notice.

### Step 6 — Synthesise

Spawn **one** `general-purpose` sub-agent. Prompt: read `~/.claude/skills/support-trends/SYNTHESISE_PROMPT.md`, then append:
```
Read /tmp/support_trends/analysis.json, /tmp/support_trends/themes/results.json,
and /tmp/support_trends/support_feedback/results.json with cat. Pick the top
findings, group by audience (exec / support), write one-line so_what per finding,
and emit /tmp/support_trends/synthesise/results.json per the schema.
```

Strict schema (rejected by `apply_synthesise.py` if violated):

```json
{
  "findings": [
    {
      "claim": "...",
      "metric": "...",
      "evidence_keys": ["ECS-123", ...],
      "audience": ["exec"|"support"|"both"],
      "so_what": "...",
      "confidence": "high|medium|low"
    }
  ]
}
```

```bash
python3 ~/.claude/skills/support-trends/apply_synthesise.py
```

### Step 7 — Report

```bash
python3 ~/.claude/skills/support-trends/report.py
```

Pure renderer. Reads `analysis.json`, `themes/results.json`, `support_feedback/results.json`, `synthesise/results.json`. Writes:

- `/tmp/support_trends/report.md` (terminal preview)
- `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/{year}/Support Trends — {team} — {window}.md`

Window naming: `{YYYY-MM}` for monthly default, `{start}_to_{end}` for custom.

### Report shape

```markdown
---
title: Support Trends — {team} — {window}
team: "[[{vault_dir}]]"
window: {start} → {end}
created: {YYYY-MM-DD}
tags: [support-trends]
---

# Findings

- {claim} ({metric}) — [[ECS-123]] [[ECS-234]] ...
  → {so_what}
- ...

## To Support
- {claim — charter drift / containment / categorisation} ({metric}) — [[ECS-...]] ...
  → {so_what}
- ...

# Themes
| Theme | Count | Tickets |
|---|---|---|
| pms-sync-yardi | 12 | [[ECS-...]] ... |

# Numbers
- Volume table (created vs resolved by bucket)
- Defect rate
- Age buckets
- L2 / triage signals (time-to-first-engineer, reopens, quick-close, reassign-out, won't-do rate)
```

No section emits prose beyond `so_what` lines from the synthesise agent. Number tables are direct renders of `analysis.json`.

## Failure modes

- **Sub-agent timeout / failure**: section omitted with `[unavailable]` notice; rest of report renders.
- **Synthesise rejected**: report falls back to rendering raw findings from `analysis.json` grouped by `audience_hint`.
- **Empty window** (no tickets): report renders headers + "No tickets in window" notice.
- **Themes vocabulary drift**: vocabulary file is per-team; corruption is recoverable by deleting `/tmp/support_trends/themes_vocabulary.json`.
