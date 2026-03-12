---
name: sprint-summary
description: Generate sprint summary from Jira data into Obsidian vault
disable-model-invocation: true
argument-hint: "[sprint-name] [--team <name>] [--dry-run]"
allowed-tools: Read Edit Write Glob Bash AskUserQuestion
---

# Sprint Summary

Pull Jira sprint data for your teams, optionally gather goals/highlights/blockers interactively, and write a structured sprint summary markdown file into the Obsidian vault.

## Architecture Note

This skill uses **up to three data sources**:

1. **Jira Agile REST API** (via `curl` in a `bash -c` subshell) — for sprint metadata: board discovery, sprint listing, sprint goals, and sprint dates. The Atlassian MCP tools do not expose these endpoints.
2. **Atlassian MCP tools** (`searchJiraIssuesUsingJql`) — for querying issues within a sprint. Story points (`customfield_10021`) are returned correctly via MCP.
3. **GitLab REST API** (via `curl`, optional) — for merge request metrics. Requires `GITLAB_TOKEN` (with `read_api` scope) and `GITLAB_PROJECT_ID` in `~/.sprint_summary_env`. If not configured, the Engineering Metrics section is omitted.

**Shell compatibility:** The env file uses `export` syntax. Always use `bash -c 'source ~/.sprint_summary_env && ...'` for curl commands — `source` alone does not work in zsh subshells spawned by the Bash tool.

## Instructions

Follow these steps exactly:

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for optional parameters:

1. **Sprint name** (optional) — a quoted or unquoted sprint name (e.g., `"COPS Sprint 2026 4"`). This is any positional text that isn't a flag.
2. **`--team <name>`** (optional) — filter to a single team by its vault directory name (e.g., `--team ACE`).
3. **`--dry-run`** (optional) — preview output without writing any files.

If no arguments are provided, default to: latest completed sprint, all teams.

Show the parsed values to the user for confirmation:
```
Sprint: {sprint_name or "latest completed"}
Team: {team_filter or "all"}
Dry run: {yes/no}
```

### Step 2 — Load environment

Load environment variables using a bash subshell to ensure proper export handling:
```bash
bash -c 'source ~/.obsidian_env && source ~/.sprint_summary_env && echo "VAULT: $(eval echo $OBSIDIAN_VAULT_PATH)" && echo "TEAMS: $(eval echo $OBSIDIAN_TEAMS_PATH)" && echo "JIRA: $JIRA_BASE_URL" && echo "SPRINT_TEAMS: $SPRINT_TEAMS" && echo "AUTH: $JIRA_EMAIL" && echo "GITLAB_URL: ${GITLAB_URL:-not set}" && echo "GITLAB_PROJECT_ID: ${GITLAB_PROJECT_ID:-not set}" && echo "GITLAB_TOKEN: ${GITLAB_TOKEN:+set}"'
```

Verify:
- `OBSIDIAN_VAULT_PATH` and `OBSIDIAN_TEAMS_PATH` are set. If not, stop and tell the user to configure `~/.obsidian_env`.
- `JIRA_BASE_URL`, `JIRA_STORY_POINTS_FIELD`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, and `SPRINT_TEAMS` are set. If not, stop and tell the user to configure `~/.sprint_summary_env`.
- The teams path exists on disk. If not, stop and tell the user.

**Important:** Never echo or display `$JIRA_API_TOKEN` or `$GITLAB_TOKEN`. Only confirm the email is set.

**GitLab config check:**
- If both `GITLAB_TOKEN` and `GITLAB_PROJECT_ID` are set → confirm "GitLab MR metrics: enabled".
- If only one is set → warn "GitLab partially configured — both GITLAB_TOKEN and GITLAB_PROJECT_ID are required. Skipping MR metrics."
- If neither is set → note "GitLab MR metrics: skipped (not configured)" — this is not an error.

### Step 3 — Resolve teams

Parse `$SPRINT_TEAMS` into a list of team objects. The format is comma-separated entries, each entry is pipe-delimited: `VAULT_DIR|PROJECT_KEY|BOARD_ID|DISPLAY_NAME`.

For each entry, extract:
- **vault_dir** — directory name under the teams path (e.g., `ACE`)
- **project_key** — Jira project key (e.g., `ACE`)
- **board_id** — Jira board ID (may be empty — will be discovered in Step 4)
- **display_name** — human-readable team name (e.g., `Admin Experience and Configuration`)

