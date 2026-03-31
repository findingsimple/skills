---
name: sprint-pulse
description: >-
  Generates mid-sprint alerts for a team's active sprint — flags stale items,
  support ticket risks, and outstanding questions from Jira and GitLab data.
  Use when the user asks for sprint pulse, sprint alerts, daily standup check,
  or mid-sprint health.
disable-model-invocation: true
argument-hint: "[--team <name>] [--dry-run]"
allowed-tools: Bash Read Glob AskUserQuestion
---

# Sprint Pulse

Generates actionable mid-sprint alerts by analysing Jira sprint data, GitLab MR activity, and support tickets. Designed to surface problems during a sprint — not after.

Three alert types:
- **Stale items** — issues in progress with no recent activity (script-detected)
- **Support tickets** — new, unacknowledged, or SLA-risk tickets (script-detected)
- **Outstanding questions** — unanswered questions in comments/MRs (agent-detected)

See [alerts.md](alerts.md) for full alert definitions, thresholds, and how to add new alerts.

## Environment Variables

Uses existing vars from other sprint skills plus two new ones:

| Variable | Required | Purpose |
|----------|----------|---------|
| `JIRA_BASE_URL` | Yes | Jira instance URL |
| `JIRA_EMAIL` | Yes | Atlassian email for Basic auth |
| `JIRA_API_TOKEN` | Yes | Atlassian API token |
| `OBSIDIAN_TEAMS_PATH` | Yes | Teams subdirectory in Obsidian vault |
| `SPRINT_TEAMS` | Yes | Pipe-delimited team config: `vault_dir\|project_key\|board_id\|display_name` (comma-separated for multiple teams) |
| `GITLAB_URL` | Yes | GitLab instance URL |
| `GITLAB_TOKEN` | Yes | GitLab personal access token |
| `GITLAB_PROJECT_ID` | Yes | GitLab project numeric ID |
| `SUPPORT_PROJECT_KEY` | No | Jira project key for the support ticket project |
| `SUPPORT_BOARD_ID` | No | Jira board ID for the support board (used to read column configuration and determine which statuses are initial/intake) |
| `SUPPORT_TEAM_LABEL` | No | Labels used on the support project to filter tickets by team. Pipe-delimited per team, matching `SPRINT_TEAMS` order. Each team slot can have multiple comma-separated labels (OR logic). Example: `label-a,label-b\|label-c` means team 0 matches tickets labelled `label-a` OR `label-b`, team 1 matches `label-c`. |
| `SUPPORT_TEAM_FIELD_VALUES` | No | Values for the Team custom field (`cf[10600]`) to filter support tickets. Same pipe-delimited format as `SUPPORT_TEAM_LABEL`. Tickets matching **either** the label or the Team field are included. Example: `TeamA,TeamB\|TeamC`. |

## Architecture

| File | Purpose |
|------|---------|
| `jira_client.py` | Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_search_all`, `jira_get_changelog`, `jira_get_comments` |
| `gitlab_client.py` | GitLab API client — `load_gitlab_env`, `gitlab_get`, `search_mrs_for_issue`, `get_mr_notes` |
| `setup.py` | Validates env, discovers active sprint, parses team config |
| `fetch.py` | Fetches sprint issues + changelogs + comments + MRs + MR notes + support tickets → `/tmp/` |
| `analyze.py` | Runs deterministic alerts (stale items, support tickets) → `/tmp/` |
| `alerts.md` | Alert definitions, thresholds, output templates |

## Instructions

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for:
- `--team <name>` — team display name or vault_dir (optional)
- `--dry-run` — print output but do not save to vault

### Step 2 — Run setup

```bash
python3 ~/.claude/skills/sprint-pulse/setup.py
```

If `setup.py` exits with a non-zero status, display the error and stop.

Read the setup output to identify teams and active sprints. If `--team` was specified, match it against the team list (case-insensitive partial match on display_name or vault_dir). If no team was specified and multiple teams exist, ask the user which team to run for.

Record the selected team's values:
- `vault_dir`, `project_key`, `board_id`, `display_name`
- Active sprint: `sprint_id`, `sprint_name`, `start_date`, `end_date`
- Support config: `support_project_key`, `support_label`, `support_team_field` (from setup output)

If the selected team has no active sprint, inform the user and stop.

Show:
```
Team: {display_name}
Sprint: {sprint_name} ({start_date} to {end_date})
```

### Step 3 — Save board config for fetch

Read `/tmp/sprint_pulse_setup.json` to extract the board configuration for the selected team. Write it to a temp file for fetch.py:

```bash
python3 -c "
import json
with open('/tmp/sprint_pulse_setup.json') as f:
    data = json.load(f)
config = data.get('board_configs', {}).get('{vault_dir}', [])
with open('/tmp/sprint_pulse_board_config.json', 'w') as f:
    json.dump(config, f)
print('Board config: %d columns' % len(config))
support_config = data.get('support_board_config', [])
with open('/tmp/sprint_pulse_support_board_config.json', 'w') as f:
    json.dump(support_config, f)
print('Support board config: %d columns' % len(support_config))
"
```

### Step 4 — Fetch data

```bash
python3 ~/.claude/skills/sprint-pulse/fetch.py \
  --team-vault-dir "{vault_dir}" \
  --board-id "{board_id}" \
  --sprint-id "{sprint_id}" \
  --sprint-name "{sprint_name}" \
  --start-date "{start_date}" \
  --end-date "{end_date}" \
  --project-key "{project_key}" \
  --support-project-key "{support_project_key}" \
  --support-label "{support_label}" \
  --support-team-field "{support_team_field}" \
  --board-config-json "/tmp/sprint_pulse_board_config.json" \
  --support-board-config-json "/tmp/sprint_pulse_support_board_config.json"
