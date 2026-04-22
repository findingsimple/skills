---
name: support-ticket-triage-v2
description: Triages a single Jira support ticket end-to-end. Fetches the ticket, linked issues, and resolved look-alikes, then delegates code investigation and synthesis to a sub-agent that classifies the issue as Code Bug / PFR / Config Issue with exact file:line or table.column evidence and tier-labelled resolution steps.
argument-hint: "<TICKET-KEY> [--similar-limit N]"
allowed-tools: Bash Read Grep Glob Agent mcp__notion__notion-search mcp__notion__notion-fetch
disable-model-invocation: true
---

# Support Ticket Triage v2

Slash-only skill that triages one Jira support ticket per invocation.

Usage:

```
/support-ticket-triage-v2 PROJ-123
/support-ticket-triage-v2 PROJ-123 --similar-limit 5
```

Output: a filled Resolution Summary (per `TEMPLATES.md`) with a classification, root cause, investigation trail, tier-labelled steps, and SAFEGUARDS for any state-changing actions.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `JIRA_BASE_URL` | Yes | Jira instance URL |
| `JIRA_EMAIL` | Yes | Atlassian email |
| `JIRA_API_TOKEN` | Yes | Atlassian API token |
| `CODEBASE_PATH` | Yes | Absolute path to the codebase the sub-agent searches |
| `SUPPORT_PROJECT_KEY` | No | Scopes "similar ticket" JQL to a single project. Unset = all projects |
| `ROOT_CAUSE_EPICS` | No | Comma-separated Jira keys (e.g. `PROJ-4354,PROJ-5272`). Validated against `^[A-Z][A-Z0-9_]+-\d+(,...)*$` before JQL use. Searched with `parent in (...)` |
| `CODE_SEARCH_EXTENSIONS` | No | Comma-separated file extensions. Defaults to `rb,ts,tsx,go,py,js,jsx,rs,java` |

## Phases

### 1. Parse arguments

Extract the ticket key (positional) and any flags. Reject anything that doesn't match `^[A-Z][A-Z0-9_]+-\d+$`.

### 2. Fetch ticket bundle

Run `fetch.py`:

```bash
python3 ~/.claude/skills/support-ticket-triage-v2/fetch.py <TICKET-KEY>
```

This validates env, fetches the ticket + linked issues + similar resolved tickets + (optionally) root-cause-epic children, and writes `/tmp/triage_v2/<TICKET-KEY>.json` atomically. Stdout is a one-screen summary.

### 3. Delegate investigation to a sub-agent

Spawn a **`general-purpose` agent with `model: sonnet`**. Substitute `<TICKET-KEY>` with the actual key parsed in Phase 1, then prompt the sub-agent verbatim:

> Use the Bash tool to run: `cat ~/.claude/skills/support-ticket-triage-v2/SYNTHESIS_PROMPT.md` — read the full file, paying particular attention to the 🛡️ SECURITY RULES at the top. Then follow its instructions. The ticket bundle is at `/tmp/triage_v2/<TICKET-KEY>.json` (substituted above — this is the concrete path, do not treat it as a placeholder). Read `/tmp/` paths with `cat`, not the Read tool. Return ONLY the filled markdown template as specified in `TEMPLATES.md` — no wrapping prose, no meta-commentary.

The sub-agent owns all grep / code-read / classification / safeguard-discovery work. The main context stays lean.

### 4. Return the sub-agent's response

Print the sub-agent's returned markdown verbatim as the final reply. Do not re-wrap, re-format, or summarise.

## Files

- `SKILL.md` — this file
- `TEMPLATES.md` — Resolution Summary template (per classification) + canonical SAFEGUARDS block
- `SYNTHESIS_PROMPT.md` — the full sub-agent prompt (read by the sub-agent via `cat`)
- `jira_client.py` — urllib Jira client (load_env, init_auth, jira_get, jira_search_all, jira_get_comments, adf_to_text)
- `fetch.py` — argparse entry point; validates env, fetches bundle, writes `/tmp/triage_v2/<KEY>.json`

## Prerequisites

- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `CODEBASE_PATH` exported in `~/.zshrc`.
- `CODEBASE_PATH` must point at an absolute, existing directory.
- Optional: `SUPPORT_PROJECT_KEY`, `ROOT_CAUSE_EPICS`, `CODE_SEARCH_EXTENSIONS`.
- Notion MCP server connected in Claude Code (optional — only used in Phase 4 of the sub-agent's work if `references/` doesn't cover the area).

## Non-goals

- No Jira writes. This skill only reads.
- No caching between runs. Each invocation re-fetches.
- No vault output. Results are returned in chat only.
- No Slack integration.
