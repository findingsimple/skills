---
name: reflect
description: Reflects on the current Claude Code session, archives lessons to the iCloud-synced Obsidian vault, and proposes promoting durable rules into the repo CLAUDE.md, ~/.claude/CLAUDE.md, or memory. Use when the user asks to reflect, retro, capture learnings, or wrap up a session.
disable-model-invocation: true
argument-hint: "(no args) | --skip-archive"
allowed-tools: Bash Read Edit Write
---

# Reflect

End-of-session retrospective. Two-tier flow:
1. **Archive** — auto-save a structured reflection file to the iCloud-synced Obsidian vault (cross-laptop durable note).
2. **Promote** — for each lesson worth generalizing, propose a CLAUDE.md / memory / global-CLAUDE.md addition with per-item user approval.

The conversation is the input. There is no Python script — Claude reads its own current context and produces the reflection directly.

## When to use this

- "Reflect on this session"
- "Retro / wrap up"
- "What did we learn? Capture it."
- "Run /reflect"

## Why this exists (and why it's different from the obvious version)

A simple `/reflect` that just saves a templated archive file fills a `Reflections/` directory with write-only artifacts that nobody re-reads. The value lives in the **flow-out**: lessons that show up across multiple sessions should migrate into CLAUDE.md (so they're loaded automatically) or memory (so they're personalised), with the reflection file becoming an index of "lessons that became rules" vs "lessons that didn't make the cut".

