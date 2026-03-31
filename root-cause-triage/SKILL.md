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
| `analyze.py` | Mode: analyze — structural scoring, duplicate detection, loads enrichment + autofill data, writes Raw Analysis + Enriched Analysis reports + `/tmp/triage_analysis.json` |
| `build_prompts.py` | Mode: analyze — builds agent batch prompts from analysis JSON + enrichment/autofill data, writes to `/tmp/triage_prompts/` |
| `merge_results.py` | Mode: analyze — collects agent output files from `/tmp/triage_prompts/`, merges into `/tmp/triage_analysis_enriched.json` + `/tmp/triage_duplicates/clusters.json` |
| `QUALITY_PROMPT.md` | Agent prompt reference for raw quality assessment (Step A2a) — embedded in `build_prompts.py` |
| `POST_ENRICH_QUALITY_PROMPT.md` | Agent prompt reference for post-enrichment quality assessment (Step A2b) — embedded in `build_prompts.py` |
| `DUPLICATE_PROMPT.md` | Agent prompt reference for semantic duplicate detection (Step A2c) — embedded in `build_prompts.py` |

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

Run structural and semantic analysis on the collected Obsidian knowledge base. Produces two informational reports — no Jira mutations:
- **Raw Analysis** — Jira data only (template scores, text-similarity duplicates, structural signals)
- **Enriched Analysis** — full picture with classification, root cause, autofill, and semantic duplicates

### Step A1 — Run analyze.py

```bash
python3 ~/.claude/skills/root-cause-triage/analyze.py [--issue KEY] [--status STATUS]
```

Analyzes all statuses by default. Pass `--status "To Triage"` to filter to a single board column.

This reads collected data (from `/tmp/triage_collect/` or Obsidian files), loads enrichment results (from `/tmp/triage_enrich/result_*.json`) and autofill results (from `/tmp/triage_autofill/result_*.json`), runs template completeness scoring, text-similarity duplicate detection, and resolution status assessment against the full knowledge base, and writes two reports:

- `{TRIAGE_OUTPUT_PATH}/Analysis/Raw Analysis - {YYYY-MM-DD}.md`
- `{TRIAGE_OUTPUT_PATH}/Analysis/Enriched Analysis - {YYYY-MM-DD}.md`

Results are also saved to `/tmp/triage_analysis.json`.

Resolution status is derived from board column + linked dev tickets (via "blocks", "is implemented by", "implements" relationships). Values: `unresolved`, `in_progress`, `roadmapped`, `resolved`, `rejected`, `blocked`.

If `analyze.py` exits with a non-zero status, display the error and stop.

### Step A2 — Build agent prompts

```bash
python3 ~/.claude/skills/root-cause-triage/build_prompts.py all [--batch-size 10]
```

This builds all three prompt types from `/tmp/triage_analysis.json`:
- **raw-quality** (A2a) — assesses ticket quality based on raw Jira descriptions only
- **post-enrich-quality** (A2b) — assesses quality using combined evidence (raw + enrichment + autofill)
- **duplicates** (A2c) — semantic duplicate detection using enriched root cause analyses

Output: `/tmp/triage_prompts/{type}/batch_N.txt` + `batches.json` manifest per type.

Each batch prompt instructs the agent to write its JSON results to a specific output file in `/tmp/triage_prompts/{type}/results_batch_N.json`.

### Step A2a/A2b — Spawn quality assessment agents

Read the batch manifests:
- `/tmp/triage_prompts/raw-quality/batches.json`
- `/tmp/triage_prompts/post-enrich-quality/batches.json`

For each batch in both manifests, spawn a `general-purpose` agent using **model: opus** with the prompt:

> "Use the Bash tool to run: `cat {batch_file}` — Then follow the instructions in the file exactly."

> **Note:** Agents cannot use the Read tool on `/tmp/` paths due to sandbox permissions. Always instruct agents to use `cat` via the Bash tool to read batch files.

Launch all batches concurrently — they are independent. Run A2a and A2b batches in parallel.

### Step A2c — Spawn duplicate detection agent

Read `/tmp/triage_prompts/duplicates/batches.json`. Spawn a single `general-purpose` agent using **model: opus** with:

> "Use the Bash tool to run: `cat {batch_file}` — Then follow the instructions in the file exactly."

This can run concurrently with A2a/A2b agents.

The agent identifies two types of clusters:
- **`duplicate`** — issues describing the same root cause; one is primary, others are duplicates
- **`related`** — issues sharing a theme but needing separate implementations (useful for prioritisation)

### Step A2d — Merge agent results

Once all agents have completed:

```bash
python3 ~/.claude/skills/root-cause-triage/merge_results.py
```

This collects agent output files from `/tmp/triage_prompts/`, merges A2a/A2b results into `/tmp/triage_analysis_enriched.json`, and saves A2c duplicate clusters to `/tmp/triage_duplicates/clusters.json`.

