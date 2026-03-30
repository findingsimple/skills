---
name: root-cause-triage
description: Collects root cause ticket data to Obsidian knowledge base and analyzes for duplicates, quality, and completeness. Use when the user asks to collect root cause data, check for duplicate issues, or assess issue quality.
disable-model-invocation: true
argument-hint: "[collect|analyze] [--issue KEY] [--status STATUS] [--dry-run] [--force]"
---

# Root Cause Triage

Two modes for working with root cause tickets under the triage board:

- **collect** — fetch Jira data and build a per-issue Obsidian knowledge base
- **analyze** — structural + semantic analysis on collected data (completeness, duplicates, quality assessment)

Uses `TRIAGE_BOARD_ID`, `TRIAGE_PARENT_ISSUE_KEY`, and `TRIAGE_OUTPUT_PATH` environment variables.

## Board Columns

TO TRIAGE → MORE INFO REQUIRED → READY FOR DEVELOPMENT → IN PROGRESS → REJECTED → COMPLETED / ROADMAPPED

## Architecture

### Key files

| File | Purpose |
|------|---------|
| `jira_client.py` | Shared Jira API client — `load_env`, `init_auth`, `jira_get`, `jira_post`, `jira_search_all`, `adf_to_text` |
| `collect.py` | Mode: collect — fetch issues + linked issue details, save per-issue JSON to `/tmp/triage_collect/` |
| `summarize.py` | Mode: collect — read per-issue JSON, generate Obsidian Markdown with extractive summaries |
| `enrich.py` | Mode: collect — prepare agent batches and apply enriched summaries to Markdown files |
| `ENRICH_PROMPT.md` | Agent prompt template for linked issue summarization and root cause synthesis |
| `autofill.py` | Mode: collect — auto-fill missing template sections for 0/5 issues using agent synthesis |
| `AUTOFILL_PROMPT.md` | Agent prompt template for template section autofill |
| `analyze.py` | Mode: analyze — structural scoring, duplicate detection, loads enrichment data, writes report + `/tmp/triage_analysis.json` |
| `QUALITY_PROMPT.md` | Agent prompt for raw quality assessment (used by analyze mode Step A2a) |
| `POST_ENRICH_QUALITY_PROMPT.md` | Agent prompt for post-enrichment quality assessment (used by analyze mode Step A2b) |
| `DUPLICATE_PROMPT.md` | Agent prompt for semantic duplicate detection using enriched data (used by analyze mode Step A2c) |

## Instructions

### Step 1 — Parse mode and arguments

Parse `$ARGUMENTS` to determine the mode:

- First positional argument: `collect` or `analyze`
- If no mode specified, ask the user which mode to run
- Remaining arguments are passed through to the relevant script

Show the parsed values:
```
Mode: {collect|analyze}
Board: {TRIAGE_BOARD_ID}
Epic: {TRIAGE_PARENT_ISSUE_KEY}
Output: {TRIAGE_OUTPUT_PATH}
```

---

## Mode: Collect

Build a per-issue Obsidian knowledge base from Jira data. Four steps:
1. `collect.py` fetches all data from Jira and saves per-issue JSON to `/tmp/triage_collect/`
2. `summarize.py` reads the JSON files, generates Obsidian Markdown with extractive summaries
3. `enrich.py` + agent calls produce quality linked issue summaries and a root cause analysis synthesis
4. `autofill.py` + agent calls auto-fill missing template sections for 0/5 issues using enrichment + linked issue evidence

### Step C1 — Run collect.py

```bash
python3 ~/.claude/skills/root-cause-triage/collect.py [--issue KEY] [--status STATUS] [--dry-run] [--force]
```

Pass through any arguments from Step 1. If `collect.py` exits with a non-zero status, display the error and stop.

If `--dry-run` was specified, show the output and stop — do not proceed to summarization.

### Step C2 — Run summarize.py

```bash
python3 ~/.claude/skills/root-cause-triage/summarize.py [--issue KEY] [--force] [--dry-run]
```

Pass through `--issue`, `--force`, and `--dry-run` flags from Step 1.

