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


def augment_jira_links_in_content(content, links, self_keys=None):
    """Append wiki links to existing [KEY](url) markdown links when a vault page exists.

    Transforms: [PDE-1234](https://...) → [PDE-1234](https://...) ([[page\\|PDE-1234]])
    Skips keys already followed by a wiki link.
    self_keys: set of lowercase keys to skip (prevents self-linking).
    """
    if self_keys is None:
        self_keys = set()
    total = 0

    # Match [KEY](url) NOT already followed by ([[...]])
    # The negative lookahead prevents double-augmenting
    pattern = re.compile(
        r"\[([A-Z]+-\d+)\]\(https?://[^)]+\)(?!\s*\(\[\[)"
    )

    result = []
    last_end = 0
    for m in pattern.finditer(content):
        key = m.group(1)
        if key.lower() in self_keys:
            continue
        wiki_link = links.get(key.lower())
        if not wiki_link:
            continue
        result.append(content[last_end:m.end()])
        result.append(" (%s)" % wiki_link)
        total += 1
        last_end = m.end()

    if total == 0:
        return content, 0
    result.append(content[last_end:])
    return "".join(result), total


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

    # Augment existing [KEY](url) markdown links with wiki links
    content, c = augment_jira_links_in_content(content, links, self_keys)
    total_changes += c

    # Recompute protected ranges after augmentation
    if c > 0:
        protected_ranges = find_protected_ranges(content)

    # Link bare Jira keys
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


def generate_team_hub(team_dir, team_name, teams_path, dry_run):
    """Generate a team hub page linking to members, sprint timeline, and retros."""
    # Discover team members (subdirs with a .md profile inside)
    members = []
    for d in sorted(os.listdir(team_dir)):
        full = os.path.join(team_dir, d)
        if not os.path.isdir(full) or d in ("Retros", "Sprints"):
            continue
        profile = os.path.join(full, "%s.md" % d)
        if os.path.isfile(profile):
            members.append(d)

    # Discover retros
    retros = []
    retro_dir = os.path.join(team_dir, "Retros")
    if os.path.isdir(retro_dir):
        for f in sorted(os.listdir(retro_dir), reverse=True):
            if f.endswith(".md") and not f.startswith("_"):
                retros.append(f[:-3])

    # Check for sprint timeline
    timeline_path = os.path.join(team_dir, "Sprints", "_Timeline.md")
    has_timeline = os.path.isfile(timeline_path)

    lines = [
        "---",
        "type: team-hub",
        "team: %s" % team_name,
        "generated: true",
        "---",
        "",
        "# %s" % team_name,
        "",
        "## Team Members",
        "",
    ]

    for name in members:
        lines.append("- [[%s]]" % name)

    lines.append("")

    if has_timeline:
        lines.append("## Sprints")
        lines.append("")
        lines.append("See [[_Timeline\\|Sprint Timeline]]")
        lines.append("")

    if retros:
        lines.append("## Retros")
        lines.append("")
        for name in retros:
            # Extract date from "Retro - YYYY-MM-DD"
            date = name.replace("Retro - ", "") if name.startswith("Retro - ") else name
            lines.append("- [[%s\\|%s]]" % (name, date))
        lines.append("")

    content = "\n".join(lines)
    out_path = os.path.join(team_dir, "%s.md" % team_name)

    if not dry_run:
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, out_path)

    return out_path


def add_person_backlink(filepath, person_name, dry_run):
    """Add a [[Person]] wiki link to a file that lives in that person's directory.

    For files with YAML frontmatter: adds a 'person' field with wiki link.
    For files without frontmatter: prepends a link line at the top.
    Returns True if the file was modified.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return False

    # Check for YAML frontmatter
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            frontmatter = content[4:end]
            # Skip if person field already exists
            if "\nperson:" in frontmatter or frontmatter.startswith("person:"):
                return False
            # Insert person field at end of frontmatter
            new_content = content[:end] + "\nperson: \"[[%s]]\"" % person_name + content[end:]
        else:
            # Unclosed frontmatter — skip to avoid corruption
            return False
    else:
        # No frontmatter — skip if bold person link already at top
        if content.startswith("**[[%s]]**" % person_name):
            return False
        new_content = "**[[%s]]**\n\n" % person_name + content

    if not dry_run:
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, filepath)

    return True


def link_frontmatter_fields(filepath, team_hubs, person_links, dry_run):
    """Add wiki links to team: and participants: fields in YAML frontmatter.

    team: COPS → team: "[[COPS]]"
    participants: [Alice, Bob] → participants: ["[[Alice]]", "[[Bob]]"]

    team_hubs: set of team names that have hub pages.
    person_links: dict of lowercase name -> name (for participant matching).
    Returns (changed, count) like process_file.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return False, 0

    if not content.startswith("---\n"):
        return False, 0

    end = content.find("\n---\n", 4)
    if end == -1:
        return False, 0

    frontmatter = content[4:end]
    new_frontmatter = frontmatter
    count = 0

    # Link team: field
    team_match = re.search(r'^team:\s*"?([^"\n]+)"?\s*$', new_frontmatter, re.MULTILINE)
    if team_match:
        team_name = team_match.group(1).strip()
        # Skip if already a wiki link
        if "[[" not in team_name and team_name in team_hubs:
            old_line = team_match.group(0)
            new_line = 'team: "[[%s]]"' % team_name
            new_frontmatter = new_frontmatter.replace(old_line, new_line, 1)
            count += 1

    # Link participants: field
    parts_match = re.search(r'^participants:\s*\[([^\]]+)\]\s*$', new_frontmatter, re.MULTILINE)
    if parts_match:
        raw_list = parts_match.group(1)
        # Skip if already contains wiki links
        if "[[" not in raw_list:
            names = [n.strip() for n in raw_list.split(",")]
            linked_names = []
            for name in names:
                if name.lower() in person_links:
                    linked_names.append('"[[%s]]"' % name)
                    count += 1
                else:
                    linked_names.append(name)
            if count > 0:
                new_list = "participants: [%s]" % ", ".join(linked_names)
                old_list = parts_match.group(0)
                new_frontmatter = new_frontmatter.replace(old_list, new_list, 1)

    if count == 0:
        return False, 0

    new_content = "---\n" + new_frontmatter + content[end:]

    if not dry_run:
        tmp_path = filepath + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, filepath)

    return True, count


