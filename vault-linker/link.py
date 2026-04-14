#!/usr/bin/env python3
"""Scan Obsidian vault files and add [[wiki links]] to known entities."""

import argparse
import os
import re
import sys


# ---------------------------------------------------------------------------
# Entity discovery
# ---------------------------------------------------------------------------

def build_vault_links(vault_base):
    """Walk the vault and build a lookup dict of linkable entities.

    Returns dict mapping lowercase lookup key -> wiki link string.
    Also returns a list of person names for ordered matching (longest first).
    """
    links = {}
    person_names = []

    for root, dirs, files in os.walk(vault_base):
        for f in files:
            if not f.endswith(".md") or f.startswith("_"):
                continue
            name = f[:-3]  # strip .md
            rel = os.path.relpath(os.path.join(root, f), vault_base)
            parts = rel.split(os.sep)

            # Person pages: Teams/{team}/{person}/{person}.md
            if (len(parts) == 4 and parts[0] == "Teams"
                    and parts[2] == name and parts[1] not in ("Logs",)):
                links[name.lower()] = "[[%s]]" % name
                person_names.append(name)

            # Also match emoji-prefixed person pages like "🦧 Me.md"
            # where the directory name differs (directory = "Me", file = "🦧 Me")
            if (len(parts) == 4 and parts[0] == "Teams"
                    and parts[2] != name and parts[1] not in ("Logs",)):
                # Map the directory name (plain) to the file link
                dir_name = parts[2]
                links[dir_name.lower()] = "[[%s\\|%s]]" % (name, dir_name)

            # Incident pages: Incidents/YYYY-MM-DD — ID — Title.md
            if len(parts) == 2 and parts[0] == "Incidents":
                # Try to extract INC-NN pattern
                inc_match = re.search(r"INC-\d+", name, re.IGNORECASE)
                if inc_match:
                    key = inc_match.group().upper()
                    links[key.lower()] = "[[%s\\|%s]]" % (name, key)

            # Triage issue pages: Root Cause Triage/Issues/KEY — Summary.md
            if ("Root Cause Triage" in rel and "Issues" in rel
                    and not f.startswith("_")):
                key_match = re.match(r"^([A-Z]+-\d+)", name)
                if key_match:
                    key = key_match.group()
                    links[key.lower()] = "[[%s\\|%s]]" % (name, key)

    # Sort person names longest first to avoid partial matches
    person_names.sort(key=len, reverse=True)
    return links, person_names


# ---------------------------------------------------------------------------
# Safe text replacement
# ---------------------------------------------------------------------------

def find_protected_ranges(content):
    """Find ranges that should not be modified: frontmatter, code blocks, existing links, URLs."""
    protected = []

    # YAML frontmatter
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            protected.append((0, end + 4))
        else:
            # Unclosed frontmatter — protect entire file to avoid YAML corruption
            protected.append((0, len(content)))

    # Fenced code blocks (``` or ~~~)
    for m in re.finditer(r"^(`{3,}|~{3,}).*?\n.*?^\1\s*$", content, re.MULTILINE | re.DOTALL):
        protected.append((m.start(), m.end()))

    # Inline code
    for m in re.finditer(r"`[^`]+`", content):
        protected.append((m.start(), m.end()))

    # Existing wiki links [[...]]
    for m in re.finditer(r"\[\[[^\]]+\]\]", content):
        protected.append((m.start(), m.end()))

    # Markdown links [text](url)
    for m in re.finditer(r"\[[^\]]*\]\([^)]*\)", content):
        protected.append((m.start(), m.end()))

    # Raw URLs
    for m in re.finditer(r"https?://\S+", content):
        protected.append((m.start(), m.end()))

    # Sort and merge overlapping ranges
    protected.sort()
    merged = []
    for start, end in protected:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def is_protected(pos, length, ranges):
    """Check if a position+length overlaps any protected range."""
    end = pos + length
    for r_start, r_end in ranges:
        if pos < r_end and end > r_start:
            return True
        if r_start > end:
            break
    return False


def replace_entity(content, pattern, replacement, protected_ranges):
    """Replace all non-protected occurrences of pattern with replacement.

    Returns (new_content, count).
    """
    result = []
    last_end = 0
    count = 0

    for m in pattern.finditer(content):
        if is_protected(m.start(), m.end() - m.start(), protected_ranges):
            continue
        result.append(content[last_end:m.start()])
        result.append(replacement)
        count += 1
        last_end = m.end()

    if count == 0:
        return content, 0
    result.append(content[last_end:])
    return "".join(result), count


