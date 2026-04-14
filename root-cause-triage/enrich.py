#!/usr/bin/env python3
"""Prepare enrichment batches and apply agent results to Obsidian Markdown files.

Two subcommands:
  prepare  — read JSON files, build batched prompts, write to /tmp/triage_enrich/
  apply    — read agent results from /tmp/triage_enrich/, update Markdown files
"""

import argparse
import glob
import json
import os
import re
import sys

from jira_client import load_env


ENV_KEYS = ["JIRA_BASE_URL", "TRIAGE_OUTPUT_PATH"]

COLLECT_DIR = "/tmp/triage_collect"
ENRICH_DIR = "/tmp/triage_enrich"
PROMPT_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "ENRICH_PROMPT.md")

BATCH_SIZE = 5  # issues per agent call — keeps context manageable
MAX_DESC_CHARS = 1500  # truncate very long linked issue descriptions


def parse_args():
    parser = argparse.ArgumentParser(description="Enrich Obsidian files with agent-quality summaries")
    sub = parser.add_subparsers(dest="command")

    prep = sub.add_parser("prepare", help="Build batched prompts from collected JSON")
    prep.add_argument("--issue", help="Prepare a single issue by key")
    prep.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Issues per batch (default: %d)" % BATCH_SIZE)
    prep.add_argument("--force", action="store_true", help="Re-prepare issues that already have enrichment results")

    apply_p = sub.add_parser("apply", help="Apply agent results to Markdown files")
    apply_p.add_argument("--issue", help="Apply results for a single issue")
    apply_p.add_argument("--dry-run", action="store_true", help="Show what would be updated without writing")

    return parser.parse_args()


def load_prompt_template():
    with open(PROMPT_TEMPLATE_PATH) as f:
        return f.read()


def build_issue_block(data):
    """Build the prompt block for a single root cause issue."""
    key = data["key"]
    summary = data["summary"]
    description = data.get("description", "") or ""

    lines = [
        "### %s — %s" % (key, summary),
        "",
        "**Description:**",
        description[:800] if description else "*(No description)*",
        "",
    ]

    linked = data.get("linked_issues", {})
    has_descriptions = False

    for group_label, issues in linked.items():
        for issue in issues:
            idesc = issue.get("description", "")
            if not idesc:
                continue
            has_descriptions = True
            truncated = idesc[:MAX_DESC_CHARS]
            if len(idesc) > MAX_DESC_CHARS:
                truncated += "..."
            lines.append("**Linked: %s — %s** (%s, %s)" % (
                issue["key"], issue.get("summary", ""), group_label, issue.get("status", ""),
            ))
            lines.append(truncated)
            lines.append("")

    if not has_descriptions:
        return None  # nothing to enrich

    return "\n".join(lines)


def cmd_prepare(args):
    """Build batched prompts and write to /tmp/triage_enrich/."""
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

    # Build issue blocks, skipping those without linked descriptions
    blocks = []
    for jf in json_files:
        with open(jf) as f:
            data = json.load(f)
        key = data["key"]

        # Skip if already enriched (unless --force)
        result_path = os.path.join(ENRICH_DIR, "result_%s.json" % key)
        if not args.force and os.path.exists(result_path):
            continue

        block = build_issue_block(data)
        if block:
            blocks.append({"key": key, "block": block})

    if not blocks:
        print("No issues need enrichment.")
        return

    # Create batches
    os.makedirs(ENRICH_DIR, mode=0o700, exist_ok=True)
    batch_size = args.batch_size
    batches = []
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i:i + batch_size]
        batch_num = len(batches) + 1
        keys = [b["key"] for b in batch]

        prompt = template + "\n".join(b["block"] for b in batch)
        prompt_path = os.path.join(ENRICH_DIR, "batch_%03d.txt" % batch_num)
        with open(prompt_path, "w") as f:
            f.write(prompt)

        meta = {"batch": batch_num, "keys": keys, "prompt_path": prompt_path}
        batches.append(meta)

    # Write batch index
    index_path = os.path.join(ENRICH_DIR, "batches.json")
    tmp_path = index_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(batches, f, indent=2)
    os.replace(tmp_path, index_path)

    print("--- Prepare Complete ---")
    print("Issues to enrich: %d" % len(blocks))
    print("Batches created: %d (batch size: %d)" % (len(batches), batch_size))
    print("Batch prompts: %s/batch_*.txt" % ENRICH_DIR)
    print("Batch index: %s" % index_path)
    print("\nReady for agent enrichment step.")


