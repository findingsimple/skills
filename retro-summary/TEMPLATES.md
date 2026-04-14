# Output Templates

Use the template that matches the detected retro format.

## Rose/Thorn/Bud template

```markdown
---
date: {YYYY-MM-DD}
team: "[[{team_name}]]"
type: retro
format: rose-thorn-bud
source: {figma_url}
generated_at: {ISO 8601 UTC timestamp}
participants: ["[[{author1}]]", "[[{author2}]]", ...]
---

# Retro — {display_date}

## Summary

{Key Themes section from agent synthesis}

## Possible Action Items

{Action Items checklist from agent synthesis}

## Rose (Strengths & Positives)

{Highlights section from agent synthesis}

### Raw Feedback
{For each Rose sticky:}
- **[[{Author}]]**: {text}

## Thorn (Challenges & Negatives)

{Challenges section from agent synthesis}

### Raw Feedback
{For each Thorn sticky:}
- **[[{Author}]]**: {text}

## Bud (Opportunities & Growth)

{Opportunities section from agent synthesis}

### Raw Feedback
{For each Bud sticky:}
- **[[{Author}]]**: {text}
```

## Wind/Sun/Anchor/Reef template

```markdown
---
date: {YYYY-MM-DD}
team: "[[{team_name}]]"
type: retro
format: wind-sun-anchor-reef
source: {figma_url}
generated_at: {ISO 8601 UTC timestamp}
participants: ["[[{author1}]]", "[[{author2}]]", ...]
---

# Retro — {display_date}

## Summary

{Key Themes section from agent synthesis}

## Possible Action Items

{Action Items checklist from agent synthesis}

## Momentum (Wind & Sun)

{Momentum section from agent synthesis}

### Wind — Helped us forward

#### Raw Feedback
{For each Wind sticky:}
- **[[{Author}]]**: {text}

### Sun — Made us feel good

#### Raw Feedback
{For each Sun sticky:}
- **[[{Author}]]**: {text}

## Friction & Blockers (Anchor)

{Friction & Blockers section from agent synthesis}

### Raw Feedback
{For each Anchor sticky:}
- **[[{Author}]]**: {text}

## Risks to Watch (Reef)

{Risks to Watch section from agent synthesis}

### Raw Feedback
{For each Reef sticky:}
- **[[{Author}]]**: {text}
```

## Rules

- Sort raw feedback stickies within each category by vote count (highest first), then alphabetically by author.
- Wrap each author name in Raw Feedback sections with Obsidian wiki links: `**[[{Author}]]**`.
- Wrap each participant name and the team name in YAML frontmatter with wiki links: `"[[{Name}]]"`. This connects retros to person pages and team hubs in the Obsidian graph.
- The `display_date` in the heading should be human-readable (e.g., "5 November 2025").
- The `participants` frontmatter list should be sorted alphabetically.
- The `generated_at` timestamp should be current UTC time in ISO 8601 format.
- The `source` should be the original FigJam URL provided by the user.
- **Idempotent:** Running again for the same retro date overwrites the previous file.