def generate_plans_index(vault_base, dry_run):
    """Generate Plans/_Index.md listing all plans reverse-chronologically."""
    plans_dir = os.path.join(vault_base, "Plans")
    if not os.path.isdir(plans_dir):
        return None

    files = sorted(
        (f for f in os.listdir(plans_dir)
         if f.endswith(".md") and not f.startswith("_")),
        reverse=True,
    )

    if not files:
        return None

    lines = [
        "---",
        "type: index",
        "scope: plans",
        "generated: true",
        "---",
        "",
        "# Plans",
        "",
        "| Date | Plan |",
        "|------|------|",
    ]

    for f in files:
        name = f[:-3]
        # Extract date from filename (YYYY-MM-DD prefix)
        date = name[:10] if len(name) >= 10 else ""
        # Read first H1 heading for the title
        title = name
        filepath = os.path.join(plans_dir, f)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except (OSError, UnicodeDecodeError):
            pass
        lines.append("| %s | [%s](<%s.md>) |" % (date, title, name))

    lines.append("")
    content = "\n".join(lines)
    out_path = os.path.join(plans_dir, "_Index.md")

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

    # Add person backlinks to bonusly and review cycle files
    backlinks_added = 0
    if args.scope in ("all", "bonusly"):
        for team_dir in _list_team_dirs(args.teams_path):
            for person_dir in _list_subdirs(team_dir):
                person_name = os.path.basename(person_dir)
                fb_dir = os.path.join(person_dir, "Feedback")
                if not os.path.isdir(fb_dir):
                    continue
                for f in os.listdir(fb_dir):
                    if not f.endswith(".md"):
                        continue
                    fpath = os.path.join(fb_dir, f)
                    if add_person_backlink(fpath, person_name, args.dry_run):
                        backlinks_added += 1
                        rel = os.path.relpath(fpath, args.vault_base)
                        print("  %s: +backlink to [[%s]]" % (rel, person_name))

    if backlinks_added > 0:
        print("\nPerson backlinks added: %d" % backlinks_added)

    # Link frontmatter fields (team:, participants:) across all team files
    frontmatter_links = 0
    if args.scope == "all":
        # Build set of team names that have hub pages
        team_hubs = set()
        for team_dir in _list_team_dirs(args.teams_path):
            team_hubs.add(os.path.basename(team_dir))

        # Build person lookup for participant linking
        person_lookup = {n.lower(): n for n in person_names}

        # Collect all files under Teams that could have team:/participants: frontmatter
        fm_targets = []
        for team_dir in _list_team_dirs(args.teams_path):
            # Person profiles
            for person_dir in _list_subdirs(team_dir):
                profile = os.path.join(person_dir, "%s.md" % os.path.basename(person_dir))
                if os.path.isfile(profile):
                    fm_targets.append(profile)
            # Retros
            retro_dir = os.path.join(team_dir, "Retros")
            if os.path.isdir(retro_dir):
                for f in os.listdir(retro_dir):
                    if f.endswith(".md") and not f.startswith("_"):
                        fm_targets.append(os.path.join(retro_dir, f))
            # Sprint files (summaries, metrics, pulses in subdirs)
            sprints_dir = os.path.join(team_dir, "Sprints")
            if os.path.isdir(sprints_dir):
                for sd in os.listdir(sprints_dir):
                    sd_path = os.path.join(sprints_dir, sd)
                    if os.path.isdir(sd_path):
                        for f in os.listdir(sd_path):
                            if f.endswith(".md"):
                                fm_targets.append(os.path.join(sd_path, f))

        for fpath in sorted(set(fm_targets)):
            changed, count = link_frontmatter_fields(fpath, team_hubs, person_lookup, args.dry_run)
            if changed:
                frontmatter_links += count
                rel = os.path.relpath(fpath, args.vault_base)
                print("  %s: +%d frontmatter links" % (rel, count))

    if frontmatter_links > 0:
        print("\nFrontmatter links added: %d" % frontmatter_links)

    # Generate index and hub pages
    indexes_created = []

    if args.scope == "all":
        for team_dir in _list_team_dirs(args.teams_path):
            team_name = os.path.basename(team_dir)

            idx_path = generate_team_hub(team_dir, team_name, args.teams_path, args.dry_run)
            if idx_path:
                indexes_created.append(os.path.relpath(idx_path, args.vault_base))

            idx_path = generate_sprint_timeline(team_dir, team_name, args.dry_run)
            if idx_path:
                indexes_created.append(os.path.relpath(idx_path, args.vault_base))

        idx_path = generate_plans_index(args.vault_base, args.dry_run)
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
