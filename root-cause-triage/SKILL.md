---
name: root-cause-triage
description: Triages root cause tickets — collects data to Obsidian knowledge base, analyzes for duplicates/quality, or runs the full triage workflow. Use when the user asks to triage bugs, collect root cause data, check for duplicate issues, or run the triage process.
disable-model-invocation: true
argument-hint: "[collect|analyze|triage] [--issue KEY] [--status STATUS] [--dry-run] [--force]"
---

# Root Cause Triage

Three modes for working with root cause tickets under the triage board:

- **collect** — fetch Jira data and build a per-issue Obsidian knowledge base
- **analyze** — run analysis on collected data (informational, no Jira mutations)
- **triage** — the original workflow: fetch, assess, transition (with user confirmation)

Uses `TRIAGE_BOARD_ID`, `TRIAGE_PARENT_ISSUE_KEY`, and `TRIAGE_OUTPUT_PATH` environment variables.

## Board Columns

TO TRIAGE → MORE INFO REQUIRED → READY FOR DEVELOPMENT → IN PROGRESS → REJECTED → COMPLETED / ROADMAPPED

## Architecture

### Key files

| File | Purpose |
|------|---------|
| `jira_client.py` | Shared Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_post`, `jira_search_all`, `adf_to_text` |
| `collect.py` | Mode: collect — fetch issues + linked issue details, save per-issue JSON to `/tmp/triage_collect/` |
| `analyze.py` | Mode: analyze — read collected data, score completeness, detect duplicates, write Obsidian report |
| `fetch.py` | Mode: triage — single-pass fetch + analysis for the triage workflow |
| `triage.py` | Mode: triage — transition execution, comment posting, history writing |

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

Run analysis on the collected Obsidian knowledge base. Produces an informational report — no Jira mutations.

### Step A1 — Run analyze.py

```bash
python3 ~/.claude/skills/root-cause-triage/analyze.py [--issue KEY] [--status STATUS] [--all-statuses] [--output-json]
```

This reads collected data (from `/tmp/triage_collect/` or Obsidian files), runs template completeness scoring and text-similarity duplicate detection against the full knowledge base, and writes an analysis report to `{TRIAGE_OUTPUT_PATH}/Analysis/`.

### Step A2 — Present results

Show the summary table from analyze.py output to the user. Highlight:
- Issues flagged as potential duplicates (with similarity scores)
- Issues with many linked support tickets (signal of real user impact)
- Issues missing key template sections
- Any grouping opportunities (issues that could be combined)

This is informational — the user reviews the report in Obsidian and decides next steps.

---

## Mode: Triage

The original single-pass workflow: fetch → assess → transition. Use this when you want to actually move issues between statuses on the board.

### Step T1 — Parse arguments

Parse `$ARGUMENTS` for optional parameters:

1. **`--dry-run`** (optional) — preview analysis and planned transitions without executing any changes in Jira.

### Step T2 — Run fetch.py

```bash
python3 ~/.claude/skills/root-cause-triage/fetch.py
```

This discovers the board configuration, fetches issues in "To Triage" status, analyzes their descriptions, and outputs a summary table. Results are saved to `/tmp/triage_issues.json`.

If `fetch.py` exits with a non-zero status, display the error output to the user and stop.

If no issues are found in "To Triage", inform the user and stop.

### Step T3 — Agent quality assessment

Read `/tmp/triage_issues.json` and load recent triage history:

```bash
cat ~/.claude/skills/root-cause-triage/triage_history.json 2>/dev/null || echo "[]"
```

Spawn a `general-purpose` agent using **model: opus** to assess the quality of each issue's description beyond what regex can detect.

Build the prompt by iterating over all issues in the JSON. The agent runs in a forked context with no conversation history — include everything it needs inline.

**Important:** Truncate each issue's description to **800 characters** before including it in the prompt. This keeps the prompt manageable when there are many issues to triage. The agent is assessing the *root cause framing and PM-actionability*, not parsing full technical detail.

```
You are helping triage root cause tickets on behalf of a product team. Your job is to assess whether each ticket contains enough information for a **product manager** to understand the root issue and design a solution — without needing to chase the submitting engineer for clarification.

{If triage history has entries from the last 90 days, include this section (cap to most recent 20 entries):}
## Recent Triage History

Use this as a rough calibration signal for consistency — if a new ticket resembles a previously rejected one for the same reason, apply the same standard. This is summary-level context, not a precise comparison.

{For each entry in history, grouped by action:}
**{action} ({count} tickets):** {comma-separated list of "KEY — summary (quality_note if present)"}
{end for}

