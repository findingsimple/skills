# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository (`~/.claude/skills/`) stores custom Claude Code skills.

## Repository Location

This repo lives at `~/.claude/skills/` — the standard directory for user-defined Claude Code skills.

## Skill Structure

Each skill lives in its own directory with a `SKILL.md` file:

```
~/.claude/skills/
  bonusly-sync/
    SKILL.md          # Skill definition (frontmatter + prompt)
  feedback-perf/
    SKILL.md
  retro-summary/
    SKILL.md
  sprint-summary/
    SKILL.md
```

## Conventions

- **One directory per skill** — skill name matches directory name
- **SKILL.md is the entire skill** — skills are prompt-driven, no separate scripts
- **Secrets in env files** — API tokens and credentials go in `~/.bonusly_env` (or similar), never in the repo
- **Use `--dry-run`** — skills that write files should support a dry-run flag for safe testing
- **No real names in skill files** — use placeholder names in examples within SKILL.md
- **Sandbox-safe commands** — consolidate API calls and data processing into self-contained Python scripts written to `/tmp/` via bash heredoc (`cat << 'PYEOF' > /tmp/script.py`). Use `urllib.request` inside Python instead of curl to avoid sandbox approval prompts. Never use the Write/Edit tool for `/tmp/` files (triggers "outside working directory" prompts). Never use inline `python3 -c` with complex quoting (triggers "obfuscation" warnings).
- **Avoid MCP for large payloads** — MCP tool responses load fully into conversation context. For endpoints that return large payloads (e.g., Jira issue changelogs), process in Python scripts instead. This prevents context exhaustion and forced compaction.
- **Keep context lean** — skills that make many API calls should process data in Python scripts that save results to `/tmp/*.json` files. Only print summaries to stdout. The markdown generation step reads from these files rather than keeping raw API data in context.

## Environment Files

| File | Contents |
|------|----------|
| `~/.obsidian_env` | `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TEAMS_PATH` — shared by all vault-related skills |
| `~/.bonusly_env` | `BONUSLY_API_TOKEN` |
| `~/.sprint_summary_env` | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `SPRINT_TEAMS` — Jira credentials and team config |
