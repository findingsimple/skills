"""Advisory pipeline lockfile for support-trends.

The pipeline is a sequence of separately-invoked Python scripts (setup → fetch
→ analyze → multiple sub-agent steps → report). The cache directory
`/tmp/support_trends/` is shared across the run. Two pipelines running
concurrently — typically a manual run overlapping with a scheduled one — will
clobber each other's intermediate state, producing a Frankenstein
`analysis.json` whose fields come from two different windows. The leadership-
conversation report then quotes inconsistent numbers.

This module provides an *advisory* lock the orchestrator (SKILL.md) acquires
on entry (Step 2) and releases on success (Step 7). The threat model is
accidental concurrent runs, not malicious bypass.

Because the pipeline spans many Python processes (each exits between steps),
PID liveness can't be used as a freshness signal — the originating process
is gone by the time apply_themes runs. Staleness is timestamp-based: a lock
older than STALE_LOCK_AFTER_SECONDS is assumed to belong to a crashed run
and is reclaimed.

Lock file format (single line):
    <iso8601_acquired_at>|<team_vault_dir_or_->|<originating_pid>

The PID is informational only — it lets a user investigating a stale lock
see which process started it.
"""

import os
import sys
from datetime import datetime, timezone


CACHE_DIR = "/tmp/support_trends"
LOCK_PATH = os.path.join(CACHE_DIR, ".run.lock")
# Generous: longest legitimate pipeline observed is ~10 minutes, but the
# synthesis sub-agent has timed out at 30+ minutes in production. 4 hours
# means a crashed run is reclaimed within a working day, and a frozen-but-
# legitimate run isn't accidentally clobbered by a parallel manual run.
STALE_LOCK_AFTER_SECONDS = 4 * 3600


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_lock():
    try:
        with open(LOCK_PATH) as f:
            line = f.read().strip()
    except (OSError, FileNotFoundError):
        return None
    if not line:
        return None
    parts = line.split("|", 2)
    if len(parts) < 1:
        return None
    return {
        "acquired_at": parts[0],
        "team": parts[1] if len(parts) >= 2 else "-",
        "pid": parts[2] if len(parts) >= 3 else "?",
    }


def _is_stale(existing):
    """Return True iff the lock's acquired_at is older than the stale cutoff,
    OR the timestamp is unparseable (corrupt → reclaim)."""
    try:
        acq_dt = datetime.fromisoformat(existing["acquired_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, TypeError):
        return True
    if acq_dt.tzinfo is None:
        acq_dt = acq_dt.replace(tzinfo=timezone.utc)
    age_s = (datetime.now(timezone.utc) - acq_dt).total_seconds()
    return age_s > STALE_LOCK_AFTER_SECONDS


def acquire(team_vault_dir):
    """Try to take the pipeline lock. Returns (acquired: bool, message: str).

    Caller is expected to exit non-zero when acquired is False.
    """
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    existing = _read_lock()
    if existing is not None and not _is_stale(existing):
        return False, (
            "Another support-trends run is in progress: started %s for team "
            "%s (PID %s). If that run is dead, remove %s and retry — or wait "
            "%d hours for the stale-lock auto-reclaim." % (
                existing["acquired_at"], existing["team"], existing["pid"],
                LOCK_PATH, STALE_LOCK_AFTER_SECONDS // 3600))
    if existing is not None:
        # Stale — log and clobber. We must unlink before O_EXCL or the open
        # below races against the corpse and we falsely report "lost race".
        print("WARNING: clobbering stale support-trends lock at %s "
              "(acquired_at=%s, team=%s, pid=%s)" % (
                  LOCK_PATH, existing.get("acquired_at", "?"),
                  existing.get("team", "?"), existing.get("pid", "?")),
              file=sys.stderr)
        try:
            os.unlink(LOCK_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            return False, "Failed to remove stale lock %s: %s" % (LOCK_PATH, e)

    # O_EXCL handles the race where two acquirers both saw "no lock" (or both
    # decided the same lock was stale). The loser sees FileExistsError and
    # treats the existing lock as authoritative.
    try:
        fd = os.open(LOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_lock()
        return False, (
            "Lost race to another support-trends run that just started "
            "(team %s). Retry once that run finishes." % (
                existing["team"] if existing else "?"))
    try:
        with os.fdopen(fd, "w") as f:
            f.write("%s|%s|%d\n" % (
                _now_iso(),
                team_vault_dir or "-",
                os.getpid()))
    except OSError as e:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass
        return False, "Failed to write lock file: %s" % e
    return True, "Acquired pipeline lock for team %s" % (team_vault_dir or "-")


def release():
    """Best-effort lock removal. Tolerates a missing or already-clobbered lock.

    We don't refuse to release a lock that names a different team — a stale
    lock from team A may have been clobbered by team B's setup, and team B's
    final report.py should still clear that lock. The risk is symmetric: the
    threat model is accidental concurrent runs, not adversarial behaviour."""
    try:
        os.unlink(LOCK_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        print("WARNING: failed to remove pipeline lock %s: %s" % (LOCK_PATH, e),
              file=sys.stderr)
