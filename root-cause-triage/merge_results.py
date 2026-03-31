#!/usr/bin/env python3
"""Merge agent results from analyze mode Steps A2a, A2b, A2c.

Reads agent output files from /tmp/triage_prompts/, merges with the base
analysis from /tmp/triage_analysis.json, and writes the enriched analysis.

Usage:
    python3 merge_results.py [--check]       # merge all available results
    python3 merge_results.py --check         # check which results are present (no merge)

Output:
    /tmp/triage_analysis_enriched.json       — merged analysis with agent assessments
    /tmp/triage_duplicates/clusters.json     — semantic duplicate clusters (from A2c)

Prints a summary of merge counts and any missing/failed batches.
"""

import argparse
import json
import os
import re
import sys


ANALYSIS_PATH = "/tmp/triage_analysis.json"
PROMPT_BASE = "/tmp/triage_prompts"
ENRICHED_PATH = "/tmp/triage_analysis_enriched.json"
DUPLICATES_DIR = "/tmp/triage_duplicates"


def extract_json(text):
    """Extract JSON from text that may contain preamble or markdown fences.

    Handles: plain JSON, ```json ... ```, and text preamble before JSON.
    """
    if not text:
        return None

    # Strip markdown fences
    text = re.sub(r"```(?:json)?\s*\n?", "", text).strip()
    text = re.sub(r"\n?```\s*$", "", text).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find first [ or { and last ] or }
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue

    return None


def load_batch_results(prompt_type):
    """Load results from agent output files for a given prompt type.

    Returns (results_by_key, stats) where stats has counts for loaded/missing/failed.
    """
    manifest_path = os.path.join(PROMPT_BASE, prompt_type, "batches.json")
    if not os.path.exists(manifest_path):
        return {}, {"loaded": 0, "missing": 0, "failed": 0, "total": 0}

    with open(manifest_path) as f:
        manifest = json.load(f)

    results = {}
    stats = {"loaded": 0, "missing": 0, "failed": 0, "total": manifest["batch_count"]}

    for batch in manifest["batches"]:
        output_file = batch["output_file"]

        if not os.path.exists(output_file):
            stats["missing"] += 1
            print("  MISSING: %s" % output_file, file=sys.stderr)
            continue

        with open(output_file) as f:
            raw = f.read()

        data = extract_json(raw)
        if data is None:
            stats["failed"] += 1
            print("  FAILED to parse: %s" % output_file, file=sys.stderr)
            continue

        # Handle both array and single-object responses
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            stats["failed"] += 1
            print("  UNEXPECTED format in: %s" % output_file, file=sys.stderr)
            continue

        stats["loaded"] += 1
        for item in data:
            key = item.get("key")
            if key:
                results[key] = item

    return results, stats


def merge_raw_quality(issues_by_key, raw_results):
    """Merge A2a raw quality results into issues."""
    merged = 0
    for key, result in raw_results.items():
        if key in issues_by_key:
            issues_by_key[key]["quality"] = result.get("quality")
            issues_by_key[key]["quality_note"] = result.get("quality_note")
            issues_by_key[key]["duplicate_assessment"] = result.get("duplicate_assessment")
            issues_by_key[key]["duplicate_note"] = result.get("duplicate_note")
            issues_by_key[key]["recurrence_assessment"] = result.get("recurrence_assessment")
            issues_by_key[key]["recurrence_note"] = result.get("recurrence_note")
            issues_by_key[key]["recommended_action"] = result.get("recommended_action")
            merged += 1
    return merged


def merge_post_enrich(issues_by_key, post_results):
    """Merge A2b post-enrichment quality results into issues."""
    merged = 0
    for key, result in post_results.items():
        if key in issues_by_key:
            issues_by_key[key]["post_enrich_quality"] = result.get("post_enrich_quality")
            issues_by_key[key]["post_enrich_note"] = result.get("post_enrich_note")
            issues_by_key[key]["post_enrich_action"] = result.get("post_enrich_action")
            merged += 1
    return merged


def save_duplicates(dup_results):
    """Save A2c duplicate clusters to /tmp/triage_duplicates/clusters.json."""
    os.makedirs(DUPLICATES_DIR, exist_ok=True)

    if isinstance(dup_results, dict):
        # Single batch — the results file contains the clusters directly
        clusters = dup_results
    elif isinstance(dup_results, list):
        clusters = dup_results
    else:
        print("WARNING: Unexpected duplicate results format", file=sys.stderr)
        return 0

    output_path = os.path.join(DUPLICATES_DIR, "clusters.json")
    with open(output_path, "w") as f:
        json.dump(clusters, f, indent=2)

    dup_count = sum(1 for c in clusters if c.get("type") == "duplicate")
    rel_count = sum(1 for c in clusters if c.get("type") == "related")
    return dup_count, rel_count


