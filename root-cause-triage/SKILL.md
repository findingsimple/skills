---
name: root-cause-triage
description: Collects root cause ticket data to Obsidian knowledge base and analyzes for duplicates, quality, and completeness. Use when the user asks to collect root cause data, check for duplicate issues, or assess issue quality.
disable-model-invocation: true
argument-hint: "[collect|analyze] [--issue KEY] [--status STATUS] [--include-done] [--all-statuses] [--dry-run] [--force]"
---

# Root Cause Triage

Two modes for working with root cause tickets under the triage board:

- **collect** — fetch Jira data and build a per-issue Obsidian knowledge base
- **analyze** — structural + semantic analysis on collected data (completeness, duplicates, quality assessment)
- **triage** — deprecated (was: fetch, assess, transition)

Uses `TRIAGE_BOARD_ID`, `TRIAGE_PARENT_ISSUE_KEY`, and `TRIAGE_OUTPUT_PATH` environment variables.

## Board Columns

TO TRIAGE → MORE INFO REQUIRED → READY FOR DEVELOPMENT → IN PROGRESS → REJECTED → COMPLETED / ROADMAPPED

## Architecture

### Key files

| File | Purpose |
|------|---------|
| `jira_client.py` | Shared Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_post`, `jira_search_all`, `adf_to_text` |
| `collect.py` | Mode: collect — fetch issues + linked issue details, save per-issue JSON to `/tmp/triage_collect/` |
| `summarize.py` | Mode: collect — read per-issue JSON, generate Obsidian Markdown with extractive summaries |
| `enrich.py` | Mode: collect — prepare agent batches and apply enriched summaries to Markdown files |
| `ENRICH_PROMPT.md` | Agent prompt template for linked issue summarization and root cause synthesis |
| `analyze.py` | Mode: analyze — structural scoring, duplicate detection, writes report + `/tmp/triage_analysis.json` |
| `QUALITY_PROMPT.md` | Agent prompt for semantic quality assessment (used by analyze mode Step A2) |
| `fetch.py` | (Deprecated — was used by triage mode) |
| `triage.py` | (Deprecated — was used by triage mode) |

## Instructions

### Step 1 — Parse mode and arguments

Parse `$ARGUMENTS` to determine the mode:

- First positional argument: `collect`, `analyze`, or `triage`
- If no mode specified, ask the user which mode to run
- Remaining arguments are passed through to the relevant script

Show the parsed values:
```
Mode: {collect|analyze|triage}
Board: {TRIAGE_BOARD_ID}
Epic: {TRIAGE_PARENT_ISSUE_KEY}
Output: {TRIAGE_OUTPUT_PATH}
```

---

## Mode: Collect

Build a per-issue Obsidian knowledge base from Jira data. Three steps:
1. `collect.py` fetches all data from Jira and saves per-issue JSON to `/tmp/triage_collect/`
2. `summarize.py` reads the JSON files, generates Obsidian Markdown with extractive summaries
3. `enrich.py` + agent calls produce quality linked issue summaries and a root cause analysis synthesis

### Step C1 — Run collect.py

```bash
python3 ~/.claude/skills/root-cause-triage/collect.py [--issue KEY] [--status STATUS] [--dry-run] [--force]
```

Pass through any arguments from Step 1. If `collect.py` exits with a non-zero status, display the error and stop.

If `--dry-run` was specified, show the output and stop — do not proceed to summarization.

### Step C2 — Run summarize.py

```bash
python3 ~/.claude/skills/root-cause-triage/summarize.py [--issue KEY] [--force] [--dry-run]
```

Pass through `--issue`, `--force`, and `--dry-run` flags from Step 1.

The script:
- Reads per-issue JSON from `/tmp/triage_collect/`
- Skips issues that already have a Markdown file (unless `--force`)
- Generates frontmatter (key, board_column, status, issue_type, priority, reporter, created, parent_epic, collected_at, linked_issue_count)
- Writes full description including subtask content
- Groups linked issues by relationship type (duplicates, relates to, causes)
- Summarizes linked issue descriptions using keyword-prioritized extraction (3-5 key sentences)
- For large "causes" groups (>10 items), writes a paragraph summary with a compact list
- Writes Markdown files to `{TRIAGE_OUTPUT_PATH}/Issues/{KEY} — {sanitized summary}.md`

**`board_column` resolution:** The board maps specific Jira status IDs to named columns (e.g., status "Backlog" id:10002 → column "To Triage"). Issues whose status is not mapped to any board column (e.g., "Closed", "Cancelled") fall back to using the raw Jira status name.

**Board count vs collect count:** `collect.py` uses `parent = {TRIAGE_PARENT_ISSUE_KEY}` (direct children only), while the board filter uses `parentEpic in (...)` which traverses the full hierarchy and spans multiple parent epics. This means the board count will typically be slightly higher because it includes: (1) the parent epic itself, (2) subtasks of child issues (e.g., Code subtasks that appear in Rejected), and (3) children of other parent epics on the board. Subtask data is already captured within the parent issue's JSON, so this gap is expected and not a data loss.

### Step C3 — Agent enrichment

This step uses agents to replace the extractive summaries from Step C2 with quality, contextual summaries and adds a "Root Cause Analysis" synthesis section to each issue.

If `--dry-run` was specified in Step 1, skip this step.

**C3a — Prepare batches:**

```bash
python3 ~/.claude/skills/root-cause-triage/enrich.py prepare [--issue KEY] [--batch-size 5] [--force]
```

Pass through `--issue` and `--force` from Step 1. This reads the collected JSON, builds agent prompts grouped into batches, and writes them to `/tmp/triage_enrich/`. Issues without linked issue descriptions are skipped.

If no issues need enrichment, stop here.

**C3b — Run agent batches:**

Read `/tmp/triage_enrich/batches.json` to get the list of batches. For each batch:

1. Read the batch prompt file (e.g., `/tmp/triage_enrich/batch_001.txt`)
2. Spawn a `general-purpose` agent using **model: sonnet** with the prompt
3. Parse the agent's JSON response — if not valid JSON, strip markdown fences and retry parsing
4. For each issue in the response, save to `/tmp/triage_enrich/result_{KEY}.json`

Run up to 3 agent batches concurrently to balance speed with reliability. If a batch fails to parse, log the raw response to `/tmp/triage_enrich/failed_batch_{N}.txt` and continue with remaining batches.

**C3c — Apply results:**

```bash
python3 ~/.claude/skills/root-cause-triage/enrich.py apply [--issue KEY] [--dry-run]
```

This reads the agent results and updates the Markdown files:
- Inserts a "## Root Cause Analysis" section above Description (first section after the heading)
- Replaces extractive `**Summary:**` lines with agent-quality summaries

After applying, report:
```
Enrichment complete:
- {n} files enriched with root cause analysis
- {n} linked issue summaries upgraded
- {n} skipped (no enrichment result)
```

---

## Mode: Analyze

Run structural and semantic analysis on the collected Obsidian knowledge base. Produces an informational report — no Jira mutations.

### Step A1 — Run analyze.py

```bash
python3 ~/.claude/skills/root-cause-triage/analyze.py [--issue KEY] [--status STATUS] [--all-statuses]
```

This reads collected data (from `/tmp/triage_collect/` or Obsidian files), runs template completeness scoring and text-similarity duplicate detection against the full knowledge base, writes an analysis report to `{TRIAGE_OUTPUT_PATH}/Analysis/`, and saves results to `/tmp/triage_analysis.json`.

If `analyze.py` exits with a non-zero status, display the error and stop.

### Step A2 — Agent quality assessment

Read `/tmp/triage_analysis.json` and load triage history:

```bash
cat ~/.claude/skills/root-cause-triage/triage_history.json 2>/dev/null || echo "[]"
```

Read [QUALITY_PROMPT.md](QUALITY_PROMPT.md) for the full agent prompt. Build it by iterating over all issues in the analysis JSON, using each issue's `description` field (already truncated to 800 chars by analyze.py).

Spawn a `general-purpose` agent using **model: opus** with the constructed prompt.

Parse the agent's JSON response. If the response is not valid JSON, attempt to extract a JSON array from within the response (strip markdown fences or preamble). If that also fails, log the raw response to `/tmp/triage_agent_raw.txt`, inform the user of the failure, and fall back to structural-only results (skip to Step A4).

**Batching:** If there are more than 10 issues, process them in batches of 10, spawning a separate agent call for each batch. Merge the results before proceeding.

For each issue, merge the agent's `quality`, `quality_note`, `duplicate_assessment`, `recurrence_assessment`, and `recommended_action` with the structural analysis from `/tmp/triage_analysis.json`.

Save merged results to `/tmp/triage_analysis_enriched.json` using a bash heredoc.

### Step A3 — Update Obsidian report

Read the analysis report that `analyze.py` wrote to `{TRIAGE_OUTPUT_PATH}/Analysis/Analysis - {YYYY-MM-DD}.md`. Append a "Quality Assessment" section with the agent's findings:

```markdown
## Quality Assessment