If `--team` was specified, filter to only the matching team (match against `vault_dir`, case-insensitive). If no match found, list available teams and stop.

Verify each team's vault directory exists at `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/`. If not, warn but continue with other teams.

### Step 4 — Find board and sprint

Use the **Jira Agile REST API** via `curl` for this step. The Atlassian MCP does not support board or sprint endpoints.

**All curl commands MUST use `bash -c` to ensure env vars are properly sourced:**
```bash
bash -c 'source ~/.sprint_summary_env && curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" "..."'
```

#### 4a. Discover board (skip if board_id is set in config)

If `board_id` is empty for a team, discover it:

```bash
bash -c 'source ~/.sprint_summary_env && curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_BASE_URL/rest/agile/1.0/board?projectKeyOrId={PROJECT_KEY}&type=scrum"' \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for b in data.get('values', []):
    print(f\"{b['id']}|{b['name']}|{b['type']}\")
"
```

If multiple boards exist, prefer the one whose name contains the project key or "Scrum". Record the `board_id`.

Tell the user the discovered board ID so they can update `~/.sprint_summary_env` to skip discovery next time.

#### 4b. Find sprint and get details

Run **one curl per team** to get the latest closed sprint with full details. This combines the sprint listing and detail fetch into a single call:

**If no sprint name (latest completed) — run all teams in parallel:**

```bash
bash -c 'source ~/.sprint_summary_env && curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_BASE_URL/rest/agile/1.0/board/{BOARD_ID}/sprint?state=closed&maxResults=50"' \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
sprints = sorted(data.get('values', []), key=lambda s: s.get('completeDate', s.get('endDate', '')), reverse=True)
if sprints:
    s = sprints[0]
    print(f\"ID: {s['id']}\")
    print(f\"Name: {s['name']}\")
    print(f\"State: {s['state']}\")
    print(f\"Start: {s.get('startDate', 'N/A')}\")
    print(f\"End: {s.get('endDate', 'N/A')}\")
    print(f\"Completed: {s.get('completeDate', 'N/A')}\")
    print(f\"Goal: {s.get('goal', '')}\")
else:
    print('NO_SPRINTS_FOUND')
"
```

**If a sprint name was provided**, add `state=active,closed` and filter by name:

```bash
bash -c 'source ~/.sprint_summary_env && curl -s -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
  "$JIRA_BASE_URL/rest/agile/1.0/board/{BOARD_ID}/sprint?state=active,closed&maxResults=50"' \
  | python3 -c "
import sys, json
name_filter = '{SPRINT_NAME}'.lower()
data = json.load(sys.stdin)
for s in data.get('values', []):
    if name_filter in s['name'].lower():
        print(f\"ID: {s['id']}\")
        print(f\"Name: {s['name']}\")
        print(f\"State: {s['state']}\")
        print(f\"Start: {s.get('startDate', 'N/A')}\")
        print(f\"End: {s.get('endDate', 'N/A')}\")
        print(f\"Completed: {s.get('completeDate', 'N/A')}\")
        print(f\"Goal: {s.get('goal', '')}\")
        break
else:
    print('NO_MATCH_FOUND')
"
```

Record for each team: `sprint_id`, `sprint_name`, `start_date`, `end_date`, `goal`, `board_id`.

If no matching sprint is found for a team, warn the user and skip that team.

**Note on pagination:** If there are many closed sprints, the most recent may not be in the first 50. The API returns sprints ordered by backlog position (oldest first for closed). If `isLast` is false in the response, paginate forward or use a high `startAt` value.

### Step 5 — Fetch sprint data

Use the **Atlassian MCP tool** `searchJiraIssuesUsingJql` for issue queries. Use `maxResults: 50` per CLAUDE.md requirements. Query using the **sprint ID** (numeric) for reliability:

```
sprint = {sprintId} AND project = {PROJECT_KEY} ORDER BY issuetype, priority DESC
```

Request fields: `summary`, `status`, `issuetype`, `assignee`, `priority`, and the story points field (`customfield_10021`).

**Run MCP queries for all teams in parallel** when possible.

**Pagination:** If `totalCount` exceeds 50, make additional queries using the `nextPageToken` parameter until all issues are fetched.

**If the MCP result is too large** (the tool saves results to a file), parse it with python3. The MCP wraps results as `[{type: "text", text: "<json>"}]`:

