#!/usr/bin/env python3
"""Build agent prompts for analyze mode Steps A2a, A2b, A2c.

Reads /tmp/triage_analysis.json (+ enrichment/autofill data for A2b/A2c),
builds batch prompt files, and writes a manifest for agent orchestration.

Usage:
    python3 build_prompts.py raw-quality [--batch-size 10]
    python3 build_prompts.py post-enrich-quality [--batch-size 10]
    python3 build_prompts.py duplicates
    python3 build_prompts.py all [--batch-size 10]

Output directories:
    /tmp/triage_prompts/raw-quality/       batch_N.txt + batches.json
    /tmp/triage_prompts/post-enrich-quality/ batch_N.txt + batches.json
    /tmp/triage_prompts/duplicates/        prompt.txt + batches.json
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone


ANALYSIS_PATH = "/tmp/triage_analysis.json"
ENRICH_DIR = "/tmp/triage_enrich"
AUTOFILL_DIR = "/tmp/triage_autofill"
HISTORY_PATH = os.path.expanduser("~/.claude/skills/root-cause-triage/triage_history.json")
PROMPT_BASE = "/tmp/triage_prompts"


def load_analysis():
    with open(ANALYSIS_PATH) as f:
        return json.load(f)


def load_enrichments():
    results = {}
    for path in glob.glob(os.path.join(ENRICH_DIR, "result_*.json")):
        key = os.path.basename(path).replace("result_", "").replace(".json", "")
        with open(path) as f:
            results[key] = json.load(f)
    return results


def load_autofills():
    results = {}
    for path in glob.glob(os.path.join(AUTOFILL_DIR, "result_*.json")):
        key = os.path.basename(path).replace("result_", "").replace(".json", "")
        with open(path) as f:
            results[key] = json.load(f)
    return results


def load_history():
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def build_history_section(history):
    """Build triage history section for raw quality prompt (last 90 days, cap 20)."""
    if not history:
        return ""

    now = datetime.now(timezone.utc)
    recent = []
    for entry in history:
        try:
            entry_date = datetime.fromisoformat(entry.get("date", ""))
            if (now - entry_date).days <= 90:
                recent.append(entry)
        except (ValueError, TypeError):
            continue

    if not recent:
        return ""

    recent = recent[-20:]  # cap to most recent 20

    # Group by action
    by_action = {}
    for entry in recent:
        action = entry.get("action", "unknown")
        by_action.setdefault(action, []).append(entry)

    lines = [
        "## Recent Triage History",
        "",
        "Use this as a rough calibration signal for consistency.",
        "",
    ]
    for action, entries in by_action.items():
        items = ", ".join(
            "%s — %s%s" % (
                e.get("key", "?"),
                e.get("summary", "")[:60],
                " (%s)" % e["quality_note"] if e.get("quality_note") else "",
            )
            for e in entries
        )
        lines.append("**%s (%d tickets):** %s" % (action, len(entries), items))

    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Raw quality prompt (Step A2a)
# ---------------------------------------------------------------------------

RAW_QUALITY_HEADER = """You are assessing the quality of root cause tickets on behalf of a product team. Your job is to determine whether each ticket contains enough information for a **product manager** to understand the root issue and design a solution — without needing to chase the submitting engineer for clarification.

{history_section}The bar is not technical completeness. The bar is: *could a PM read this and know what went wrong, why it matters, and what needs to be fixed?*

Some tickets may not contain much detail but from the title e.g. "No UI for bulk updating unit details" it is clear enough what the issue is e.g. product/functionality gap.

Tickets may use a template with sections like Background Context, Steps to Reproduce, Actual Results, Expected Results, and Analysis. But the template may be partially filled, absent entirely, or the key detail may come from subtask content appended to the description. Treat the full description as your source — don't penalise for template non-compliance if the substance is there.