def cmd_apply(args):
    """Apply agent results back to Obsidian Markdown files."""
    env = load_env(ENV_KEYS)
    jira_base = env["JIRA_BASE_URL"]
    output_path = env["TRIAGE_OUTPUT_PATH"]
    issues_dir = os.path.join(output_path, "Issues")

    # Load all result files
    result_files = sorted(glob.glob(os.path.join(ENRICH_DIR, "result_*.json")))
    if not result_files:
        print("No enrichment results found in %s/" % ENRICH_DIR)
        print("Run the agent enrichment step first.")
        sys.exit(1)

    results = {}
    for rf in result_files:
        with open(rf) as f:
            data = json.load(f)
        results[data["key"]] = data

    if args.issue:
        if args.issue not in results:
            print("ERROR: No enrichment result for %s" % args.issue)
            sys.exit(1)
        results = {args.issue: results[args.issue]}

    # Find and update Markdown files
    updated = 0
    skipped = 0

    for key, enrichment in results.items():
        # Find the markdown file for this key
        pattern = os.path.join(issues_dir, "%s — *.md" % key)
        matches = glob.glob(pattern)
        if not matches:
            print("  SKIP %s — no Markdown file found" % key)
            skipped += 1
            continue

        md_path = matches[0]
        with open(md_path) as f:
            content = f.read()

        # Insert classification into frontmatter
        classification = enrichment.get("classification", "")
        if classification and classification != "unknown":
            if "classification:" in content:
                content = re.sub(r'^classification:.*$', 'classification: %s' % classification, content, flags=re.MULTILINE)
            else:
                # Insert before tags line in frontmatter
                content = re.sub(r'^(linked_issue_count:.*)\n(tags:)', r'\1\nclassification: %s\n\2' % classification, content, flags=re.MULTILINE)
            # Update tags to include classification
            cls_tag = classification.replace("_", "-")
            if cls_tag and ("tags:" in content) and (cls_tag not in content):
                content = re.sub(r'^tags: \[root-cause\]', 'tags: [root-cause, %s]' % cls_tag, content, flags=re.MULTILINE)

        # Insert or replace Root Cause Analysis section (before Description)
        analysis = enrichment.get("root_cause_analysis", "")
        if analysis:
            rca_section = "## Root Cause Analysis\n\n%s" % analysis

            if "## Root Cause Analysis" in content:
                # Replace existing
                content = re.sub(
                    r'## Root Cause Analysis\n\n.*?(?=\n## )',
                    rca_section + "\n\n",
                    content,
                    flags=re.DOTALL,
                )
            elif "## Description" in content:
                # Insert before Description
                content = content.replace(
                    "## Description",
                    rca_section + "\n\n## Description",
                )

        # Replace per-linked-issue summaries
        linked_summaries = enrichment.get("linked_summaries", {})
        for linked_key, summary in linked_summaries.items():
            # Replace extractive summary with agent summary
            # Pattern: "- **Summary:** <old text>" after the linked issue header
            old_pattern = r'(\[%s\].*?\n- \*\*Status:\*\* .*?\n)- \*\*Summary:\*\* .*' % re.escape(linked_key)
            new_text = r'\g<1>- **Summary:** %s' % summary.replace('\\', '\\\\')
            content = re.sub(old_pattern, new_text, content)

        if args.dry_run:
            print("  [would update] %s" % key)
        else:
            with open(md_path, "w") as f:
                f.write(content)
            updated += 1

    if args.dry_run:
        print("\nDry run: %d files would be updated, %d skipped" % (len(results) - skipped, skipped))
    else:
        print("\n--- Apply Complete ---")
        print("Updated: %d" % updated)
        print("Skipped: %d" % skipped)


def main():
    args = parse_args()
    if args.command == "prepare":
        cmd_prepare(args)
    elif args.command == "apply":
        cmd_apply(args)
    else:
        print("Usage: enrich.py {prepare|apply} [options]")
        print("  prepare  — build batched prompts from collected JSON")
        print("  apply    — apply agent results to Markdown files")
        sys.exit(1)


if __name__ == "__main__":
    main()