def link_people_in_content(content, links, person_names, protected_ranges):
    """Replace person names with wiki links in bold patterns and table cells."""
    total = 0
    for name in person_names:
        wiki_link = links.get(name.lower())
        if not wiki_link:
            continue

        # Pattern 1: **Name** (bold) — common in retros, bonusly
        bold_pattern = re.compile(r"\*\*%s\*\*" % re.escape(name))
        bold_replacement = "**%s**" % wiki_link
        content, c1 = replace_entity(content, bold_pattern, bold_replacement, protected_ranges)
        total += c1

        # Pattern 2: Bare name in table cells: | ... Name ... |
        # Only match whole name (word boundaries)
        bare_pattern = re.compile(r"(?<=\| )%s(?= \|)" % re.escape(name))
        bare_replacement = wiki_link
        content, c2 = replace_entity(content, bare_pattern, bare_replacement, protected_ranges)
        total += c2

        # Recompute protected ranges after modifications to this name
        if c1 + c2 > 0:
            protected_ranges = find_protected_ranges(content)

    return content, total


def link_jira_keys_in_content(content, links, protected_ranges, self_keys=None):
    """Replace bare Jira keys with wiki links to matching vault pages.

    self_keys: set of lowercase keys to skip (prevents self-linking).
    """
    if self_keys is None:
        self_keys = set()
    total = 0
    # Match Jira keys: PREFIX-DIGITS not already in a link
    pattern = re.compile(r"\b([A-Z]+-\d+)\b")

    result = []
    last_end = 0
    for m in pattern.finditer(content):
        key = m.group(1)
        if key.lower() in self_keys:
            continue
        wiki_link = links.get(key.lower())
        if not wiki_link:
            continue
        if is_protected(m.start(), m.end() - m.start(), protected_ranges):
            continue
        result.append(content[last_end:m.start()])
        result.append(wiki_link)
        total += 1
        last_end = m.end()

    if total == 0:
        return content, 0
    result.append(content[last_end:])
    return "".join(result), total


# ---------------------------------------------------------------------------
# File processing
# ---------------------------------------------------------------------------

def collect_target_files(vault_base, teams_path, scope):
    """Collect files to process based on scope."""
    targets = []

    if scope in ("all", "retros"):
        # Team retro files
        for team_dir in _list_team_dirs(teams_path):
            retro_dir = os.path.join(team_dir, "Retros")
            if os.path.isdir(retro_dir):
                for f in os.listdir(retro_dir):
                    if f.endswith(".md"):
                        targets.append(os.path.join(retro_dir, f))

    if scope in ("all", "bonusly"):
        # Bonusly feedback files
        for team_dir in _list_team_dirs(teams_path):
            for person_dir in _list_subdirs(team_dir):
                fb_dir = os.path.join(person_dir, "Feedback")
                if os.path.isdir(fb_dir):
                    for f in os.listdir(fb_dir):
                        if f.startswith("Bonusly") and f.endswith(".md"):
                            targets.append(os.path.join(fb_dir, f))

    if scope in ("all", "incidents"):
        # Incident files
        inc_dir = os.path.join(vault_base, "Incidents")
        if os.path.isdir(inc_dir):
            for f in os.listdir(inc_dir):
                if f.endswith(".md") and not f.startswith("_"):
                    targets.append(os.path.join(inc_dir, f))

    if scope in ("all", "triage"):
        # Root cause triage issue files
        triage_dir = os.path.join(vault_base, "Root Cause Triage", "Issues")
        if os.path.isdir(triage_dir):
            for f in os.listdir(triage_dir):
                if f.endswith(".md") and not f.startswith("_"):
                    targets.append(os.path.join(triage_dir, f))

    if scope == "all":
        # My Teams.md
        my_teams = os.path.join(teams_path, "My Teams.md")
        if os.path.isfile(my_teams):
            targets.append(my_teams)

    return sorted(set(targets))


def _list_team_dirs(teams_path):
    """List team directories (ACE, COPS, etc.), excluding Logs."""
    if not os.path.isdir(teams_path):
        return []
    dirs = []
    for d in os.listdir(teams_path):
        full = os.path.join(teams_path, d)
        if os.path.isdir(full) and d not in ("Logs", "Me"):
            dirs.append(full)
    return dirs


def _list_subdirs(path):
    """List immediate subdirectories."""
    if not os.path.isdir(path):
        return []
    return [os.path.join(path, d) for d in os.listdir(path)
            if os.path.isdir(os.path.join(path, d))]