```bash
python3 -c "
import json, sys
with open('{RESULT_FILE}') as f:
    raw = json.load(f)
issues = json.loads(raw[0]['text'])['issues']
print(f'Total: {len(issues)}')
for issue in issues:
    f = issue['fields']
    key = issue['key']
    itype = f['issuetype']['name']
    status = f['status']['name']
    cat = f['status']['statusCategory']['name']
    assignee = f['assignee']['displayName'] if f.get('assignee') else 'Unassigned'
    pts = f.get('customfield_10021')
    pts = '-' if pts is None else pts
    pri = f.get('priority', {}).get('name', 'Default')
    print(f'{key}|{itype}|{status}|{cat}|{assignee}|{pts}|{pri}|{f[\"summary\"]}')
"
```

Process the results:

1. **Separate issues** into two groups:
   - **Completed** — status category is "Done" (includes statuses: Done, Closed, Resolved, Cancelled)
   - **Carry-over** — everything else (In Progress, To Do, In Review, On Hold, Open, Planned, Backlog, etc.)

2. **Calculate metrics:**
   - **Points committed** — sum of story points for all issues in the sprint (skip issues with no points)
   - **Points completed** — sum of story points for completed issues only
   - **Completion rate** — `(points_completed / points_committed) * 100`, rounded to nearest integer. If zero committed, show `N/A`.
   - **Issue counts by type** — count of completed vs total for each issue type (Story, Bug, Task, etc.)
   - **Total issues** — completed count and total count

3. **For each issue, record:**
   - Issue key (e.g., `COPS-123`)
   - Summary
   - Issue type
   - Status
   - Assignee display name (or "Unassigned")
   - Story points (or `-` if none)
   - Priority

### Step 5b — Fetch GitLab MR data

**Skip this step entirely** if `GITLAB_TOKEN` or `GITLAB_PROJECT_ID` are not set. Omit the Engineering Metrics section from the output.

Query GitLab for merged MRs within the sprint window, match them to sprint issues via branch names, and calculate metrics.

#### 5b.1 — Fetch merged MRs

Query all merged MRs updated within the sprint window (with 1-day buffer on each side):

```bash
bash -c 'source ~/.sprint_summary_env && curl -s \
  --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  "$GITLAB_URL/api/v4/projects/$GITLAB_PROJECT_ID/merge_requests?state=merged&updated_after={start_date_minus_1}&updated_before={end_date_plus_1}&per_page=100"'
```

**Paginate** if the response contains 100 items — increment the `page` param (add `&page=2`, `&page=3`, etc.) until fewer than 100 results are returned.

#### 5b.2 — Filter and match MRs

1. **Filter** to MRs where `merged_at` falls within the sprint date range (inclusive).
2. **Match to sprint issues** — extract issue keys from `source_branch` using a regex built from the configured project keys. For example, with teams ACE and COPS, use: `(ACE|COPS)-\d+`. Only keep MRs whose branch contains a matching issue key that exists in the sprint issue list from Step 5.
3. MRs without a matching issue key (infra work, etc.) are excluded.

#### 5b.3 — Extract MR details

For each matched MR, extract:
- `iid`, `title`, `web_url`
- `author.name`, `source_branch`
- `created_at`, `merged_at` → calculate time-to-merge in hours
- `merge_user.name`

#### 5b.4 — Calculate aggregate metrics

- **Total MRs merged** — count of matched MRs
- **Avg time to merge** — mean of (merged_at - created_at) in hours, 1 decimal place
- **Median time to merge** — median of same
- **MRs per person** — count grouped by `author.name`, with per-author avg time to merge
- **Longest time to merge** — max value with MR title for context

**Time display format:** `"4h"` for <24h, `"2d 4h"` for >=24h.

### Step 6 — Interactive prompts

Use `AskUserQuestion` to gather optional context. Each question needs at least 2 options.

**If generating summaries for multiple teams**, ask about goals per team. Highlights and blockers can apply to all.

**Sprint goals:** For each team, if a goal was retrieved from the Jira API in Step 4:
- "Sprint goal from Jira for {team}: '{goal_text}'. Use this?"
- Options: "Use Jira goal", "Enter custom goals" (user types via Other)

If no goal from API: "What were the sprint goals for {team}?" with options "Enter goals" (via Other), "Skip"

**Highlights:** "Any highlights or wins to call out?"
- Options: "Enter highlights" (via Other), "Skip"