```

If `fetch.py` exits with a non-zero status, display the error and stop.

### Step 5 — Run deterministic alerts

```bash
python3 ~/.claude/skills/sprint-pulse/analyze.py
```

This reads `/tmp/sprint_pulse_data.json` and writes `/tmp/sprint_pulse_alerts.json`.

### Step 6 — Detect outstanding questions (sub-agent)

Spawn a **general-purpose agent** with:
- The path `/tmp/sprint_pulse_data.json`
- The full text of the **analysis instructions** below

The agent must read `/tmp/sprint_pulse_data.json` via Bash `cat` (not the Read tool), analyse the data, and return a structured list of outstanding questions. Each question should include: issue key, summary, source (Jira comment or MR note with link), question text (first 150 chars), time ago, and who asked it. If no questions are found, return "No outstanding questions detected."

The main agent resumes from **Step 7** with the returned questions.

**Analysis instructions for the agent:**

Analyse two sets of items for outstanding unanswered questions:

1. **Active sprint issues** — where `is_active` is true and the issue has comments or MR notes
2. **Open support tickets** — entries in `support_tickets` that have a `comments` array (non-closed tickets with fetched comments)

**What to look for:**
- A comment or MR note ending with `?` that has no reply for more than 4 hours
- A comment explicitly asking for input, feedback, or a decision with no response
- An MR comment thread where the last message is a question from the MR author
- A comment @mentioning someone with a question and no follow-up from that person

**What to ignore:**
- Rhetorical questions in issue descriptions
- Questions that the same person answered themselves in a follow-up
- Automated bot comments or system notes
- Questions asked less than 4 hours ago (give people time to respond)
- MR threads that are marked as resolved

For each detected question, note:
- The issue key and summary
- Where the question was found (Jira comment or MR note with MR link)
- The question text (first 150 characters)
- How long ago it was asked
- Who asked it (if identifiable)

### Step 7 — Generate output

Read `/tmp/sprint_pulse_alerts.json` for the deterministic alerts. Combine with any outstanding questions detected in Step 6.

Generate the output in this format:

```markdown
# Sprint Pulse — {display_name} — {today's date YYYY-MM-DD}

## Sprint Snapshot
- Active sprint: {sprint_name} ({start_date} to {end_date})
- To do: {todo_count} | In progress: {in_progress_count} | In review: {in_review_count} | Done: {completed}/{total}

## Alerts

### Stale Items ({count})
{For each stale item:}
- **{key}** "{summary}" — {column}, no activity in {days_stale} days.
  [Assignee] update status or flag blockers today.

{If no stale items: "No stale items detected."}

### Support Tickets ({new_count} new, {unack_count} unacknowledged, {sla_count} at SLA risk)
{For each new ticket:}
- **{key}** ({priority}) "{summary}" — created {created_date}.

{For each unacknowledged ticket:}
- **{key}** ({priority}) "{summary}" — open for {hours_open}h, unacknowledged.
  [Eng Lead] acknowledge or assign today.

{For each SLA risk ticket:}
- **{key}** ({priority}) "{summary}" — {days_remaining}d remaining of {sla_days}d SLA ({days_elapsed}d elapsed).
  [Eng Lead] resolve or escalate immediately.

{If no support alerts: "No support ticket alerts."}

### Highest Priority Support ({count})
{For each Highest priority ticket that is not Closed or Awaiting Customer:}
- **{key}** "{summary}" — {status}, open for {days_open}d.

{If count >= 2:}
> [PM], [Eng Lead] and [EM] Review the impact on the sprint commitment.

{If no highest priority tickets: "No highest priority support tickets."}

### Outstanding Questions ({count})
{For each detected question:}
- **{key}** "{summary}" — unanswered question in {source} ({time_ago}): "{question_excerpt}"
  [{relevant_role}] respond today to unblock progress.

{If no questions detected: "No outstanding questions detected."}

```

**Important formatting rules:**
- Issue keys must link to Jira: `[KEY]({JIRA_BASE_URL}/browse/KEY)` in the vault file
- MR references must link to GitLab: `[!{iid}]({web_url})`
- Use role labels like `[Assignee]`, `[Eng Lead]`, `[PM]`, `[Triad]` — do not resolve to real names
- `[Triad]` means Eng Lead + Product Manager + Product Designer

Print the full output to stdout so the user can copy-paste it.

### Step 8 — Save to vault

If `--dry-run` was **not** specified:

Write the output as a markdown file with YAML frontmatter to:
`{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints/{sprint_name} - Pulse - {YYYY-MM-DD}.md`

Frontmatter:
```yaml
---
type: sprint-pulse
team: {vault_dir}
project_key: {project_key}
sprint_name: "{sprint_name}"
date: {YYYY-MM-DD}
alert_count: {total_alerts}
stale_count: {stale_count}
support_new_count: {new_count}
support_unack_count: {unack_count}
support_sla_count: {sla_count}
question_count: {question_count}
generated: {ISO 8601 UTC timestamp}
---
```

Create the output directory if needed:
```bash
mkdir -p "{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints"
```

Use the Write tool to create the file.

Report:
```
Pulse written to: {file_path}
```

If `--dry-run` was specified, skip saving and report:
```
Dry run — output not saved to vault.
```