def process_file(filepath, links, person_names, dry_run):
    """Process a single file, adding wiki links. Returns (changed, change_count)."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError) as e:
        print("  WARNING: skipping %s: %s" % (os.path.basename(filepath), e),
              file=sys.stderr)
        return False, 0

    protected_ranges = find_protected_ranges(content)
    total_changes = 0

    # Extract self-keys from filename to prevent self-linking
    filename = os.path.basename(filepath)
    self_keys = set()
    for m in re.finditer(r"[A-Z]+-\d+", filename):
        self_keys.add(m.group().lower())

    # Link people
    content, c = link_people_in_content(content, links, person_names, protected_ranges)
    total_changes += c

    # Recompute protected ranges after people linking
    if c > 0:
        protected_ranges = find_protected_ranges(content)

    # Link Jira keys
    content, c = link_jira_keys_in_content(content, links, protected_ranges, self_keys)
    total_changes += c

    if total_changes == 0:
        return False, 0

    if not dry_run:
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, filepath)

    return True, total_changes


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

def generate_sprint_timeline(team_dir, team_name, dry_run):
    """Generate Sprints/_Timeline.md for a team."""
    sprints_dir = os.path.join(team_dir, "Sprints")
    if not os.path.isdir(sprints_dir):
        return None

    sprint_dirs = sorted(
        (d for d in os.listdir(sprints_dir)
         if os.path.isdir(os.path.join(sprints_dir, d))),
        key=lambda d: d,
    )

    if not sprint_dirs:
        return None

    lines = [
        "---",
        "type: index",
        "scope: sprints",
        "team: %s" % team_name,
        "generated: true",
        "---",
        "",
        "# %s Sprint Timeline" % team_name,
        "",
        "| Sprint | Summary | Metrics |",
        "|--------|---------|---------|",
    ]

    for sprint_dir_name in sprint_dirs:
        sprint_path = os.path.join(sprints_dir, sprint_dir_name)
        summary_file = None
        metrics_file = None
        for f in os.listdir(sprint_path):
            if not f.endswith(".md"):
                continue
            name = f[:-3]
            if name.endswith("- Metrics"):
                metrics_file = name
            elif "Pulse" not in name:
                summary_file = name

        summary_link = "[[%s\\|Summary]]" % summary_file if summary_file else "-"
        metrics_link = "[[%s\\|Metrics]]" % metrics_file if metrics_file else "-"
        lines.append("| %s | %s | %s |" % (sprint_dir_name, summary_link, metrics_link))

    lines.append("")
    content = "\n".join(lines)
    out_path = os.path.join(sprints_dir, "_Timeline.md")

    if not dry_run:
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, out_path)

    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Add wiki links to Obsidian vault files")
    p.add_argument("--vault-base", required=True, help="Path to vault base (e.g., HappyCo dir)")
    p.add_argument("--teams-path", required=True, help="Path to Teams directory")
    p.add_argument("--scope", default="all",
                   choices=["all", "retros", "bonusly", "incidents", "triage"],
                   help="Which files to process")
    p.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isdir(args.vault_base):
        print("ERROR: vault base not found: %s" % args.vault_base, file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(args.teams_path):
        print("ERROR: teams path not found: %s" % args.teams_path, file=sys.stderr)
        sys.exit(1)

    print("Discovering linkable entities...")
    links, person_names = build_vault_links(args.vault_base)
    print("  Found %d people, %d total linkable entities" % (len(person_names), len(links)))

    print("\nCollecting target files (scope: %s)..." % args.scope)
    targets = collect_target_files(args.vault_base, args.teams_path, args.scope)
    print("  Found %d target files" % len(targets))

    if args.dry_run:
        print("\n--- DRY RUN (no files will be modified) ---\n")

    changed_files = 0
    total_links_added = 0

    for filepath in targets:
        changed, count = process_file(filepath, links, person_names, args.dry_run)
        if changed:
            changed_files += 1
            total_links_added += count
            rel = os.path.relpath(filepath, args.vault_base)
            print("  %s: +%d links" % (rel, count))

    print("\n--- Summary ---")
    print("Files scanned: %d" % len(targets))
    print("Files modified: %d" % changed_files)
    print("Links added: %d" % total_links_added)

    # Generate index pages
    indexes_created = []

    if args.scope == "all":
        for team_dir in _list_team_dirs(args.teams_path):
            team_name = os.path.basename(team_dir)
            idx_path = generate_sprint_timeline(team_dir, team_name, args.dry_run)
            if idx_path:
                indexes_created.append(os.path.relpath(idx_path, args.vault_base))

    if indexes_created:
        action = "Would create" if args.dry_run else "Created"
        print("\n%s index pages:" % action)
        for p in indexes_created:
            print("  %s" % p)

    if args.dry_run:
        print("\nRe-run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