Watch for placeholder/dummy text such as `<brief technical changes>`, `<file locations>`, `<release flag ticket & link>`, or similar angle-bracket or brace-delimited fragments — treat these as unfilled regardless of surrounding content.

A strong root cause statement identifies *what specific system behaviour is wrong*, *why it is wrong* (the underlying cause, not just the symptom), and *under what conditions it manifests*. A ticket that only describes symptoms without reaching this level of diagnosis should be flagged as `more_info`, regardless of how detailed the symptoms are.

For each ticket, assess:
1. Can a PM clearly understand **what the root issue is** — not just symptoms, but the underlying cause?
2. Is there enough context to **scope a solution** — i.e., what system/behaviour needs to change?
3. Are there **red flags** — contradictions, pure symptoms with no cause identified, or content that is clearly placeholder/boilerplate?
4. If flagged as a **text-similarity duplicate**, does the content support or contradict that conclusion?
5. If flagged as a **possible recurrence**, does this look like the same failure mode recurring, or a different issue that happened to match on keywords?
6. Is this just a product gap? Do we have lots of users/tickets calling for the functionality?

---

"""

RAW_QUALITY_FOOTER = """
**Quality scale:**
- `"good"` — A PM can understand the root cause and scope a fix without follow-up questions.
- `"thin"` — The root cause direction is identifiable but key details are missing (e.g., which system, what triggers it, or how severe).
- `"vague"` — Only symptoms are described, or the content is too ambiguous to identify a specific root cause.

**Recommended action criteria:**
- `"ready"` — Quality is good; sufficient for development.
- `"more_info"` — Quality is thin or vague; needs clarification before development.
- `"duplicate"` — Content confirms the duplicate flag from text similarity.
- `"skip"` — The ticket appears to already be in progress, has been reassigned, or is otherwise not appropriate for assessment at this time.

**Constraint:** `quality` and `recommended_action` must be consistent — `"good"` maps to `"ready"` (unless duplicate or skip applies), and `"thin"`/`"vague"` maps to `"more_info"`. Do not return `"good"` with `"more_info"` or `"thin"` with `"ready"`.

Return a JSON array — one object per ticket — with this structure:
[
  {{
    "key": "PROJ-1234",
    "quality": "good | thin | vague",
    "quality_note": "one sentence — what is missing or unclear for a PM, or null if quality is good",
    "duplicate_assessment": "confirmed | unlikely | n/a",
    "duplicate_note": "one sentence if assessment differs from structural flag, otherwise null",
    "recurrence_assessment": "likely | unlikely | n/a",
    "recurrence_note": "one sentence if this looks like a recurring failure mode, otherwise null",
    "recommended_action": "ready | more_info | duplicate | skip"
  }}
]

