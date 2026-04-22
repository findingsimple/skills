---
name: bank-statement-to-markdown
description: Converts St.George Bank PDF statements (Complete Freedom transaction accounts and Amplify Signature credit cards) into agent-friendly Markdown files with YAML frontmatter, an account summary, and a transaction table. Use when the user asks to convert, extract, or parse bank statement PDFs into Markdown.
disable-model-invocation: true
argument-hint: "[<pdf-path>...] [--dry-run] [--output-dir <path>]"
allowed-tools: Read Write Bash Glob
---

# Bank Statement to Markdown

Converts St.George Bank PDF statements into Markdown files optimised for later consumption by an agent. Each statement becomes a single `.md` file in the same directory as the source PDF, using the same base filename.

The skill operates locally — it reads the PDF, writes the `.md` beside it, and transmits nothing externally. Source PDFs are never deleted or modified. Extracted values (account numbers, BSB, card numbers, Qantas FF numbers) are copied verbatim into the output, so keep `$STATEMENTS_PATH` on a volume you'd be comfortable storing bank data on.

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `STATEMENTS_PATH` | Yes | Directory containing the source PDFs. Output `.md` files are written here alongside each PDF. |

## Source file naming (illustrative, not required)

Statement type is detected from the PDF header, not the filename — any `.pdf` in `$STATEMENTS_PATH` will be scanned. The following filename patterns are what St.George's export typically produces, and the `.md` output keeps the same base name:

- **Complete Freedom (transaction account):** `CompleteFreedom-{account-number}-{DDMmmYYYY}.pdf` — e.g. `CompleteFreedom-XXXXXXXXX-26Mar2026.pdf`
- **Amplify Signature (credit card):** `AmplifySignature-{card-number}-{DDMmmYYYY}.pdf` — e.g. `AmplifySignature-XXXXXXXXXXXXXXXX-04Jan2026.pdf`

Output: same base filename with `.md` extension, same directory.

## Instructions

### Step 0 — Parse arguments

Check `$ARGUMENTS` for flags and positional args:

- If `$ARGUMENTS` contains `--dry-run`, enable **dry-run mode**. In this mode the skill prints the generated Markdown to output but does not write any files. Dry-run prints the full rendered Markdown for every matched PDF — pass a specific PDF path when previewing, otherwise a folder with many statements will flood the conversation.
- If `$ARGUMENTS` contains `--output-dir <path>`, write the `.md` files to `<path>` instead of alongside the source PDFs. Use this when previewing output for review without committing it to `$STATEMENTS_PATH`. Mutually exclusive with `--dry-run` — if both are supplied, stop and ask which the user meant.
- Any remaining arguments (after removing `--dry-run` and `--output-dir <path>`) are treated as specific PDF paths to process. Paths may be absolute or relative to `$STATEMENTS_PATH`.
- If no PDF paths are supplied, the skill processes every PDF in `$STATEMENTS_PATH` that does not already have a matching `.md` file.

Examples:
- `/bank-statement-to-markdown` — process all unprocessed PDFs in `$STATEMENTS_PATH`
- `/bank-statement-to-markdown CompleteFreedom-XXXXXXXXX-26Mar2026.pdf` — process a single PDF
- `/bank-statement-to-markdown --dry-run` — preview all unprocessed PDFs without writing
- `/bank-statement-to-markdown --output-dir ~/review` — write to a review directory instead of `$STATEMENTS_PATH`

### Step 1 — Resolve paths

Check that `STATEMENTS_PATH` is set:
```bash
echo "$STATEMENTS_PATH"
```

If empty, stop and tell the user to add `export STATEMENTS_PATH=/path/to/statements` to `~/.zshrc`.

Verify the resolved path exists with `ls`. If it doesn't, stop and tell the user.

### Step 2 — Identify PDFs to process

- If specific PDF paths were supplied in Step 0, use those directly. Resolve relative paths against `$STATEMENTS_PATH`.
- Otherwise, use Glob to find `{STATEMENTS_PATH}/*.pdf` and skip any PDF whose sibling `.md` file already exists. Report the count to the user.

### Step 3 — Extract each PDF

For each PDF:

