---
name: feedback-perf
description: Capture and manage performance review feedback in Obsidian vault
disable-model-invocation: true
argument-hint: "capture <name>: <feedback> | capture [mid|eoy] <name>: <feedback>"
allowed-tools: Read Edit Write Glob Bash Agent
---

# Performance Review Feedback

Capture performance feedback for team members into their review cycle documents in the Obsidian vault.

## Subcommands

- **capture** — Append a dated feedback note to a team member's review cycle document
- **synthesize** — Distill captured feedback and Bonusly data into polished review answers using the Performance Reviewer agent (Opus)

## Instructions

### Step 0 — Resolve paths

Check that `OBSIDIAN_TEAMS_PATH` is set:
```bash
echo "$OBSIDIAN_TEAMS_PATH"
```

The **teams base path** is `$OBSIDIAN_TEAMS_PATH` — the directory containing team/person subdirectories.

If not set, stop and tell the user to add `export OBSIDIAN_TEAMS_PATH=/path/to/vault/Teams` to `~/.zshrc`.

Verify the resolved path exists with `ls`.

### Step 1 — Discover team members

Scan the vault for person profile notes. Use Glob to find markdown files:

```
{teams_base}/**/*.md
```

A valid profile file must:
- Have YAML frontmatter (delimited by `---`)
- Not be inside a `Feedback/`, `1:1s/`, `Logs/`, or `Me/` subdirectory
- Have a `team` value that is either empty or a **single team name** (e.g., `"TeamA"` or `"TeamB"` — skip comma-separated values like `"TeamA, TeamB"` which indicate a manager, not a team member)

For each valid profile file, extract:
- **name**: the parent directory name (the person's full name)
- **team**: the `team` frontmatter value (may be empty for cross-functional colleagues)
- **person_dir**: the full path to the person's directory

Read only the first 15 lines of each candidate file to check frontmatter. Build a list of people: `[ { name, team, person_dir }, ... ]`

**Review cycle files** for each person:
- Mid Year: `{person_dir}/Feedback/Mid Year Review Cycle - {year}.md`
- EOY: `{person_dir}/Feedback/EOY Review Cycle - {year}.md`

---

### Subcommand: `capture`

**Usage:**
```
/feedback-perf capture <name>: <feedback>
/feedback-perf capture mid <name>: <feedback>
/feedback-perf capture eoy <name>: <feedback>
```

**Examples:**
```
/feedback-perf capture Alex: Great cross-team collaboration on auto-assign
/feedback-perf capture eoy Jordan: Led the migration to the new auth system
/feedback-perf capture mid Sam: Improved deploy pipeline reliability significantly
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
## What should this person keep doing that contributes to the success of the company?

xxx

## What should this person change or stop as it is not contributing to a high performance culture?

xxx

## Given what I know of this person's performance, I would always want them on my team.

xxx
```

**EOY template:**
```
==Living document for capturing feedback for upcoming {year} EOY Review Cycle for {Full Name}==
## What are the most significant accomplishments this person has demonstrated this year?

xxx

## What areas for growth and improvement do you recommend for this person?

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

---

### Subcommand: `synthesize`

**Usage:**
```
/feedback-perf synthesize <name>
/feedback-perf synthesize mid <name>
/feedback-perf synthesize eoy <name>
```

Follow these steps exactly:

#### Step 2 — Parse arguments

Parse `$ARGUMENTS` to extract:

1. **Subcommand** — must be `synthesize`. If missing or unrecognized, show usage help and stop.
2. **Cycle override** (optional) — `mid` or `eoy` immediately after `synthesize`. If absent, determine from current month:
   - Jan–Jun → `mid` (Mid Year Review Cycle)
   - Jul–Dec → `eoy` (EOY Review Cycle)
3. **Name** — everything after the subcommand (and optional cycle). Trim whitespace.

If name is missing, show usage help and stop.

#### Step 3 — Match team member

Same matching logic as the `capture` subcommand (case-insensitive, first name, partial, full name).

#### Step 4 — Gather all inputs

Collect the following files for the matched person. Read each one that exists:

1. **Person profile** — `{person_dir}/{Full Name}.md`
2. **Target review cycle document** — based on cycle type and year (same path resolution as `capture`)
3. **Bonusly recognition files** — all `{person_dir}/Feedback/Bonusly - *.md` files for the current year
4. **Prior review packets** — all files in `{person_dir}/Feedback/*/Review Packet - *.md`

If the target review cycle document has no `## Captured Feedback` section or it's empty (no bullet points), warn the manager that there's no captured feedback to synthesize and ask if they want to proceed with only Bonusly and prior review data.

#### Step 5 — Spawn the Performance Reviewer agent

Use the Agent tool to spawn the `performance-reviewer` agent with the following prompt. Include the full content of every file gathered in Step 4 in the prompt — the agent runs in a forked context and has no access to the conversation history.

```
You are synthesizing a {cycle_type} performance review for {Full Name} ({team} team).

## Review Cycle
{cycle_type}: {Mid Year | EOY}
Year: {year}

## Person Profile
{contents of profile .md}

## Review Cycle Document (target — write responses for each question)
{contents of review cycle .md}

## Bonusly Peer Recognition
{contents of all Bonusly files, or "No Bonusly data available for this period."}

## Prior Review Packets
{contents of prior review packets, or "No prior review packets available."}

---

Synthesize the captured feedback, peer recognition, and prior context into draft responses for each review question in the review cycle document. Follow your output format exactly.
```

#### Step 6 — Present results

Display the agent's output directly to the manager. Do NOT automatically write it into the review cycle document.

Ask the manager:
- "Would you like me to write these draft responses into the review document?"
- "Would you like to adjust anything first?"

Only write to the document if the manager confirms. When writing, replace the placeholder content (`xxx`/`xxxx`) under each question heading with the draft responses, but **preserve the `## Captured Feedback` section unchanged**.
