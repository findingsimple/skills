---
name: bonusly-sync
description: Sync previous month's Bonusly recognition to Obsidian vault
disable-model-invocation: true
argument-hint: "--dry-run or vault-path"
---

# Bonusly Recognition Sync

Sync the previous month's Bonusly recognition (given and received) for tracked team members into their `Feedback/` folders in the Obsidian vault.

## Architecture

This skill uses **two Python scripts** (`bonusly_client.py` and `generate.py`) in the skill directory. They handle all API calls, data processing, and file generation.

### Data sources (accessed within the Python scripts)

1. **Bonusly REST API** — bonus data: received and given recognition, with child bonuses (add-ons) and pagination.

### Key principles

**Idempotent:** Running multiple times in the same month will overwrite existing files for that period. This is intentional — re-running picks up any late +1 add-ons or recognition posted after the first sync.

## Instructions

Follow these steps exactly. The skill requires **1 bash command** (1 python3 run) plus vault scanning and user interaction.

### Step 0 — Parse arguments

Check `$ARGUMENTS` for flags and positional args:

- If `$ARGUMENTS` contains `--dry-run`, enable **dry-run mode**. In this mode, all steps run normally (API calls, data fetching, markdown generation) but no files are created or modified — the script prints file paths and content to output for review.
- Any remaining argument (after removing `--dry-run`) is treated as the vault path.

Examples:
- `/bonusly-sync --dry-run` — dry run with vault from env
- `/bonusly-sync --dry-run /path/to/vault` — dry run with custom vault
- `/bonusly-sync /path/to/vault` — normal run with custom vault

### Step 1 — Resolve paths

Load environment variables:
```bash
source ~/.obsidian_env
echo "$OBSIDIAN_TEAMS_PATH"
```

The **teams base path** is determined by the first non-empty source:

1. Path from `$ARGUMENTS` (after removing `--dry-run` if present) — treated as the teams directory
2. `$OBSIDIAN_TEAMS_PATH` from `~/.obsidian_env`

If neither is set, stop and tell the user to either pass a path as an argument (`/bonusly-sync /path/to/teams`) or add `export OBSIDIAN_TEAMS_PATH=/path/to/vault/Teams` to `~/.obsidian_env`.

Verify the resolved path exists with `ls`. If it doesn't exist, stop and tell the user.

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

Scan the teams directory for person notes with an `email` field in their YAML frontmatter. Use Glob to find all markdown files:

```
{teams_base}/**/*.md
```

For each file found, use Read to check the first 20 lines for YAML frontmatter containing an `email:` field. Extract:
- **email**: the email address from frontmatter
- **person name**: the parent directory name (the directory the file is in, which is the person's name)
- **person dir**: the full path to the person's directory

Skip files that don't have an `email` field in frontmatter. Skip files inside `Feedback/` subdirectories.

Build a list of people: `[ { name, email, dir }, ... ]`

### Step 5 — Write people file and run generate.py

Write the discovered people list to `/tmp/bonusly_people.json` using a bash heredoc:

```bash
cat << 'EOF' > /tmp/bonusly_people.json
[
  {"name": "Alex Chen", "email": "alex@example.com", "dir": "/path/to/vault/Teams/Team/Alex Chen"},
  ...
]
EOF
```

Then run:

```bash
python3 ~/.claude/skills/bonusly-sync/generate.py --people-file /tmp/bonusly_people.json --start-time "{START_TIME}" --end-time "{END_TIME}" --period "{PERIOD}" --period-label "{PERIOD_LABEL}" --teams-base "{teams_base}" [--dry-run]
```

Pass `--dry-run` if the user specified it in Step 0.

The script fetches received and given bonuses for each person (with pagination), generates per-person markdown files, writes a sync log, and outputs a summary table.

**Output paths:**
- Per-person files: `{person_dir}/Feedback/Bonusly - {PERIOD}.md`
- Sync log: `{teams_base}/Logs/Bonusly Sync - {PERIOD}.md`

### Step 6 — Confirm completion

After the generate script finishes, show the user the summary output from the script.
