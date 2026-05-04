---
name: review-memory
description: Reviews accumulated per-project Claude memory entries and proposes migrations to repo CLAUDE.md, global ~/.claude/CLAUDE.md, or deletion. Use when the user asks to review, sweep, audit, or clean up their Claude memory, or to check whether memories belong in the repo instead.
disable-model-invocation: true
argument-hint: "[--project <encoded-name-or-path>]"
allowed-tools: Bash Read Edit Write Glob
---

# Review Memory

Sweep `~/.claude/projects/*/memory/` across every project on this machine. For each memory entry, propose ONE action — keep, move to repo CLAUDE.md, move to global `~/.claude/CLAUDE.md`, or delete — with a reason. User approves per item before any side effects.

## When to use this

- "Review my memory"
- "Are there memories that should be in the repo?"
- "Sweep / audit / clean up memory"
- "Check what's in memory across my projects"

## Why memory vs CLAUDE.md vs global

- **Repo `CLAUDE.md`** — code conventions, design rules, anything that should apply to anyone touching this repo regardless of whose machine they're on. *"How we build skills here."*
- **Project memory** (`~/.claude/projects/<encoded>/memory/`) — personal collaboration preferences with Claude in this project, project ephemera, watch-points only relevant to Claude's behavior in this repo. *"How Claude should work with the user on this repo."*
- **Global `~/.claude/CLAUDE.md`** — preferences that apply to *every* repo on this machine. *"Universal rules and per-user defaults."*

A memory file that reads "always emit `[[X]]` only for vault-resident entities" is a code rule and belongs in the repo. A memory file that reads "user wants terse responses" is a preference and stays in memory.

## Architecture

- **`discover.py`** — deterministic scan; writes inventory to `/tmp/review_memory/inventory.json`. No judgment, no migrations.
- **This SKILL.md** — orchestrates classification (model judgment) + per-item user approval + execution.

The conversation is the loop: scan → propose → confirm → act.

## Argument allow-list

- `--project <name-or-path>` — filter to a single project. Accepts either the encoded directory name (e.g. `-Users-jasonconroy--claude-skills`) or the decoded CWD path (e.g. `/Users/jasonconroy/.claude/skills`). Validated by `discover.py` against the projects list — anything that doesn't match a real project is ignored.

No other arguments. No `--auto` mode (per-item approval is the point).

## Instructions

Follow these steps exactly.

### Step 1 — Run discovery

```bash
python3 ~/.claude/skills/review-memory/discover.py [--project ARG]
```

This writes `/tmp/review_memory/inventory.json`. The script prints a one-line summary; capture project count, memory count, and flagged-duplicate count.

If the script exits non-zero, surface its stderr message to the user and stop.

### Step 2 — Read the inventory

```bash
cat /tmp/review_memory/inventory.json
```

The inventory is structured as:
- `projects[]` — one record per project memory dir
  - `project_name` — encoded dir name
  - `decoded_path` — likely original CWD
  - `decoded_path_exists` — `true` if that path is still present on this machine
  - `repo_claude_md_exists` — `true` if `<decoded_path>/CLAUDE.md` is readable
  - `memories[]` — per-file: filename, mtime_iso, age_days, frontmatter (name/description/type), body_preview, body_full, dedup_signal

Don't print the raw JSON to the user — work from it.

### Step 3 — Walk per-project

For each project record:

1. Print a **project header** showing:
   - Decoded path
   - `decoded_path_exists` flag (warn if false: "this project dir no longer exists on this machine — memory is orphaned")
   - Memory count + age range (oldest to newest)

2. For each memory entry, classify into ONE of:

   - **keep** — true personal preference, project ephemera, watch-point. *"User wants terse output." "Don't run_in_background when output is needed for the next step."*
   - **move-to-repo** — code/design rule applicable to anyone touching this codebase. *"Always validate env vars used in JQL with anchored regex." References a file path, function name, or specific code pattern.*
   - **move-to-global** — preference that applies across every project on this machine. *"User's author name is 'findingsimple'." "Atlassian cloudId convention."*
   - **delete** — duplicate of an existing CLAUDE.md rule (check `dedup_signal`), stale fact contradicted by current code, or one-shot context that's no longer relevant.

   For each entry, prepare:
   - The chosen action
   - One-sentence reason
   - If moving to a CLAUDE.md: the proposed bullet text in the existing file's voice (lead with **bold rule**, then explanation, reference if applicable)
   - If keeping: no further action

