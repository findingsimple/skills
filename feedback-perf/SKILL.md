---
name: feedback-perf
description: Capture and manage performance review feedback in Obsidian vault
disable-model-invocation: true
argument-hint: "capture <name>: <feedback> | capture [mid|eoy] <name>: <feedback>"
allowed-tools: Read Edit Write Glob Bash
---

# Performance Review Feedback

Capture performance feedback for team members into their review cycle documents in the Obsidian vault.

## Subcommands

- **capture** — Append a dated feedback note to a team member's review cycle document

## Instructions

### Step 0 — Resolve vault path

Use the first non-empty source found:

1. `$OBSIDIAN_VAULT_PATH` from `~/.obsidian_env`

To load it:
```bash
source ~/.obsidian_env
echo "$OBSIDIAN_VAULT_PATH"
```

If not set, stop and tell the user to add `export OBSIDIAN_VAULT_PATH=/path/to/vault` to `~/.obsidian_env`.

Verify the resolved path exists with `ls`. The **teams base path** is `{vault_path}/HappyCo/Teams`.

### Step 1 — Discover team members

Scan the vault for person profile notes. Use Glob to find markdown files:

```
{teams_base}/**/*.md
```

A valid profile file must:
- Have YAML frontmatter with a `team` field
- Have a non-empty `team` value that is a **single team name** (e.g., `"ACE"` or `"COPS"` — skip comma-separated values like `"ACE, COPS"`)
- Not be inside a `Feedback/`, `1:1s/`, or `Logs/` subdirectory

For each valid profile file, extract:
- **name**: the parent directory name (the person's full name)
- **team**: the `team` frontmatter value
- **person_dir**: the full path to the person's directory

Read only the first 15 lines of each candidate file to check frontmatter. Build a list of people: `[ { name, team, person_dir }, ... ]`

**Review cycle files** for each person:
- Mid Year: `{person_dir}/Feedback/Mid Year Review Cycle - {year}.md`
- EOY: `{person_dir}/Feedback/EOY Review Cycle - {year}.md`

---

### Subcommand: `capture`

**Usage:**
```
/feedback capture <name>: <feedback>
/feedback capture mid <name>: <feedback>
/feedback capture eoy <name>: <feedback>
```

**Examples:**
```
/feedback capture Alex: Great cross-team collaboration on auto-assign
/feedback capture eoy Jordan: Led the migration to the new auth system
/feedback capture mid Sam: Improved deploy pipeline reliability significantly
```

Follow these steps exactly:

#### Step 2 — Parse arguments

Parse `$ARGUMENTS` to extract:

1. **Subcommand** — must be `capture`. If missing or unrecognized, show usage help and stop.
2. **Cycle override** (optional) — `mid` or `eoy` immediately after `capture`. If absent, determine from current month:
   - Jan–Jun → `mid` (Mid Year Review Cycle)
   - Jul–Dec → `eoy` (EOY Review Cycle)
3. **Name** — everything before the first `:` (after removing subcommand and optional cycle). Trim whitespace.
4. **Feedback content** — everything after the first `:`. Trim whitespace.

If name or feedback content is missing, show usage help and stop.

#### Step 3 — Match team member

Match the provided name against the discovered team members (from Step 1). Use flexible matching:
- Case-insensitive
- First name only (e.g., "Alex" matches "Alex Chen")
- Partial match (e.g., "Jor" matches "Jordan Park")
- Full name (e.g., "Alex Chen")

If zero matches, tell the user the name wasn't recognized and list available team members.
If multiple matches, tell the user the name is ambiguous and list the matches.

#### Step 4 — Resolve review cycle file

Build the file path:
- Determine the current year using today's date.
- Use the cycle type (from Step 2) to select the file:
  - `mid` → `{person_dir}/Feedback/Mid Year Review Cycle - {year}.md`
  - `eoy` → `{person_dir}/Feedback/EOY Review Cycle - {year}.md`

Read the file. If it doesn't exist, create it with the appropriate template:

**Mid Year template:**
```
==Living document for capturing feedback for upcoming {year} Mid Review Cycle for {Full Name}==
## What should this person keep doing that contributes to the success of HappyCo?

xxx

## What should this person change or stop as it is not contributing to a high performance culture?

xxx

## Given what I know of this person's performance, I would always want them on my team.

xxx
```

**EOY template:**
```
==Living document for capturing feedback for upcoming {year} EOY Review Cycle for {Full Name}==
## What are the most significant accomplishments this HappyCo'er has demonstrated this year?

xxx

## What areas for growth and improvement do you recommend for this HappyCo'er?

xxx
```

#### Step 5 — Append feedback

Look for an existing `## Captured Feedback` section in the file.

- **If the section exists:** Append the new bullet to the end of that section (before the next `##` heading or end of file).
- **If the section does not exist:** Add it at the end of the file:

```markdown

## Captured Feedback

- **{YYYY-MM-DD}** — {feedback content}
```

The date should be today's date.

Use the Edit tool to make the change. Do not modify any other content in the file.

#### Step 6 — Confirm

Output a short confirmation:

```
Captured feedback for {Full Name} → {cycle_type} review ({year})
"{feedback content}"
```

Where `{cycle_type}` is "Mid Year" or "EOY".