The script:
- Reads per-issue JSON from `/tmp/triage_collect/`
- Skips issues that already have a Markdown file (unless `--force`)
- Generates frontmatter (key, board_column, status, issue_type, priority, reporter, created, parent_epic, collected_at, linked_issue_count)
- Writes full description including subtask content
- Groups linked issues by relationship type (duplicates, relates to, causes)
- Summarizes linked issue descriptions using keyword-prioritized extraction (3-5 key sentences)
- For large "causes" groups (>10 items), writes a paragraph summary with a compact list
- Writes Markdown files to `{TRIAGE_OUTPUT_PATH}/Issues/{KEY} — {sanitized summary}.md`

**`board_column` resolution:** The board maps specific Jira status IDs to named columns (e.g., status "Backlog" id:10002 → column "To Triage"). Issues whose status is not mapped to any board column (e.g., "Closed", "Cancelled") fall back to using the raw Jira status name.

**Board count vs collect count:** `collect.py` uses `parent = {TRIAGE_PARENT_ISSUE_KEY}` (direct children only), while the board filter uses `parentEpic in (...)` which traverses the full hierarchy and spans multiple parent epics. This means the board count will typically be slightly higher because it includes: (1) the parent epic itself, (2) subtasks of child issues (e.g., Code subtasks that appear in Rejected), and (3) children of other parent epics on the board. Subtask data is already captured within the parent issue's JSON, so this gap is expected and not a data loss.

### Step C3 — Agent enrichment

This step uses agents to replace the extractive summaries from Step C2 with quality, contextual summaries and adds a "Root Cause Analysis" synthesis section to each issue.

If `--dry-run` was specified in Step 1, skip this step.

**C3a — Prepare batches:**

```bash
python3 ~/.claude/skills/root-cause-triage/enrich.py prepare [--issue KEY] [--batch-size 5] [--force]
```

Pass through `--issue` and `--force` from Step 1. This reads the collected JSON, builds agent prompts grouped into batches, and writes them to `/tmp/triage_enrich/`. Issues without linked issue descriptions are skipped.

If no issues need enrichment, stop here.

**C3b — Run agent batches:**

Read `/tmp/triage_enrich/batches.json` to get the list of batches. For each batch:

1. Spawn a `general-purpose` agent using **model: sonnet** with the prompt: "Use the Bash tool to run: cat /tmp/triage_enrich/batch_NNN.txt — Then follow the instructions in the file exactly. Return ONLY the JSON response as specified in the prompt."
   > **Note:** Agents cannot use the Read tool on `/tmp/` paths due to sandbox permissions. Always instruct agents to use `cat` via the Bash tool to read batch files.
2. Parse the agent's JSON response — if not valid JSON, strip markdown fences and retry parsing
3. For each issue in the response, save to `/tmp/triage_enrich/result_{KEY}.json`

Run up to 3 agent batches concurrently to balance speed with reliability. If a batch fails to parse, log the raw response to `/tmp/triage_enrich/failed_batch_{N}.txt` and continue with remaining batches.

**C3c — Apply results:**

```bash
python3 ~/.claude/skills/root-cause-triage/enrich.py apply [--issue KEY] [--dry-run]
```

This reads the agent results and updates the Markdown files:
- Adds `classification` to frontmatter (one of: `code_bug`, `feature_request`, `config_issue`, `docs_gap`, `process_gap`)
- Inserts a "## Root Cause Analysis" section above Description (first section after the heading)
- Replaces extractive `**Summary:**` lines with agent-quality summaries

After applying, report:
```
Enrichment complete:
- {n} files enriched with root cause analysis
- {n} linked issue summaries upgraded
- {n} skipped (no enrichment result)
```

### Step C4 — Template section autofill

This step auto-fills the 5 template sections (Background Context, Steps to reproduce, Actual Results, Expected Results, Analysis) for issues that score 0/5. It uses enrichment data + linked issue evidence to synthesise section content with per-section confidence levels (high/medium/low).

If `--dry-run` was specified in Step 1, skip this step.

**C4a — Prepare batches:**

```bash
python3 ~/.claude/skills/root-cause-triage/autofill.py prepare [--issue KEY] [--batch-size 10] [--force]
```