### Step 4 — Present batch and confirm

Render the proposals as a single review block per project:

```
## Project: /Users/jasonconroy/.claude/skills

  1. feedback_pulse_terse_output.md
     Action: KEEP
     Reason: Personal preference about output verbosity in pulse runs — not a code rule.

  2. feedback_some_old_rule.md
     Action: DELETE
     Reason: Duplicate of CLAUDE.md "Atomic JSON writes" convention (line 185).

  3. project_some_design_note.md
     Action: MOVE-TO-REPO
     Reason: Code rule about pipeline orchestration — belongs in CLAUDE.md Conventions.
     Proposed CLAUDE.md bullet:
       - **Pipeline cache lifetime** — orchestrated pipelines must call `clear_stale_state()` ...
```

Then ask the user:
- "Approve all? Skip specific items? Edit any proposed text?"

Wait for explicit user confirmation before proceeding. Do NOT execute on assumed approval.

### Step 5 — Execute approved actions

For each approved item, in this order:

1. **Move-to-repo:** Read `<decoded_path>/CLAUDE.md`, find the relevant section (Conventions, Security Notes, Skill Authoring Checklist — match by topic), use Edit to insert the new bullet. Keep within existing list ordering and voice. Then delete the memory file and update the project's `MEMORY.md` index (remove the line; preserve the rest of the index).
2. **Move-to-global:** Same flow but target `~/.claude/CLAUDE.md`. **Confirm again per item** before editing — global config is high-blast-radius and a single approve-all should not greenlight global edits.
3. **Delete:** Remove the memory file and the corresponding line from the project's `MEMORY.md` index.
4. **Keep:** No-op.

After each action, print a one-line confirmation.

### Step 6 — Commit (per project)

If the project's decoded path is a git repo and `CLAUDE.md` was modified, prepare a commit. Show the diff to the user, draft a commit message, ask for approval before committing. One commit per project (not per item) — the conventions added in this sweep are a single coherent batch.

Suggested message format:
```
docs(memory-sweep): promote N memories to CLAUDE.md

- "<memory name>" → Conventions §
- "<memory name>" → Security Notes §

Memories deleted from project memory after promotion.
```

Per the user's `~/.claude/CLAUDE.md`, commit messages MUST NOT include `Co-Authored-By` lines.

For projects that aren't git repos, skip the commit step but still print "files modified — not in a git repo, no commit attempted".

### Step 7 — Print summary

```
Reviewed 2 projects, 27 memories.
  /Users/jasonconroy/.claude/skills: 8 kept, 3 moved-to-repo, 2 deleted
  /Users/jasonconroy/hppy/connect:   12 kept, 1 moved-to-global, 1 deleted
Commits: 1 in skills, 0 in hppy/connect (not a git repo)
```

## Safety rules

- Never delete a memory file before user confirms.
- Never edit `~/.claude/CLAUDE.md` without per-item confirmation (separate from the project-level approve-all).
- Atomic write protection is built into `discover.py`; orphaned `.tmp` files in `/tmp/review_memory/` are safe to ignore.
- If a project's `decoded_path_exists` is `false`, surface the orphaned memories to the user but DO NOT auto-delete — the user may have multiple machines and the project may live on another one.
- If a memory file's frontmatter is malformed (missing `name`/`type`), surface it as "uncategorisable — propose: keep with note for user to manually review".

## Examples

```
/review-memory
```
Sweep all projects with memory dirs.

```
/review-memory --project /Users/jasonconroy/.claude/skills
```
Sweep one project by decoded path.

```
/review-memory --project -Users-jasonconroy-hppy-connect
```
Sweep one project by encoded directory name.