**IMPORTANT:** Write ONLY the JSON to {output_path} using the Bash tool. No preamble, no commentary, no markdown fences — just the raw JSON starting with `[` and ending with `]`. After writing, confirm the file was saved."""


def format_raw_issue(iss):
    """Format a single issue for the raw quality prompt."""
    lines = []
    lines.append("## %s — %s" % (iss["key"], iss["summary"]))
    lines.append("**Completeness score:** %d/%d" % (iss["filled_count"], iss["total_sections"]))
    lines.append("**Structural recommendation:** %s" % iss["recommendation"])
    if iss.get("linked_issue_count", 0) > 0:
        lines.append("**Linked issues:** %d" % iss["linked_issue_count"])
    if iss.get("linked_support_count", 0) > 0:
        lines.append("**Support tickets:** %d" % iss["linked_support_count"])
    if iss.get("duplicate_of"):
        lines.append("**Flagged as duplicate of:** %s (%.0f%% text similarity)" % (
            iss["duplicate_of"], iss["duplicate_score"] * 100))
    if iss.get("recurrence_of"):
        lines.append("**Possible recurrence of resolved ticket:** %s (%.0f%% similarity)" % (
            iss["recurrence_of"], iss["recurrence_score"] * 100))
    desc = iss.get("description") or "(empty)"
    lines.append("")
    lines.append("**Description:**")
    lines.append(desc)
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_raw_quality(issues, batch_size):
    out_dir = os.path.join(PROMPT_BASE, "raw-quality")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    history = load_history()
    history_section = build_history_section(history)

    batches = []
    for i in range(0, len(issues), batch_size):
        batch = issues[i:i + batch_size]
        batch_num = i // batch_size
        output_path = os.path.join(out_dir, "results_batch_%d.json" % batch_num)

        prompt = RAW_QUALITY_HEADER.format(history_section=history_section)
        for iss in batch:
            prompt += format_raw_issue(iss)
        prompt += RAW_QUALITY_FOOTER.format(output_path=output_path)

        batch_file = os.path.join(out_dir, "batch_%d.txt" % batch_num)
        with open(batch_file, "w") as f:
            f.write(prompt)

        batches.append({
            "batch_num": batch_num,
            "batch_file": batch_file,
            "output_file": output_path,
            "issue_count": len(batch),
            "keys": [iss["key"] for iss in batch],
        })

    manifest = {"type": "raw-quality", "batch_count": len(batches), "batches": batches}
    manifest_path = os.path.join(out_dir, "batches.json")
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_path, manifest_path)

    return manifest


# ---------------------------------------------------------------------------
# Post-enrichment quality prompt (Step A2b)
# ---------------------------------------------------------------------------

POST_ENRICH_HEADER = """You are performing a POST-ENRICHMENT quality assessment of root cause tickets. These tickets have been through an AI enrichment pipeline that synthesised evidence from linked issues and support tickets.

For each ticket below you will see:
1. **Raw description** — the original Jira content (often empty placeholder text)
2. **Enrichment data** — AI-generated classification and root cause analysis from linked issue evidence
3. **Autofill sections** — AI-generated template sections with confidence levels

Your job: assess whether the **combined evidence** (raw + enrichment + autofill) gives a PM enough to understand the root issue and scope a solution.

Key differences from a raw assessment:
- A ticket with an empty raw description but high-confidence autofill sections across all 5 areas IS actionable
- A ticket with medium/low confidence autofill AND empty raw description may still need human review
- Resolution patterns in the Analysis autofill section are particularly valuable

## Quality scale (post-enrichment)
- **good** — Combined evidence is sufficient for a PM to understand the root cause and scope a fix.
- **thin** — Some evidence gaps remain despite enrichment.
- **vague** — Enrichment did not materially improve understanding.

## Rules
- Assess the **combined** evidence, not just the raw description
- High-confidence autofill sections with resolution patterns from linked tickets are strong signal
- Issues with many linked tickets AND high-confidence autofill are almost certainly actionable
- Do not penalise for empty raw descriptions if enrichment/autofill compensates
- **Constraint:** `quality` and `recommended_action` must be consistent — `"good"` maps to `"ready"` (unless duplicate or skip applies), and `"thin"`/`"vague"` maps to `"more_info"`.

---

## Issues to assess

"""

POST_ENRICH_FOOTER = """
Return a JSON array — one object per ticket:
[
  {{
    "key": "PROJ-1234",
    "post_enrich_quality": "good | thin | vague",
    "post_enrich_note": "one sentence or null",
    "post_enrich_action": "ready | more_info | duplicate | skip"
  }}
]

