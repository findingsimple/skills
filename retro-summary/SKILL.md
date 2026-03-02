---
name: retro-summary
description: Extract and summarize retrospectives from FigJam boards into Obsidian vault
disable-model-invocation: true
argument-hint: "<figjam-url> [--team <name>] [--dry-run] [--list]"
allowed-tools: Read Edit Write Glob Bash Agent WebFetch mcp__figma__get_figjam
---

# Retro Summary

Extract retrospective data from a FigJam board (Rose/Thorn/Bud format), categorize stickies, synthesize themes with AI, and write a structured summary to the Obsidian vault.

## Instructions

Follow these steps exactly:

### Step 0 — Parse arguments

Parse `$ARGUMENTS` for the FigJam URL and optional flags:

1. **FigJam URL** (required) — a `figma.com/board/...` URL. Extract `fileKey` from the URL path and `nodeId` from the `node-id` query parameter (converting `-` to `:`). If no `node-id` parameter is present, default to `0:1` (the page root).
2. **`--team <name>`** (optional) — explicitly set the team name for the output path. If not provided, the team is inferred from the board name (see Step 2).
3. **`--dry-run`** (optional) — preview output without writing any files to the vault.
4. **`--list`** (optional) — list all retro sections on the board with their dates and node IDs, then stop.

If no URL is provided, show usage help and stop:
```
Usage: /retro-summary <figjam-url> [--team <name>] [--dry-run] [--list]

Examples:
  /retro-summary https://figma.com/board/abc123/TeamA-Retro?node-id=45-678
  /retro-summary https://figma.com/board/abc123/Retro --team TeamA
  /retro-summary https://figma.com/board/abc123/Retro --list
  /retro-summary https://figma.com/board/abc123/Retro --dry-run
```

### Step 1 — Load environment

Load environment variables:
```bash
source ~/.obsidian_env
echo "VAULT: $OBSIDIAN_VAULT_PATH"
echo "TEAMS: $OBSIDIAN_TEAMS_PATH"
```

Both `OBSIDIAN_VAULT_PATH` and `OBSIDIAN_TEAMS_PATH` must be set. If either is missing, stop and tell the user to add them to `~/.obsidian_env`.

Verify the teams path exists with `ls`. If it doesn't exist, stop and tell the user.

### Step 2 — Fetch board data & resolve team

Call `mcp__figma__get_figjam` with the extracted `fileKey` and `nodeId`.

If the nodeId is a page (`0:1`) or a top-level frame, the response will contain multiple `<section>` elements — one per retro session. Each section has a `name` attribute containing the retro date (e.g., "5 Nov 2025").

If the nodeId points to a specific section, the response will contain just that section's stickies.

**Resolve team name** (used for the output path):

1. If `--team` was provided, use that value directly.
2. Otherwise, infer from the FigJam board/file name in the URL path segment (e.g., `TeamA-Retro` → `TeamA`, `TeamB-Retrospective` → `TeamB`). The team name is the portion before the first hyphen or the word "Retro"/"Retrospective" (case-insensitive).
3. If the team cannot be inferred, scan `{OBSIDIAN_TEAMS_PATH}` for existing team directories and use `AskUserQuestion` to let the user pick which team this retro belongs to.

Verify the resolved team directory exists at `{OBSIDIAN_TEAMS_PATH}/{team_name}/`. If it doesn't exist, stop and tell the user.

### Step 3 — Section selection

**If `--list` flag is set:**
- Parse all `<section>` elements from the response
- Display each section's name (date) and node ID in a table:
  ```
  Retro sections found:
  | # | Date          | Node ID |
  |---|---------------|---------|
  | 1 | 5 Nov 2025    | 45:678  |
  | 2 | 19 Nov 2025   | 45:679  |
  | ...                         |
  ```
- Stop execution.

**If the URL points to a specific section node (not `0:1`):**
- Use that section directly.

**If the URL points to the full board (node `0:1` or no node-id):**
- List all available retro sections with their dates.
- Use `AskUserQuestion` to let the user pick which retro to summarize. Present the section dates as options (up to 4 most recent; include "Other" for older ones).
- After selection, re-fetch the specific section by calling `mcp__figma__get_figjam` with the selected section's node ID for cleaner data.

### Step 4 — Extract stickies from target section

Parse the section data to extract all sticky notes:

1. **Find column headers** — Locate `<text>` elements containing "Rose", "Thorn", and "Bud" (case-insensitive). Record each header's x-coordinate position.

2. **Calculate category boundaries** — Compute midpoints between adjacent headers:
   - Rose/Thorn boundary = (Rose_x + Thorn_x) / 2
   - Thorn/Bud boundary = (Thorn_x + Bud_x) / 2

3. **Extract stickies** — For each `<sticky>` element, extract:
   - `id` — the sticky's node ID
   - `x` — the sticky's x-coordinate position
   - `author` — the author attribute (contributor name)
   - `text` — the text content of the sticky

4. **Filter** — Remove stickies with empty or whitespace-only text content.

