#!/usr/bin/env python3
"""Discover Claude Code per-project memory entries and produce an inventory.

Walks ~/.claude/projects/*/memory/ across every recorded project on this
machine, captures frontmatter + body + mtime for each memory file, and
flags candidates that look like duplicates of an existing repo CLAUDE.md
rule (substring match on memory `name`).

The classification step (keep / move-to-repo / move-to-global / delete) is
NOT done here — it lives in the orchestrating skill, where the model can
apply judgment. This script's job is purely deterministic discovery.

Output: /tmp/review_memory/inventory.json (atomic write, 0o700 cache dir).
"""

import argparse
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone


PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")
GLOBAL_CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
CACHE_DIR = "/tmp/review_memory"
INVENTORY_PATH = os.path.join(CACHE_DIR, "inventory.json")

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?\n)---\s*\n(.*)\Z", re.DOTALL)
_FRONT_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$")


def ensure_cache_dir(path):
    """Create cache dir with 0o700, rejecting symlinks; chmod-repair if pre-existing."""
    if os.path.islink(path):
        raise SystemExit("ERROR: %s is a symlink — refusing to write." % path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)  # exist_ok=True doesn't repair perms on a pre-existing dir


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
    os.replace(tmp, path)


def decode_project_dir(name):
    """Decode encoded project dir name back to the CWD path.

    Encoding (observed): `/` → `-`, `/.` → `--`. Decoder walks chars.
    e.g. `-Users-jasonconroy--claude-skills` → `/Users/jasonconroy/.claude/skills`.
    """
    out = []
    i = 0
    while i < len(name):
        if name[i] == "-":
            if i + 1 < len(name) and name[i + 1] == "-":
                out.append("/.")
                i += 2
            else:
                out.append("/")
                i += 1
        else:
            out.append(name[i])
            i += 1
    return "".join(out)


def parse_frontmatter(text):
    """Return (frontmatter_dict, body) — dict empty if no frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    front = {}
    for line in m.group(1).splitlines():
        kv = _FRONT_KV_RE.match(line)
        if kv:
            key, val = kv.group(1), kv.group(2)
            # Strip surrounding quotes if present
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            front[key] = val
    return front, m.group(2)


def load_claude_md(path):
    """Read a CLAUDE.md if present; return text or empty string."""
    try:
        with open(path) as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return ""


def dedup_signal(memory_name, memory_body, claude_md_text):
    """Heuristic: does this memory look like it duplicates an existing CLAUDE.md rule?

    Returns a short reason string if a likely match is found, else "".
    Conservative — caller (the model) makes the final call.
    """
    if not claude_md_text:
        return ""
    name_lower = (memory_name or "").lower()
    if name_lower and len(name_lower) > 8 and name_lower in claude_md_text.lower():
        return "memory `name` substring appears in CLAUDE.md"
    # First sentence of body
    first = (memory_body or "").strip().split("\n", 1)[0].split(".")[0].strip()
    if first and len(first) > 30 and first.lower() in claude_md_text.lower():
        return "memory's first sentence appears verbatim in CLAUDE.md"
    return ""


def discover_project(project_dir, project_name):
    """Build the inventory record for one project's memory dir."""
    decoded_path = decode_project_dir(project_name)
    repo_claude_md_path = os.path.join(decoded_path, "CLAUDE.md")
    repo_claude_md = load_claude_md(repo_claude_md_path)

    record = {
        "project_name": project_name,
        "decoded_path": decoded_path,
        "decoded_path_exists": os.path.isdir(decoded_path),
        "repo_claude_md_path": repo_claude_md_path,
        "repo_claude_md_exists": bool(repo_claude_md),
        "memory_dir": project_dir,
        "memories": [],
    }

    try:
        entries = sorted(os.listdir(project_dir))
    except OSError as e:
        record["error"] = "could not list memory dir: %s" % e
        return record

    for fname in entries:
        if fname == "MEMORY.md" or not fname.endswith(".md"):
            continue
        path = os.path.join(project_dir, fname)
        try:
            with open(path) as f:
                text = f.read()
            st = os.stat(path)
        except OSError as e:
            record["memories"].append({
                "filename": fname, "error": "unreadable: %s" % e,
            })
            continue

        front, body = parse_frontmatter(text)
        record["memories"].append({
            "filename": fname,
            "path": path,
            "size_bytes": st.st_size,
            "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_days": int((datetime.now(timezone.utc).timestamp() - st.st_mtime) / 86400),
            "frontmatter": {
                "name": front.get("name", ""),
                "description": front.get("description", ""),
                "type": front.get("type", ""),
            },
            "body_preview": body.strip()[:400],
            "body_full": body.strip(),
            "dedup_signal": dedup_signal(front.get("name", ""), body, repo_claude_md),
        })

    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--project", help="Filter to one project (encoded dir name OR decoded path)")
    args = ap.parse_args()

    if not os.path.isdir(PROJECTS_ROOT):
        print("ERROR: %s does not exist — is Claude Code installed?" % PROJECTS_ROOT, file=sys.stderr)
        sys.exit(1)

    ensure_cache_dir(CACHE_DIR)

    projects = []
    flagged_count = 0
    total_memories = 0

    for project_name in sorted(os.listdir(PROJECTS_ROOT)):
        memory_dir = os.path.join(PROJECTS_ROOT, project_name, "memory")
        if not os.path.isdir(memory_dir):
            continue
        if args.project:
            decoded = decode_project_dir(project_name)
            if args.project not in (project_name, decoded):
                continue

        record = discover_project(memory_dir, project_name)
        if not record["memories"] and "error" not in record:
            continue
        projects.append(record)
        total_memories += len(record["memories"])
        flagged_count += sum(1 for m in record["memories"] if m.get("dedup_signal"))

    inventory = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "global_claude_md_path": GLOBAL_CLAUDE_MD,
        "global_claude_md_exists": os.path.isfile(GLOBAL_CLAUDE_MD),
        "project_count": len(projects),
        "memory_count": total_memories,
        "flagged_as_possible_duplicate": flagged_count,
        "projects": projects,
    }
    atomic_write_json(INVENTORY_PATH, inventory)

    print("Inventory written: %s" % INVENTORY_PATH)
    print("Projects with memories: %d  |  Total memories: %d  |  Flagged as possible duplicates: %d" % (
        len(projects), total_memories, flagged_count))


if __name__ == "__main__":
    main()
