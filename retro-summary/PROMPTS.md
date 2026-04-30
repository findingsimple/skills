# Synthesis Agent Prompts

Use the Agent tool to spawn a `general-purpose` agent with the prompt variant matching the detected template. Include all categorized stickies — the agent runs in a forked context and has no access to the conversation history.

## Rose/Thorn/Bud prompt

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

## One Shot/Hallucinations/Context Optimisation prompt

```
You are analyzing retrospective data from an AI-focused sprint retro dated {section_date}.

The retro uses a Rose/Thorn/Bud variant with AI-themed labels:
- One Shot = wins, things that worked well with AI-assisted work
- Hallucinations = challenges, friction, pain points (AI and otherwise)
- Context Optimisation = opportunities, ideas for future AI-assisted sprints

## One Shot Stickies — What went well
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Hallucinations Stickies — What were the challenges
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Context Optimisation Stickies — Opportunities
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

{If reflection_questions present, append one block per question:}
## Reflection — {question_text}
{For each sticky: "- **{Author}**: {text}"}

---

Analyze this retrospective data and produce the following sections. Write in a professional but warm team-oriented tone. Reference specific feedback where relevant. Prioritize items with more votes.

### Key Themes
Identify 3-5 recurring patterns that emerge across categories (and reflection responses, if present). Each theme should have a short title and 1-2 sentence explanation referencing specific stickies. Where possible, surface AI-specific themes (e.g., skill-building, local dev friction, multi-tasking limits, context/plan quality).

### One Shot — What went well
Synthesize the One Shot stickies into a cohesive paragraph about AI-assisted wins. Group related wins together. Call out standout items (skills, agents, triage bot, etc.).

### Hallucinations — Challenges
Synthesize the Hallucinations stickies into a cohesive paragraph about pain points. Group related issues together (e.g., local dev breakage, token limits, multi-tasking overload, AI oversight of detail). Note severity by vote count and how many people raised similar concerns.

### Context Optimisation — Opportunities
Synthesize the Context Optimisation stickies into a cohesive paragraph about growth areas. Connect opportunities to challenges where relevant (e.g., "fix worktrees" addresses the parallel-work pain).

{If reflection_questions present:}
### Reflections
For each reflection question, produce a short synthesis paragraph that captures the collective mood or view. Use the question text as a sub-heading (####).

### Action Items
Distill concrete, actionable next steps from Context Optimisation stickies, reflection responses, and highly-voted items across all categories. Format as a markdown checklist:
- [ ] Action item description

Focus on items that are specific and assignable. Aim for 3-7 action items.

Return ONLY the markdown content for these sections, with no preamble or explanation.
```

## Wind/Sun/Anchor/Reef prompt

```
You are analyzing retrospective data from a team retro session dated {section_date}.

The retro uses Wind/Sun/Anchor/Reef format:
- Wind = things that helped us move forward (practices, decisions, actions that accelerated the team)
- Sun = things that made us feel good (culture, morale, celebrations, positive team moments)
- Anchor = things that held us back (blockers, pain points, friction, things that slowed the team)
- Reef = future risks on the horizon (things to watch out for, potential future blockers)

## Wind Stickies — Helped us forward
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Sun Stickies — Made us feel good
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Anchor Stickies — Held us back
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

## Reef Stickies — Future risks
{For each sticky: "- **{Author}**: {text}" + " (N votes)" if voted}

---

Analyze this retrospective data and produce the following sections. Write in a professional but warm team-oriented tone. Reference specific feedback where relevant. Prioritize items with more votes.

### Key Themes
Identify 3-5 recurring patterns that emerge across all four categories. Each theme should have a short title and 1-2 sentence explanation referencing specific stickies.

### Momentum (Wind & Sun)
Synthesize the Wind and Sun stickies into a cohesive paragraph. Distinguish between things that helped the team move faster (Wind) and things that energized or motivated the team (Sun). Group related items together.

### Friction & Blockers (Anchor)
Synthesize the Anchor stickies into a cohesive paragraph about pain points and blockers. Group related issues together. Note severity based on vote counts and how many people raised similar concerns.

### Risks to Watch (Reef)
Synthesize the Reef stickies into a cohesive paragraph about upcoming risks and things the team should watch for. Connect to Anchor items where a current problem may escalate.

### Action Items
Distill concrete, actionable next steps from the Reef and Anchor stickies, plus highly-voted Wind/Sun items worth preserving or building on. Format as a markdown checklist:
- [ ] Action item description

Focus on items that are specific and assignable. Aim for 3-7 action items.

Return ONLY the markdown content for these five sections (Key Themes through Action Items), with no preamble or explanation.
```