To check which results are present before merging:

```bash
python3 ~/.claude/skills/root-cause-triage/merge_results.py --check
```

If any batches are missing, re-spawn the failed agents before merging.

### Step A3 — Update Obsidian reports

Read both reports that `analyze.py` wrote. Update each by replacing the HTML comment placeholders with content from Steps A2a–A2c.

#### Raw Analysis (`Raw Analysis - {YYYY-MM-DD}.md`)

**Replace `<!-- PLACEHOLDER:RAW_QUALITY -->` with Quality Assessment** (from Step A2a):

```markdown
| Key | Quality | Note | Dup Assessment | Recurrence | Recommended Action |
|-----|---------|------|----------------|------------|--------------------|
| [KEY](JIRA_BASE_URL/browse/KEY) | good/thin/vague | note or -- | confirmed/unlikely/n/a | likely/unlikely/n/a | ready/more_info/duplicate/skip |
{... one row per issue, no blank lines between rows ...}

### Issues Flagged as Thin or Vague
{Per-issue breakdown for quality != "good"}
```

**Replace `<!-- PLACEHOLDER:RAW_TOP10 -->` with Top 10 Highest Value Ready Issues:**

Build this by evaluating all "ready" issues (from structural recommendation) using these ranking factors:
1. **Linked ticket count** — more links = wider impact
2. **Support ticket count** — direct user pain signal
3. **Issue age** — how long the problem has been outstanding (use `created` date)
4. **Related cluster membership** — addressing one issue may address several (use duplicate clusters from Step A2c if available)
5. **Board column** — issues already in "Ready for Development" on the board are closer to action

```markdown
| # | Key | Summary | Links | Support | Age | Reasoning |
|---|-----|---------|-------|---------|-----|-----------|
{Top 10 rows with brief reasoning for each}
```

#### Enriched Analysis (`Enriched Analysis - {YYYY-MM-DD}.md`)

**Replace `<!-- PLACEHOLDER:ENRICHED_SUMMARY -->` with post-enrichment recommendation counts:**

Derive counts from the merged results in `/tmp/triage_analysis_enriched.json`:

```markdown
**By recommendation (post-enrichment):**
- Good (ready for development): N
- Thin (needs more information): N
- Vague (needs more information): N
- Potential duplicates (semantic): N
```

**Replace `<!-- PLACEHOLDER:ENRICHED_COMPARISON -->` with Raw vs Enriched Comparison:**

```markdown
| Metric | Raw Assessment | Post-Enrichment |
|--------|---------------|-----------------|
| Good | N | N |
| Thin | N | N |
| Vague | N | N |

Enrichment upgraded **N** issues from vague/thin to good.
```

**Replace `<!-- PLACEHOLDER:ENRICHED_QUALITY -->` with Post-Enrichment Quality Assessment** (from Step A2b):

```markdown
| Key | Raw Quality | Post-Enrichment | Note | Action |
|-----|-------------|-----------------|------|--------|
{Mark upgrades with **↑**, one row per issue}
```

**Replace `<!-- PLACEHOLDER:ENRICHED_DUPLICATES -->` with Confirmed Duplicates** (from Step A2c):

```markdown
| Primary | Duplicate(s) | Rationale |
|---------|-------------|-----------|
{One row per duplicate cluster}
```

**Replace `<!-- PLACEHOLDER:ENRICHED_CLUSTERS -->` with Related Clusters** (from Step A2c):

```markdown
{Per-theme breakdown with tickets and overlap rationale}
```

**Replace `<!-- PLACEHOLDER:ENRICHED_TOP10 -->` with Top 10 Highest Value Ready Issues:**

Same ranking factors as the Raw report, plus:
6. **Quality upgrade** — issues that went from vague/thin to good with high-confidence autofill are stronger candidates
7. **Classification** — code bugs with clear root cause analysis may be more immediately actionable than feature requests

```markdown
| # | Key | Summary | Classification | Links | Support | Age | Reasoning |
|---|-----|---------|---------------|-------|---------|-----|-----------|
{Top 10 rows with brief reasoning for each}
```

Use the Read tool to load each report, replace the placeholder comments with the content, and use the Write tool to save.

### Step A4 — Present results

Show a consolidated summary referencing both reports. Highlight:
- Where to find the **Raw Analysis** (Jira-only assessment) and **Enriched Analysis** (full picture)
- The quality upgrade from enrichment (how many issues moved from vague/thin to good)
- Issues that remain thin/vague even after enrichment (need human review)
- Confirmed semantic duplicates (with rationale)
- Related clusters (for prioritisation)
- Top issues from each report's Top 10 list
- Issues where structural analysis says "ready" but agent says "thin" or "vague" (disagreement)
- Issues with many linked support tickets (signal of real user impact)

This is informational — the user reviews the reports in Obsidian and decides next steps.
