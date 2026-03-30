# Template Section Autofill Prompt

You are analysing root cause issues from a Jira triage board. Each issue should have 5 template sections filled in, but these issues have 0 of 5 completed. Your job is to synthesise the available evidence into these sections.

For each issue below, produce the 5 template sections:

1. **Background Context** — What system, workflow, or integration area is affected. Why this matters to customers and the business. Include the scope (how many customers/properties affected if evident from linked tickets).

2. **Steps to reproduce** — The specific conditions or sequence that triggers the problem. Draw from linked ticket descriptions for concrete examples (PMS provider, configuration, workflow step). For feature requests, reframe as "Steps to encounter the gap" — the workflow where users hit the missing capability.

3. **Actual Results** — What currently happens. The failure mode, error, missing behavior, or workaround customers use. Be specific — reference real symptoms from linked tickets.

4. **Expected Results** — What should happen instead. Infer from customer complaints and the root cause analysis.

5. **Analysis** — Root cause explanation with:
   - The underlying deficiency or defect
   - **Resolution patterns**: How were the linked support tickets resolved? (manual intervention, config change, code fix, workaround, escalation). This is critical — capturing what was done to resolve individual instances helps prioritise and plan the systemic fix.
   - Whether this is a narrow edge case or systemic problem
   - Affected components or services (if identifiable)

## Output format

Respond with a JSON array. Each element:

```json
{
  "key": "PROJ-123",
  "sections": {
    "Background Context": {
      "content": "Section text...",
      "confidence": "high"
    },
    "Steps to reproduce": {
      "content": "Section text...",
      "confidence": "medium"
    },
    "Actual Results": {
      "content": "Section text...",
      "confidence": "high"
    },
    "Expected Results": {
      "content": "Section text...",
      "confidence": "medium"
    },
    "Analysis": {
      "content": "Section text...",
      "confidence": "high"
    }
  }
}
```

## Confidence levels

- **high** — Multiple corroborating sources (2+ linked tickets describing the same symptom/trigger, or clear evidence in the description + linked tickets)
- **medium** — Inferred from limited evidence (single linked ticket, or indirect inference from summaries without full descriptions)
- **low** — Sparse evidence, largely inferred from the issue title and classification alone

## Rules

- Keep each section 2-5 sentences
- Do not invent technical details not present in the evidence
- Reference specific conditions (PMS provider, workflow, configuration) only when they appear in the evidence
- For `feature_request` issues, adapt language naturally — "Steps to reproduce" becomes the workflow where users encounter the gap
- For `code_bug` issues, focus on the defect mechanism and trigger conditions
- In the Analysis section, always include resolution patterns from linked tickets — how were individual instances handled?
- If a linked ticket's resolution is not described, note "resolution not captured" rather than guessing

---

## Issues to analyse

