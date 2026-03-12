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

## Environment Files

| File | Contents |
|------|----------|
| `~/.obsidian_env` | `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TEAMS_PATH` — shared by all vault-related skills |
| `~/.bonusly_env` | `BONUSLY_API_TOKEN` |
| `~/.sprint_summary_env` | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_STORY_POINTS_FIELD`, `SPRINT_TEAMS` — Jira credentials and team config; `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_PROJECT_ID` — optional GitLab MR metrics |