**IMPORTANT:** Write ONLY the JSON to {output_path} using the Bash tool. No preamble, no commentary, no markdown fences — just the raw JSON starting with `[` and ending with `]`. After writing, confirm the file was saved."""


def format_post_enrich_issue(iss, enrichments, autofills):
    """Format a single issue for the post-enrichment quality prompt."""
    key = iss["key"]
    lines = []
    lines.append("## %s — %s" % (key, iss["summary"]))
    lines.append("**Completeness score:** %d/%d" % (iss["filled_count"], iss["total_sections"]))
    if iss.get("linked_issue_count", 0) > 0:
        lines.append("**Linked issues:** %d" % iss["linked_issue_count"])
    if iss.get("linked_support_count", 0) > 0:
        lines.append("**Support tickets:** %d" % iss["linked_support_count"])
    if iss.get("duplicate_of"):
        lines.append("**Flagged as duplicate of:** %s (%.0f%% text similarity)" % (
            iss["duplicate_of"], iss["duplicate_score"] * 100))

    desc = iss.get("description") or "(empty)"
    lines.append("")
    lines.append("**Raw Description:**")
    lines.append(desc)
    lines.append("")

    # Enrichment data
    enrich = enrichments.get(key)
    if enrich:
        classification = enrich.get("classification", "unknown")
        rca = (enrich.get("root_cause_analysis") or "")[:300]
        lines.append("**Classification:** %s" % classification)
        lines.append("**Root Cause Analysis:** %s" % rca)
        lines.append("")
    else:
        lines.append("**Enrichment:** (none available)")
        lines.append("")

    # Autofill data — sections is a dict: {"Section Name": {"content": "...", "confidence": "high"}}
    autofill = autofills.get(key)
    if autofill and isinstance(autofill.get("sections"), dict):
        lines.append("**Autofill Sections:**")
        for name, section_data in autofill["sections"].items():
            confidence = section_data.get("confidence", "unknown")
            content = (section_data.get("content") or "")[:300]
            lines.append("- **%s** (confidence: %s): %s" % (name, confidence, content))
        lines.append("")
    else:
        lines.append("**Autofill:** (none available)")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_post_enrich_quality(issues, batch_size):
    out_dir = os.path.join(PROMPT_BASE, "post-enrich-quality")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    enrichments = load_enrichments()
    autofills = load_autofills()

    batches = []
    for i in range(0, len(issues), batch_size):
        batch = issues[i:i + batch_size]
        batch_num = i // batch_size
        output_path = os.path.join(out_dir, "results_batch_%d.json" % batch_num)

        prompt = POST_ENRICH_HEADER
        for iss in batch:
            prompt += format_post_enrich_issue(iss, enrichments, autofills)
        prompt += POST_ENRICH_FOOTER.format(output_path=output_path)

        batch_file = os.path.join(out_dir, "batch_%d.txt" % batch_num)
        with open(batch_file, "w") as f:
            f.write(prompt)

        batches.append({
            "batch_num": batch_num,
            "batch_file": batch_file,
            "output_file": output_path,
            "issue_count": len(batch),
            "keys": [iss["key"] for iss in batch],
        })

    manifest = {"type": "post-enrich-quality", "batch_count": len(batches), "batches": batches}
    manifest_path = os.path.join(out_dir, "batches.json")
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_path, manifest_path)

    return manifest


# ---------------------------------------------------------------------------
# Duplicate detection prompt (Step A2c)
# ---------------------------------------------------------------------------

DUPLICATE_HEADER = """You are identifying **duplicate and overlapping** root cause tickets based on enriched evidence. These tickets have been through an AI enrichment pipeline that synthesised root cause analyses from linked support tickets.

Two tickets are duplicates if they describe **the same underlying deficiency** — even if they use different words, reference different customers, or were filed at different times. Common patterns:
- Same missing UI/interface described from different angles
- Same integration bug reported for different customers or PMS providers
- A specific bug ticket that is a subset of a broader feature gap ticket
- Tickets that would be resolved by the same code change