1. Use the Read tool on the PDF. St.George PDFs are text-extractable, so this produces structured data directly.
2. Identify the statement type from the PDF header. Exactly one of `COMPLETE FREEDOM` or `AMPLIFY SIGNATURE Statement` must be present. If neither matches — including scanned/image-only PDFs that yield no text, or statements from a different product (e.g. home loan, Westpac PDF dropped in by mistake) — skip the file, add it to an "unsupported" list, and continue with the next PDF. Do not attempt to guess a template.
3. Extract the Account Summary (opening balance, total credits/debits, closing balance, period, account number) and every transaction row.
4. Ignore pagination artefacts: `SUB TOTAL CARRIED FORWARD TO/FROM ...`, page headers/footers, payment slips, complaints blurb, PIN-safety boilerplate.

### Step 4 — Write Markdown

Use the template for the matching statement type (see below). Output path: same directory as the source PDF, same base filename with `.md` extension. If `--output-dir <path>` was specified, write to that directory instead (still using the PDF's base filename + `.md`).

If `--dry-run` was specified, print the generated Markdown to output and skip the Write step.

### Step 5 — Confirm completion

Report to the user:
- The list of written (or, under `--dry-run`, previewed) files.
- The list of PDFs skipped because the header didn't match a supported statement type (from Step 3.2), if any.
- Any PDFs that were identified as supported but failed to extract cleanly — report explicitly; do not silently skip.
- A one-line sensitivity reminder: the output files contain unmasked account/card/BSB/FF numbers — keep them on a trusted volume, and delete any previews written to `--output-dir` once reviewed.

## Template conventions

In the templates below:
- `{snake_case}` tokens are placeholders — replace them with the value extracted from the PDF (verbatim, no masking).
- `YYYY-MM-DD` / `DD/MM/YYYY` / `DD Mmm` are literal date format illustrations — render actual dates in that shape.
- `$X,XXX.XX` / `x,xxx.xx` in summary-table rows shows the expected formatting for currency and balance values — substitute the real numbers (with thousands separators, two decimal places) from the PDF.
- `{N}` / `{number}` indicate a numeric value to be extracted (no decoration beyond what the YAML expects).

Both templates use YAML frontmatter for machine-readable fields and a Markdown summary + transaction table for human/agent reading.

## Template — Complete Freedom

Transactions have `Debit`, `Credit`, and `Balance` columns. Balance may go negative (represented in the PDF as `23.44 -` or `23.44-` with a trailing dash — render as `-23.44`; preserve thousands separators, so `60,830.96 -` becomes `-60,830.96`). Merge multi-line transaction descriptions into a single cell.

```markdown
---
account_name: Complete Freedom (Transaction Account)
account_holder: "{account_holder}"
bsb: "{bsb}"
account_number: "{account_number}"
statement_number: {statement_number}
statement_period_start: "YYYY-MM-DD"
statement_period_end: "YYYY-MM-DD"
opening_balance: {number}
total_credits: {number}
total_debits: {number}
closing_balance: {number}
currency: AUD
---

# Complete Freedom Statement No. {statement_number} — {DD Mmm YYYY}

## Account Summary

| Field | Value |
|---|---|
| Account Name | Complete Freedom |
| Account Holder | {account_holder} |
| BSB | {bsb} |
| Account Number | {account_number} |
| Statement Period | DD/MM/YYYY — DD/MM/YYYY |
| Statement No. | {statement_number} |
| Opening Balance | $X,XXX.XX |
| Total Credits | $X,XXX.XX |
| Total Debits | $X,XXX.XX |
| Closing Balance | $X,XXX.XX |

## Transactions

| Date | Description | Debit | Credit | Balance |
|---|---|---:|---:|---:|
| DD Mmm | OPENING BALANCE | | | x,xxx.xx |
| DD Mmm | <merged description> | debit | | balance |
| DD Mmm | <merged description> | | credit | balance |
| ...
| DD Mmm | CLOSING BALANCE | | | x,xxx.xx |

## Interest Details

| Period | Credit Interest | Debit Interest |
|---|---:|---:|
| Year to Date | $0.00 | $0.00 |
| Previous Year | $0.00 | $0.00 |
```

Notes:
- Heading matches the PDF's "Interest Details" section. Newer statements use an "Interest & Withholding Tax Information" table with financial-year columns — in that case use the PDF's heading verbatim and reproduce the FY column structure.
- If the closing balance is negative, note `(overdrawn)` and add a callout: `> NOTE: Account closed overdrawn.`

## Template — Amplify Signature

Credit card statements use a single `Amount A$` column with `CR` suffix denoting credits. Split into `Debit (A$)` and `Credit (A$)` in the Markdown.

```markdown
---
account_name: Amplify Signature (Credit Card)
account_holder: "{account_holder}"
account_number: "{account_number}"
statement_period_start: "YYYY-MM-DD"
statement_period_end: "YYYY-MM-DD"
opening_balance: {number}
total_credits: {number}
total_debits: {number}
closing_balance: {number}
credit_limit: {number}
available_credit: {number}
payment_due_date: "YYYY-MM-DD"
minimum_payment_due: {number}
monthly_payment_balance: {number}
currency: AUD
---

# Amplify Signature Statement — {DD Mmm YYYY}

## Account Summary

| Field | Value |
|---|---|
| Account Name | Amplify Signature (Visa) |
| Account Holder | {account_holder} |
| Account Number | {account_number} |
| Statement Period | DD/MM/YYYY — DD/MM/YYYY |
| Opening Balance | $X,XXX.XX |
| Total New Credits | $X,XXX.XX |
| Total New Debits | $X,XXX.XX |
| Closing Balance | $X,XXX.XX |
| Credit Limit | $X,XXX.XX |
| Available Credit | $XXX.XX |
| Minimum Payment Due | $XXX.XX (by DD/MM/YYYY) |
| Monthly Payment Balance | $X,XXX.XX |

### Balance Categories

| Category | Interest Rate | Balance |
|---|---|---|
| Cash Advances | 21.99% | $0.00 |
| Purchases | 20.99% | $X,XXX.XX |
| **Total** | | **$X,XXX.XX** |

### Qantas Points Summary

| Item | Value |
|---|---|
| Qantas Frequent Flyer Number | {qff_number} |
| Points earned — Australian merchants | X,XXX |
| Points earned — Overseas merchants | XXX |
| Bonus points earned | 0 |
| Total points for transfer to Qantas | X,XXX |

## Transactions

| Date | Description | Debit (A$) | Credit (A$) |
|---|---|---:|---:|
| DD Mmm | GOOGLE WORKSPACE SYDNEY AU | 31.50 | |
| DD Mmm | PHONE/INTERNET TFR FROM 0000XXXXXXXXX | | 500.00 |
| ...
```

Notes:
- For overseas/foreign transactions, the PDF shows the foreign currency amount on the next line (e.g. `24.99 USD`). Fold that into the description in parentheses: `NETFLIX.COM NETFLIX.COM US (24.99 USD)`.
- If a foreign transaction has no currency line (some merchants bill in AUD via an offshore acquirer — e.g. `ARLO 408-638-3750 IE`), leave the description as-is. The trailing `FOREIGN TRANSACTION FEE` row is the signal that it was treated as foreign; do not fabricate a currency amount.
- The `FOREIGN TRANSACTION FEE` is a separate debit line — keep it as its own row.
- The Closing Balance row is implicit — do not add a final table row for it; the Account Summary already captures it.
- Merchant refunds come through as `Amount A$ CR` on the single-column layout — convert those to the `Credit (A$)` column.

## Accuracy checklist

- Every transaction line in the PDF must appear in the Markdown (exclude only pagination artefacts).
- Preserve transaction order as it appears in the PDF — rows are ordered by posting date, not transaction date, so dates may appear out-of-sequence (e.g. a `13 Jan` row between two `12 Jan` rows). This ordering matters for reconciliation — do not re-sort chronologically.
- Amounts must be exact. Never round.
- Dates should stay in the format shown on the statement (`DD Mmm`), without adding the year.
- Descriptions should be verbatim — compress multi-line text into one line but do not paraphrase.
- If the user asks you to convert a new month, only write that one file — don't re-generate existing ones unless asked.
