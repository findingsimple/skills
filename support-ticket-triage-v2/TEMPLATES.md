# Resolution Summary Template

The sub-agent fills this template and returns it as the final output. Every cited Jira key, Notion page, Slack thread, and code location must be rendered as a clickable hyperlink (or `path/to/file.ext:line`). Tag every action step with its support tier:

- `[L2]` — fixable in UI, admin portal, or config record; no code or DB access needed.
- `[ENG]` — requires code change, direct DB intervention, infrastructure access, or a deploy.

For every resolution step that modifies system state, include the `⚠️ SAFEGUARDS` block defined at the bottom of this file.

---

## Template

```markdown
## 🎫 <KEY> — <Summary>
**Status:** <status> | **Priority:** <priority> | **Reporter:** <reporter>

### 🏷️ Classification
<🐛 Code Bug | 🚀 PFR | ⚙️ Config Issue>

### 🔍 What's Happening
<2–3 sentences. What the user sees vs what the system does.>

### 🎯 Expected Outcome
<What the user expects.>

### 🗂️ Product & System Context
- **Product area:** <e.g. Inspections>
- **Affected system:** <service, integration, or infrastructure component>
- **Framework / stack:** <e.g. Go + Twirp / Rails API / React>

### 🧠 Root Cause
<Frame: "<Component> does not <handle/validate/guard> X, causing Y when Z.">
<Code Bug: exact file, function, line.>
<Config Issue: exact table, column, expected value.>
<PFR: state that no implementation exists; describe what would need to be built.>
<Never leave blank or "unknown" — the investigation phase is mandatory.>

### 🔬 Investigation Trail
- **References:** <`references/path/to/file.md` — what it covers, or "none relevant">
- **Notion docs:** <[Title](url) — what it covers, or "none searched / none found">
- **Similar resolved tickets:** <[KEY](url) — how they were resolved, or "none found">
- **Code references:** <`path/to/file.ext:N` → `Name` — what it does, or "not investigated">

### ✅ Resolution Steps

<!-- Pick ONE block below that matches the classification. Delete the others. -->

<!-- === CODE BUG === -->
1. [ENG] Fix the defect in `<file>:<line>` → `<function>`
   > **Framework:** <Rails controller / Go Twirp handler / React component / worker>
   > **Layer:** <Frontend | Backend | Both>
   > **What to fix:** <specific logic change>
   > 📎 Reference: [<MR or doc>](<url>)

   ⚠️ SAFEGUARDS — see definition below.
2. [ENG] Add a regression test covering <the edge case>.
3. [ENG] Deploy and verify against the reporter's account.

<!-- === CONFIG ISSUE === -->
1. [L2 or ENG] Check the config record for this customer.
   > **Table:** `<table_name>`
   > **Column:** `<column_name>`
   > **Expected value:** `<value or format>`
   > **Via UI:** <Admin → Path → Setting>, if available
   > **Direct query (if UI unavailable):**
   > ```sql
   > SELECT id, <column_name>, updated_at
   > FROM <table_name>
   > WHERE organization_id = <org_id>;
   > ```

   ⚠️ SAFEGUARDS — see definition below.
2. [L2] Trigger a re-sync via <Admin → Path → Button>, if applicable.
3. [ENG] (Optional) Add a validation or admin alert to catch this misconfiguration earlier.

<!-- === PFR === -->
1. [ENG] Confirm with Product that this behaviour is desired.
   > **What would need to be built:** <brief description>
   > **Affected area:** <service/module>

   ⚠️ SAFEGUARDS — see definition below.
2. [L2] Inform the reporter that this is not currently supported; log as a feature request.
3. [L2] Link this ticket to the relevant PFR epic (or create one).

### 📋 Notes for Engineer
<Ambiguities, risks, edge cases, related follow-up tickets, suggested improvements.>

### 🔑 Resolution Owner
<[L2 alone] | [L2 + ENG] | [ENG only]>
> <One-sentence justification.>
```

---

## ⚠️ SAFEGUARDS block (canonical definition)

Inline the following under any action step that modifies system state. Fill every bullet — if a bullet is genuinely not applicable, write "N/A — <reason>" rather than deleting it.

```
┌─ ⚠️  SAFEGUARDS ────────────────────────────────────────────────┐
│ Before:                                                         │
│  • <Action to take first, e.g. enable dry_run on <ServiceClass>,│
│    pause the <ScheduledJob> cron entry, take a DB snapshot,     │
│    notify the customer, set feature flag off.>                  │
│                                                                 │
│ ⚡ Implications:                                                 │
│  • <What this change triggers downstream — queued work,         │
│    webhook emissions, org-wide vs per-property scope,           │
│    irreversible data mutations.>                                │
│                                                                 │
│ 🔄 Rollback:                                                    │
│  • <How to undo — revert the flag, restore from snapshot,       │
│    cancel queued jobs, run the inverse migration.>              │
└─────────────────────────────────────────────────────────────────┘
```

**Always check these safeguard categories before filling the block:**

- `dry_run` / `test_mode` / `preview_mode` flags on the affected import/sync jobs.
- Feature flags that must be set before or after a deploy.
- Scheduled jobs or background workers that must be paused during the change.
- Org-wide vs property-specific scope (changes that appear scoped to one record but affect many).
- Webhook or external-system emissions triggered by the change (`after_save`, event publish, audit log).
- Irreversible data mutations (data that cannot be recovered without a snapshot).
