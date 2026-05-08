"""Shared helpers for parsing the CHARTER_TEAMS env var and slugifying team
names for filesystem use.

`CHARTER_TEAMS` declares the canonical taxonomy of teams a routing-audit pipeline
treats as valid `should_be_at` targets and that a charter doc references with
H2 sections. Format (pipe-delimited slots, optional comma-separated aliases per
slot):

    "TeamA|TeamB|TeamC"
    "TeamA:alpha,team-a|TeamB|TeamC:gamma,team-c"

The parser tolerates whitespace and skips any canonical/alias that fails
`TEAM_NAME_RE` with a stderr WARNING, so a single malformed slot doesn't break
the rest of the env var.

`norm_team` resolves a free-form display name back to its canonical via the
alias map.

`slugify_team` turns a canonical (possibly containing spaces, `&`, etc.) into
a filesystem-safe component matching `FILENAME_TEAM_RE`. Raises `ValueError`
if the canonical can't be reduced to a safe slug.
"""

import re
import sys


TEAM_NAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9 _\-&]{0,63}\Z", re.ASCII)
FILENAME_TEAM_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_\-]{0,63}\Z", re.ASCII)


def parse_charter_teams(env_value):
    """Parse a CHARTER_TEAMS env value. Returns ([canonicals in declared order],
    {lowercased_alias_or_canonical: canonical})."""
    canonicals = []
    aliases = {}
    if not env_value:
        return canonicals, aliases
    for slot in env_value.split("|"):
        slot = slot.strip()
        if not slot:
            continue
        if ":" in slot:
            canonical, alias_csv = slot.split(":", 1)
            canonical = canonical.strip()
            alias_list = [a.strip() for a in alias_csv.split(",") if a.strip()]
        else:
            canonical = slot
            alias_list = []
        if not TEAM_NAME_RE.match(canonical):
            print("WARNING: CHARTER_TEAMS: skipping invalid canonical name %r" % canonical,
                  file=sys.stderr)
            continue
        canonicals.append(canonical)
        aliases[canonical.lower()] = canonical
        for a in alias_list:
            if not TEAM_NAME_RE.match(a):
                print("WARNING: CHARTER_TEAMS: skipping invalid alias %r for %s" % (a, canonical),
                      file=sys.stderr)
                continue
            aliases[a.lower()] = canonical
    return canonicals, aliases


def norm_team(s, alias_map):
    """Resolve a free-form team name against an alias_map. Returns the canonical
    name on hit, None on miss or non-string input."""
    if not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    return alias_map.get(raw.lower())


def slugify_team(canonical):
    """Reduce a canonical team name to a safe filename component.

    Collapses runs of non-alphanumerics into a single underscore, strips
    leading/trailing underscores, then re-validates the result against
    FILENAME_TEAM_RE. Note: `'Leasing & CRM'` and `'Leasing CRM'` collide on
    `'Leasing_CRM'`. Callers that need to distinguish them must enforce
    uniqueness upstream.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", canonical).strip("_")
    if not FILENAME_TEAM_RE.match(slug):
        raise ValueError("Cannot slugify %r to a safe filename" % canonical)
    return slug