This skill enforces:
- Specificity over recap (every bullet must cite a moment, not a generic claim)
- Capped section length (anti-bloat: short sessions don't get padded, long sessions get pruned)
- Explicit promotion proposals (not silent dumping into CLAUDE.md)
- Per-item approval before any rule migration
- A "Promoted to" footer recording where each lesson actually landed

## Archive location

`${OBSIDIAN_VAULT_PATH}/Reflections/<YYYY-MM-DD>-<slug>.md`

The vault is iCloud-synced, so reflections written from one laptop are visible on the other within iCloud's sync window — no git push/pull required. Per the repo's "Sandbox-safe commands" convention, write the file via Bash heredoc (`cat << 'SKILL_EOF' > path`), NOT via the Write/Edit tool (which can fail on iCloud paths and trigger TCC prompts).

If `OBSIDIAN_VAULT_PATH` is unset, fall back to `~/.claude/reflections/<YYYY-MM-DD>-<slug>.md` and warn the user that setting the env var would enable cross-laptop sync.

## Argument allow-list

- `--skip-archive` — produce the reflection in-conversation only, do not write a file. Useful for short ad-hoc reflections the user doesn't want archived.

No other arguments.

## Instructions

Follow these steps exactly.

### Step 1 — Pre-check

If the conversation has fewer than ~10 substantive turns (excluding pure tool-call exchanges), ask the user:
> "This session is short — proceed with full reflection, or skip?"

Default to skip if the user doesn't reply with a clear yes.

If `--skip-archive` is passed, skip the archive write but still walk the framework and propose promotions.

### Step 2 — Resolve archive path

```bash
echo "$OBSIDIAN_VAULT_PATH"
```

- If non-empty: `ARCHIVE_DIR=${OBSIDIAN_VAULT_PATH}/Reflections`
- If empty: `ARCHIVE_DIR=~/.claude/reflections` and tell the user `OBSIDIAN_VAULT_PATH` isn't set (one-line warning, not a stop).

```bash
mkdir -p "$ARCHIVE_DIR"
```

Compute `<YYYY-MM-DD>` from today's date and `<slug>` from the **gist** of the conversation (kebab-case, ≤6 words, no version numbers, no dates). Examples:
- "Added unit tests for `_lib/`" → `lib-unit-tests`
- "Fixed broken wiki-links in support-trends report" → `support-trends-wiki-link-fix`
- "Designed reflect and review-memory skills" → `meta-skills-design`

If a file with that name already exists, append `-2`, `-3`, etc.

### Step 3 — Generate the reflection

Walk these seven sections, populated from current conversation context. Apply the discipline rules below.

#### Section 1 — Context (1 line, mandatory)

What was being worked on. Not a recap — git has the diff. Just enough framing that the reflection makes sense in 6 months.

> e.g. "Added unit tests to `_lib/` clients after fixing a wiki-link bug in support-trends."

#### Section 2 — What worked, repeat this (≤4 bullets)

Patterns/decisions worth doing again. **Each bullet must cite a specific moment** — what the user said, what the model decided, what the test showed. No generic claims.

> ✅ "Adding the `author=None` test exposed a real production NPE in `gitlab_client.get_mr_notes` that had been latent for months."
> ❌ "Tests are useful."

#### Section 3 — What went sideways (≤4 bullets)

Friction, false starts, things that took longer than expected. Concrete description of the friction, not just "it was hard".

> ✅ "Re-running the support-trends report required manually crafting a `.run.lock` file with the existing session UUID — `report.py` standalone is blocked by `verify_session()`."
> ❌ "Pipeline orchestration is finicky."

#### Section 4 — Tips & tricks (≤3 bullets)

Concrete reusable hacks discovered. Lower bar than "convention" — just useful tricks worth remembering.

> e.g. "To re-render a support-trends report from cache without re-fetching: read `setup.json.session`, write a `.run.lock` with that UUID, run `report.py`, delete the lock."

#### Section 5 — Generalization opportunities (≤4 bullets)

For each lesson, ask "what is this a specific instance of?" Each bullet **must end with a proposed promotion target**:
- → repo CLAUDE.md
- → global `~/.claude/CLAUDE.md`
- → project memory
- → keep here only

If the lesson is already covered by an existing CLAUDE.md/memory rule, write `→ already covered (no promotion needed)` and reference the rule. Don't propose duplicates.

#### Section 6 — Action items (checkbox list)

Concrete follow-ups. Format:
```
- [ ] {action} ({file path / context})
```

Specific, not speculative. "Maybe we should consider..." doesn't qualify; "Add `--from-cache` flag to `support-trends/report.py:main`" does.

#### Section 7 — Promoted to (footer placeholder)

Leave as `_(populated by Step 5 after user approves promotions)_` for now.

### Step 4 — Write the archive (unless `--skip-archive`)

Render the seven sections to markdown with this frontmatter:

```yaml
---
title: "<slug>"
date: <YYYY-MM-DD>
repo: <basename of cwd, e.g. "skills">
session_type: reflection
tags: [reflection]
---
```

Frontmatter rules:
- `tags: [reflection]` — aligns with the repo's standard tags convention.
- `repo:` is plain text, not a wiki link (the repo isn't a vault entity).
- File paths and commit SHAs in the body are plain text or markdown hyperlinks (per the "Wiki links only for vault-resident entities" convention).
- Apply the Slack channel escape rule (wrap `#word` in inline code) and the pipe escape rule (`\|` in any wiki link aliases) on conversation-quoted content.

Write via Bash heredoc:

```bash
cat << 'SKILL_EOF' > "${ARCHIVE_DIR}/${DATE}-${SLUG}.md"
---
title: "${SLUG}"
...
SKILL_EOF
```

Use `SKILL_EOF` (not `EOF`) to avoid collision with any markdown-embedded `EOF` strings.

Print the archive path to the user.

### Step 5 — Per-item promotion approval

For every "Generalization opportunities" bullet that proposes a promotion (NOT `→ keep here only` and NOT `→ already covered`), present a single review block:

```
## Proposed promotions

  1. "Wiki links only for vault entities"
     Target: <repo>/CLAUDE.md → Conventions §
     Proposed bullet:
       - **Wiki links only for vault-resident entities** — only emit `[[X]]` when ...
     Reason: Bit us today; applies to every vault-writing skill.

  2. "Test field=None at API boundary"
     Target: <repo>/CLAUDE.md → Conventions §
     Proposed bullet: ...
     Reason: ...

  3. "User wants per-item approval before global CLAUDE.md edits"
     Target: ~/.claude/CLAUDE.md (global)
     Proposed bullet: ...
     Reason: Cross-project preference.
```

Ask: "Approve all? Skip specific items? Edit any proposed text?"

Wait for explicit confirmation. Per-item approval is required for global CLAUDE.md edits even within a batch approval — high-blast-radius config gets a second nod.

### Step 6 — Execute approved promotions

For each approved item:

1. Edit the target file (project CLAUDE.md, global CLAUDE.md, or write a memory file + update MEMORY.md index).
2. If the target is in a git repo and the user wants a commit, draft and commit (no `Co-Authored-By` per the user's global rule).
3. Append a line to the reflection file's "Promoted to" footer:
   ```
   - "<insight>" → <target file path> (commit <sha if applicable>)
   ```
   Use Edit to replace the placeholder section.

### Step 7 — Final output

Print to the user:
- Archive path written
- Number of promotions: approved / skipped / pending
- Any commits created (with SHA)

Keep this terse — under 5 lines.

## Discipline rules baked into the framework

1. **Memory-vs-CLAUDE.md split** — code rule → repo, personal preference → memory, cross-project preference → global. Never inverted.
2. **No memory entries that just summarize what we did.** Memories are for surprises, decisions, and forward-looking guidance. A memory of the form "today we added tests for _lib/" is wrong — that's git history, not memory.
3. **No CLAUDE.md additions for one-shot context.** Only repeated-application rules.
4. **Each bullet cites a specific moment.** Generic claims get cut.
5. **Per-item approval gate.** Archive auto-writes; CLAUDE.md/memory writes are gated.
6. **Cap section length.** Padding is anti-feature.

## Examples

```
/reflect
```
Full reflection, archive to vault, propose promotions.

```
/reflect --skip-archive
```
Reflect in-conversation only, propose promotions but no archive file.
