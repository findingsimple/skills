---
name: charter-boundaries
description: >-
  Generates a per-team Charter Boundaries draft (routing decision aid) by
  combining the existing narrative charter, hand-curated mis-allocation
  examples, and routing-audit evidence across all configured teams. Use when
  the user wants to clarify ambiguous team boundaries that drive support-ticket
  charter drift. Produces a Markdown draft per focus team into the Obsidian
  vault for the team to refine.
disable-model-invocation: true
argument-hint: "[--charters <path>] [--examples <path>] [--window <Nd>] [--from-cache]"
allowed-tools: Bash Read Agent
---

# Charter Boundaries

Builds a per-team **Charter Boundaries** Markdown draft into the Obsidian vault. Each draft is a routing decision aid (rather than a prose charter) with six sections:

1. **Owns** — concrete ownership items extracted from the existing charter blurb.
2. **Should own (frequently mis-routed away)** — tickets that landed at another team but belong here, sourced from the user's curated `examples.md`. This is the *inbound* drift counterpart to "Does not own".
3. **Does not own (common mistakes)** — clusters of misrouted tickets observed by the routing-audit pipeline, grouped by theme. *Outbound* drift, clean misroutes only.
4. **Boundary disputes** — `split_charter` cases from the audit grouped by candidate team. Tickets to discuss with another team's PM to resolve ownership; agreed calls feed the *Edge case registry* below.
5. **Boundary rules** — if/then language already in the charter (most teams have none).
6. **Edge case registry** — empty placeholder for the team to populate over time.

Outputs go to `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Charter Clarification/{end_year}/Charter Boundaries — {team} — {YYYY-MM-DD}.md` for every team that has a `SPRINT_TEAMS` slot.

The skill **does not modify Jira**. It only reads ticket data via the existing `support-routing-audit` pipeline and writes Markdown to the vault.

---

## Required user-supplied inputs

Drop the following into `~/.claude/skills/charter-boundaries/.scratch/` (already gitignored):

- **`charters.md`** — the team's current narrative charters. Per-team H2 sections (e.g. `## ♠️ Team ACE (Admin Configuration & Experience)`, `## 📣 Team Echo (...)`) — the parser strips emoji, "Team " prefixes, and parenthetical aliases. Sections are matched against `CHARTER_TEAMS` canonicals.
- **`examples.md`** — a small set of hand-curated mis-allocation examples. Free-text lines matching `Assigned to <X>, but should belong to <Y> (we rerouted): https://.../browse/ECS-NNNN` are extracted as anchor patterns for the synthesis sub-agent.

Both files are treated as untrusted by the synthesis prompt (they may contain pasted external content).

---

## Environment variables

No new env vars. Reuses the existing variables already configured for `support-routing-audit`:

| Variable | Purpose |
|----------|---------|
| `CHARTER_TEAMS` | Pipe-delimited canonical team names (with optional comma-separated aliases per slot). |
| `SPRINT_TEAMS` | Comma-separated `vault_dir|project_key|board_id|display_name` slots. Determines which teams get vault drafts. |
| `OBSIDIAN_TEAMS_PATH` | Vault root for per-team draft output. |
| `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Per-team audit pipeline. |
| `SUPPORT_PROJECT_KEY`, `SUPPORT_TEAM_LABEL`, `SUPPORT_TEAM_FIELD_VALUES` | Support-ticket query in the audit pipeline. |
| `CHARTERS_PATH` (optional) | Override path to `charters.md` (must resolve under `OBSIDIAN_TEAMS_PATH` or this skill dir). |

---

## Argument allow-lists

- `--charters <path>` — absolute path; must resolve under `OBSIDIAN_TEAMS_PATH` or `~/.claude/skills/charter-boundaries/`. Symlinks rejected. Default: `.scratch/charters.md`.
- `--examples <path>` — same containment rule. Default: `.scratch/examples.md`.
- `--window <Nd>` — pattern `\A\d{1,3}d\Z` (e.g. `30d`, `90d`). Default `30d`. Sets the audit window.
- `--from-cache` — boolean. Re-renders `report.py` from existing `draft.json` without re-running audits or the synthesis agent.
- `--dry-run` (passed to `report.py`) — skip vault write; print to stdout + `/tmp`.

---

## Pipeline

Run sequentially. Skip to step 7 with `report.py --from-cache` to re-render an existing draft.

### Step 1 — Setup

```
cd ~/.claude/skills/charter-boundaries && python3 setup.py [--window 30d]
```

Validates env, parses `CHARTER_TEAMS` into canonicals + alias map, resolves `.scratch/charters.md` and `.scratch/examples.md`, picks the focus teams (those with a `SPRINT_TEAMS` slot), and writes `/tmp/charter-boundaries/setup.json`.

### Step 2 — Parse user inputs

```
python3 parse_inputs.py
```

Reads `charters.md` (per-team H2 sections) and `examples.md` (flat misroute list), splits per team using the alias map, and writes `/tmp/charter-boundaries/inputs.json` keyed by canonical team name with `{charter_blurb, examples}`. Examples are attached to the team referenced in their `to_team` field (the team that should have received the ticket).

### Step 3 — Per-team audit loop

For each focus team in `setup.json["focus_teams"]`, run the existing `support-routing-audit` pipeline directly (not via `/support-routing-audit` slash):

```
cd ~/.claude/skills/support-routing-audit
python3 setup.py --team <CANONICAL> --start <YYYY-MM-DD> --end <YYYY-MM-DD>
python3 fetch.py
python3 build_prompt.py
# Spawn the audit sub-agent with AUDIT_PROMPT.md to produce results.json.
python3 apply.py
```

Then immediately, **before the next team overwrites `audit.json`**:

```
cd ~/.claude/skills/charter-boundaries
python3 snapshot.py --team <CANONICAL>
```

`snapshot.py` validates that the upstream `audit.json["focus_team"]` matches `--team` and copies the file to `/tmp/charter-boundaries/audits/<TeamSlug>.json` with an atomic write.

Use the same `--start` / `--end` for every team so all snapshots cover the same window.

### Step 4 — Build the synthesis bundle

```
cd ~/.claude/skills/charter-boundaries && python3 build_prompt.py
```

Aggregates `setup.json` + `inputs.json` + every `audits/*.json` into `/tmp/charter-boundaries/bundle.json`. Filters audit tickets to `verdict == should_be_elsewhere` AND `confidence in {high, medium}` AND `out_of_charter_work == true`. Wraps untrusted free-text fields (charter blurbs, example raw lines, ticket summaries, audit reasoning) as `{"_untrusted": true, "text": "..."}`.

### Step 5 — Synthesis sub-agent

Spawn a sub-agent with `SYNTHESISE_PROMPT.md`. The agent reads `bundle.json` and writes `/tmp/charter-boundaries/synthesise/results.json` with per-team `owns_seed`, `boundary_rules_seed`, `does_not_own_clusters`, and `edge_cases_seed`.

The prompt enforces the standard untrusted-content security banner (no reads outside the skill dir + `/tmp/charter-boundaries/`, no network, redact suspected credentials).

### Step 6 — Validate + merge

```
python3 apply.py
```

Validates the agent's `results.json`:

- `theme_id` is kebab-case ASCII.
- `target_team` is in `allowed_teams`.
- `evidence_keys` resolve to real tickets in the team's audit snapshot.
- `anchored_by_curated` resolve to real curated examples for that team.
- Free-text fields are word-boundary-truncated to safe lengths.
- Clusters with fewer than 2 valid evidence_keys are dropped (single occurrences = noise).

Writes `/tmp/charter-boundaries/draft.json`.

### Step 7 — Render

```
python3 report.py [--dry-run]
```

Renders one Markdown file per focus team into `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Charter Clarification/{end_year}/Charter Boundaries — {team} — {YYYY-MM-DD}.md`. External Jira keys render as `[KEY](https://.../browse/KEY)` (markdown hyperlinks, not wiki links — Jira keys aren't vault-resident). Team names in frontmatter use `[[Team]]` because team hubs are vault-resident.

Pass `--from-cache` to re-render from an existing `draft.json` without re-running the pipeline. Useful when iterating on report formatting.

---

## Tests

```
cd ~/.claude/skills/charter-boundaries && python3 -m unittest test_parse_inputs test_apply -v
```

`test_parse_inputs.py` covers the H2 splitter, alias matching (with emoji + parens stripping), the misroute regex, and example-to-target attachment.

`test_apply.py` covers cluster validation: theme_id format, target_team allow-list, evidence_keys filtering against the bundle, anchored_by_curated filtering, and word-boundary truncation.

Both suites run without network access.

---

## Output shape

Each per-team draft contains:

- YAML frontmatter (`team: "[[X]]"`, `period_start`, `period_end`, `generated_at`, `charters_source`, `examples_source`, `tags: [charter, draft]`).
- `## Owns` — bulleted list seeded from the charter blurb.
- `## Should own (frequently mis-routed away)` — tickets from the user's curated `examples.md` where `to_team == this team`, grouped by `from_team`, rendered as Jira links. Empty for teams with no inbound-drift examples; an empty-state hint tells the user how to add new examples.
- `## Does not own (common mistakes)` — one section per cluster, each with description, boundary rule, and `**Evidence:**` line linking to Jira tickets.
- `## Boundary disputes` — `split_charter` cases from the audit, grouped by candidate team, one bullet per ticket: link + priority + summary + agent reasoning. The user takes this list to other teams' standups for ownership conversations.
- `## Boundary rules` — bulleted list (often empty initially).
- `## Edge case registry` — placeholder table for the team to fill in.

The team owner is expected to review and refine each section. The "Does not own" clusters, "Should own" examples, and "Boundary disputes" tickets are the data-grounded parts — the rest is scaffolding.

### Drift directions captured

The skill captures three classes of drift:

- **Outbound (clean misroutes)** — tickets that landed at this team but belong elsewhere with high confidence. Sourced from the routing-audit pipeline. Lives in *Does not own (common mistakes)*.
- **Boundary disputes** — tickets the audit flagged as `split_charter` with at least medium confidence and a candidate team that isn't the focus. The work touches multiple charters; the agent identifies a likely owner but isn't confident enough to call it a misroute. Lives in *Boundary disputes*.
- **Inbound** — tickets that landed at another team but belong here. Sourced from the user's hand-curated `examples.md`. Lives in *Should own (frequently mis-routed away)*.

The audit pipeline only sees teams with `SPRINT_TEAMS` slots, so it can't auto-detect inbound drift today. Curated examples close the gap — when L2 reroutes a ticket, add a line to `examples.md` and the next run picks it up.
