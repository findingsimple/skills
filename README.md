# Claude Code Skills

Custom skills for [Claude Code](https://claude.ai/code), stored in `~/.claude/skills/`.

## What are Skills?

Skills are reusable prompt-based capabilities that extend Claude Code. They can be invoked as slash commands (e.g., `/my-skill`) during a Claude Code session.

## Skills

| Skill | Command | Description |
|-------|---------|-------------|
| [bonusly-sync](bonusly-sync/) | `/bonusly-sync` | Sync previous month's Bonusly recognition to Obsidian vault |

## Usage

Place skill directories in this repo. Each skill needs a `SKILL.md` file with YAML frontmatter and prompt instructions.

### bonusly-sync

Pulls the previous month's Bonusly recognition (given and received) for tracked team members and saves markdown files into each person's `Feedback/` folder in the Obsidian vault.

```bash
/bonusly-sync              # sync to vault (path from ~/.bonusly_env)
/bonusly-sync --dry-run    # preview without writing files
/bonusly-sync /path/to/vault  # use a custom vault path
```

**Prerequisites:**
- `~/.bonusly_env` with `BONUSLY_API_TOKEN` and `OBSIDIAN_VAULT_PATH`
- Person notes under `{vault}/HappyCo/Teams/` with `email` in YAML frontmatter
