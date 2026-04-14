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
  incident-kb/
    SKILL.md            # Skill definition (frontmatter + step-by-step instructions)
    confluence_client.py # Confluence API client (load_env, auth, get page, get children, CQL search, adf_to_text, storage_to_text)
    jira_client.py      # Jira API client (copied from root-cause-triage)
    setup.py            # Validates env, tests Jira + Confluence API connectivity
    fetch.py            # Crawls Confluence retros + Jira INC epics, cross-references, saves to /tmp/incident_kb/
    generate.py         # Reads /tmp/ JSON, writes per-incident Obsidian markdown + trend/recurrence reports
  retro-summary/
    SKILL.md
    PROMPTS.md          # Synthesis agent prompts per retro template (rose-thorn-bud, wind-sun-anchor-reef)
    TEMPLATES.md        # Output file templates per retro format (frontmatter + sections)
  root-cause-triage/
    SKILL.md
    jira_client.py      # Jira API client (load_env, auth, get/post, cursor-based paginated search)
    collect.py          # Mode: collect — fetch issues + linked details, save per-issue JSON to /tmp/triage_collect/
    summarize.py        # Mode: collect — read per-issue JSON, generate Obsidian Markdown with extractive summaries
    enrich.py           # Mode: collect — prepare agent batches (prepare) and apply enriched summaries (apply)
    ENRICH_PROMPT.md    # Agent prompt template for linked issue summarization and root cause synthesis
    autofill.py         # Mode: collect — auto-fill missing template sections using agent synthesis
    AUTOFILL_PROMPT.md  # Agent prompt template for template section autofill
    analyze.py          # Mode: analyze — structural scoring, duplicate detection, writes JSON to /tmp/
    build_prompts.py    # Mode: analyze — builds agent prompt batches from analysis JSON
    merge_results.py    # Mode: analyze — merges agent results + enrichment into enriched JSON
    report.py           # Mode: analyze — reads enriched JSON + clusters, writes Raw + Enriched Analysis reports
    QUALITY_PROMPT.md   # Agent prompt for raw quality assessment (analyze Step A2a)
    POST_ENRICH_QUALITY_PROMPT.md  # Agent prompt for post-enrichment quality assessment (analyze Step A2b)
    DUPLICATE_PROMPT.md # Agent prompt for semantic duplicate detection (analyze Step A2c)
  sprint-metrics/
    SKILL.md
    jira_client.py      # Jira + GitLab API client (load_env, auth, jira_get, jira_search_all, gitlab_get, gitlab_get_all)
    setup.py            # Validates env, discovers boards/sprints
    generate.py         # Fetches GitLab MR data, calculates metrics + DORA (deployment frequency, lead time), writes markdown
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
  vault-linker/
    SKILL.md            # Skill definition (frontmatter + step-by-step instructions)
    link.py             # Scans vault for entities, adds [[wiki links]] to existing files, generates index pages
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
- **Sandbox-safe commands** — consolidate API calls and data processing into permanent Python scripts in the skill directory. Each skill with API calls has its own `*_client.py` (API utilities) and processing scripts. Use `urllib.request` inside Python instead of curl to avoid sandbox approval prompts. Never use the Write/Edit tool for `/tmp/` files or files outside the working directory (e.g., Obsidian vault paths) — use `cat << 'SKILL_EOF' > path` via Bash instead. This also avoids macOS TCC prompts for iCloud paths in scheduled tasks. Always use `SKILL_EOF` as the heredoc delimiter (not `EOF`) to avoid collision with markdown content. Never use inline `python3 -c` with complex quoting (triggers "obfuscation" warnings). Spawned agents (via the Agent tool) cannot use the Read tool on `/tmp/` paths — instruct them to use `cat` via the Bash tool instead.
- **Avoid MCP for large payloads** — MCP tool responses load fully into conversation context. For endpoints that return large payloads (e.g., Jira issue changelogs), process in Python scripts instead. This prevents context exhaustion and forced compaction.
- **Keep context lean** — skills that make many API calls should process data in Python scripts that save results to `/tmp/*.json` files. Only print summaries to stdout. The markdown generation step reads from these files rather than keeping raw API data in context.
- **Delegate to sub-agents for token reduction** — when a step gathers raw data (API responses, file contents, search results) only to pass it to an analysis or synthesis step, delegate both the gathering and analysis to a sub-agent via the Agent tool. The sub-agent runs in its own context window and returns only a distilled summary. Rule of thumb: if a step reads 5+ files or processes 50+ items just to produce a summary, delegate it. Save intermediate data to `/tmp/*.json` so the sub-agent can read it via Bash `cat` (not the Read tool).
- **Atomic JSON writes to `/tmp/`** — always write to a `.tmp` file first, then `os.replace()` to the final path. This prevents truncated JSON from blocking future runs if a process is interrupted mid-write. Pattern: `with open(path + ".tmp", "w") as f: json.dump(data, f); os.replace(path + ".tmp", path)`. Use `.json.tmp` suffix (not a separate `.tmp` extension) so existing `*.json` glob patterns safely ignore orphaned temp files.
- **Restrictive `/tmp/` permissions** — create `/tmp/` cache directories with `os.makedirs(path, mode=0o700, exist_ok=True)`. This prevents other local users from reading cached incident/sprint data. Do NOT apply `mode=0o700` to Obsidian vault output directories.
- **Validate env vars used in queries** — environment variables interpolated into JQL or CQL queries (e.g., project keys) should be validated against an expected format (e.g., `^[A-Z][A-Z0-9_]+$`) before use, to prevent injection.
- **Gitignore runtime artifacts** — if a skill writes persistent files into its own directory (e.g., `triage_history.json`), add them to `.gitignore`. Only `/tmp/` files are ephemeral by default; anything in the skill directory will be tracked by git otherwise.
- **Scheduled execution** — the `schedules/` directory contains macOS LaunchAgent templates and an `install.sh` script for running skills on a cron-like schedule. Templates use `{{HOME}}` placeholders resolved at install time. Generated `.plist` files are gitignored; only `.plist.template` files are committed.
- **Keep README in sync with SKILL.md** — when a SKILL.md's capabilities, supported formats, or prerequisites change, update the corresponding entry in `README.md` too. The README is the public-facing summary; stale entries mislead users.
- **Escape pipes in wiki link aliases** — Obsidian wiki links with aliases (`[[filename|display]]`) break inside markdown tables because `|` is the column separator. Always use `\|` instead: `[[filename\|display]]`. Obsidian renders `\|` correctly in all contexts (tables, lists, paragraphs). This applies to all skills that emit `[[...|...]]` links — vault-linker, incident-kb, root-cause-triage, and any future skill writing wiki links. Plain `[[Name]]` links (no alias) are unaffected.
- **No wiki links for sprint table assignees** — linking person names in sprint summary tables adds graph noise without navigational value (~400 links that all point to the same few people). Person links in retros and bonusly entries are more meaningful — they represent feedback/recognition context. Sprint summaries should keep assignee names as plain text.
- **Wiki links in YAML frontmatter** — skills that generate vault files should emit wiki links in frontmatter fields that reference vault entities. Use `team: "[[TeamName]]"` for team references, `person: "[[Person Name]]"` for person references, and `participants: ["[[Name1]]", "[[Name2]]"]` for participant lists. Obsidian recognises wiki links in frontmatter values and includes them in the graph view. This connects files to team hubs and person pages from the moment they're created, without needing a separate vault-linker pass.
- **Index pages use wiki links** — index/catalog pages (`_Index.md`, `_Recurrence Report.md`, `_Timeline.md`) should use `[[wiki links]]` so AI agents can follow links to discover vault content. Index pages are excluded from Obsidian's graph view via the vault's `graph.json` search filter to prevent mega-hub visual noise.

## Environment Variables

All environment variables are exported in `~/.zshrc`. Python scripts access them via `os.environ.get()`.

| Variable | Used by |
|----------|---------|
| `OBSIDIAN_VAULT_PATH` | retro-summary |
| `OBSIDIAN_TEAMS_PATH` | bonusly-sync, feedback-perf, retro-summary, sprint-pulse |
| `BONUSLY_API_TOKEN` | bonusly-sync |
| `JIRA_BASE_URL` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage, incident-kb |
| `JIRA_EMAIL` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage, incident-kb |
| `JIRA_API_TOKEN` | sprint-summary, sprint-metrics, sprint-pulse, root-cause-triage, incident-kb |
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
| `RETRO_PARENT_PAGE_ID` | incident-kb |
| `RETRO_TEMPLATE_PAGE_ID` | incident-kb |
| `INC_PROJECT_KEY` | incident-kb |
| `INCIDENT_KB_OUTPUT_PATH` | incident-kb |
