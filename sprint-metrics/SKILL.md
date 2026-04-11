---
name: sprint-metrics
description: Generates engineering metrics (MR counts, time to merge, review turnaround, cycle time, DORA deployment frequency, DORA lead time) from GitLab for a Jira sprint. Use when the user asks for sprint metrics, merge request stats, cycle time data, DORA metrics, or engineering performance numbers.
disable-model-invocation: true
argument-hint: "[sprint-name] [--team <name>] [--dry-run]"
allowed-tools: Bash Read Glob AskUserQuestion
---

# Sprint Metrics

Pull GitLab merge request data linked to Jira sprint issues and generate engineering metrics into the Obsidian vault.

## Architecture

This skill uses **two Python scripts** (`setup.py` and `generate.py`) in the skill directory. They handle all API calls, data processing, and file generation.

### Data sources

1. **Existing sprint summary file** (preferred) — reads issue keys and sprint metadata from the markdown frontmatter and issue links, avoiding Jira API calls.
2. **Jira Agile REST API** (fallback) — sprint metadata: board discovery, sprint listing, dates.
3. **Jira Greenhopper Sprint Report API** (fallback) — issue keys in the sprint (completed + not completed).
4. **GitLab REST API** — merge request search by issue key, commits, approvals, and notes.

### Metrics calculated

- **MR counts** — merged, open, total, per author
- **Time to Merge (TTM)** — MR created → merged (average + median)
- **Review Turnaround** — MR created → first non-author response (comment or approval, whichever is earlier) (average + median)
- **Time to Approval** — MR created → first approval (average + median)
- **Cycle Time** — first commit on branch → MR merged (average + median)
- **Deployment Frequency (DORA)** — total MRs merged to default branch by team members per day (deploy count / sprint days), with DORA rating. Also reports days-with-deploys as a spread indicator
- **Lead Time for Changes (DORA)** — first commit to merge for sprint-linked MRs (median + P90), with DORA rating

### How MRs are linked to sprint issues

For each Jira issue key in the sprint, the script searches GitLab MRs where the key appears in the title, description, or source branch name. This relies on the team convention of including the Jira key in branch names (e.g., `PROJ-123-fix-widget`).

## Instructions

Follow these steps exactly. The skill requires **only 2 bash commands** (2 python3 runs) plus user interaction prompts.

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
python3 ~/.claude/skills/sprint-metrics/setup.py
```

This validates environment (including GitLab credentials), discovers boards, and lists recent closed sprints. Output format:

```
=== ENV ===
VAULT: {path}
TEAMS: {path}
JIRA: {base_url}
AUTH: {email}
GITLAB: {gitlab_url}
GITLAB_PROJECT: {project_id}

=== TEAMS ===
{vault_dir}|{project_key}|{board_id}|{display_name}
...

=== SPRINTS ===
{id}|{name}|{end_date}|{goal_preview}
...
```

### Step 3 — Select team and sprint

**Check for existing sprint summary first:**
- Look for a matching sprint summary file in `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints/Sprint {N}/{sprint_name} - {end_date}.md`
- If found, use `--summary-file` mode (skips Jira sprint report API, reads issue keys from the summary)
- If not found, fall back to manual sprint/team selection and Jira API

**Team selection (when no summary file):**
- If `--team` was specified, filter to matching team (case-insensitive on vault_dir). If no match, list available teams and stop.
- If multiple teams exist and no `--team` flag, use `AskUserQuestion` to let the user choose **one** team. Use `multiSelect: false`.

**Sprint selection (when no summary file):**
- If a sprint name was provided via arguments, match it against the sprint list. If no match, tell the user and stop.
- If no sprint name was provided, use `AskUserQuestion` with up to 4 options (most recent closed sprints). Mark the first as "(Recommended)".

Record: `sprint_id`, `sprint_name`, `start_date`, `end_date`, `board_id`, selected team info.

### Step 4 — Run generate.py

**With summary file (preferred):**
```bash
python3 ~/.claude/skills/sprint-metrics/generate.py --summary-file "{SUMMARY_PATH}" [--dry-run]
```

**Without summary file (fallback):**
```bash
python3 ~/.claude/skills/sprint-metrics/generate.py --sprint-id {ID} --sprint-name "{NAME}" --start-date "{DATE}" --end-date "{DATE}" --board-id {ID} --team-vault-dir "{DIR}" --team-project-key "{KEY}" --team-display-name "{NAME}" [--dry-run]
```

The script:
1. Reads issue keys from summary file (or fetches from Jira sprint report if no summary)
2. Searches GitLab for MRs linked to those issues (parallel, 10 workers)
3. For each MR, fetches commits (cycle time), approvals (time to approval), and notes (review turnaround) in parallel
4. Excludes stale MRs (open + created before sprint start) from metric calculations, listed separately
5. Calculates aggregate and per-author metrics
6. Writes markdown to the Obsidian vault

**Output path:** `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints/Sprint {N}/{sprint_name} - {end_date} - Metrics.md`

### Step 5 — Confirm completion

After the generate script finishes, show the user the summary:

```
Sprint metrics generated:
- {team}: {sprint_name} → {MR count} MRs | TTM: {avg} | Review: {avg} | Cycle: {avg} | DORA Deploy: {rating} | DORA Lead: {rating}
- File: {file_path}
```