**Blockers:** "Any blockers or concerns to note?"
- Options: "Enter blockers" (via Other), "Skip"

### Step 7 — Generate markdown

For each team, build the sprint summary markdown using this template:

```markdown
---
type: sprint-summary
team: {vault_dir}
project_key: {project_key}
sprint_name: "{sprint_name}"
sprint_id: {sprint_id}
start_date: {YYYY-MM-DD}
end_date: {YYYY-MM-DD}
points_committed: {N}
points_completed: {N}
completion_rate: {N}
mrs_merged: {N}              # only if GitLab data present
avg_time_to_merge_hours: {N} # only if GitLab data present
generated: {YYYY-MM-DDTHH:MM:SSZ}
source: jira
---

# {sprint_name} — {display_name}

**{start_date_display} to {end_date_display}**

## Sprint Goals

{Sprint goal from Jira API, user-provided goals, or "<!-- Add sprint goals here -->"}

## Metrics

| Metric | Value |
|--------|-------|
| Points Committed | {points_committed} |
| Points Completed | {points_completed} |
| Completion Rate | {completion_rate}% |
| Issues Completed | {completed_count} / {total_count} |

### By Issue Type

| Type | Completed | Total | Points |
|------|-----------|-------|--------|
{For each issue type: | {type} | {completed} | {total} | {points} |}

## Engineering Metrics

{Only include this section if GitLab data is available and matching MRs were found. Omit entirely otherwise.}

| Metric | Value |
|--------|-------|
| Merge Requests Merged | {total_mrs} |
| Avg Time to Merge | {avg_ttm} |
| Median Time to Merge | {median_ttm} |
| Longest Time to Merge | {max_ttm} ({mr_title}) |

### MRs by Author

| Author | MRs | Avg Time to Merge |
|--------|-----|-------------------|
| {name} | {count} | {avg} |

### Merge Request Details

| MR | Issue | Author | Time to Merge |
|----|-------|--------|---------------|
| [!{iid}]({web_url}) | [{KEY}]({JIRA_BASE_URL}/browse/{KEY}) | {author} | {ttm} |

## Completed Work

{For each issue type that has completed items, grouped under ### {Issue Type}:}

### {Issue Type}

| Key | Summary | Assignee | Points |
|-----|---------|----------|--------|
{For each completed issue of this type:}
| [{KEY}]({JIRA_BASE_URL}/browse/{KEY}) | {summary} | {assignee} | {points} |

## Carry-Over

{If carry-over items exist:}

| Key | Summary | Status | Assignee | Points |
|-----|---------|--------|----------|--------|
{For each carry-over issue:}
| [{KEY}]({JIRA_BASE_URL}/browse/{KEY}) | {summary} | {status} | {assignee} | {points} |

{If no carry-over items: "No carry-over items."}

## Highlights

{User-provided highlights text, or "<!-- Add highlights and wins here -->"}

## Blockers & Risks

{User-provided blockers text, or "<!-- Add blockers and risks here -->"}

## Notes

<!-- Add additional notes here -->
```

**Formatting rules:**
- `start_date_display` and `end_date_display` should be human-readable (e.g., "3 Mar 2026")
- Issue keys should be linked to Jira using `{JIRA_BASE_URL}/browse/{KEY}`
- Points should show `-` if no story points assigned
- Sort completed work within each type group by points descending (highest first), then alphabetically by key
- Sort carry-over items by points descending (highest first), then alphabetically by key
- The `generated` timestamp should be current UTC time in ISO 8601 format

### Step 8 — Write to vault

For each team, write the output file.

**Output path:** `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints/{sprint_name} - {end_date}.md`

Where `{end_date}` is in `YYYY-MM-DD` format.

Create the `Sprints/` directory if it doesn't exist:
```bash
mkdir -p "{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Sprints"
```

**Normal mode:** Use the Write tool to create the file. Confirm to the user:
```
Sprint summary written to: {file_path}
```

**Dry-run mode:** Print the full file content to output instead of writing. Prefix with:
```
**DRY RUN** — would write to: {file_path}
```

**Idempotent:** Running again for the same sprint overwrites the previous file.

After all teams are processed, show a final summary:
```
Sprint summaries generated:
- {team1}: {sprint_name} ({completion_rate}%, {points_completed}/{points_committed} pts) → {file_path}
- {team2}: {sprint_name} ({completion_rate}%, {points_completed}/{points_committed} pts) → {file_path}
```
