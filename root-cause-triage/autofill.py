#!/usr/bin/env python3
"""Auto-fill missing template sections for 0/5 issues using agent synthesis.

Two subcommands:
  prepare  — identify 0/5 issues, build batched prompts from collected + enrichment data
  apply    — read agent results, insert auto-filled sections into Obsidian Markdown files
"""

import argparse
import glob
import json
import os
import re
import sys

from jira_client import load_env, ensure_tmp_dir

# Import scoring logic from analyze.py
sys.path.insert(0, os.path.dirname(__file__))
from analyze import analyze_description


ENV_KEYS = ["TRIAGE_OUTPUT_PATH"]

COLLECT_DIR = "/tmp/triage_collect"
ENRICH_DIR = "/tmp/triage_enrich"
AUTOFILL_DIR = "/tmp/triage_autofill"
PROMPT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "AUTOFILL_PROMPT.md")

BATCH_SIZE = 10
MAX_DESC_CHARS = 1500


def parse_args():
    parser = argparse.ArgumentParser(description="Auto-fill template sections for 0/5 issues")
    sub = parser.add_subparsers(dest="command")

    prep = sub.add_parser("prepare", help="Build batched prompts for 0/5 issues")
    prep.add_argument("--issue", help="Prepare a single issue by key")
    prep.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Issues per batch (default: %d)" % BATCH_SIZE)
    prep.add_argument("--force", action="store_true", help="Re-prepare issues that already have autofill results")
    prep.add_argument("--max-score", type=int, default=0, help="Include issues scoring up to this value (default: 0)")

    apply_p = sub.add_parser("apply", help="Apply autofill results to Markdown files")
    apply_p.add_argument("--issue", help="Apply results for a single issue")
    apply_p.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing")

    return parser.parse_args()


def load_prompt_template():
    with open(PROMPT_TEMPLATE_PATH) as f:
        return f.read()


def load_enrichment(key):
    """Load enrichment result for an issue, if available."""
    path = os.path.join(ENRICH_DIR, "result_%s.json" % key)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def count_evidence(data, enrichment):
    """Count how many linked issues exist and how many have descriptions."""
    total_linked = 0
    with_desc = 0
    linked = data.get("linked_issues", {})
    if isinstance(linked, dict):
        for items in linked.values():
            if isinstance(items, list):
                for item in items:
                    total_linked += 1
                    if item.get("description", "").strip():
                        with_desc += 1
    linked_summaries = 0
    if enrichment:
        linked_summaries = len(enrichment.get("linked_summaries", {}))
    return total_linked, with_desc, linked_summaries


def build_issue_block(data, enrichment):
    """Build the prompt block for a single issue."""
    key = data["key"]
    summary = data["summary"]
    description = data.get("description", "") or ""
    classification = ""
    rca = ""
    linked_summaries = {}

    if enrichment:
        classification = enrichment.get("classification", "")
        rca = enrichment.get("root_cause_analysis", "")
        linked_summaries = enrichment.get("linked_summaries", {})

    total_linked, with_desc, _ = count_evidence(data, enrichment)

    lines = [
        "### %s — %s" % (key, summary),
        "",
        "**Classification:** %s" % (classification or "unknown"),
        "**Linked tickets:** %d total, %d with descriptions" % (total_linked, with_desc),
        "",
    ]

    # Include root cause analysis from enrichment
    if rca:
        lines.append("**Root Cause Analysis (from prior enrichment):**")
        lines.append(rca)
        lines.append("")

    # Include the raw description (may contain partial info mixed with template boilerplate)
    if description.strip():
        truncated = description[:800]
        if len(description) > 800:
            truncated += "..."
        lines.append("**Raw description:**")
        lines.append(truncated)
        lines.append("")

    # Include enrichment-quality linked summaries
    if linked_summaries:
        lines.append("**Linked issue summaries (from prior enrichment):**")
        for lkey, lsummary in linked_summaries.items():
            lines.append("- **%s:** %s" % (lkey, lsummary))
        lines.append("")

    # Include raw linked issue descriptions for issues not covered by enrichment summaries
    linked = data.get("linked_issues", {})
    if isinstance(linked, dict):
        raw_added = 0
        for group_label, items in linked.items():
            if not isinstance(items, list):
                continue
            for item in items:
                ikey = item.get("key", "")
                # Skip if already covered by enrichment summary
                if ikey in linked_summaries:
                    continue
                idesc = item.get("description", "")
                if not idesc:
                    continue
                if raw_added == 0:
                    lines.append("**Additional linked issue descriptions:**")
                truncated = idesc[:MAX_DESC_CHARS]
                if len(idesc) > MAX_DESC_CHARS:
                    truncated += "..."
                lines.append("- **%s — %s** (%s, %s)" % (
                    ikey, item.get("summary", ""), group_label, item.get("status", ""),
                ))
                lines.append("  %s" % truncated)
                raw_added += 1

        if raw_added > 0:
            lines.append("")

    # Include linked issue stubs (key + summary + status) for issues without descriptions
    stubs = []
    if isinstance(linked, dict):
        for group_label, items in linked.items():
            if not isinstance(items, list):
                continue
            for item in items:
                ikey = item.get("key", "")
                if ikey in linked_summaries:
                    continue
                if item.get("description", ""):
                    continue
                stubs.append("- %s — %s (%s, %s)" % (
                    ikey, item.get("summary", "")[:80], group_label, item.get("status", ""),
                ))
    if stubs:
        lines.append("**Linked issues (summary only, no description):**")
        lines.extend(stubs[:15])
        if len(stubs) > 15:
            lines.append("- ... and %d more" % (len(stubs) - 15))
        lines.append("")

    return "\n".join(lines)