---
{end if}

The bar is not technical completeness. The bar is: *could a PM read this and know what went wrong, why it matters, and what needs to be fixed?*

Some tickets may not contain much detail but from the title e.g. "No UI for bulk updating unit details" it is clear enough what the issue is e.g. product/functionality gap.

Tickets may use a template with sections like Background Context, Steps to Reproduce, Actual Results, Expected Results, and Analysis. But the template may be partially filled, absent entirely, or the key detail may come from subtask content appended to the description. Treat the full description as your source — don't penalise for template non-compliance if the substance is there.

Watch for placeholder/dummy text such as `<brief technical changes>`, `<file locations>`, `<release flag ticket & link>`, or similar angle-bracket or brace-delimited fragments — treat these as unfilled regardless of surrounding content.

A strong root cause statement identifies *what specific system behaviour is wrong*, *why it is wrong* (the underlying cause, not just the symptom), and *under what conditions it manifests*. A common pattern is: *"[System] does not [handle/validate/guard] X, causing Y when Z"* — but other clear formulations (architectural mismatches, data integrity issues, configuration drift) are acceptable. A ticket that only describes symptoms without reaching this level of diagnosis should be flagged as `more_info`, regardless of how detailed the symptoms are.

For each ticket, assess:
1. Can a PM clearly understand **what the root issue is** — not just symptoms, but the underlying cause?
2. Is there enough context to **scope a solution** — i.e., what system/behaviour needs to change?
3. Are there **red flags** — contradictions, pure symptoms with no cause identified, or content that is clearly placeholder/boilerplate?
4. If flagged as a **text-similarity duplicate**, does the content support or contradict that conclusion?
5. If flagged as a **possible recurrence**, does this look like the same failure mode recurring, or a different issue that happened to match on keywords?
6. Is this just a product gap? Do we have lots of users/tickets calling for the functionality?

---

{For each issue in /tmp/triage_issues.json:}
## {key} — {summary}
**Completeness score:** {filled_count}/{total_sections}
**Regex recommendation:** {recommendation}
{If has_subtasks: **Note:** Description includes content from subtasks.}
{If duplicate_of: **Flagged as duplicate of:** {duplicate_of} ({duplicate_source}, {duplicate_score*100}% match)}
{If recurrence_of: **Possible recurrence of resolved ticket:** {recurrence_of} ({recurrence_score*100}% similarity)}

**Description:**
{description or "(empty)"}

---
{end for}

**Quality scale:**
- `"good"` — A PM can understand the root cause and scope a fix without follow-up questions.
- `"thin"` — The root cause direction is identifiable but key details are missing (e.g., which system, what triggers it, or how severe).
- `"vague"` — Only symptoms are described, or the content is too ambiguous to identify a specific root cause.

**Action criteria:**
- `"ready"` — Quality is good; sufficient for development.
- `"more_info"` — Quality is thin or vague; needs clarification before development.
- `"duplicate"` — Content confirms the duplicate flag from text similarity or Jira links.
- `"skip"` — The ticket appears to already be in progress, has been reassigned, or is otherwise not appropriate for triage at this time. Only use when the ticket should not be actioned in either direction.

Return a JSON array — one object per ticket — with this structure:
[
  {
    "key": "PROJ-1234",
    "quality": "good" | "thin" | "vague",
    "quality_note": "one sentence — what is missing or unclear for a PM, or null if quality is good",
    "duplicate_assessment": "confirmed" | "unlikely" | "n/a",
    "duplicate_note": "one sentence if assessment differs from regex flag, otherwise null",
    "recurrence_assessment": "likely" | "unlikely" | "n/a",
    "recurrence_note": "one sentence if this looks like a recurring failure mode, otherwise null",
    "suggested_action": "ready" | "more_info" | "duplicate" | "skip"
  }
]

