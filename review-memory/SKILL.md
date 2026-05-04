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

3. For each entry, also assign a **triage bucket** (this is the per-item approval pacing, separate from the action choice):

   - **obvious-keep** — true personal preference with no plausible alternate classification. The bullet is a behaviour-shaping rule about how the user wants Claude to interact, not about code. *"Always ask before running destructive git commands."*
   - **obvious-delete-duplicate** — `dedup_signal` is non-empty AND the memory's name or first sentence appears verbatim (or near-verbatim) in the project's existing CLAUDE.md. Low judgment, high confidence.
   - **needs-judgment** — anything else. Includes every move-to-repo and move-to-global proposal (because the wording matters), every entry where the dedup heuristic is ambiguous, every orphaned-project entry, and every malformed-frontmatter entry.

   Default bias: when in doubt, push to `needs-judgment`. The user prefers extra approval prompts over silent migrations.

   For each entry, prepare:
   - The chosen action + triage bucket
   - One-sentence reason
   - If moving to a CLAUDE.md: the proposed bullet text in the existing file's voice (lead with **bold rule**, then explanation, reference if applicable). State which **target section** you'd insert under (or `unknown — ask user`).
   - If keeping or duplicate-deleting: no further preparation

### Step 4 — Present in two phases

Render proposals per project in this order:

**Phase A — batch confirm (obvious-keep + obvious-delete-duplicate):**

```
## Project: /Users/<you>/path/to/repo

  Phase A — batch (will execute on a single approval):
    KEEP:                feedback_pulse_terse_output.md
    KEEP:                feedback_no_background_tasks.md
    DELETE (duplicate):  feedback_some_old_rule.md  → CLAUDE.md "Atomic JSON writes" convention
    DELETE (duplicate):  feedback_other_dup.md      → CLAUDE.md "Validate env vars" convention
```

Ask: "Approve Phase A batch as a single action?"

If approved: execute Phase A immediately (delete-duplicates remove files + index lines; keeps are no-op). Print one line per executed action.

If skipped or edited: drop into per-item flow for Phase A entries before moving to Phase B.

**Phase B — per-item walk (needs-judgment):**

```
  Phase B — per-item review (each requires its own approval):

    1. project_some_design_note.md
       Action: MOVE-TO-REPO
       Target section: Conventions  (or: "unknown — please tell me which section")
       Reason: Code rule about pipeline orchestration; references a specific file pattern.
       Proposed CLAUDE.md bullet:
         - **Pipeline cache lifetime** — orchestrated pipelines must call `clear_stale_state()` ...

    2. project_team_naming_thing.md
       Action: MOVE-TO-GLOBAL  ⚠️ would apply to EVERY project on this machine
       Reason: Preference about how Claude refers to team names — not project-specific.
       Currently-affected project list: skills, hppy/connect (any other repo touching teams will be affected after promotion)
       Proposed ~/.claude/CLAUDE.md bullet: ...
```

For each Phase B item, ask explicitly: "Approve, skip, or edit?" — do NOT batch-confirm. Per-item is the point.

For any **MOVE-TO-GLOBAL** proposal, the approval block must enumerate which projects will be newly affected by the global rule (use the inventory's `projects[]` list to name them). The user must be able to see what they're committing every other repo to before approving.

### Step 5 — Execute approved actions

For each approved item, in this order:

1. **Move-to-repo:** Read `<decoded_path>/CLAUDE.md`. **Section selection rule:** if the proposal in Phase B named a target section AND that section exists in the file, insert the new bullet there in the existing list ordering and voice. Otherwise, ASK THE USER which section to insert under — never invent a new top-level section without explicit approval, and never silently fall back to "the closest-named section". Then delete the memory file and update the project's `MEMORY.md` index (read the index first to confirm the line shape, typically `- [filename](filename) — short blurb`; remove only that line, preserve the rest).
2. **Move-to-global:** Same flow but target `~/.claude/CLAUDE.md`. The Phase B per-item approval already gated this; no second confirmation here.
3. **Delete:** Remove the memory file and the corresponding line from the project's `MEMORY.md` index (same line-shape note as above).
4. **Keep:** No-op.

After each action, print a one-line confirmation.

### Step 6 — Commit (per project)

If the project's decoded path is a git repo and `CLAUDE.md` was modified, prepare a commit. **All git commands MUST use `git -C "$DECODED_PATH"` because the user's cwd is not the project being modified** — running plain `git status` or `git commit` would target the wrong repo.

```bash
DECODED_PATH="<decoded_path from inventory>"
git -C "$DECODED_PATH" status
git -C "$DECODED_PATH" diff CLAUDE.md MEMORY.md
git -C "$DECODED_PATH" add CLAUDE.md MEMORY.md
git -C "$DECODED_PATH" commit -m "$(cat <<'COMMIT_EOF'
docs(memory-sweep): promote N memories to CLAUDE.md

- "<memory name>" → Conventions §
- "<memory name>" → Security Notes §

Memories deleted from project memory after promotion.
COMMIT_EOF
)"
```

Show the diff to the user, draft the commit message, ask for approval before committing. One commit per project (not per item) — the conventions added in this sweep are a single coherent batch. Per the user's `~/.claude/CLAUDE.md`, commit messages MUST NOT include `Co-Authored-By` lines.

To check whether `<decoded_path>` is a git repo at all (not every project is):

```bash
git -C "$DECODED_PATH" rev-parse --git-dir 2>/dev/null && echo "is git repo" || echo "not a git repo"
```

For projects that aren't git repos, skip the commit step but still print "files modified at $DECODED_PATH — not a git repo, no commit attempted".

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
