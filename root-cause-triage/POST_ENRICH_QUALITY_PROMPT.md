# Post-Enrichment Quality Assessment Prompt

You are performing a POST-ENRICHMENT quality assessment of root cause tickets. These tickets have been through an AI enrichment pipeline that synthesised evidence from linked issues and support tickets.

For each ticket below you will see:
1. **Raw description** — the original Jira content (often empty placeholder text)
2. **Enrichment data** — AI-generated classification and root cause analysis from linked issue evidence
3. **Autofill sections** — AI-generated template sections (Background Context, Steps to reproduce, Actual Results, Expected Results, Analysis) with confidence levels

Your job: assess whether the **combined evidence** (raw + enrichment + autofill) gives a PM enough to understand the root issue and scope a solution. The autofill content is flagged as agent-generated — weigh high-confidence sections more heavily than low-confidence ones.

Key differences from a raw assessment:
- A ticket with an empty raw description but high-confidence autofill sections across all 5 areas IS actionable
- A ticket with medium/low confidence autofill AND empty raw description may still need human review
- Resolution patterns in the Analysis autofill section are particularly valuable — if present, the ticket is more actionable

## Output format

Respond with a JSON array. Each element:

```json
{
  "key": "PROJ-123",
  "quality": "good" | "thin" | "vague",
  "quality_note": "one sentence or null",
  "duplicate_assessment": "confirmed" | "unlikely" | "n/a",
  "duplicate_note": "one sentence if assessment differs from structural flag, otherwise null",
  "recurrence_assessment": "likely" | "unlikely" | "n/a",
  "recurrence_note": "one sentence if this looks like a recurring failure mode, otherwise null",
  "recommended_action": "ready" | "more_info" | "duplicate" | "skip"
}
```

## Quality scale (post-enrichment)

- **good** — Combined evidence is sufficient for a PM to understand the root cause and scope a fix.
- **thin** — Some evidence gaps remain despite enrichment (e.g., low confidence on key sections, missing resolution patterns).
- **vague** — Enrichment did not materially improve understanding — still too ambiguous to act on.

## Rules

- Assess the **combined** evidence, not just the raw description
- High-confidence autofill sections with resolution patterns from linked tickets are strong signal
- Issues with many linked tickets AND high-confidence autofill are almost certainly actionable
- If only enrichment root cause is available (no autofill), assess whether that one-liner plus the title gives enough context
- Do not penalise for empty raw descriptions if enrichment/autofill compensates
- **Constraint:** `quality` and `recommended_action` must be consistent — `"good"` maps to `"ready"` (unless duplicate or skip applies), and `"thin"`/`"vague"` maps to `"more_info"`. Do not return `"good"` with `"more_info"` or `"thin"` with `"ready"`.

Return ONLY the JSON array with no preamble.

---

## Issues to assess

