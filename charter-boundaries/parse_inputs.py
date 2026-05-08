#!/usr/bin/env python3
"""Parse charters.md (per-team H2 sections) and examples.md (flat misroute list).

Reads /tmp/charter-boundaries/setup.json and writes /tmp/charter-boundaries/inputs.json
keyed by canonical team name with `{charter_blurb, examples}`. Examples are attached
to the team that *should* have received the ticket (the `to_team`)."""

import json
import os
import re
import sys

import _libpath  # noqa: F401
from jira_client import atomic_write_json


CACHE_DIR = "/tmp/charter-boundaries"

# H2 heading: `## ` followed by optional emoji + whitespace + the team label.
# We capture everything after `## ` and normalise it post-match.
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+", re.MULTILINE)
_HR_RE = re.compile(r"^---\s*$", re.MULTILINE)

# Strip leading non-letter prefix (emoji, punctuation, whitespace) and an
# optional `Team ` prefix; drop a trailing ` (parenthetical full name)`.
_HEADING_LEAD_RE = re.compile(r"^[^A-Za-z]+", re.UNICODE)
_HEADING_TEAM_PREFIX_RE = re.compile(r"^Team\s+", re.IGNORECASE)
_HEADING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Example line: "Assigned to <X>, but should belong to <Y> ... <URL ending in KEY>".
# `from`/`to` capture letters/spaces/&; URL is any happyco.atlassian.net browse link.
_EXAMPLE_RE = re.compile(
    r"Assigned\s+to\s+([A-Za-z][A-Za-z &]*?)"  # from team
    r"\s*(?:,)?\s*but\s+should\s+belong\s+to\s+([A-Za-z][A-Za-z &]*?)"  # to team
    r"\s*(?:\([^)]*\))?\s*:?\s*"
    r"(https?://\S+/browse/([A-Z][A-Z0-9_]*-\d+))",
    re.IGNORECASE,
)


def _normalise_heading(raw):
    """Map a raw H2 heading like '♠️ Team ACE (Admin Configuration & Experience)'
    or '🚢 Team Delivery' or 'Optigo / Lending' to a lookup string we can match
    against alias_map (lowercased canonical or alias). Slash-suffixes are dropped
    so 'Optigo / Lending' resolves to 'Optigo'."""
    s = raw.strip()
    s = _HEADING_LEAD_RE.sub("", s)
    s = _HEADING_TEAM_PREFIX_RE.sub("", s)
    s = _HEADING_PAREN_RE.sub("", s)
    if "/" in s:
        s = s.split("/", 1)[0]
    return s.strip()


def _split_charter_sections(text):
    """Yield (heading_raw, body_text) pairs for each H2 section in `text`.
    Body runs from after the heading line up to the next H1, H2, `---`, or EOF.
    """
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return
    # End-of-section markers: any subsequent H1, H2, or `---` after the heading.
    breaks = sorted(
        [m.start() for m in _H1_RE.finditer(text)]
        + [m.start() for m in _H2_RE.finditer(text)]
        + [m.start() for m in _HR_RE.finditer(text)]
    )
    for m in matches:
        heading_raw = m.group(1)
        body_start = m.end()
        # First break strictly after body_start ends this section.
        next_break = next((b for b in breaks if b > m.start() and b > body_start), len(text))
        body = text[body_start:next_break].strip()
        yield heading_raw, body


def _parse_charters(text, alias_map):
    """Returns ({canonical: blurb}, [unmatched_headings])."""
    per_team = {}
    unmatched = []
    for heading_raw, body in _split_charter_sections(text):
        norm = _normalise_heading(heading_raw)
        canonical = alias_map.get(norm.lower())
        if not canonical:
            unmatched.append(heading_raw)
            continue
        # If a team's heading appears multiple times, concatenate the bodies.
        if canonical in per_team:
            per_team[canonical] = per_team[canonical] + "\n\n" + body
        else:
            per_team[canonical] = body
    return per_team, unmatched


def _parse_examples(text, alias_map):
    """Returns ([{from_team, to_team, ticket_key, url, raw}], [unmatched_lines])."""
    examples = []
    unmatched = []
    for line in text.splitlines():
        stripped = line.strip()
        m = _EXAMPLE_RE.search(line)
        if not m:
            # Heuristic: only flag lines that look like they were intended as examples.
            if stripped and ("should belong" in stripped.lower() or "rerouted" in stripped.lower()):
                unmatched.append(stripped)
            continue
        from_raw = m.group(1).strip()
        to_raw = m.group(2).strip()
        url = m.group(3).strip()
        ticket_key = m.group(4).strip()
        from_canonical = alias_map.get(from_raw.lower())
        to_canonical = alias_map.get(to_raw.lower())
        if not (from_canonical and to_canonical):
            unmatched.append(stripped)
            continue
        examples.append({
            "from_team": from_canonical,
            "to_team": to_canonical,
            "ticket_key": ticket_key,
            "url": url,
            "raw": stripped,
        })
    return examples, unmatched


def _attach_examples_to_targets(allowed_teams, examples):
    """Build {canonical: [examples where to_team == canonical]}."""
    by_target = {t: [] for t in allowed_teams}
    for ex in examples:
        if ex["to_team"] in by_target:
            by_target[ex["to_team"]].append(ex)
    return by_target


def main():
    setup_path = os.path.join(CACHE_DIR, "setup.json")
    if not os.path.exists(setup_path):
        print("ERROR: %s not found. Run setup.py first." % setup_path, file=sys.stderr)
        sys.exit(1)
    with open(setup_path, "r", encoding="utf-8") as f:
        setup = json.load(f)

    allowed_teams = setup["allowed_teams"]
    alias_map = setup["team_alias_map"]
    charters_path = setup["charters_path"]
    examples_path = setup["examples_path"]

    with open(charters_path, "r", encoding="utf-8") as f:
        charters_text = f.read()
    with open(examples_path, "r", encoding="utf-8") as f:
        examples_text = f.read()

    per_team_blurbs, unmatched_headings = _parse_charters(charters_text, alias_map)
    examples, unmatched_examples = _parse_examples(examples_text, alias_map)
    examples_by_target = _attach_examples_to_targets(allowed_teams, examples)

    teams_out = {}
    for canonical in allowed_teams:
        teams_out[canonical] = {
            "charter_blurb": per_team_blurbs.get(canonical, ""),
            "examples": examples_by_target.get(canonical, []),
        }

    print("=== CHARTERS ===")
    for canonical in allowed_teams:
        blurb = teams_out[canonical]["charter_blurb"]
        n_examples = len(teams_out[canonical]["examples"])
        size_indicator = "%d chars" % len(blurb) if blurb else "MISSING"
        print("  %-15s  charter=%s  examples_targeting=%d" % (canonical, size_indicator, n_examples))
    if unmatched_headings:
        print("\nUnmatched H2 headings (no canonical match):")
        for h in unmatched_headings:
            print("  - %r" % h)

    if unmatched_examples:
        print("\nUnmatched example lines (could not parse from→to+key):")
        for line in unmatched_examples:
            print("  - %s" % line)

    inputs_data = {
        "teams": teams_out,
        "all_examples": examples,
        "unmatched_charter_headings": unmatched_headings,
        "unmatched_example_lines": unmatched_examples,
    }
    atomic_write_json(os.path.join(CACHE_DIR, "inputs.json"), inputs_data)
    print("\nInputs saved to %s/inputs.json" % CACHE_DIR)


if __name__ == "__main__":
    main()
