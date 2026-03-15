---
name: root-cause-triage
description: Triage root cause tickets on the PDE board — analyze completeness, recommend transitions, execute with user confirmation
disable-model-invocation: true
argument-hint: "[--dry-run]"
---

# Root Cause Triage

Automate weekly triage of root cause tickets. Uses `TRIAGE_BOARD_ID` and `TRIAGE_PARENT_ISSUE_KEY` from `~/.sprint_summary_env` to identify the board and parent epic. Analyzes ticket descriptions for completeness, recommends transitions, and executes confirmed actions.

## Board Columns

TO TRIAGE → MORE INFO REQUIRED → READY FOR DEVELOPMENT → IN PROGRESS → REJECTED → COMPLETED / ROADMAPPED

## Architecture

This skill uses **two Python scripts** (`fetch.py` and `triage.py`) in the skill directory. Both reuse `~/.sprint_summary_env` for Jira credentials.

### Data flow

1. `fetch.py` — discovers board column→status mapping, fetches "To Triage" issues via JQL, analyzes description completeness, outputs summary table + `/tmp/triage_issues.json`
2. Claude presents results, user confirms/overrides actions
3. Claude writes `/tmp/triage_actions.json` with confirmed actions
4. `triage.py` — reads actions file, discovers transition IDs, executes transitions + adds comments for "More Info Required"

## Instructions

Follow these steps exactly.

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for optional parameters:

1. **`--dry-run`** (optional) — preview analysis and planned transitions without executing any changes in Jira.

Show the parsed values:
```
Mode: {dry-run or live}
Board: {TRIAGE_BOARD_ID} (Root Cause Triage)
Epic: {TRIAGE_PARENT_ISSUE_KEY}
```

### Step 2 — Run fetch.py

```bash
python3 ~/.claude/skills/root-cause-triage/fetch.py
```

This discovers the board configuration, fetches issues in "To Triage" status, analyzes their descriptions, and outputs a summary table. Results are saved to `/tmp/triage_issues.json`.

If no issues are found in "To Triage", inform the user and stop — nothing to triage.

### Step 3 — User review

Present the fetch.py output table to the user. For each issue, show:
- Issue key + summary
- Completeness score (sections filled / 5)
- Missing sections (if any)
- Recommendation: "Ready for Dev" or "More Info Required"

Then ask the user to review and confirm. The user may:
- **Accept all** recommendations as-is
- **Override** individual issues (change recommendation)
- **Skip** individual issues (no action taken)

Collect the final action list.

### Step 4 — Execute transitions

Write the confirmed actions to `/tmp/triage_actions.json` using a bash heredoc:

```bash
cat << 'EOF' > /tmp/triage_actions.json
[
  {"key": "PROJ-1234", "action": "ready", "missing_sections": []},
  {"key": "PROJ-1235", "action": "more_info", "missing_sections": ["Steps to reproduce", "Analysis"]}
]
EOF
```

Action values: `"ready"` (move to Ready for Development), `"more_info"` (move to More Info Required), `"skip"` (no action — omit from file).

Then run:

```bash
python3 ~/.claude/skills/root-cause-triage/triage.py --actions-file /tmp/triage_actions.json [--dry-run]
```

Pass `--dry-run` if the user specified it in Step 1.

### Step 5 — Report results

Show the user a summary of what was done:

```
Triage complete:
- {n} moved to Ready for Development
- {n} moved to More Info Required (comments added)
- {n} skipped
- {n} errors (if any)

Board: {JIRA_BASE_URL}/jira/software/boards/{TRIAGE_BOARD_ID}
```
