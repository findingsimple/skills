---
name: sprint-summary
description: Generates a sprint summary from Jira data into Obsidian vault. Use when the user asks for a sprint summary, sprint report, or wants to review what was completed in a sprint.
disable-model-invocation: true
argument-hint: "[sprint-name] [--team <name>] [--dry-run]"
allowed-tools: Bash Read Glob AskUserQuestion
---

# Sprint Summary

Pull Jira sprint data for a team and write a structured sprint summary markdown file into the Obsidian vault.

## Architecture

This skill uses **two Python scripts** (`setup.py` and `generate.py`) in the skill directory that handle all API calls, data processing, and file generation. The scripts are permanent files — no heredocs or temp file writing needed.

### Data sources (accessed within the Python scripts)

1. **Jira Agile REST API** — sprint metadata: board discovery, sprint listing, sprint goals, and sprint dates. The Atlassian MCP tools do not expose these endpoints.
2. **Jira Greenhopper Sprint Report API** — point-in-time sprint data: parent issues with frozen statuses at sprint close, pre-calculated point sums, punted (removed) issues, and scope change tracking.
3. **Jira REST API v3** — subtask and support ticket queries (project key from `SUPPORT_PROJECT_KEY` env var).

### Key principles

**Point-in-time principle:** All metrics and statuses reflect the state at sprint close, not the current state.

**Completion classification (CRITICAL):** An issue is "completed" if and only if it appears in `completedIssues` from the Sprint Report API. An issue in `issuesNotCompletedInCurrentSprint` is NOT completed — even if its `done` boolean is `true`.

## Instructions

Follow these steps exactly. The entire skill should require **only 2 bash commands** (2 python3 runs) plus user interaction prompts.

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for optional parameters:

1. **Sprint name** (optional) — a quoted or unquoted sprint name (e.g., `"PROJ Sprint 2026 4"`).
2. **`--team <name>`** (optional) — filter to a single team by its vault directory name (e.g., `--team TeamA`).
3. **`--dry-run`** (optional) — preview output without writing any files.

If no arguments are provided, default to: latest completed sprint, all teams.

Show the parsed values to the user for confirmation:
```
Sprint: {sprint_name or "latest completed"}
Team: {team_filter or "all"}
Dry run: {yes/no}
```

### Step 2 — Run setup.py

```bash
python3 ~/.claude/skills/sprint-summary/setup.py
```

This validates environment, discovers boards, and lists recent closed sprints. Output format:

```
=== ENV ===
VAULT: {path}
TEAMS: {path}
JIRA: {base_url}
AUTH: {email}

=== TEAMS ===
{vault_dir}|{project_key}|{board_id}|{display_name}
...

=== SPRINTS ===
{id}|{name}|{end_date}|{goal_preview}
...
```

### Step 3 — Select team and sprint

**Team selection:**
- If `--team` was specified, filter to matching team (case-insensitive on vault_dir). If no match, list available teams and stop.
- If multiple teams exist and no `--team` flag, use `AskUserQuestion` to let the user choose **one** team. Use `multiSelect: false`.

**Sprint selection:**
- If a sprint name was provided via arguments, match it against the sprint list. If no match, tell the user and stop.
- If no sprint name was provided, use `AskUserQuestion` with up to 4 options (most recent closed sprints). Mark the first as "(Recommended)".

Record: `sprint_id`, `sprint_name`, `start_date`, `end_date`, `goal`, `board_id`, selected team info.

### Step 4 — Sprint goals

If a goal was retrieved from the Jira API (shown in setup output), use it automatically — just confirm: `"Sprint goal: {goal_text}"`.

Only use `AskUserQuestion` if **no goal** was found.

### Step 5 — Run generate.py

```bash
python3 ~/.claude/skills/sprint-summary/generate.py --sprint-id {ID} --sprint-name "{NAME}" --start-date "{DATE}" --end-date "{DATE}" --goal "{GOAL}" --board-id {ID} --team-vault-dir "{DIR}" --team-project-key "{KEY}" --team-display-name "{NAME}" [--dry-run]
```

The script fetches sprint report data, subtasks, and support tickets, calculates metrics, generates markdown, and writes to the Obsidian vault.

**Output path:** `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints/Sprint {N}/{sprint_name} - {end_date}.md`

### Step 6 — Confirm completion

After the generate script finishes, show the user the summary:

```
Sprint summary generated:
- {team}: {sprint_name} ({completion_rate}%, {points_completed}/{points_committed} pts) → {file_path}
```