Pass through `--issue` and `--force` from Step 1. This identifies 0/5 issues, loads their enrichment results + collected data, and builds agent prompts in batches of 10. Writes to `/tmp/triage_autofill/`.

If no issues need autofill, stop here.

**C4b — Run agent batches:**

Read `/tmp/triage_autofill/batches.json` to get the list of batches. For each batch:

1. Spawn a `general-purpose` agent using **model: sonnet** with the prompt: "Use the Bash tool to run: cat /tmp/triage_autofill/batch_NNN.txt — Then follow the instructions in the file exactly. Return ONLY the JSON response as specified in the prompt."
2. Parse the agent's JSON response — if not valid JSON, strip markdown fences and retry parsing
3. For each issue in the response, save to `/tmp/triage_autofill/result_{KEY}.json`

Run up to 3 agent batches concurrently. If a batch fails to parse, log the raw response to `/tmp/triage_autofill/failed_batch_{N}.txt` and continue.

**C4c — Apply results:**

```bash
python3 ~/.claude/skills/root-cause-triage/autofill.py apply [--issue KEY] [--dry-run]
```

This reads agent results and updates Markdown files:
- Adds `autofill: agent-generated` to frontmatter
- Inserts a "## Auto-filled Template Sections" block with per-section confidence levels (high/medium/low)
- Each section header includes an evidence note (N linked tickets, M with descriptions)

---

## Mode: Analyze

Run structural and semantic analysis on the collected Obsidian knowledge base. Produces an informational report — no Jira mutations.

### Step A1 — Run analyze.py

```bash
python3 ~/.claude/skills/root-cause-triage/analyze.py [--issue KEY] [--status STATUS]
```

Analyzes all statuses by default. Pass `--status "To Triage"` to filter to a single board column.

This reads collected data (from `/tmp/triage_collect/` or Obsidian files), loads enrichment results (from `/tmp/triage_enrich/result_*.json`) for classification and root cause previews, runs template completeness scoring, text-similarity duplicate detection, and resolution status assessment against the full knowledge base, writes an analysis report to `{TRIAGE_OUTPUT_PATH}/Analysis/`, and saves results to `/tmp/triage_analysis.json`.

Resolution status is derived from board column + linked dev tickets (via "blocks", "is implemented by", "implements" relationships). Values: `unresolved`, `in_progress`, `roadmapped`, `resolved`, `rejected`, `blocked`.

If `analyze.py` exits with a non-zero status, display the error and stop.

### Step A2a — Raw quality assessment

Assesses ticket quality based on **raw Jira descriptions only**. This tells you which Jira tickets need their descriptions backfilled — useful signal independent of the knowledge base.

Read `/tmp/triage_analysis.json` and load triage history:

```bash
cat ~/.claude/skills/root-cause-triage/triage_history.json 2>/dev/null || echo "[]"
```

Read [QUALITY_PROMPT.md](QUALITY_PROMPT.md) for the full agent prompt. Build it by iterating over all issues in the analysis JSON, using each issue's `description` field (already truncated to 800 chars by analyze.py).

Spawn a `general-purpose` agent using **model: opus** with the constructed prompt.

Parse the agent's JSON response. If the response is not valid JSON, attempt to extract a JSON array from within the response (strip markdown fences or preamble). If that also fails, log the raw response to `/tmp/triage_agent_raw.txt`, inform the user of the failure, and fall back to structural-only results (skip to Step A4).

**Batching:** If there are more than 10 issues, process them in batches of 10, spawning a separate agent call for each batch. Run up to 3 batches concurrently. Merge the results before proceeding.

For each issue, merge the agent's `quality`, `quality_note`, `duplicate_assessment`, `recurrence_assessment`, and `recommended_action` with the structural analysis from `/tmp/triage_analysis.json`.

Save merged results to `/tmp/triage_analysis_enriched.json` using a bash heredoc.

### Step A2b — Post-enrichment quality assessment

Assesses ticket quality based on the **combined evidence**: raw description + enrichment data (classification, root cause analysis) + autofill sections (template sections with confidence levels). This tells you which issues are PM-ready given the full knowledge base.