5. **Categorize** — Assign each sticky to a category based on its x-coordinate:
   - x < Rose/Thorn boundary → **Rose**
   - x < Thorn/Bud boundary → **Thorn**
   - x >= Thorn/Bud boundary → **Bud**

6. **Count votes** — Look for `<stamp>` elements and reaction `<instance>` elements near each sticky (within ~150px proximity based on position). Record the vote count per sticky.

### Step 5 — Present extracted data

Show the user a structured summary of what was extracted:

```
Retro: {section_name}

Rose ({count} stickies):
  - {Author}: {text} {vote_indicator}
  - ...

Thorn ({count} stickies):
  - {Author}: {text} {vote_indicator}
  - ...

Bud ({count} stickies):
  - {Author}: {text} {vote_indicator}
  - ...

Participants: {comma-separated list of unique authors}
```

Where `{vote_indicator}` is shown only for stickies with votes, formatted as `(N votes)`.

Ask the user for confirmation before proceeding to synthesis: "Proceed with AI synthesis?"

### Step 6 — AI synthesis

Use the Agent tool to spawn a `general-purpose` agent with the following prompt. Include all categorized stickies in the prompt — the agent runs in a forked context and has no access to the conversation history.

```
You are analyzing retrospective data from a team retro session dated {section_date}.

The retro uses Rose/Thorn/Bud format:
- Rose = strengths, wins, things going well
- Thorn = challenges, pain points, friction
- Bud = opportunities, ideas, growth areas

## Rose Stickies
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Thorn Stickies
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Bud Stickies
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

---

Analyze this retrospective data and produce the following sections. Write in a professional but warm team-oriented tone. Reference specific feedback where relevant. Prioritize items with more votes.

### Key Themes
Identify 3-5 recurring patterns that emerge across all three categories. Each theme should have a short title and 1-2 sentence explanation referencing specific stickies.

### Highlights
Synthesize the Rose stickies into a cohesive paragraph about what went well. Group related wins together. Call out any standout items.

### Challenges
Synthesize the Thorn stickies into a cohesive paragraph about pain points and friction. Group related issues together. Note severity based on vote counts and how many people raised similar concerns.

### Opportunities
Synthesize the Bud stickies into a cohesive paragraph about growth areas and ideas. Connect opportunities to the challenges where relevant.

### Action Items
Distill concrete, actionable next steps from the Bud stickies and highly-voted items across all categories. Format as a markdown checklist:
- [ ] Action item description

Focus on items that are specific and assignable. Aim for 3-7 action items.

Return ONLY the markdown content for these five sections (Key Themes through Action Items), with no preamble or explanation.
```

Present the agent's synthesis to the user for review. Ask: "Write this retro summary to the vault?" (Skip this confirmation in dry-run mode — just proceed to output.)

### Step 7 — Write to vault

**Parse the section date** from the section name (e.g., "5 Nov 2025") into `YYYY-MM-DD` format for the frontmatter and file name.

**Build the output file** using the template below.

**Collect participants** — deduplicate all sticky authors into a sorted list.

**Output path:** `{OBSIDIAN_TEAMS_PATH}/{team_name}/Retros/Retro - {YYYY-MM-DD}.md`

Create the `Retros/` directory if it doesn't exist:
```bash
mkdir -p "{OBSIDIAN_TEAMS_PATH}/{team_name}/Retros"
```

**Normal mode:** Use the Write tool to create the file. Confirm to the user:
```
Retro summary written to: {file_path}
```

**Dry-run mode:** Print the full file content to output instead of writing. Prefix with:
```
**DRY RUN** — would write to: {file_path}
```

### Output Template

```markdown
---
date: {YYYY-MM-DD}
team: {team_name}
type: retro
format: rose-thorn-bud
source: {figma_url}
generated_at: {ISO 8601 UTC timestamp}
participants: [{author1}, {author2}, ...]
---

# Retro — {display_date}

## Summary

{Key Themes section from agent synthesis}

## Action Items

{Action Items checklist from agent synthesis}

## Rose (Strengths & Positives)

{Highlights section from agent synthesis}

### Raw Feedback
{For each Rose sticky:}
- **{Author}**: {text}

## Thorn (Challenges & Negatives)

{Challenges section from agent synthesis}

### Raw Feedback
{For each Thorn sticky:}
- **{Author}**: {text}

## Bud (Opportunities & Growth)

{Opportunities section from agent synthesis}

### Raw Feedback
{For each Bud sticky:}
- **{Author}**: {text}
```

**Rules:**
- Sort raw feedback stickies within each category by vote count (highest first), then alphabetically by author.
- The `display_date` in the heading should be human-readable (e.g., "5 November 2025").
- The `participants` frontmatter list should be sorted alphabetically.
- The `generated_at` timestamp should be current UTC time in ISO 8601 format.
- The `source` should be the original FigJam URL provided by the user.
- **Idempotent:** Running again for the same retro date overwrites the previous file.
