---
name: retro-summary
description: Extracts and summarizes retrospectives from FigJam boards into Obsidian vault. Use when the user shares a FigJam URL, asks to summarize a retro, or wants to pull retrospective data into Obsidian.
disable-model-invocation: true
argument-hint: "<figjam-url> [--team <name>] [--dry-run] [--list]"
allowed-tools: Read Edit Write Glob Bash Agent WebFetch mcp__figma__get_figjam
---

# Retro Summary

Extract retrospective data from a FigJam board, categorize stickies, synthesize themes with AI, and write a structured summary to the Obsidian vault.

Supported retro formats:
- **Rose/Thorn/Bud** — 3-column layout (Rose = strengths, Thorn = challenges, Bud = opportunities)
- **Wind/Sun/Anchor/Reef** — 2×2 grid layout (Wind = helped us forward, Sun = made us feel good, Anchor = held us back, Reef = future risks)

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

Check that the required environment variables are set:
```bash
echo "VAULT: $OBSIDIAN_VAULT_PATH"
echo "TEAMS: $OBSIDIAN_TEAMS_PATH"
```

Both `OBSIDIAN_VAULT_PATH` and `OBSIDIAN_TEAMS_PATH` must be set. If either is missing, stop and tell the user to add them to `~/.zshrc`.

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
- Check the vault for existing retro files (`Retro - {YYYY-MM-DD}.md`) and mark already-processed sections with a checkmark in the listing.
- Use `AskUserQuestion` to let the user pick which retro to summarize. Present the section dates as options (up to 4 most recent; include "Other" for older ones).
- After selection, re-fetch the specific section by calling `mcp__figma__get_figjam` with the selected section's node ID for cleaner data.

### Step 4 — Detect template and extract stickies from target section

#### 4a — Detect retro template

Scan the section data for `<text>` elements to identify which template the board uses:

- If headers containing **"Rose"**, **"Thorn"**, and **"Bud"** are found → **rose-thorn-bud** template
- If headers containing **"Wind"**, **"Sun"**, **"Anchor"**, and **"Reef"** are found → **wind-sun-anchor-reef** template

Record the detected template — it controls how stickies are categorized (Step 4c) and the output format (Steps 5–7).

#### 4b — Extract stickies

For each `<sticky>` element, extract:
- `id` — the sticky's node ID
- `x` — the sticky's x-coordinate position
- `y` — the sticky's y-coordinate position
- `color` — the color attribute (e.g., `STICKY_RED`, `STICKY_GREEN`, `STICKY_YELLOW`, `CUSTOM`)
- `author` — the author attribute (contributor name)
- `text` — the text content of the sticky

**Filter** — Remove stickies that:
- Have empty or whitespace-only text content
- Have node ID prefixes significantly lower than the section's own node ID prefix (stale stickies from a previous retro). For example, if the section node is `281:2986`, stickies with prefixes like `191:xxx` or `205:xxx` are likely leftovers — exclude them.

#### 4c — Categorize stickies

**For rose-thorn-bud template** (3-column layout):

Record each header's x-coordinate. Use sticky color as the **primary** indicator, with nearest-header proximity as a fallback:
- `STICKY_RED` → **Rose**
- `STICKY_GREEN` → **Bud**
- `CUSTOM` (blue) or any other color → assign to the **nearest column header** by x-distance

  **Why nearest-header instead of midpoints:** Rose stickies consistently extend well past the Rose/Thorn midpoint because the Rose column is wide. Using nearest-header naturally handles this.

**For wind-sun-anchor-reef template** (2×2 grid layout):

Record each header's x **and** y coordinates. Assign each sticky to the nearest header using **2D Euclidean distance**: `sqrt((sx - hx)² + (sy - hy)²)`. The header with the smallest distance wins.

Category meanings:
- **Wind** — "Helped us forward" (process wins, helpful practices, decisions that accelerated the team)
- **Sun** — "Made us feel good" (culture, morale, celebrations, positive team moments)
- **Anchor** — "Held us back" (blockers, pain points, friction, things that slowed the team)
- **Reef** — "Future risks ahead" (risks on the horizon, things to watch out for, potential future blockers)

#### 4d — Count votes

Look for `<stamp>` and `<instance>` elements near each sticky (within ~200px proximity). **Deduplicate by type**: if the same stamp/instance type appears multiple times near a sticky (e.g., 3 "Thumbs up"), count it as **1 vote**. Each unique reaction type counts as 1 vote. Record the total unique vote count per sticky.

### Step 5 — Present extracted data

Show the user a structured summary of what was extracted. Use category names that match the detected template.

**Rose/Thorn/Bud:**
```
Retro: {section_name}

Rose ({count} stickies):
  - {Author}: {text} {vote_indicator}

Thorn ({count} stickies):
  - {Author}: {text} {vote_indicator}

Bud ({count} stickies):
  - {Author}: {text} {vote_indicator}

Participants: {comma-separated list of unique authors}
```

**Wind/Sun/Anchor/Reef:**
```
Retro: {section_name}

Wind — Helped us forward ({count} stickies):
  - {Author}: {text} {vote_indicator}

Sun — Made us feel good ({count} stickies):
  - {Author}: {text} {vote_indicator}

Anchor — Held us back ({count} stickies):
  - {Author}: {text} {vote_indicator}

Reef — Future risks ({count} stickies):
  - {Author}: {text} {vote_indicator}

Participants: {comma-separated list of unique authors}
```

Where `{vote_indicator}` is shown only for stickies with votes, formatted as `(N votes)`.

Ask the user: "Does the categorization look correct? If counts need adjusting, tell me. Otherwise I'll proceed with synthesis and write to vault."

If the user provides corrected counts or moves specific stickies, adjust accordingly and re-display the corrected summary before proceeding.

### Step 6 — AI synthesis

Read [PROMPTS.md](PROMPTS.md) for the full agent prompt matching the detected template. Use the Agent tool to spawn a `general-purpose` agent with that prompt, including all categorized stickies.

After synthesis completes, proceed directly to writing the file (the user already confirmed in Step 5). In `--dry-run` mode, print the output instead of writing.

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

### Output Templates

Read [TEMPLATES.md](TEMPLATES.md) for the full output template matching the detected format, including frontmatter structure and formatting rules.
