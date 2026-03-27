---
name: root-cause-triage
description: Collects root cause ticket data to Obsidian knowledge base and analyzes for duplicates, quality, and completeness. Use when the user asks to collect root cause data, check for duplicate issues, or assess issue quality.
disable-model-invocation: true
argument-hint: "[collect|analyze] [--issue KEY] [--status STATUS] [--all-statuses] [--dry-run] [--force]"
allowed-tools: Bash Read Write Glob Agent
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

Build a per-issue Obsidian knowledge base from Jira data. This is a two-step process:
1. `collect.py` fetches all data from Jira and saves per-issue JSON to `/tmp/triage_collect/`
2. Claude processes each issue's JSON one at a time, summarizes linked issue descriptions, and writes the final Markdown file to Obsidian

### Step C1 — Run collect.py

```bash
python3 ~/.claude/skills/root-cause-triage/collect.py [--issue KEY] [--status STATUS] [--dry-run] [--force]
```

Pass through any arguments from Step 1. If `collect.py` exits with a non-zero status, display the error and stop.

If `--dry-run` was specified, show the output and stop — do not proceed to summarization.

### Step C2 — Summarize and write Markdown files

After `collect.py` completes, process each issue's JSON file in `/tmp/triage_collect/` one at a time.

For each `{KEY}.json` file:

1. Read the JSON file
2. Check if a file matching `{TRIAGE_OUTPUT_PATH}/Issues/{KEY} — *.md` already exists — if so, skip unless `--force` was specified
3. Summarize the issue data into a Markdown file with this format:

**Filename:** `{KEY} — {summary sanitized for filesystem}.md` (replace `/`, `\`, `:`, `*`, `?`, `"`, `<`, `>`, `|` with `-`; truncate to ~80 chars if very long)

**`board_column` resolution:** The board maps specific Jira status IDs to named columns (e.g., status "Backlog" id:10002 → column "To Triage"). Issues whose status is not mapped to any board column (e.g., "Closed", "Cancelled") fall back to using the raw Jira status name. This means `board_column` always has a value — either the mapped column name or the Jira status name. The board filter spans multiple parent epics; `collect.py` queries by a single parent epic, so the issue count may differ from what the board displays.

```markdown
---
key: {key}
board_column: {board_column}
status: {status}
issue_type: {issue_type}
priority: {priority}
reporter: {reporter}
created: {created}
parent_epic: {TRIAGE_PARENT_ISSUE_KEY}
collected_at: {collected_at}
linked_issue_count: {linked_issue_count}
---

# [{key}]({JIRA_BASE_URL}/browse/{key}) — {summary}

## Description

{description text — full plain text including subtask content}

## Linked Issues

{For each link group (e.g., "duplicates", "relates to", "causes"):}

### {Group Label} ({count})

{For each linked issue in the group:}
#### [{linked_key}]({JIRA_BASE_URL}/browse/{linked_key}) — {linked_summary} *({linked_issue_type})*
- **Status:** {linked_status}
{If description was fetched, summarize it in 3-5 sentences:}
- **Summary:** {summary capturing the core problem, specific conditions/customers affected, what was required to resolve it, and how it connects to the root cause}
{If no description was fetched:}
- *(stub only — no description fetched)*
```

**Summarization guidelines for linked issue descriptions:**
- Capture the core problem or observation described in the linked issue
- Note specific conditions, affected customers/properties, PMS provider, and business IDs where mentioned
- Explain what was required to resolve it (manual support intervention, configuration change, etc.)
- Connect back to how this relates to the root cause — why does this linked issue exist because of the gap?
- Keep it to 3-5 sentences — enough to understand the full context without reading the original ticket
- If the description is very short or empty, just note that

**Formatting conventions:**
- All issue keys must be hyperlinked: `[KEY](JIRA_BASE_URL/browse/KEY)`
- Filenames include the issue title: `{KEY} — {sanitized summary}.md`
- For large "causes" groups (>10 items), write a paragraph summary instead of listing each stub

4. Write the Markdown file to `{TRIAGE_OUTPUT_PATH}/Issues/{KEY}.md`

After processing all issues, report:
```
Collection complete:
- {n} Markdown files written to {TRIAGE_OUTPUT_PATH}/Issues/
- {n} skipped (already existed)
- Index updated at {TRIAGE_OUTPUT_PATH}/Issues/_index.md
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
