---
name: incident-kb
description: Builds a searchable Obsidian knowledge base from Confluence incident retrospectives and Jira INC epics. Use when the user asks to sync incidents, build an incident knowledge base, or generate incident trend/recurrence reports.
disable-model-invocation: true
argument-hint: "[--team <name>] [--dry-run] [--force] [--report-only]"
allowed-tools: Bash Read Glob
---

# Incident Knowledge Base

Crawls Confluence incident retrospective pages and Jira INC epics, cross-references them, and writes per-incident Obsidian markdown files with trend and recurrence reports.

Uses `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (shared), plus `RETRO_PARENT_PAGE_ID`, `INCIDENT_KB_OUTPUT_PATH`, and optionally `INC_PROJECT_KEY` and `RETRO_TEMPLATE_PAGE_ID`.

## Architecture

### Key files

| File | Purpose |
|------|---------|
| `confluence_client.py` | Confluence API client — `load_env`, `init_auth`, `confluence_get`, `confluence_get_children`, `confluence_get_page`, `confluence_search_cql`, `adf_to_text`, `storage_to_text` |
| `jira_client.py` | Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_search_all`, `adf_to_text` |
| `setup.py` | Validates env vars, tests Jira + Confluence connectivity, saves setup data to `/tmp/incident_kb_setup.json` |
| `fetch.py` | Crawls Confluence retro child pages + Jira INC epics, cross-references by INC key, saves JSON to `/tmp/incident_kb/` |
| `generate.py` | Reads cached JSON, writes per-incident Obsidian markdown + `_Trend Report.md` + `_Recurrence Report.md` |

### Data flow

```
Confluence retro pages ──┐
                         ├─→ /tmp/incident_kb/ ──→ {INCIDENT_KB_OUTPUT_PATH}/
Jira INC epics ──────────┘     (JSON cache)          (Obsidian markdown)
```

### Cache structure

```
/tmp/incident_kb/
  confluence/{page_id}.json   — one file per retro page
  jira/{INC-KEY}.json         — one file per INC epic
  cross_ref.json              — matched pairs + orphans
  meta.json                   — sync timestamp + counts
```

## Instructions

### Step 1 — Parse arguments

Parse `$ARGUMENTS` for flags:

| Flag | Effect |
|------|--------|
| `--team <name>` | Team name (stored in metadata) |
| `--dry-run` | Preview what would be fetched/written without side effects |
| `--force` | Re-fetch all data even if cached |
| `--report-only` | Skip fetch, regenerate reports from existing cached data |

Build the flag string to pass to Python scripts.

### Step 2 — Run setup

Run:
```
python3 ~/.claude/skills/incident-kb/setup.py
```

If it exits non-zero, display the error and stop. The setup script validates all required env vars and tests API connectivity.

### Step 3 — Fetch data

Skip this step if `--report-only` was passed.

Run:
```
python3 ~/.claude/skills/incident-kb/fetch.py [--team TEAM] [--dry-run] [--force]
```

This crawls Confluence retro pages and Jira INC epics, cross-references them, and saves JSON to `/tmp/incident_kb/`.

If `--dry-run`, show the output (what would be fetched) and stop.

### Step 4 — Generate markdown

Run:
```
python3 ~/.claude/skills/incident-kb/generate.py [--team TEAM] [--dry-run] [--force] [--report-only]
```

This reads the cached JSON and writes:
- Per-incident markdown files to `{INCIDENT_KB_OUTPUT_PATH}/` named `YYYY-MM-DD — INC-KEY — Title.md` (dates extracted from titles, falling back to created timestamps)
- Test/dummy incidents (titles containing "test incident", "test only", etc.) routed to `_test/` subdirectory
- `_Index.md` — all incidents grouped by year with severity tags
- `_Trend Report.md` — incident frequency, severity distribution, service heatmap
- `_Recurrence Report.md` — recurring labels, keyword themes, missing retros

### Step 5 — Present results

Summarize the output:
- Number of incident files written (new vs skipped)
- Highlight any orphans (retros without epics, epics without retros)
- Note the output path for the user
- If there are incidents missing retrospectives, flag them as a potential gap
