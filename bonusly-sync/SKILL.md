---
name: bonusly-sync
description: Sync previous month's Bonusly recognition to Obsidian vault
disable-model-invocation: true
argument-hint: "--dry-run or vault-path"
---

# Bonusly Recognition Sync

Sync the previous month's Bonusly recognition (given and received) for tracked team members into their `Feedback/` folders in the Obsidian vault.

## Instructions

Follow these steps exactly:

### Step 0 — Parse arguments

Check `$ARGUMENTS` for flags and positional args:

- If `$ARGUMENTS` contains `--dry-run`, enable **dry-run mode**. In this mode, all steps run normally (API calls, data fetching, markdown generation) but instead of writing files with the Write tool, **print the file path and full content to the output** so the user can review it. No files are created or modified.
- Any remaining argument (after removing `--dry-run`) is treated as the vault path.

Examples:
- `/bonusly-sync --dry-run` — dry run with vault from env
- `/bonusly-sync --dry-run /path/to/vault` — dry run with custom vault
- `/bonusly-sync /path/to/vault` — normal run with custom vault

### Step 1 — Resolve vault path

Use the first non-empty source found:

1. Vault path from `$ARGUMENTS` (after removing `--dry-run` if present)
2. `$OBSIDIAN_VAULT_PATH` from `~/.obsidian_env`

To load it:
```bash
source ~/.obsidian_env
echo "$OBSIDIAN_VAULT_PATH"
```

If neither is set, stop and tell the user to either pass a vault path as an argument (`/bonusly-sync /path/to/vault`) or add `export OBSIDIAN_VAULT_PATH=/path/to/vault` to `~/.obsidian_env`.

Verify the resolved vault path exists with `ls`. If it doesn't exist, stop and tell the user.

### Step 2 — Load API token

Run:
```bash
source ~/.bonusly_env
echo "$BONUSLY_API_TOKEN" | head -c 8
```

Confirm that `BONUSLY_API_TOKEN` is set (the echo should print something). If it's empty, stop and tell the user to create `~/.bonusly_env` with `export BONUSLY_API_TOKEN=your_token`.

### Step 3 — Calculate date range

Use `python3` to calculate the previous month's date range:

```bash
python3 -c "
from datetime import date, timedelta
today = date.today()
first_of_this_month = today.replace(day=1)
last_month_end = first_of_this_month - timedelta(days=1)
first_of_last_month = last_month_end.replace(day=1)
print(f'{first_of_last_month.isoformat()}T00:00:00Z')
print(f'{first_of_this_month.isoformat()}T00:00:00Z')
print(first_of_last_month.strftime('%Y-%m'))
print(first_of_last_month.strftime('%B %Y'))
"
```

Capture the four output lines as: `START_TIME`, `END_TIME`, `PERIOD` (e.g. `2026-01`), and `PERIOD_LABEL` (e.g. `January 2026`).

### Step 4 — Discover people

Scan the vault for person notes with an `email` field in their YAML frontmatter. Use Glob to find all markdown files under `{vault}/HappyCo/Teams/`:

```
{vault}/HappyCo/Teams/**/*.md
```

