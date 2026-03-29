# Linked Issue Enrichment Prompt

You are analysing root cause issues from a Jira triage board. Each root cause issue has linked support/bug tickets that provide evidence of the problem's real-world impact.

For each root cause issue below, produce:

1. **Per-linked-issue summaries** — For each linked issue that has a description, write a 2-4 sentence summary capturing:
   - The core problem or symptom the customer experienced
   - Specific conditions that triggered it (PMS provider, configuration, workflow)
   - What was required to resolve it (manual intervention, config change, code fix)
   - How it connects to the parent root cause

2. **Root cause analysis** — A 3-6 sentence synthesis across ALL linked issues that captures:
   - The underlying gap or deficiency this root cause represents
   - The pattern of impact across customers (common triggers, affected workflows)
   - The business consequence (manual support burden, data integrity, customer frustration)
   - Whether the linked issues suggest a narrow or systemic problem

3. **Classification** — Based on the description and linked issue evidence, classify the root cause into exactly one category:
   - `code_bug` — A defect in existing code (incorrect logic, missing edge case, regression)
   - `feature_request` — Missing functionality or a gap in product capability
   - `config_issue` — Misconfiguration, incorrect setup, or a configuration gap
   - `docs_gap` — Missing, outdated, or unclear documentation (runbooks, setup guides, API docs)
   - `process_gap` — Manual process failure or workflow gap (not documentation)
   - `unknown` — Insufficient evidence to classify

   Do NOT rely on the Jira issue type (Bug, Story, etc.) — classify based on the actual evidence in the descriptions.

Respond with a JSON array. Each element:

```json
{
  "key": "PROJ-123",
  "classification": "code_bug",
  "root_cause_analysis": "Synthesis paragraph...",
  "linked_summaries": {
    "PROJ-456": "Summary of linked issue...",
    "PROJ-789": "Summary of linked issue..."
  }
}
```

Rules:
- Only include linked issues that had descriptions provided — skip stubs
- If a linked issue description is just a template with no real content, note "Template only — no substantive description"
- Do not invent details not present in the descriptions
- Reference specific customers, properties, or PMS providers only when they illustrate the pattern (not as identifying info)
- Keep per-linked-issue summaries factual and concise (2-4 sentences)
- The root cause analysis should synthesise, not just list what the linked issues say

---

## Issues to analyse

