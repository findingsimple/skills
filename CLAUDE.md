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
    jira_client.py      # Jira API client (load_env, auth, get/post, cursor-based paginated search)
    collect.py          # Mode: collect — fetch issues + linked details, save per-issue JSON to /tmp/triage_collect/
    analyze.py          # Mode: analyze — read collected data, score completeness, detect duplicates, write Obsidian report
    fetch.py            # Mode: triage — single-pass fetch + analysis for the triage workflow
    triage.py           # Mode: triage — transition execution, comment posting, history writing
  sprint-metrics/
    SKILL.md
    jira_client.py      # Jira + GitLab API client (load_env, auth, jira_get, jira_search_all, gitlab_get)
    setup.py            # Validates env, discovers boards/sprints
    generate.py         # Fetches GitLab MR data, calculates metrics, writes markdown
  sprint-pulse/
    SKILL.md            # Skill definition (frontmatter + step-by-step instructions)
    alerts.md           # Alert definitions, thresholds, output templates
    jira_client.py      # Jira API client (load_env, auth, get, search, changelog, comments)
    gitlab_client.py    # GitLab API client (load_gitlab_env, get, search MRs, MR notes)
    setup.py            # Validates env, discovers active sprint, parses team config (labels + Team field)
    fetch.py            # Fetches sprint issues + changelogs + comments + MRs + MR notes + support tickets (with comments for open tickets)
    analyze.py          # Runs deterministic alerts (stale items, support to-do/unack/SLA, highest priority)
  sprint-summary/
    SKILL.md
    jira_client.py      # Jira API client (load_env, auth, jira_get, jira_search_all)
    setup.py            # Validates env, discovers boards/sprints
    generate.py         # Fetches sprint report data, generates summary markdown
  schedules/
    install.sh                              # Install/unload macOS LaunchAgents from templates
    com.claude.sprint-pulse.plist.template  # Runs /sprint-pulse weekdays at 08:30
```

## Skill Authoring Checklist

When creating or modifying a SKILL.md, verify:

- [ ] **Description**: third-person verb form ("Generates..."), includes "Use when..." trigger clause
- [ ] **Frontmatter**: `allowed-tools` restricts to only needed tools; `disable-model-invocation: true` for user-triggered workflows
- [ ] **SKILL.md under 500 lines**: extract prompts, templates, and reference content into separate files (progressive disclosure)
- [ ] **References one level deep**: SKILL.md → reference file, never reference → reference
- [ ] **No time-sensitive info**: no "recently added" or "new feature" — content must read correctly in 6 months
- [ ] **Consistent terminology**: same term for same concept throughout (don't alternate "issue"/"ticket"/"bug")
- [ ] **Solve, don't punt**: scripts handle errors with actionable messages, not generic "something went wrong"
- [ ] **Examples use placeholders**: `PROJ-123`, `TeamA`, `Alex Chen` — no real names, keys, or identifiers

Full best practices: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices

## Conventions

- **One directory per skill** — skill name matches directory name. Each skill is self-contained and independently deployable. Shared utilities like `jira_client.py` and `setup.py` are intentionally duplicated per skill rather than extracted into a shared module — this keeps skills decoupled so changes to one never break another.
- **SKILL.md defines the skill** — skills are prompt-driven; some also include Python scripts for API calls and data processing
- **Secrets in `~/.zshrc`** — API tokens and credentials are exported in `~/.zshrc`, never in the repo
- **Use `--dry-run`** — skills that write files should support a dry-run flag for safe testing
- **No PII or business-specific info in skill files** — use placeholder names (e.g., Alex Chen, Jordan Park) and generic identifiers (e.g., `PROJ-123`, `TeamA`) in SKILL.md examples. Never hardcode real project keys, team names, board names, or company-specific identifiers in code or examples — parameterize via environment variables instead
- **Sandbox-safe commands** — consolidate API calls and data processing into permanent Python scripts in the skill directory. Each skill with API calls has its own `*_client.py` (API utilities) and processing scripts. Use `urllib.request` inside Python instead of curl to avoid sandbox approval prompts. Never use the Write/Edit tool for `/tmp/` files (triggers "outside working directory" prompts). Never use inline `python3 -c` with complex quoting (triggers "obfuscation" warnings).
- **Avoid MCP for large payloads** — MCP tool responses load fully into conversation context. For endpoints that return large payloads (e.g., Jira issue changelogs), process in Python scripts instead. This prevents context exhaustion and forced compaction.
- **Keep context lean** — skills that make many API calls should process data in Python scripts that save results to `/tmp/*.json` files. Only print summaries to stdout. The markdown generation step reads from these files rather than keeping raw API data in context.
- **Gitignore runtime artifacts** — if a skill writes persistent files into its own directory (e.g., `triage_history.json`), add them to `.gitignore`. Only `/tmp/` files are ephemeral by default; anything in the skill directory will be tracked by git otherwise.
- **Scheduled execution** — the `schedules/` directory contains macOS LaunchAgent templates and an `install.sh` script for running skills on a cron-like schedule. Templates use `{{HOME}}` placeholders resolved at install time. Generated `.plist` files are gitignored; only `.plist.template` files are committed.
- **Keep README in sync with SKILL.md** — when a SKILL.md's capabilities, supported formats, or prerequisites change, update the corresponding entry in `README.md` too. The README is the public-facing summary; stale entries mislead users.

## Environment Variables

All environment variables are exported in `~/.zshrc`. Python scripts access them via `os.environ.get()`.

| Variable | Used by |
|----------|---------|
| `OBSIDIAN_VAULT_PATH` | retro-summary |
| `OBSIDIAN_TEAMS_PATH` | bonusly-sync, feedback-perf, retro-summary, sprint-pulse |
| `BONUSLY_API_TOKEN` | bonusly-sync |
| `JIRA_BASE_URL` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage |
| `JIRA_EMAIL` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage |
| `JIRA_API_TOKEN` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage |
| `SPRINT_TEAMS` | sprint-summary, sprint-metrics, sprint-pulse |
| `GITLAB_URL` | sprint-metrics, sprint-pulse |
| `GITLAB_TOKEN` | sprint-metrics, sprint-pulse |
| `GITLAB_PROJECT_ID` | sprint-metrics, sprint-pulse |
| `TRIAGE_BOARD_ID` | root-cause-triage |
| `TRIAGE_PARENT_ISSUE_KEY` | root-cause-triage |
| `TRIAGE_OUTPUT_PATH` | root-cause-triage |
| `SUPPORT_PROJECT_KEY` | sprint-summary, sprint-pulse |
| `SUPPORT_BOARD_ID` | sprint-pulse |
| `SUPPORT_TEAM_LABEL` | sprint-pulse |
| `SUPPORT_TEAM_FIELD_VALUES` | sprint-pulse |