For each file found, use Read to check the first 20 lines for YAML frontmatter containing an `email:` field. Extract:
- **email**: the email address from frontmatter
- **person name**: the parent directory name (the directory the file is in, which is the person's name)
- **person dir**: the full path to the person's directory

Skip files that don't have an `email` field in frontmatter. Skip files inside `Feedback/` subdirectories.

Build a list of people: `[ { name, email, dir }, ... ]`

### Step 5 — Fetch bonuses for each person

For each person, fetch both received and given bonuses using `curl` and parse with `python3`.

**Fetch received bonuses** (with pagination):

```bash
source ~/.bonusly_env && curl -s -H "Authorization: Bearer $BONUSLY_API_TOKEN" \
  "https://bonus.ly/api/v1/bonuses?start_time={START_TIME}&end_time={END_TIME}&receiver_email={email}&include_children=true&limit=100&skip=0"
```

**Fetch given bonuses** (with pagination):

```bash
source ~/.bonusly_env && curl -s -H "Authorization: Bearer $BONUSLY_API_TOKEN" \
  "https://bonus.ly/api/v1/bonuses?start_time={START_TIME}&end_time={END_TIME}&giver_email={email}&include_children=true&limit=100&skip=0"
```

For each API call:
1. Parse the JSON response with `python3`
2. Check if `result` array has items
3. If the response contains 100 items, fetch the next page with `skip=100`, `skip=200`, etc. until fewer than 100 items are returned
4. Combine all pages

From each bonus, extract:
- `created_at` — format as `YYYY-MM-DD`
- `amount` — points awarded
- `giver.full_name` — who gave it
- `receiver.full_name` — who received it
- `reason` — the recognition text (plain text with hashtags)
- `child_count` — number of +1 add-ons
- `child_bonuses` — array of add-on bonuses, each with `giver.full_name`, `amount`, and `reason`

### Step 6 — Generate markdown files

For each person who has at least one received or given bonus, create a markdown file.

**File path:** `{person_dir}/Feedback/Bonusly - {PERIOD}.md`

Create the `Feedback/` directory if it doesn't exist: `mkdir -p {person_dir}/Feedback`

**File format:**

```markdown
---
source: bonusly
period: "{PERIOD}"
generated_at: "{CURRENT_ISO_TIMESTAMP}"
---

# Bonusly Recognition — {PERIOD_LABEL}

## Received ({TOTAL_RECEIVED_POINTS} smiles)

- **{DATE}** — +{AMOUNT} from **{GIVER_NAME}**: "{REASON}"
  - +1 from **{CHILD_GIVER_NAME}** (+{CHILD_AMOUNT}): "{CHILD_REASON}"
  - +1 from **{CHILD_GIVER_NAME}** (+{CHILD_AMOUNT})
- ...

## Given ({TOTAL_GIVEN_POINTS} smiles)

- **{DATE}** — +{AMOUNT} to **{RECEIVER_NAME}**: "{REASON}"
- ...
```

**Rules:**
- If a bonus has `child_bonuses`, list each as a sub-bullet beneath the parent. Include the child's reason in quotes if it's non-empty; omit it if the child has no reason (just a bare +1).
- Only show child bonuses under received entries (not under given entries, since those +1s aren't relevant to the person's own recognition)
- Sort entries within each section by date ascending
- If the "Received" section has no entries, omit the entire `## Received` section
- If the "Given" section has no entries, omit the entire `## Given` section
- If a person has zero received AND zero given, do NOT create a file at all
- **Normal mode:** Use the Write tool to create each file
- **Dry-run mode:** Instead of writing, output the file path and full generated markdown content so the user can review it. Do NOT create any files or directories.
- `generated_at` should be the current UTC timestamp in ISO 8601 format
- **Idempotent:** Running multiple times in the same month will overwrite the existing file for that period. This is intentional — re-running picks up any late +1 add-ons or recognition posted after the first sync.

### Step 7 — Write sync log

Write a summary log file to `{vault}/HappyCo/Teams/Logs/Bonusly Sync - {PERIOD}.md`. Create the `Logs/` directory with `mkdir -p` if it doesn't exist.

**File format:**

```markdown
---
source: bonusly
type: sync-log
period: "{PERIOD}"
synced_at: "{CURRENT_ISO_TIMESTAMP}"
---

# Bonusly Sync — {PERIOD_LABEL}

| Person | Received | Given | File |
|--------|----------|-------|------|
| Alex Chen | 3 (45 pts) | 1 (15 pts) | Created |
| Jordan Park | 0 | 0 | Skipped |
| ... | ... | ... | ... |

**{N} files created**, {M} skipped. {TOTAL} people processed.
```

In dry-run mode, do not write the log file — just print it to the output. Use `Would create` instead of `Created` in the File column.

### Step 8 — Output summary

After writing the log file, also output the same summary table to the user, prefixed with:

- **Normal mode:** `Bonusly sync complete for {PERIOD_LABEL}:`
- **Dry-run mode:** `**DRY RUN** — no files were written.`

Report the total number of files created (or previewed) and people processed.
