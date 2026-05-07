#!/usr/bin/env python3
"""Merge agent results from analyze mode Steps A2a, A2b, A2c, A2e.

Reads agent output files from /tmp/triage_prompts/, merges with the base
analysis from /tmp/triage_analysis.json, and writes the enriched analysis.

Usage:
    python3 merge_results.py [--check]       # merge all available results
    python3 merge_results.py --check         # check which results are present (no merge)

Output:
    /tmp/triage_analysis_enriched.json       — merged analysis with agent assessments
    /tmp/triage_duplicates/clusters.json     — semantic duplicate clusters (from A2c)
    /tmp/triage_type_suggestions.json        — issue type SOP suggestions (from A2e)

Prints a summary of merge counts and any missing/failed batches.
"""

import argparse
import json
import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import ensure_tmp_dir


ANALYSIS_PATH = "/tmp/triage_analysis.json"
PROMPT_BASE = "/tmp/triage_prompts"
ENRICHED_PATH = "/tmp/triage_analysis_enriched.json"
DUPLICATES_DIR = "/tmp/triage_duplicates"
ENRICH_DIR = "/tmp/triage_enrich"
TYPE_SUGGESTIONS_PATH = "/tmp/triage_type_suggestions.json"

# Allowed Jira issue types per the SOP. Used to drop hallucinated suggestions.
SOP_ALLOWED_TYPES = {"Bug", "Documentation", "Feature Gap", "Task", "Request", "Process Gap"}
SOP_LINK_KEY = "PDE-13499"


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


def _load_manifest(manifest_path):
    """Read a batches.json manifest, normalising legacy bare-list format.

    Newer manifests are `{"batches": [...], "batch_count": N}`. Pre-refactor
    manifests were a bare list. Without this guard, reading a stale
    /tmp/triage_prompts/<type>/batches.json from an old build_prompts run
    would crash with `TypeError: list indices must be integers`. Re-running
    build_prompts.py overwrites the manifest, but a clear error here saves
    debugging time when the user hasn't.
    """
    with open(manifest_path) as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return {"batches": raw, "batch_count": len(raw)}
    if isinstance(raw, dict) and "batches" in raw:
        # Defensive: fill in batch_count if absent.
        raw.setdefault("batch_count", len(raw["batches"]))
        return raw
    raise ValueError(
        "Unrecognised manifest format at %s — re-run build_prompts.py to regenerate"
        % manifest_path
    )


def load_batch_results(prompt_type):
    """Load results from agent output files for a given prompt type.

    Returns (results_by_key, stats) where stats has counts for loaded/missing/failed.
    """
    manifest_path = os.path.join(PROMPT_BASE, prompt_type, "batches.json")
    if not os.path.exists(manifest_path):
        return {}, {"loaded": 0, "missing": 0, "failed": 0, "total": 0}

    manifest = _load_manifest(manifest_path)

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


def load_enrichments():
    """Load enrichment results (classification, root cause) from /tmp/triage_enrich/."""
    results = {}
    if not os.path.isdir(ENRICH_DIR):
        return results
    for filename in os.listdir(ENRICH_DIR):
        if filename.startswith("result_") and filename.endswith(".json"):
            filepath = os.path.join(ENRICH_DIR, filename)
            try:
                with open(filepath) as f:
                    data = json.load(f)
                key = data.get("key")
                if key:
                    results[key] = data
            except Exception:
                pass
    return results


def merge_enrichments(issues_by_key, enrichments):
    """Merge enrichment data (classification, root_cause_analysis) into issues."""
    merged = 0
    for key, enrich in enrichments.items():
        if key in issues_by_key:
            issues_by_key[key]["classification"] = enrich.get("classification")
            issues_by_key[key]["root_cause_analysis"] = enrich.get("root_cause_analysis")
            merged += 1
    return merged


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
    ensure_tmp_dir(DUPLICATES_DIR)

    if isinstance(dup_results, dict):
        # Single batch — the results file contains the clusters directly
        clusters = dup_results
    elif isinstance(dup_results, list):
        clusters = dup_results
    else:
        print("WARNING: Unexpected duplicate results format", file=sys.stderr)
        return 0

    output_path = os.path.join(DUPLICATES_DIR, "clusters.json")
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(clusters, f, indent=2)
    os.replace(tmp_path, output_path)

    dup_count = sum(1 for c in clusters if c.get("type") == "duplicate")
    rel_count = sum(1 for c in clusters if c.get("type") == "related")
    return dup_count, rel_count