**Near-duplicates** are tickets that overlap significantly but have distinct scope — e.g., "no mapping tool for Entrata" vs "no mapping tool for MRI" are related but not duplicates (they'd need separate implementations). Flag these as "related" rather than "duplicate".

## Output format

Respond with a JSON array of clusters:
[
  {{
    "type": "duplicate",
    "primary": "PROJ-123",
    "duplicates": ["PROJ-456"],
    "rationale": "One sentence explaining why these are the same issue"
  }},
  {{
    "type": "related",
    "tickets": ["PROJ-111", "PROJ-222", "PROJ-333"],
    "theme": "Short theme name",
    "rationale": "One sentence explaining the overlap and why they're distinct"
  }}
]

## Rules
- A ticket should only appear as a duplicate in ONE cluster (pick the best primary)
- "related" clusters group tickets that share a theme but need separate solutions
- Don't flag tickets as related just because they're in the same domain — the overlap must be specific
- Focus on root cause similarity, not surface-level keyword matching

**IMPORTANT:** Write ONLY the JSON to {output_path} using the Bash tool. No preamble, no commentary, no markdown fences — just the raw JSON starting with `[` and ending with `]`. After writing, confirm the file was saved.

---

## Issues to compare

"""


def build_duplicates(issues):
    out_dir = os.path.join(PROMPT_BASE, "duplicates")
    os.makedirs(out_dir, mode=0o700, exist_ok=True)

    enrichments = load_enrichments()
    autofills = load_autofills()

    output_path = os.path.join(out_dir, "results.json")
    prompt = DUPLICATE_HEADER.format(output_path=output_path)

    for iss in issues:
        key = iss["key"]
        prompt += "### %s — %s\n" % (key, iss["summary"])

        enrich = enrichments.get(key)
        if enrich:
            classification = enrich.get("classification", "unknown")
            rca = (enrich.get("root_cause_analysis") or "")[:300]
            prompt += "**Classification:** %s\n" % classification
            prompt += "**Root Cause Analysis:** %s\n" % rca

        autofill = autofills.get(key)
        if autofill and isinstance(autofill.get("sections"), dict):
            analysis = autofill["sections"].get("Analysis", {})
            if analysis:
                content = (analysis.get("content") or "")[:250]
                prompt += "**Analysis:** %s\n" % content

        prompt += "\n"

    batch_file = os.path.join(out_dir, "prompt.txt")
    with open(batch_file, "w") as f:
        f.write(prompt)

    manifest = {
        "type": "duplicates",
        "batch_count": 1,
        "batches": [{
            "batch_num": 0,
            "batch_file": batch_file,
            "output_file": output_path,
            "issue_count": len(issues),
        }],
    }
    manifest_path = os.path.join(out_dir, "batches.json")
    tmp_path = manifest_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp_path, manifest_path)

    return manifest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build agent prompts for analyze mode")
    parser.add_argument("subcommand", choices=["raw-quality", "post-enrich-quality", "duplicates", "all"])
    parser.add_argument("--batch-size", type=int, default=10)
    args = parser.parse_args()

    if not os.path.exists(ANALYSIS_PATH):
        print("ERROR: %s not found. Run analyze.py first." % ANALYSIS_PATH)
        sys.exit(1)

    issues = load_analysis()
    print("Loaded %d issues from %s" % (len(issues), ANALYSIS_PATH))

    commands = [args.subcommand] if args.subcommand != "all" else [
        "raw-quality", "post-enrich-quality", "duplicates",
    ]

    for cmd in commands:
        if cmd == "raw-quality":
            manifest = build_raw_quality(issues, args.batch_size)
            print("raw-quality: %d batches written to %s" % (
                manifest["batch_count"], os.path.join(PROMPT_BASE, "raw-quality")))

        elif cmd == "post-enrich-quality":
            manifest = build_post_enrich_quality(issues, args.batch_size)
            print("post-enrich-quality: %d batches written to %s" % (
                manifest["batch_count"], os.path.join(PROMPT_BASE, "post-enrich-quality")))

        elif cmd == "duplicates":
            manifest = build_duplicates(issues)
            print("duplicates: 1 prompt written to %s" % os.path.join(PROMPT_BASE, "duplicates"))


if __name__ == "__main__":
    main()
