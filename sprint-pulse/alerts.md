# Alert Definitions

Reference file for sprint-pulse alert rules, thresholds, and output templates.

## Active Alerts

### 1. Stale In-Progress Items

**Trigger:** An issue in an active column (In Progress, In Review, etc.) with no activity for longer than the threshold.

**Activity sources checked:**
- Jira changelog (status changes, field updates)
- Jira comments
- GitLab MR updates
- GitLab MR notes/comments

**Threshold:** 1 business day with no activity from any source.

**Detection:** Script-based (`analyze.py` → `analyze_stale_items`)

**Output template:**
```
- **{key}** "{summary}" — {column} for {days_stale} days, no activity since {last_activity_date}.
  [Assignee] update status or flag blockers today.
```

---

### 2. To-Do Support Tickets

Scope: all tickets in the support project with the team's label (any column). Alerts only fire for tickets in the "To do" board column — determined by matching status IDs from the support board config.

Three sub-alerts:

#### 2a. New Tickets
**Trigger:** Support ticket created since the previous business day.
**Detection:** Script-based (`analyze.py` → `analyze_support_tickets`)
**Output template:**
```
- **{key}** ({priority}) "{summary}" — created {created_date}.
```

#### 2b. Unacknowledged
**Trigger:** Support ticket in "To do" column for more than 24 hours.
**Detection:** Script-based
**Output template:**
```
- **{key}** ({priority}) "{summary}" — open for {hours_open}h, unacknowledged.
  [Eng Lead] assign or acknowledge today.
```

#### 2c. SLA Risk
**Trigger:** Any non-closed support ticket approaching its target resolution time. Alerts fire 1 business day before the deadline (or immediately if already past).

**Target resolution times by priority:**
| Priority | Target Resolution |
|----------|-------------------|
| Highest  | 2 business days   |
| High     | 10 business days  |
| Medium   | Prioritized (no measurable SLA) |
| Low      | Discretionary (no measurable SLA) |

**Detection:** Script-based — applies to all non-closed tickets (any column), not just "To do".
**Output template:**
```
- **{key}** ({priority}) "{summary}" — {days_remaining}d remaining of {sla_days}d SLA ({days_elapsed}d elapsed).
  [Eng Lead] resolve or escalate immediately.
```

---

##### 2d. Highest Priority
**Trigger:** Support ticket with "Highest" priority that is not Closed or Awaiting Customer.
**Detection:** Script-based — applies to all non-excluded tickets (any column).
**Output template:**
```
- **{key}** "{summary}" — {status}, open for {days_open}d.
```
If 2 or more highest priority tickets are active:
```
> [PM], [Eng Lead] and [EM] Review the impact on the sprint commitment.
```

## 3. Outstanding Questions

**Trigger:** An unanswered question detected in Jira comments or GitLab MR discussions for an active sprint item.

**Detection:** Agent-based (Claude analyses comment threads in `/tmp/sprint_pulse_data.json`)

**Signals that indicate an outstanding question:**
- A comment ending with `?` that has no reply for >4 hours
- A comment explicitly asking for input/feedback with no response
- An MR comment thread where the last message is a question from the author
- A comment tagging someone (@mention) with a question and no follow-up

**Signals to ignore:**
- Rhetorical questions in descriptions
- Resolved MR discussion threads
- Questions followed by the same person answering themselves
- Automated bot comments

**Output template:**
```
- **{key}** "{summary}" — unanswered question in {source} ({time_ago}): "{question_excerpt}"
  [{relevant_role}] respond today to unblock progress.
```

---

## Adding a New Alert

### Script-detectable alert (deterministic threshold)

1. Add detection function to `analyze.py` (follow `analyze_stale_items` pattern)
2. Add the alert key to the output JSON in `analyze.py` → `main()`
3. Add the alert definition to this file with trigger, threshold, detection method, and output template
4. Add the output rendering to SKILL.md step 6 (generate output)

### Agent-detected alert (requires judgment)

1. Add the alert definition to this file with trigger signals and output template
2. Add analysis instructions to SKILL.md step 5 (agent analysis)