def cmd_prepare(args):
    """Build batched prompts for 0/5 issues."""
    template = load_prompt_template()

    # Find JSON files to process
    if args.issue:
        json_path = os.path.join(COLLECT_DIR, "%s.json" % args.issue)
        if not os.path.exists(json_path):
            print("ERROR: No collected data for %s" % args.issue)
            sys.exit(1)
        json_files = [json_path]
    else:
        json_files = sorted(glob.glob(os.path.join(COLLECT_DIR, "*.json")))

    # Filter to issues scoring at or below max_score
    blocks = []
    skipped_score = 0
    skipped_existing = 0

    for jf in json_files:
        with open(jf) as f:
            data = json.load(f)
        key = data["key"]

        # Check template score
        _, missing = analyze_description(data.get("description", ""))
        filled = 5 - len(missing)
        if filled > args.max_score:
            skipped_score += 1
            continue

        # Skip if already has autofill result (unless --force)
        result_path = os.path.join(AUTOFILL_DIR, "result_%s.json" % key)
        if not args.force and os.path.exists(result_path):
            skipped_existing += 1
            continue

        enrichment = load_enrichment(key)
        block = build_issue_block(data, enrichment)
        total_linked, with_desc, linked_summaries = count_evidence(data, enrichment)

        blocks.append({
            "key": key,
            "block": block,
            "has_enrichment": enrichment is not None,
            "total_linked": total_linked,
            "with_desc": with_desc,
        })

    if not blocks:
        print("No issues need autofill.")
        if skipped_existing > 0:
            print("(%d already have results — use --force to redo)" % skipped_existing)
        return

    # Create batches
    ensure_tmp_dir(AUTOFILL_DIR)
    batch_size = args.batch_size
    batches = []
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]
        batch_num = len(batches) + 1
        keys = [b["key"] for b in batch]

        prompt = template + "\n".join(b["block"] for b in batch)
        prompt_path = os.path.join(AUTOFILL_DIR, "batch_%03d.txt" % batch_num)
        with open(prompt_path, "w") as f:
            f.write(prompt)

        meta = {"batch": batch_num, "keys": keys, "prompt_path": prompt_path}
        batches.append(meta)

    # Write batch index
    index_path = os.path.join(AUTOFILL_DIR, "batches.json")
    tmp_path = index_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(batches, f, indent=2)
    os.replace(tmp_path, index_path)

    # Summary
    with_enrichment = sum(1 for b in blocks if b["has_enrichment"])
    without_enrichment = len(blocks) - with_enrichment

    print("--- Autofill Prepare Complete ---")
    print("Issues to autofill: %d (scoring 0/%d)" % (len(blocks), 5))
    print("  With enrichment data: %d" % with_enrichment)
    print("  Without enrichment (raw data only): %d" % without_enrichment)
    print("Skipped (score > %d): %d" % (args.max_score, skipped_score))
    print("Skipped (existing result): %d" % skipped_existing)
    print("Batches created: %d (batch size: %d)" % (len(batches), batch_size))
    print("Batch prompts: %s/batch_*.txt" % AUTOFILL_DIR)
    print("Batch index: %s" % index_path)
    print("\nReady for agent autofill step.")


