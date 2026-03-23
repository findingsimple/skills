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
    SKILL.md            # Skill definition (frontmatter + prompt)
    bonusly_client.py   # Bonusly API client (load_env, get, paginated get)
    generate.py         # Fetches bonuses, generates per-person markdown + sync log
  feedback-perf/
    SKILL.md
  retro-summary/
    SKILL.md
  root-cause-triage/
    SKILL.md
    jira_client.py      # Jira API client (load_env, auth, get/post, paginated search)
    fetch.py            # Fetches triage issues, analyzes completeness, outputs summary
    triage.py           # Executes confirmed transitions + adds comments
  sprint-metrics/
    SKILL.md
    jira_client.py      # Jira + GitLab API client (load_env, auth, jira_get, gitlab_get)
    setup.py            # Validates env, discovers boards/sprints
    generate.py         # Fetches GitLab MR data, calculates metrics, writes markdown
  sprint-summary/
    SKILL.md
    jira_client.py      # Jira API client (load_env, auth, jira_get)
    setup.py            # Validates env, discovers boards/sprints
    generate.py         # Fetches sprint report data, generates summary markdown
```

## Conventions

- **One directory per skill** — skill name matches directory name. Each skill is self-contained and independently deployable. Shared utilities like `jira_client.py` and `setup.py` are intentionally duplicated per skill rather than extracted into a shared module — this keeps skills decoupled so changes to one never break another.
- **SKILL.md defines the skill** — skills are prompt-driven; some also include Python scripts for API calls and data processing
- **Secrets in `~/.zshrc`** — API tokens and credentials are exported in `~/.zshrc`, never in the repo
- **Use `--dry-run`** — skills that write files should support a dry-run flag for safe testing
- **No real names in skill files** — use placeholder names in examples within SKILL.md
- **Sandbox-safe commands** — consolidate API calls and data processing into permanent Python scripts in the skill directory. Each skill with API calls has its own `*_client.py` (API utilities) and processing scripts. Use `urllib.request` inside Python instead of curl to avoid sandbox approval prompts. Never use the Write/Edit tool for `/tmp/` files (triggers "outside working directory" prompts). Never use inline `python3 -c` with complex quoting (triggers "obfuscation" warnings).
- **Avoid MCP for large payloads** — MCP tool responses load fully into conversation context. For endpoints that return large payloads (e.g., Jira issue changelogs), process in Python scripts instead. This prevents context exhaustion and forced compaction.
- **Keep context lean** — skills that make many API calls should process data in Python scripts that save results to `/tmp/*.json` files. Only print summaries to stdout. The markdown generation step reads from these files rather than keeping raw API data in context.

## Environment Variables

All environment variables are exported in `~/.zshrc`. Python scripts access them via `os.environ.get()`.

| Variable | Used by |
|----------|---------|
| `OBSIDIAN_VAULT_PATH` | root-cause-triage, retro-summary |
| `OBSIDIAN_TEAMS_PATH` | bonusly-sync, feedback-perf, retro-summary |
| `BONUSLY_API_TOKEN` | bonusly-sync |
| `JIRA_BASE_URL` | sprint-summary, sprint-metrics, root-cause-triage |
| `JIRA_EMAIL` | sprint-summary, sprint-metrics, root-cause-triage |
| `JIRA_API_TOKEN` | sprint-summary, sprint-metrics, root-cause-triage |
| `SPRINT_TEAMS` | sprint-summary, sprint-metrics |
| `GITLAB_URL` | sprint-metrics |
| `GITLAB_TOKEN` | sprint-metrics |
| `GITLAB_PROJECT_ID` | sprint-metrics |
| `TRIAGE_BOARD_ID` | root-cause-triage |
| `TRIAGE_PARENT_ISSUE_KEY` | root-cause-triage |