def save_type_suggestions(issues_by_key, type_results):
    """Validate A2e type suggestions and write to /tmp/triage_type_suggestions.json.

    - Drops items where suggested_type is null/empty AND no sop_link_suggestion.
    - Drops suggested_type values not in SOP_ALLOWED_TYPES.
    - Drops sop_link_suggestion values that aren't the canonical PDE-13499 key.
    - Drops items whose key isn't in the analyzed set.
    """
    suggestions = []
    invalid_type = 0
    invalid_link = 0
    unknown_key = 0

    for key, result in type_results.items():
        if key not in issues_by_key:
            unknown_key += 1
            continue

        suggested = result.get("suggested_type")
        sop_link = result.get("sop_link_suggestion")

        # Normalise empty strings to None for cleaner downstream logic.
        if suggested in ("", "null", "None"):
            suggested = None
        if sop_link in ("", "null", "None"):
            sop_link = None

        if suggested and suggested not in SOP_ALLOWED_TYPES:
            invalid_type += 1
            suggested = None
        if sop_link and sop_link != SOP_LINK_KEY:
            invalid_link += 1
            sop_link = None

        if not suggested and not sop_link:
            continue  # No-op suggestion; drop from report.

        suggestions.append({
            "key": key,
            "current_type": result.get("current_type") or issues_by_key[key].get("issue_type"),
            "suggested_type": suggested,
            "confidence": result.get("confidence"),
            "rationale": result.get("rationale"),
            "sop_link_suggestion": sop_link,
        })

    tmp_path = TYPE_SUGGESTIONS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(suggestions, f, indent=2)
    os.replace(tmp_path, TYPE_SUGGESTIONS_PATH)

    return {
        "saved": len(suggestions),
        "invalid_type": invalid_type,
        "invalid_link": invalid_link,
        "unknown_key": unknown_key,
    }


def check_status():
    """Check which results are present without merging."""
    print("Agent result status:")
    print()

    for prompt_type in ["raw-quality", "post-enrich-quality", "duplicates", "type-sop"]:
        manifest_path = os.path.join(PROMPT_BASE, prompt_type, "batches.json")
        if not os.path.exists(manifest_path):
            print("  %s: no manifest (build_prompts.py not run)" % prompt_type)
            continue

        manifest = _load_manifest(manifest_path)

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

    # Merge enrichment data (classification, root_cause_analysis)
    print("\nEnrichment data:")
    enrichments = load_enrichments()
    if enrichments:
        merged = merge_enrichments(issues_by_key, enrichments)
        print("  %d enrichment results loaded, %d issues merged" % (len(enrichments), merged))
    else:
        print("  No enrichment results found")

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

    # Handle A2e — issue type SOP suggestions
    print("\nA2e (type-sop):")
    type_results, type_stats = load_batch_results("type-sop")
    if type_results:
        type_summary = save_type_suggestions(issues_by_key, type_results)
        print("  %d batches loaded, %d valid suggestions saved" % (
            type_stats["loaded"], type_summary["saved"]))
        if type_summary["invalid_type"]:
            print("  WARNING: %d suggestions dropped (type not in SOP)" % type_summary["invalid_type"])
        if type_summary["invalid_link"]:
            print("  WARNING: %d sop_link_suggestion values dropped (not %s)" % (
                type_summary["invalid_link"], SOP_LINK_KEY))
        if type_summary["unknown_key"]:
            print("  WARNING: %d suggestions dropped (key not in analyzed set)" % type_summary["unknown_key"])
        if type_stats["missing"]:
            print("  WARNING: %d batches missing" % type_stats["missing"])
        if type_stats["failed"]:
            print("  WARNING: %d batches failed to parse" % type_stats["failed"])
    else:
        print("  No results found")

    # Save enriched analysis
    tmp_path = ENRICHED_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(issues, f, indent=2)
    os.replace(tmp_path, ENRICHED_PATH)

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

    type_count = 0
    if os.path.exists(TYPE_SUGGESTIONS_PATH):
        try:
            with open(TYPE_SUGGESTIONS_PATH) as f:
                type_count = len(json.load(f))
        except (json.JSONDecodeError, OSError):
            type_count = 0

    print("\n" + "=" * 50)
    print("MERGE SUMMARY")
    print("=" * 50)
    print("Raw quality:      %d good, %d thin, %d vague" % (raw_good, raw_thin, raw_vague))
    print("Post-enrichment:  %d good, %d thin, %d vague" % (post_good, post_thin, post_vague))
    print("Upgrades:         %d issues vague/thin → good" % upgrades)
    print("Type SOP flags:   %d issues with suggested type change or PDE-13499 link" % type_count)
    print("\nSaved to %s" % ENRICHED_PATH)
    if type_count:
        print("Type suggestions saved to %s" % TYPE_SUGGESTIONS_PATH)


if __name__ == "__main__":
    main()