def cmd_apply(args):
    """Apply autofill results to Obsidian Markdown files."""
    env = load_env(ENV_KEYS)
    output_path = env["TRIAGE_OUTPUT_PATH"]
    issues_dir = os.path.join(output_path, "Issues")

    # Load all result files
    result_files = sorted(glob.glob(os.path.join(AUTOFILL_DIR, "result_*.json")))
    if not result_files:
        print("No autofill results found in %s/" % AUTOFILL_DIR)
        print("Run the agent autofill step first.")
        sys.exit(1)

    results = {}
    for rf in result_files:
        with open(rf) as f:
            data = json.load(f)
        results[data["key"]] = data

    if args.issue:
        if args.issue not in results:
            print("ERROR: No autofill result for %s" % args.issue)
            sys.exit(1)
        results = {args.issue: results[args.issue]}

    # Load evidence counts from collected data for the header
    evidence_counts = {}
    for key in results:
        json_path = os.path.join(COLLECT_DIR, "%s.json" % key)
        if os.path.exists(json_path):
            with open(json_path) as f:
                data = json.load(f)
            enrichment = load_enrichment(key)
            total, with_desc, _ = count_evidence(data, enrichment)
            evidence_counts[key] = (total, with_desc)

    updated = 0
    skipped = 0

    for key, autofill in results.items():
        # Find the markdown file
        pattern = os.path.join(issues_dir, "%s — *.md" % key)
        matches = glob.glob(pattern)
        if not matches:
            print("  SKIP %s — no Markdown file found" % key)
            skipped += 1
            continue

        md_path = matches[0]
        with open(md_path) as f:
            content = f.read()

        sections = autofill.get("sections", {})
        if not sections:
            print("  SKIP %s — no sections in result" % key)
            skipped += 1
            continue

        # Build the auto-filled section block
        total_linked, with_desc = evidence_counts.get(key, (0, 0))
        section_lines = [
            "## Auto-filled Template Sections",
            "",
            "> [!note] Agent-generated from %d linked tickets (%d with descriptions). Review before using." % (total_linked, with_desc),
            "",
        ]

        for section_name in ["Background Context", "Steps to reproduce", "Actual Results", "Expected Results", "Analysis"]:
            section_data = sections.get(section_name, {})
            if isinstance(section_data, str):
                # Handle case where agent returned plain string instead of {content, confidence}
                section_content = section_data
                confidence = "unknown"
            else:
                section_content = section_data.get("content", "")
                confidence = section_data.get("confidence", "unknown")

            section_lines.append("### %s" % section_name)
            section_lines.append("*Confidence: %s*" % confidence)
            section_lines.append("")
            section_lines.append(section_content if section_content else "*(insufficient evidence)*")
            section_lines.append("")

        autofill_block = "\n".join(section_lines)

        # Insert or replace the autofill section
        if "## Auto-filled Template Sections" in content:
            # Replace existing autofill section
            content = re.sub(
                r"## Auto-filled Template Sections\n\n.*?(?=\n## |\Z)",
                autofill_block + "\n",
                content,
                flags=re.DOTALL,
            )
        elif "## Description" in content:
            # Insert before Description
            content = content.replace(
                "## Description",
                autofill_block + "\n## Description",
            )
        elif "## Root Cause Analysis" in content:
            # Insert after Root Cause Analysis — find the next ## heading
            rca_match = re.search(r"(## Root Cause Analysis\n\n.*?)(\n## |\Z)", content, re.DOTALL)
            if rca_match:
                insert_pos = rca_match.end(1)
                content = content[:insert_pos] + "\n\n" + autofill_block + "\n" + content[insert_pos:]
        else:
            # Append at the end
            content = content.rstrip() + "\n\n" + autofill_block

        # Add autofill frontmatter field
        if "autofill:" in content:
            content = re.sub(r"^autofill:.*$", "autofill: agent-generated", content, flags=re.MULTILINE)
        else:
            # Insert before closing --- of frontmatter
            content = re.sub(
                r"^((?:linked_issue_count|classification|tags):.*)\n---",
                r"\1\nautofill: agent-generated\n---",
                content,
                flags=re.MULTILINE,
            )

        if args.dry_run:
            print("  [would update] %s" % key)
        else:
            with open(md_path, "w") as f:
                f.write(content)
            updated += 1

    if args.dry_run:
        print("\nDry run: %d files would be updated, %d skipped" % (len(results) - skipped, skipped))
    else:
        print("\n--- Autofill Apply Complete ---")
        print("Updated: %d" % updated)
        print("Skipped: %d" % skipped)


def main():
    args = parse_args()
    if args.command == "prepare":
        cmd_prepare(args)
    elif args.command == "apply":
        cmd_apply(args)
    else:
        print("Usage: autofill.py {prepare|apply} [options]")
        print("  prepare  — build batched prompts for 0/5 issues")
        print("  apply    — apply agent results to Markdown files")
        sys.exit(1)


if __name__ == "__main__":
    main()