Read [POST_ENRICH_QUALITY_PROMPT.md](POST_ENRICH_QUALITY_PROMPT.md) for the full agent prompt. Build it by iterating over all issues in the analysis JSON, adding for each issue:

1. **Raw description** from `/tmp/triage_analysis.json`
2. **Enrichment data** from `/tmp/triage_enrich/result_{KEY}.json` — classification and root cause analysis (truncated to 300 chars)
3. **Autofill sections** from `/tmp/triage_autofill/result_{KEY}.json` — each section's content (truncated to 300 chars) and confidence level

**Batching:** Same as Step A2a — batches of 10, up to 3 concurrent, model: opus.

For each issue, add the agent's `post_enrich_quality`, `post_enrich_note`, and `post_enrich_action` to `/tmp/triage_analysis_enriched.json`.

### Step A2c — Post-enrichment duplicate detection

Runs **semantic** duplicate detection using enriched root cause analyses. This catches duplicates that text similarity on empty descriptions misses — issues describing the same underlying deficiency in different words.

Read [DUPLICATE_PROMPT.md](DUPLICATE_PROMPT.md) for the full agent prompt. Build a single prompt containing all issues with: key, summary, classification, root cause analysis (truncated to 300 chars), and autofill analysis section (truncated to 250 chars).

Spawn a single `general-purpose` agent using **model: opus** with the prompt.

The agent returns two types of clusters:
- **`duplicate`** — issues describing the same root cause; one is primary, others are duplicates
- **`related`** — issues sharing a theme but needing separate implementations (useful for prioritisation)

Create the output directory (`mkdir -p /tmp/triage_duplicates`) and save results to `/tmp/triage_duplicates/clusters.json`.

### Step A3 — Update Obsidian report

Read the analysis report that `analyze.py` wrote to `{TRIAGE_OUTPUT_PATH}/Analysis/Analysis - {YYYY-MM-DD}.md`. Append three sections:

**Quality Assessment** (from Step A2a):

```markdown
## Quality Assessment

| Key | Quality | Note | Dup Assessment | Recurrence | Recommended Action |
|-----|---------|------|----------------|------------|--------------------|
| [KEY](JIRA_BASE_URL/browse/KEY) | good/thin/vague | note or -- | confirmed/unlikely/n/a | likely/unlikely/n/a | ready/more_info/duplicate/skip |
{... one row per issue, no blank lines between rows ...}

### Issues Flagged as Thin or Vague
{Per-issue breakdown for quality != "good"}
```

**Post-Enrichment Quality Assessment** (from Step A2b):

```markdown
## Post-Enrichment Quality Assessment

### Summary Comparison
| Metric | Raw Assessment | Post-Enrichment |
|--------|---------------|-----------------|
| Good | N | N |
| Thin | N | N |
| Vague | N | N |

### Post-Enrichment Ratings
| Key | Raw Quality | Post-Enrichment | Note | Recurrence | Action |
{Mark upgrades with **↑**}

### Needs More Information
{Issues where post-enrichment action is "more_info" — still thin/vague after enrichment, need human review}

### Ready for Development
{Issues where post-enrichment action is "ready" — sufficient combined evidence for a PM to scope work}
```

**Post-Enrichment Duplicate & Overlap Analysis** (from Step A2c):

```markdown
## Post-Enrichment Duplicate & Overlap Analysis

### Confirmed Duplicates
| Primary | Duplicate(s) | Rationale |

### Related Clusters
{Per-theme breakdown with tickets and overlap rationale}
```

Use the Write tool to overwrite the report file with the appended content.

### Step A4 — Present results

Show the consolidated summary comparing raw vs post-enrichment quality. Highlight:
- The quality upgrade from enrichment (how many issues moved from vague/thin to good)
- Issues that remain thin/vague even after enrichment (need human review)
- Confirmed semantic duplicates (with rationale)
- Related clusters (for prioritisation)
- Issues where structural analysis says "ready" but agent says "thin" or "vague" (disagreement)
- Issues with many linked support tickets (signal of real user impact)

This is informational — the user reviews the report in Obsidian and decides next steps.