Return ONLY the JSON array with no preamble.
```

Parse the agent's JSON response. If the response is not valid JSON, attempt to extract a JSON array from within the response (strip markdown fences or preamble). If that also fails, log the raw response to `/tmp/triage_agent_raw.txt`, inform the user of the failure, and fall back to using only the fetch.py regex recommendations for all issues.

**Batching:** If there are more than 10 issues, process them in batches of 10, spawning a separate agent call for each batch. Merge the results before proceeding.

For each issue, merge the agent's `suggested_action` and quality notes with the `fetch.py` data. The agent's suggestion takes precedence over the regex recommendation where they differ, but always surface the conflict to the user.

### Step T4 — User review

Present a consolidated table to the user combining fetch.py completeness data and agent quality assessment. For each issue show:
- Issue key + summary (linked to Jira)
- Completeness score (sections filled / 5)
- Quality rating from agent (`good` / `thin` / `vague`) + note if applicable
- Duplicate flag if applicable (source: linked or text-similarity %, agent assessment)
- Recurrence signal if applicable — "Possible recurrence of [KEY]" + agent note on whether the failure mode matches
- Final recommended action

Highlight any issues where fetch.py and the agent **disagree** so the user can make the call.

Then ask the user to review and confirm. The user may:
- **Accept all** recommendations as-is
- **Override** individual issues (change recommendation)
- **Skip** individual issues (no action taken)

Collect the final action list.

### Step T5 — Execute transitions

Before writing the actions file, re-read `/tmp/triage_issues.json` to pull `missing_sections`, `duplicate_of`, `recurrence_of`, and `summary` for each confirmed action directly from the fetch.py output. Do not reconstruct these fields from memory.

Write the confirmed actions to `/tmp/triage_actions.json` using a bash heredoc:

```bash
cat << 'EOF' > /tmp/triage_actions.json
[
  {"key": "PROJ-1234", "action": "ready", "missing_sections": [], "summary": "Login fails after password reset", "quality_note": null},
  {"key": "PROJ-1235", "action": "more_info", "missing_sections": ["Steps to reproduce", "Analysis"], "summary": "Export button unresponsive", "quality_note": "Describes symptom only — no root cause identified"}
]
EOF
```

Action values: `"ready"` (move to Ready for Development), `"more_info"` (move to More Info Required), `"duplicate"` (move to Rejected + comment citing the original), `"skip"` (no action — omit from file).

Include `"duplicate_of"` for duplicate actions and `"recurrence_of"` if flagged (both populated from `fetch.py` output). Always include `"summary"` (from `fetch.py`) and `"quality_note"` (from the agent assessment, or `null`) — these are written to the triage history file.

Then run:

```bash
python3 ~/.claude/skills/root-cause-triage/triage.py --actions-file /tmp/triage_actions.json [--dry-run]
```

Pass `--dry-run` if the user specified it in Step 1.

### Step T6 — Report results

**Live mode:** Show the user a summary of what was done:

```
Triage complete:
- {n} moved to Ready for Development
- {n} moved to More Info Required (comments added)
- {n} skipped
- {n} partial (transition succeeded but comment failed) — if any
- {n} errors (if any)

Board: {JIRA_BASE_URL}/jira/software/boards/{TRIAGE_BOARD_ID}
```

If any transitions failed or were partial, tell the user they can re-run the skill safely — already-transitioned issues will no longer appear in the "To Triage" column and will be skipped automatically. For partial failures, the user may want to manually add the missing comment.

**Dry-run mode:** Write a markdown summary file to the vault instead.

Output path: `{TRIAGE_OUTPUT_PATH}/Triage - {YYYY-MM-DD}.md`

Create the directory if needed:
```bash
mkdir -p "{TRIAGE_OUTPUT_PATH}"
```

Use the Write tool to create the file with this structure:

```markdown
---
date: {YYYY-MM-DD}
type: triage
board: {TRIAGE_BOARD_ID}
epic: {TRIAGE_PARENT_ISSUE_KEY}
generated_at: {ISO 8601 UTC timestamp}
---

# Root Cause Triage — {display date e.g. "23 March 2026"}

> Dry run — no changes were made to Jira.

## Summary

- {n} issues analysed
- {n} recommended → Ready for Development
- {n} recommended → More Info Required

[View board]({JIRA_BASE_URL}/jira/software/boards/{TRIAGE_BOARD_ID})

## Recommended Actions

| Issue | Summary | Score | Quality | Recommended Action |
|-------|---------|-------|---------|--------------------|
{For each issue: | [KEY](JIRA_BASE_URL/browse/KEY) | summary | filled/5 | good/thin/vague | Ready for Development, More Info Required, or Duplicate |}

## Issues Needing More Info

{For each issue where recommendation is "more_info":}
### [{KEY}]({JIRA_BASE_URL}/browse/{KEY}) — {summary}

{If quality_note: **Note:** quality_note}
**Missing sections:** {comma-separated list, or "None — flagged for quality reasons"}

---

{Repeat for each "more_info" issue. Omit this whole section if none.}
```

Then confirm to the user:
```
Dry run complete — triage summary written to: {file_path}
```