def check_status():
    """Check which results are present without merging."""
    print("Agent result status:")
    print()

    for prompt_type in ["raw-quality", "post-enrich-quality", "duplicates"]:
        manifest_path = os.path.join(PROMPT_BASE, prompt_type, "batches.json")
        if not os.path.exists(manifest_path):
            print("  %s: no manifest (build_prompts.py not run)" % prompt_type)
            continue

        with open(manifest_path) as f:
            manifest = json.load(f)

        present = 0
        missing = 0
        for batch in manifest["batches"]:
            if os.path.exists(batch["output_file"]):
                present += 1
            else:
                missing += 1

        status = "COMPLETE" if missing == 0 else "INCOMPLETE (%d missing)" % missing
        print("  %s: %d/%d batches — %s" % (
            prompt_type, present, manifest["batch_count"], status))

    print()


def main():
    parser = argparse.ArgumentParser(description="Merge agent results for analyze mode")
    parser.add_argument("--check", action="store_true", help="Check result status without merging")
    args = parser.parse_args()

    if args.check:
        check_status()
        return

    # Load base analysis
    if not os.path.exists(ANALYSIS_PATH):
        print("ERROR: %s not found. Run analyze.py first." % ANALYSIS_PATH)
        sys.exit(1)

    with open(ANALYSIS_PATH) as f:
        issues = json.load(f)

    issues_by_key = {iss["key"]: iss for iss in issues}
    print("Loaded %d issues from %s" % (len(issues), ANALYSIS_PATH))

    # Merge A2a — raw quality
    print("\nA2a (raw-quality):")
    raw_results, raw_stats = load_batch_results("raw-quality")
    if raw_results:
        merged = merge_raw_quality(issues_by_key, raw_results)
        print("  %d batches loaded, %d issues merged" % (raw_stats["loaded"], merged))
        if raw_stats["missing"]:
            print("  WARNING: %d batches missing" % raw_stats["missing"])
        if raw_stats["failed"]:
            print("  WARNING: %d batches failed to parse" % raw_stats["failed"])
    else:
        print("  No results found")

    # Merge A2b — post-enrichment quality
    print("\nA2b (post-enrich-quality):")
    post_results, post_stats = load_batch_results("post-enrich-quality")
    if post_results:
        merged = merge_post_enrich(issues_by_key, post_results)
        print("  %d batches loaded, %d issues merged" % (post_stats["loaded"], merged))
        if post_stats["missing"]:
            print("  WARNING: %d batches missing" % post_stats["missing"])
        if post_stats["failed"]:
            print("  WARNING: %d batches failed to parse" % post_stats["failed"])
    else:
        print("  No results found")

    # Handle A2c — duplicates
    print("\nA2c (duplicates):")
    dup_output = os.path.join(PROMPT_BASE, "duplicates", "results.json")
    if os.path.exists(dup_output):
        with open(dup_output) as f:
            raw = f.read()
        dup_data = extract_json(raw)
        if dup_data and isinstance(dup_data, list):
            dup_count, rel_count = save_duplicates(dup_data)
            print("  %d duplicate clusters, %d related clusters saved" % (dup_count, rel_count))
        else:
            print("  WARNING: Failed to parse duplicate results")
    else:
        print("  No results found")

    # Save enriched analysis
    with open(ENRICHED_PATH, "w") as f:
        json.dump(issues, f, indent=2)

    # Print summary
    raw_good = sum(1 for i in issues if i.get("quality") == "good")
    raw_thin = sum(1 for i in issues if i.get("quality") == "thin")
    raw_vague = sum(1 for i in issues if i.get("quality") == "vague")
    post_good = sum(1 for i in issues if i.get("post_enrich_quality") == "good")
    post_thin = sum(1 for i in issues if i.get("post_enrich_quality") == "thin")
    post_vague = sum(1 for i in issues if i.get("post_enrich_quality") == "vague")
    upgrades = sum(1 for i in issues
                   if i.get("quality") in ("thin", "vague")
                   and i.get("post_enrich_quality") == "good")

    print("\n" + "=" * 50)
    print("MERGE SUMMARY")
    print("=" * 50)
    print("Raw quality:      %d good, %d thin, %d vague" % (raw_good, raw_thin, raw_vague))
    print("Post-enrichment:  %d good, %d thin, %d vague" % (post_good, post_thin, post_vague))
    print("Upgrades:         %d issues vague/thin → good" % upgrades)
    print("\nSaved to %s" % ENRICHED_PATH)


if __name__ == "__main__":
    main()
