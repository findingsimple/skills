# Claude Code Skills

Custom skills for [Claude Code](https://claude.ai/code), stored in `~/.claude/skills/`.

## What are Skills?

Skills are reusable prompt-based capabilities that extend Claude Code. They can be invoked as slash commands (e.g., `/my-skill`) during a Claude Code session.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| [bank-statement-to-markdown](bank-statement-to-markdown/) | `/bank-statement-to-markdown` | Convert St.George Bank PDF statements (Complete Freedom, Amplify Signature) into agent-friendly Markdown |
| [bonusly-sync](bonusly-sync/) | `/bonusly-sync` | Sync previous month's Bonusly recognition to Obsidian vault |
| [incident-kb](incident-kb/) | `/incident-kb` | Build searchable Obsidian knowledge base from Confluence incident retros and Jira INC epics |
| [feedback-perf](feedback-perf/) | `/feedback-perf` | Capture and synthesize performance review feedback in Obsidian vault |
| [retro-summary](retro-summary/) | `/retro-summary` | Extract and summarize retrospectives from FigJam boards into Obsidian vault |
| [root-cause-suggest](root-cause-suggest/) | `/root-cause-suggest [--team <name>] [--keys K1,K2 \| --from-file path] [--since 30d]` | Suggest root-cause links for a batch of unlinked support tickets. Default mode auto-discovers unlinked tickets in the focus team's recent intake; per ticket the sub-agent recommends linking to an existing root cause, proposing a new one (clusters tickets that share an underlying issue), or skipping with a reason. Output is a Markdown report — high-confidence links are bulk-applyable, the rest are review-first. Never mutates Jira. |
| [root-cause-triage](root-cause-triage/) | `/root-cause-triage` | Collect root cause tickets to Obsidian knowledge base and analyze for duplicates, quality, and completeness |
| [sprint-metrics](sprint-metrics/) | `/sprint-metrics` | Generate engineering metrics (TTM, review turnaround, cycle time) and DORA metrics (deployment frequency, lead time) from GitLab for a sprint |
| [sprint-pulse](sprint-pulse/) | `/sprint-pulse` | Generate mid-sprint alerts and DORA snapshot from Jira sprint data, GitLab MRs, and support tickets |
| [sprint-summary](sprint-summary/) | `/sprint-summary` | Generate sprint summary from Jira data into Obsidian vault |
| [support-routing-audit](support-routing-audit/) | `/support-routing-audit [--team <name>] [--start ...] [--end ...]` | Audit support tickets routed to a focus team over a period (default current month) and flag ones that should have gone elsewhere per the configured team charters — with target team, reasoning, and confidence. Focus team and canonical team list are supplied via env vars (no team is hardcoded). Output is a terminal Markdown report ready to paste into a leadership chat. |
| [support-trends](support-trends/) | `/support-trends --team <name> [--window month\|YYYY-MM\|YYYY-MM-DD..YYYY-MM-DD]` | Build a leadership-grade monthly support trends report for one team. Three sub-agents (themes, support-feedback, synthesise) enrich a deterministic finding set; renderer rejects any claim without a metric and evidence keys. Output is a Markdown report written to the team's Obsidian vault under `Support/Trends/{year}/`, covering volume / themes / charter drift / L2 containment / categorisation / numbers. |
| [support-ticket-triage](support-ticket-triage/) | `/support-ticket-triage <KEY>` | Triage a single Jira support ticket: fetch ticket + linked + similar resolved, delegate code investigation to a sub-agent, return a filled Resolution Summary with classification (Code Bug / PFR / Config Issue), exact file:line or table.column evidence, and tier-labelled steps with safeguards |
| [vault-linker](vault-linker/) | `/vault-linker` | Add Obsidian `[[wiki links]]` to existing vault files by scanning for known entities (people, incidents, Jira keys) |
| [review-memory](review-memory/) | `/review-memory [--project <name>]` | Sweep `~/.claude/projects/*/memory/` across all projects on this machine. For each memory entry, classify as keep / move-to-repo CLAUDE.md / move-to-global CLAUDE.md / delete with reasoning, and gate every action behind per-item user approval. Discovery is deterministic; classification is the model's judgment. |
| [reflect](reflect/) | `/reflect [--skip-archive]` | End-of-session retrospective. Auto-archives a structured 7-section reflection file (Context / What Worked / What Went Sideways / Tips & Tricks / Generalization Opportunities / Action Items / Promoted To) to `${OBSIDIAN_VAULT_PATH}/Reflections/` for cross-laptop access via iCloud, then proposes promoting durable lessons into repo CLAUDE.md, `~/.claude/CLAUDE.md`, or memory with per-item user approval. |

## Scheduled Execution

The `schedules/` directory contains macOS LaunchAgent templates for running skills automatically on a recurring schedule.

**Install all schedules:**
```bash
~/.claude/skills/schedules/install.sh
```

**Unload all schedules:**
```bash
~/.claude/skills/schedules/install.sh --unload
```

**How it works:**
- Each `.plist.template` defines a LaunchAgent with `{{HOME}}` placeholders
- `install.sh` generates `.plist` files (resolving placeholders), symlinks them into `~/Library/LaunchAgents/`, and loads them via `launchctl`
- Generated `.plist` files are gitignored — only templates are committed
- Logs write to `/tmp/` (e.g., `/tmp/claude-sprint-pulse.log`)

**Current schedules:**

| Template | Schedule | Skill |
|----------|----------|-------|
| `com.claude.sprint-pulse.plist.template` | Weekdays at 08:30 | `/sprint-pulse --team TeamA` |

To add a new schedule, create a `.plist.template` in `schedules/` following the existing template pattern, then re-run `install.sh`.

## Architecture Notes

Each skill lives in its own directory with its own SKILL.md, scripts, and prompt templates. Cross-skill API clients (Jira, GitLab, Confluence, Bonusly) live in `_lib/` along with a shared `_http.py` retry helper, and are imported via a 3-line `_libpath.py` shim per skill. Per-skill `setup.py` is intentionally NOT shared, since each skill validates a different env-var set. Each `_lib/` module has a sibling `test_*.py` (no network); run with `cd _lib && python3 -m unittest discover -p 'test_*.py'`. To export a single skill standalone, copy its dir plus the `_lib/*.py` modules it imports and strip the shim — see `CLAUDE.md` for the full layout.

## Setup

### Environment variables

Add the following exports to `~/.zshrc`:

```bash
# Obsidian vault (shared by all vault-related skills)
export OBSIDIAN_VAULT_PATH="/path/to/your/vault"
export OBSIDIAN_TEAMS_PATH="$OBSIDIAN_VAULT_PATH/Teams"

# Bonusly (bonusly-sync)
export BONUSLY_API_TOKEN="your_token_here"

# Bank statements (bank-statement-to-markdown)
export STATEMENTS_PATH="/path/to/statements/folder"

# Jira (sprint-summary, sprint-metrics, root-cause-triage, incident-kb)
export JIRA_BASE_URL="https://your-instance.atlassian.net"
export JIRA_EMAIL="you@example.com"
export JIRA_API_TOKEN="your_api_token"

# Team config — VAULT_DIR|PROJECT_KEY|BOARD_ID|DISPLAY_NAME (comma-separated)
export SPRINT_TEAMS="TeamA|PROJA|123|Team Alpha,TeamB|PROJB|456|Team Beta"

# GitLab (sprint-metrics, sprint-pulse)
export GITLAB_URL="https://gitlab.com"
export GITLAB_TOKEN="your_gitlab_token"        # read_api scope
export GITLAB_PROJECT_ID="12345678"

# Support tickets (sprint-pulse, support-routing-audit)
export SUPPORT_PROJECT_KEY="SUP"
export SUPPORT_BOARD_ID="789"
export SUPPORT_TEAM_LABEL="label-a,label-b|label-c"
export SUPPORT_TEAM_FIELD_VALUES="TeamA,TeamB|TeamC"

# Charter teams allow-list (support-routing-audit) — pipe-delimited canonical
# names; optional comma-separated aliases per slot after a colon
export CHARTER_TEAMS="TeamA|TeamB|TeamC:gamma,team-c"

# Root cause triage
export TRIAGE_BOARD_ID="731"
export TRIAGE_PARENT_ISSUE_KEY="PROJ-1234"

# Support ticket triage (support-ticket-triage)
export CODEBASE_PATH="/absolute/path/to/your/codebase"
# Optional:
export CODE_SEARCH_EXTENSIONS="rb,ts,tsx,go,py,js,jsx,rs,java"
export ROOT_CAUSE_EPICS="PROJ-1234,PROJ-5678"

# Incident KB
export RETRO_PARENT_PAGE_ID="66895715"
export INCIDENT_KB_OUTPUT_PATH="$OBSIDIAN_TEAMS_PATH/TeamA/Incidents"
# Optional:
export INC_PROJECT_KEY="INC"
export RETRO_TEMPLATE_PAGE_ID="3485925377"
```

### Vault structure

Skills expect a teams directory with this structure:

```
{OBSIDIAN_TEAMS_PATH}/
  {Team Name}/
    {Person Name}/
      {Person Name}.md          # Profile with YAML frontmatter (email, team, role, etc.)
      Feedback/
        Mid Year Review Cycle - {year}.md
        EOY Review Cycle - {year}.md
        Bonusly - {year}-{month}.md   # Auto-generated by bonusly-sync
    Retros/                            # Auto-generated by retro-summary
      Retro - 2025-11-05.md
    Sprints/                           # Auto-generated by sprint-summary + sprint-metrics + sprint-pulse
      Sprint Name - 2026-03-05.md
      Sprint Name - 2026-03-05 - Metrics.md
      Sprint Name - Pulse - 2026-03-27.md
  Logs/                               # Auto-generated by bonusly-sync
```

## Usage

### bank-statement-to-markdown

Converts St.George Bank PDF statements (Complete Freedom transaction accounts and Amplify Signature credit cards) into Markdown files with YAML frontmatter, an account summary, and a transaction table. Each PDF becomes a sibling `.md` file with the same base filename.

```bash
/bank-statement-to-markdown                                  # process all unprocessed PDFs in $STATEMENTS_PATH
/bank-statement-to-markdown CompleteFreedom-XXXXXXXXX-26Mar2026.pdf  # single PDF (absolute or relative path)
/bank-statement-to-markdown --dry-run                        # preview without writing
/bank-statement-to-markdown --output-dir ~/review            # write to a review directory instead of $STATEMENTS_PATH
```

**Prerequisites:**
- `STATEMENTS_PATH` in `~/.zshrc` pointing at the directory containing the PDFs

### bonusly-sync

Pulls the previous month's Bonusly recognition (given and received) for tracked team members and saves markdown files into each person's `Feedback/` folder.

```bash
/bonusly-sync              # sync to vault
/bonusly-sync --dry-run    # preview without writing files
```

**Prerequisites:**
- `OBSIDIAN_TEAMS_PATH` and `BONUSLY_API_TOKEN` in `~/.zshrc`
- Person profile notes with `email` in YAML frontmatter

### incident-kb

Build a searchable incident knowledge base from Confluence retrospectives and Jira INC epics. Cross-references both sources, writes date-sorted per-incident files (`YYYY-MM-DD — INC-KEY — Title.md`), routes test incidents to a `_test/` subdirectory, and generates trend/recurrence reports. Per-incident files include `tags: [incident, sev/{level}]` frontmatter for Obsidian graph clustering. Slack channel references in retro content are wrapped in backticks to prevent false tag parsing.

```bash
/incident-kb                          # full pipeline: fetch + generate
/incident-kb --team TeamA             # associate with team
/incident-kb --dry-run                # preview without writing files
/incident-kb --force                  # re-fetch all data
/incident-kb --report-only            # regenerate reports from cached data
```

**Prerequisites:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `~/.zshrc` (reused from other skills)
- `RETRO_PARENT_PAGE_ID`, `INCIDENT_KB_OUTPUT_PATH` in `~/.zshrc`
- Optional: `INC_PROJECT_KEY` (defaults to "INC"), `RETRO_TEMPLATE_PAGE_ID`

### feedback-perf

Capture dated performance feedback throughout the review period, then synthesize it into draft review responses using an Opus agent.

**Capture** — append a dated note to a team member's review cycle document:
```bash
/feedback-perf capture Alex: Great cross-team collaboration on the project
/feedback-perf capture eoy Jordan: Led the migration to the new auth system
/feedback-perf capture mid Sam: Improved deploy pipeline reliability
```

**Synthesize** — distill captured feedback and Bonusly data into draft review answers:
```bash
/feedback-perf synthesize Alex
/feedback-perf synthesize eoy Jordan
```

**Prerequisites:**
- `OBSIDIAN_TEAMS_PATH` in `~/.zshrc`
- Person profile notes with YAML frontmatter
- Review cycle documents under each person's `Feedback/` directory

### retro-summary

Extract retrospective data from a FigJam board, synthesize themes with AI, and write a structured summary to the vault.

```bash
/retro-summary https://figma.com/board/abc123/TeamA-Retro          # team inferred from board name
/retro-summary https://figma.com/board/abc123/Retro --team TeamA   # explicit team
/retro-summary https://figma.com/board/abc123/Retro --list        # list available retro sections
/retro-summary https://figma.com/board/abc123/Retro --dry-run     # preview without writing
```

**Prerequisites:**
- `OBSIDIAN_VAULT_PATH` and `OBSIDIAN_TEAMS_PATH` in `~/.zshrc`
- A FigJam board using Rose/Thorn/Bud or Wind/Sun/Anchor/Reef retro format with section-based layout
- Figma MCP server connected in Claude Code

### root-cause-triage

Two modes for working with root cause tickets on a Jira board:

**Collect** — build a per-issue Obsidian knowledge base from Jira data. Fetches issues and linked issue details, writes structured Markdown with extractive summaries, then uses agents to produce quality linked issue summaries and a root cause analysis synthesis. Per-issue files include `tags: [root-cause]` frontmatter (plus classification tag after enrichment) for Obsidian graph clustering:
```bash
/root-cause-triage collect                    # full pipeline: fetch, summarize, enrich
/root-cause-triage collect --issue PROJ-1234  # collect a single issue
/root-cause-triage collect --status "To Triage" # filter by status
/root-cause-triage collect --dry-run          # preview without writing
/root-cause-triage collect --force            # overwrite existing files
/root-cause-triage collect --include-done     # include stale done items hidden by the board
/root-cause-triage collect --index-only      # regenerate _Index.md from cache (no Jira fetch)
```

**Analyze** — run structural and semantic analysis on collected data (informational, no Jira mutations):
```bash
/root-cause-triage analyze                    # analyze "To Triage" issues
/root-cause-triage analyze --all-statuses     # analyze everything
/root-cause-triage analyze --issue PROJ-1234  # analyze a single issue
```

**Prerequisites:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `TRIAGE_BOARD_ID`, `TRIAGE_PARENT_ISSUE_KEY`, and `TRIAGE_OUTPUT_PATH` in `~/.zshrc`

### sprint-pulse

Generate mid-sprint alerts and DORA snapshot by analysing Jira sprint data, GitLab MR activity, and support tickets. Surfaces stale items, support ticket issues, highest priority tickets, outstanding questions, and deployment frequency / lead time metrics.

```bash
/sprint-pulse                          # prompts for team
/sprint-pulse --team TeamA             # skip team prompt
/sprint-pulse --team TeamA --dry-run   # preview without writing
```

**Alert types:**
- **Stale items** — in-progress issues with no activity for >1 business day
- **Support tickets** — new in to-do, unacknowledged >48h (no comments or assignee), SLA risk (business day targets)
- **Highest priority** — active Highest tickets with triad review trigger when >=2
- **Outstanding questions** — unanswered questions in Jira comments and GitLab MR notes

**Prerequisites:**
- `OBSIDIAN_TEAMS_PATH`, Jira credentials, `SPRINT_TEAMS`, and GitLab credentials in `~/.zshrc`
- Optional: `SUPPORT_PROJECT_KEY`, `SUPPORT_BOARD_ID`, `SUPPORT_TEAM_LABEL`, `SUPPORT_TEAM_FIELD_VALUES` for support ticket alerts

### sprint-metrics

Generate engineering metrics from GitLab merge requests linked to Jira sprint issues. Best used after running `/sprint-summary` — it can read issue keys directly from the summary file.

```bash
/sprint-metrics                          # prompts for team and sprint
/sprint-metrics --team TeamA              # skip team prompt
/sprint-metrics "Sprint 2026 4"          # specific sprint by name
/sprint-metrics --team TeamA --dry-run    # preview without writing
```

**Metrics:** Time to Merge, Review Turnaround, Time to Approval, Cycle Time — aggregated (avg/median), per-author, and per-MR. Also includes DORA metrics: Deployment Frequency (total MRs merged to default branch by team members, divided by sprint days) and Lead Time for Changes (first commit to merge, median + P90), with DORA rating classifications (Elite/High/Medium/Low).

**Prerequisites:**
- `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TEAMS_PATH`, Jira credentials, and GitLab credentials in `~/.zshrc`
- Existing sprint summary file (preferred) or Jira API access (fallback)

### sprint-summary

Pull Jira sprint data (issues, story points, goals, dates) for configured teams and write structured sprint summary markdown files into the vault.

```bash
/sprint-summary                          # prompts for team, then shows recent closed sprints
/sprint-summary --team TeamA              # skip team prompt
/sprint-summary "Sprint 2026 4"          # specific sprint by name (skip sprint prompt)
/sprint-summary --team TeamA --dry-run    # preview without writing
```

Generates one team per run to keep context usage low. Run again for additional teams.

**Prerequisites:**
- `OBSIDIAN_VAULT_PATH`, `OBSIDIAN_TEAMS_PATH`, Jira credentials, and `SPRINT_TEAMS` in `~/.zshrc`
- A Jira API token ([generate here](https://id.atlassian.com/manage-profile/security/api-tokens))

### support-ticket-triage

Triage a single Jira support ticket end-to-end. Fetches the ticket, its linked issues, resolved look-alikes, and (optionally) tickets parented to designated root-cause epics. Then spawns a sub-agent that reads the cached bundle, investigates the local codebase, classifies the issue, and returns a filled Resolution Summary.

Classifications:
- 🐛 **Code Bug** — defect in existing code; cites exact file:line
- 🚀 **PFR** — feature doesn't exist; describes what would need to be built
- ⚙️ **Config Issue** — misconfigured DB record/flag/setting; cites exact table.column

Every state-changing resolution step includes a `⚠️ SAFEGUARDS` block (before / implications / rollback) and a `[L2]` or `[ENG]` support-tier label.

```bash
/support-ticket-triage PROJ-123                     # triage a ticket
/support-ticket-triage PROJ-123 --similar-limit 5   # fewer similar-ticket lookups
```

**Prerequisites:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `CODEBASE_PATH` in `~/.zshrc`
- Optional: `SUPPORT_PROJECT_KEY` (scopes similar-ticket JQL), `ROOT_CAUSE_EPICS` (comma-separated Jira keys, validated against `^[A-Z][A-Z0-9_]+-\d+(,...)*$`), `CODE_SEARCH_EXTENSIONS`
- Notion MCP server connected in Claude Code (optional — used only if `references/` in the codebase doesn't cover the affected area)
- Read-only: the skill never writes to Jira

### support-trends

Build a monthly Markdown digest of one team's support tickets — volume, themes, charter drift candidates, L2 containment signals, categorisation quality, and resolution-category breakdown — for a leadership audience. Three sub-agents (themes / support-feedback / synthesise) enrich a deterministic finding set produced by `analyze.py`; the report renderer is pure and rejects any claim without a metric and `evidence_keys`.

```bash
/support-trends --team TeamA                              # previous calendar month
/support-trends --team TeamA --window 2026-04             # specific month
/support-trends --team TeamA --window 2026-04-01..2026-04-15  # explicit range
```

Output lands in `{OBSIDIAN_TEAMS_PATH}/{vault_dir}/Support/Trends/{year}/` and a terminal preview at `/tmp/support_trends/report.md`.

**Prerequisites:**
- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `OBSIDIAN_TEAMS_PATH`, `SPRINT_TEAMS`, `SUPPORT_PROJECT_KEY` in `~/.zshrc`
- At least one of `SUPPORT_TEAM_LABEL` or `SUPPORT_TEAM_FIELD_VALUES` (the skill aborts otherwise to avoid querying the entire support project)
- Optional: `SUPPORT_BOARD_ID` (improves closed-status detection), `CHARTER_TEAMS` (enables charter-drift suggestions)

### vault-linker

Scan existing Obsidian vault files and add `[[wiki links]]` to known entities. Links body content, YAML frontmatter fields, and generates index/hub pages. Idempotent — safe to re-run.

```bash
/vault-linker                          # link all scopes
/vault-linker --dry-run                # preview without modifying files
/vault-linker --scope retros           # link retro files only
/vault-linker --scope incidents        # link incident files only
/vault-linker --scope triage           # link root cause triage issue files only
/vault-linker --scope bonusly          # link bonusly feedback files only
```

**Body content linking:**
- Person names → `[[Person Name]]` (in retro feedback, bonusly entries)
- INC keys → `[[YYYY-MM-DD — INC-NN — Title\|INC-NN]]` (cross-references between incidents)
- Triage keys → `[[PDE-NNNN — Summary\|PDE-NNNN]]` (in incident remediation sections)
- Augmented Jira links — appends `([[vault page\|KEY]])` after existing `[KEY](url)` markdown links when a matching vault page exists

**YAML frontmatter linking:**
- `team:` field → `"[[Team Name]]"` (connects files to team hub pages)
- `participants:` field → `["[[Name1]]", "[[Name2]]"]` (connects retros to person pages)
- `person:` backlink → `"[[Name]]"` added to bonusly and review cycle files (connects feedback to person pages)

**Index and hub pages generated:**
- `Teams/{team}/Sprints/_Timeline.md` — sprint timeline per team
- `Teams/{team}/{team}.md` — team hub with member links, sprint timeline, and retro links
- `Plans/_Index.md` — index of all plan files in the vault

> **Note:** `Incidents/_Index.md` is generated by `/incident-kb` (which has access to severity data), not by vault-linker. Index pages are excluded from Obsidian's graph view via `graph.json` filter. Sprint summary assignee names are intentionally left as plain text to avoid graph noise.

**Prerequisites:**
- `OBSIDIAN_TEAMS_PATH` in `~/.zshrc`

### review-memory

Sweep `~/.claude/projects/*/memory/` across every project on this machine. For each accumulated memory entry, propose ONE action — keep, move to repo `CLAUDE.md`, move to global `~/.claude/CLAUDE.md`, or delete — with a one-sentence reason. Per-item user approval gates every side effect; no auto mode.

```bash
/review-memory                                              # all projects
/review-memory --project /Users/jasonconroy/.claude/skills  # one project by decoded path
/review-memory --project -Users-jasonconroy-hppy-connect    # one project by encoded dir name
```

Discovery is deterministic (`discover.py` walks the projects tree, decodes dir names, captures frontmatter + body + mtime, runs a conservative substring duplicate-check against the project's `CLAUDE.md`). Classification (which entries belong where) is the model's judgment.

The split rule applied:
- **Repo `CLAUDE.md`** — code/design conventions applicable to anyone touching the repo
- **Project memory** — Claude collaboration preferences specific to this repo
- **Global `~/.claude/CLAUDE.md`** — preferences applying to every project on this machine

### reflect

End-of-session retrospective. Auto-archives a structured 7-section reflection file to the iCloud-synced Obsidian vault (cross-laptop), then proposes promoting durable lessons into `CLAUDE.md`, global config, or memory with per-item approval. The flow-out is the point — without it the archive becomes write-only.

```bash
/reflect                  # full reflection + archive + per-item promotion approval
/reflect --skip-archive   # in-conversation reflection only, no archive file
```

Sections: Context (1 line), What Worked (≤4), What Went Sideways (≤4), Tips & Tricks (≤3), Generalization Opportunities (≤4, each with an explicit promotion target), Action Items (checkboxes), Promoted To (footer recording where each lesson actually landed).

Archive lands at `${OBSIDIAN_VAULT_PATH}/Reflections/<YYYY-MM-DD>-<slug>.md` with `tags: [reflection]` frontmatter for graph clustering. Falls back to `~/.claude/reflections/` (with a warning) if the env var is unset.

Discipline rules baked into the framework: every bullet must cite a specific moment (no generic claims); section length is capped (anti-bloat); per-item approval for any CLAUDE.md/memory edits; second confirmation required for global CLAUDE.md edits even within an approve-all batch.

**Prerequisites:**
- `OBSIDIAN_VAULT_PATH` in `~/.zshrc` (optional but recommended — without it, reflections won't sync across laptops via iCloud)
