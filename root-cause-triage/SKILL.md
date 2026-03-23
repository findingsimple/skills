---
name: root-cause-triage
description: Triage root cause tickets on the PDE board — analyze completeness, recommend transitions, execute with user confirmation
disable-model-invocation: true
argument-hint: "[--dry-run]"
---

# Root Cause Triage

Automate weekly triage of root cause tickets. The goal is to determine whether each ticket gives a **product manager enough context to understand the root issue and design a solution** — not just whether a template was filled in.

Uses `TRIAGE_BOARD_ID` and `TRIAGE_PARENT_ISSUE_KEY` from `~/.sprint_summary_env` to identify the board and parent epic.

## Board Columns

TO TRIAGE → MORE INFO REQUIRED → READY FOR DEVELOPMENT → IN PROGRESS → REJECTED → COMPLETED / ROADMAPPED

## Architecture

This skill uses **three Python files** in the skill directory: `jira_client.py` (shared Jira API utilities), `fetch.py` (data fetching and analysis), and `triage.py` (transition execution). All read credentials from `~/.sprint_summary_env` via the shared `load_env` function.

### Data flow

1. `fetch.py` — discovers board column→status mapping; fetches "To Triage" issues with subtasks; augments thin descriptions with subtask content; checks Jira issue links for confirmed duplicates; runs Jaccard text similarity against all sibling issues (open → duplicate signal at ≥50%, closed → recurrence signal at ≥35%); scores template section completeness; outputs summary table + `/tmp/triage_issues.json`
2. Claude loads `triage_history.json` (most recent 20 entries), then spawns an **Opus agent** to assess PM-actionability of each description — rates quality as good/thin/vague, flags placeholder/dummy text, and evaluates duplicate and recurrence signals. Descriptions are truncated to 800 chars; batches of 10 if >10 issues.
3. Claude presents combined results to the user — completeness score, agent quality rating, duplicate/recurrence flags, and any fetch.py vs agent disagreements
4. User confirms or overrides actions; Claude re-reads `/tmp/triage_issues.json` for field accuracy, then writes `/tmp/triage_actions.json` (includes `summary` and `quality_note` for history)
5. `triage.py` — discovers transition IDs, executes transitions + adds comments in parallel; appends successful and partial actions to `~/.claude/skills/root-cause-triage/triage_history.json`

### Key files

| File | Purpose |
|------|---------|
| `jira_client.py` | Shared Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_post`, `jira_search_all` |
| `fetch.py` | Jira data fetch, duplicate/recurrence detection, completeness scoring |
| `triage.py` | Transition execution, comment posting, history writing |
| `~/.sprint_summary_env` | Jira credentials + `TRIAGE_BOARD_ID`, `TRIAGE_PARENT_ISSUE_KEY` |
| `~/.obsidian_env` | `OBSIDIAN_VAULT_PATH` — only needed for `--dry-run` |
| `/tmp/triage_issues.json` | fetch.py output — issue data + signals passed to agent |
| `/tmp/triage_actions.json` | Confirmed actions written by Claude, read by triage.py |
| `triage_history.json` | Persistent log of past decisions (90-day rolling window) |

## Instructions

Follow these steps exactly.

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for optional parameters:

1. **`--dry-run`** (optional) — preview analysis and planned transitions without executing any changes in Jira. Instead of executing, writes a summary markdown file to the Obsidian vault.

Show the parsed values:
```
Mode: {dry-run or live}
Board: {TRIAGE_BOARD_ID} (Root Cause Triage)
Epic: {TRIAGE_PARENT_ISSUE_KEY}
```

**If `--dry-run` is set**, also load the Obsidian vault path:
```bash
source ~/.obsidian_env
OBSIDIAN_VAULT_PATH=$(eval echo "$OBSIDIAN_VAULT_PATH")
echo "Vault: $OBSIDIAN_VAULT_PATH"
```

`OBSIDIAN_VAULT_PATH` must be set. If missing, stop and tell the user to add it to `~/.obsidian_env`.

### Step 2 — Run fetch.py

```bash
python3 ~/.claude/skills/root-cause-triage/fetch.py
```

This discovers the board configuration, fetches issues in "To Triage" status, analyzes their descriptions, and outputs a summary table. Results are saved to `/tmp/triage_issues.json`.

If `fetch.py` exits with a non-zero status, display the error output to the user and stop — do not proceed to the agent assessment step.

If no issues are found in "To Triage", inform the user and stop — nothing to triage.

### Step 3 — Agent quality assessment

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

Tickets may use a template with sections like Background Context, Steps to Reproduce, Actual Results, Expected Results, and Analysis. But the template may be partially filled, absent entirely, or the key detail may come from subtask content appended to the description. Treat the full description as your source — don't penalise for template non-compliance if the substance is there.

Watch for placeholder/dummy text such as `<brief technical changes>`, `<file locations>`, `<release flag ticket & link>`, or similar angle-bracket or brace-delimited fragments — treat these as unfilled regardless of surrounding content.

A strong root cause statement identifies *what specific system behaviour is wrong*, *why it is wrong* (the underlying cause, not just the symptom), and *under what conditions it manifests*. A common pattern is: *"[System] does not [handle/validate/guard] X, causing Y when Z"* — but other clear formulations (architectural mismatches, data integrity issues, configuration drift) are acceptable. A ticket that only describes symptoms without reaching this level of diagnosis should be flagged as `more_info`, regardless of how detailed the symptoms are.

For each ticket, assess:
1. Can a PM clearly understand **what the root issue is** — not just symptoms, but the underlying cause?
2. Is there enough context to **scope a solution** — i.e., what system/behaviour needs to change?
3. Are there **red flags** — contradictions, pure symptoms with no cause identified, or content that is clearly placeholder/boilerplate?
4. If flagged as a **text-similarity duplicate**, does the content support or contradict that conclusion?
5. If flagged as a **possible recurrence**, does this look like the same failure mode recurring, or a different issue that happened to match on keywords?

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

### Step 4 — User review

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

### Step 5 — Execute transitions

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

### Step 6 — Report results

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

Output path: `{OBSIDIAN_VAULT_PATH}/Root Cause Triage/Triage - {YYYY-MM-DD}.md`

Create the directory if needed:
```bash
mkdir -p "{OBSIDIAN_VAULT_PATH}/Root Cause Triage"
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