| Key | Quality | Note | Dup Assessment | Recurrence | Recommended Action |
|-----|---------|------|----------------|------------|--------------------|
{For each issue with agent data:}
| [KEY](JIRA_BASE_URL/browse/KEY) | good/thin/vague | note or -- | confirmed/unlikely/n/a | likely/unlikely/n/a | ready/more_info/duplicate/skip |

### Issues Flagged as Thin or Vague

{For each issue where quality != "good":}
#### [KEY](JIRA_BASE_URL/browse/KEY) — {summary}
- **Quality:** thin/vague
- **Note:** {quality_note}
- **Structural score:** {filled_count}/{total_sections}
- **Recommended action:** {recommended_action}
```

Use the Write tool to overwrite the report file with the appended content.

### Step A4 — Present results

Show the consolidated summary table combining structural scores AND agent quality ratings. Highlight:
- Issues where structural analysis says "ready" but agent says "thin" or "vague" (disagreement)
- Issues where agent confirms or overrides duplicate/recurrence flags
- Issues flagged as potential duplicates (with similarity scores)
- Issues with many linked support tickets (signal of real user impact)
- Issues missing key template sections

This is informational — the user reviews the report in Obsidian and decides next steps.

---

## Mode: Triage (Deprecated)

Triage mode has been deprecated. The quality assessment that was previously part of triage has been folded into analyze mode (Step A2).

If the user selects triage mode, print this message and stop:

> Triage mode is deprecated. Use `analyze` mode instead, which now includes both structural completeness scoring and agent-based quality assessment.
>
> If you need to move issues between statuses on the Jira board, do that manually or ask me to help with specific Jira transitions.
