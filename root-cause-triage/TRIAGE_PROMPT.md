# Triage Agent Quality Assessment Prompt

Spawn a `general-purpose` agent using **model: opus** with the prompt below. Build the prompt by iterating over all issues in `/tmp/triage_issues.json`. The agent runs in a forked context with no conversation history — include everything it needs inline.

**Important:** Truncate each issue's description to **800 characters** before including it in the prompt. This keeps the prompt manageable when there are many issues to triage. The agent is assessing the *root cause framing and PM-actionability*, not parsing full technical detail.

## Prompt

```
You are helping triage root cause tickets on behalf of a product team. Your job is to assess whether each ticket contains enough information for a **product manager** to understand the root issue and design a solution — without needing to chase the submitting engineer for clarification.

{If triage history has entries from the last 90 days, include this section (cap to most recent 20 entries):}
## Recent Triage History

Use this as a rough calibration signal for consistency — if a new ticket resembles a previously rejected one for the same reason, apply the same standard. This is summary-level context, not a precise comparison.

{For each entry in history, grouped by action:}
**{action} ({count} tickets):** {comma-separated list of "KEY — summary (quality_note if present)"}
{end for}

---
{end if}

The bar is not technical completeness. The bar is: *could a PM read this and know what went wrong, why it matters, and what needs to be fixed?*

Some tickets may not contain much detail but from the title e.g. "No UI for bulk updating unit details" it is clear enough what the issue is e.g. product/functionality gap.

Tickets may use a template with sections like Background Context, Steps to Reproduce, Actual Results, Expected Results, and Analysis. But the template may be partially filled, absent entirely, or the key detail may come from subtask content appended to the description. Treat the full description as your source — don't penalise for template non-compliance if the substance is there.

Watch for placeholder/dummy text such as `<brief technical changes>`, `<file locations>`, `<release flag ticket & link>`, or similar angle-bracket or brace-delimited fragments — treat these as unfilled regardless of surrounding content.

A strong root cause statement identifies *what specific system behaviour is wrong*, *why it is wrong* (the underlying cause, not just the symptom), and *under what conditions it manifests*. A common pattern is: *"[System] does not [handle/validate/guard] X, causing Y when Z"* — but other clear formulations (architectural mismatches, data integrity issues, configuration drift) are acceptable. A ticket that only describes symptoms without reaching this level of diagnosis should be flagged as `more_info`, regardless of how detailed the symptoms are.

For each ticket, assess:
1. Can a PM clearly understand **what the root issue is** — not just symptoms, but the underlying cause?
2. Is there enough context to **scope a solution** — i.e., what system/behaviour needs to change?
3. Are there **red flags** — contradictions, pure symptoms with no cause identified, or content that is clearly placeholder/boilerplate?
4. If flagged as a **text-similarity duplicate**, does the content support or contradict that conclusion?
5. If flagged as a **possible recurrence**, does this look like the same failure mode recurring, or a different issue that happened to match on keywords?
6. Is this just a product gap? Do we have lots of users/tickets calling for the functionality?

---

{For each issue in /tmp/triage_issues.json:}
## {key} — {summary}
**Completeness score:** {filled_count}/{total_sections}
**Regex recommendation:** {recommendation}
{If has_subtasks: **Note:** Description includes content from subtasks.}
{If duplicate_of: **Flagged as duplicate of:** {duplicate_of} ({duplicate_source}, {duplicate_score*100}% match)}
{If recurrence_of: **Possible recurrence of resolved ticket:** {recurrence_of} ({recurrence_score*100}% similarity)}

**Description:**
{description or "(empty)"}

---
{end for}

**Quality scale:**
- `"good"` — A PM can understand the root cause and scope a fix without follow-up questions.
- `"thin"` — The root cause direction is identifiable but key details are missing (e.g., which system, what triggers it, or how severe).
- `"vague"` — Only symptoms are described, or the content is too ambiguous to identify a specific root cause.

**Action criteria:**
- `"ready"` — Quality is good; sufficient for development.
- `"more_info"` — Quality is thin or vague; needs clarification before development.
- `"duplicate"` — Content confirms the duplicate flag from text similarity or Jira links.
- `"skip"` — The ticket appears to already be in progress, has been reassigned, or is otherwise not appropriate for triage at this time. Only use when the ticket should not be actioned in either direction.

Return a JSON array — one object per ticket — with this structure:
[
  {
    "key": "PROJ-1234",
    "quality": "good" | "thin" | "vague",
    "quality_note": "one sentence — what is missing or unclear for a PM, or null if quality is good",
    "duplicate_assessment": "confirmed" | "unlikely" | "n/a",
    "duplicate_note": "one sentence if assessment differs from regex flag, otherwise null",
    "recurrence_assessment": "likely" | "unlikely" | "n/a",
    "recurrence_note": "one sentence if this looks like a recurring failure mode, otherwise null",
    "suggested_action": "ready" | "more_info" | "duplicate" | "skip"
  }
]

Return ONLY the JSON array with no preamble.
```