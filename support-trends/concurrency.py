"""Advisory pipeline lockfile + session-token enforcement for support-trends.

The pipeline is a sequence of separately-invoked Python scripts (setup → fetch
→ analyze → bundle → 3 sub-agents → 3 apply scripts → report). The cache
directory `/tmp/support_trends/` is shared across the run. Two pipelines
running concurrently — typically a manual run overlapping with a scheduled
one — will clobber each other's intermediate state, producing a Frankenstein
`analysis.json` whose fields come from two different windows. The leadership-
conversation report then quotes inconsistent numbers.

This module enforces three layers of protection:

1. **Advisory lock** (`acquire` / `release`) — held setup→report; prevents two
   full orchestrator runs running concurrently.

2. **Session token** (`verify_session`) — every mid-pipeline script verifies
   that the session UUID in `setup.json` matches the one in the lock file
   before doing anything. This catches the foot-gun where someone runs
   `bundle.py` or `analyze.py` standalone, outside an orchestrator — those
   scripts silently corrupt downstream state because the lock was acquired
   for a different session (or no session at all).

3. **Stale-state clear** (`clear_stale_state`) — setup.py calls this after
   acquiring the lock, removing the previous run's `bundle.json`,
   `analysis.json`, `data*.json`, and the three sub-agent `results.json`
   files. Without this, if Team B's themes sub-agent crashes before writing
   its `results.json`, `apply_themes` would merge **Team A's leftover
   results** into Team B's `analysis.json`. Validators catch hallucinated
   keys but two teams sharing a project (label-distinguished) can have
   overlapping in-window keys that bleed through silently. KEPT across runs:
   the lock itself, JQL probe cache, persistent themes vocabulary.

Because the pipeline spans many Python processes (each exits between steps),
PID liveness can't be used as a freshness signal — the originating process
is gone by the time apply_themes runs. Staleness is timestamp-based: a lock
older than STALE_LOCK_AFTER_SECONDS is assumed to belong to a crashed run
and is reclaimed.

Lock file format (single line):
    <iso8601_acquired_at>|<team_vault_dir_or_->|<originating_pid>|<session_uuid>

The PID is informational only — it lets a user investigating a stale lock
see which process started it. The session_uuid is the load-bearing field for
verify_session: mid-pipeline scripts compare it against `setup.json.session`
and refuse to proceed on mismatch.
"""

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone


CACHE_DIR = "/tmp/support_trends"
LOCK_PATH = os.path.join(CACHE_DIR, ".run.lock")
SETUP_PATH = os.path.join(CACHE_DIR, "setup.json")
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
    parts = line.split("|", 3)
    if len(parts) < 1:
        return None
    return {
        "acquired_at": parts[0],
        "team": parts[1] if len(parts) >= 2 else "-",
        "pid": parts[2] if len(parts) >= 3 else "?",
        # session is empty for legacy locks (pre-session-token); verify_session
        # treats empty as "no session" and forces re-run via setup.py.
        "session": parts[3] if len(parts) >= 4 else "",
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
    """Try to take the pipeline lock. Returns (acquired: bool, message: str, session: str).

    `session` is a fresh UUID written into the lock file's 4th field; callers
    (setup.py) must persist it into setup.json so mid-pipeline scripts can
    verify they're operating on the same session via verify_session().

    On failure, session is "" and caller is expected to exit non-zero.
    """
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    existing = _read_lock()
    if existing is not None and not _is_stale(existing):
        return False, (
            "Another support-trends run is in progress: started %s for team "
            "%s (PID %s). If that run is dead, remove %s and retry — or wait "
            "%d hours for the stale-lock auto-reclaim." % (
                existing["acquired_at"], existing["team"], existing["pid"],
                LOCK_PATH, STALE_LOCK_AFTER_SECONDS // 3600)), ""
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
            return False, "Failed to remove stale lock %s: %s" % (LOCK_PATH, e), ""

    session = uuid.uuid4().hex
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
                existing["team"] if existing else "?")), ""
    try:
        with os.fdopen(fd, "w") as f:
            f.write("%s|%s|%d|%s\n" % (
                _now_iso(),
                team_vault_dir or "-",
                os.getpid(),
                session))
    except OSError as e:
        try:
            os.unlink(LOCK_PATH)
        except OSError:
            pass
        return False, "Failed to write lock file: %s" % e, ""
    return True, "Acquired pipeline lock for team %s (session %s)" % (
        team_vault_dir or "-", session[:8]), session


def verify_session():
    """Mid-pipeline scripts call this at the top of main() to confirm they're
    operating inside a live orchestrator session. Returns (ok: bool, message: str).

    Three failure modes the caller must distinguish:

    1. **Lock missing** — no acquire ever ran, or report.py released. Either
       the user is running this script standalone (foot-gun) or the previous
       run completed and they should re-orchestrate from setup.py.

    2. **setup.json missing** — lock exists but setup.py never wrote setup.json
       (or it was deleted). Stale lock + corrupt cache; user should remove
       the lock and re-run setup.

    3. **Session mismatch** — both files exist but their session UUIDs differ.
       Most common cause: the orchestrator was killed mid-run, the lock auto-
       reclaimed by a new acquire, then the user reran an intermediate script
       that's still pointing at the old setup.json. Re-run setup.py.

    Caller exits non-zero on False. We return rather than exit so the caller
    can fold the message into its own logging convention.
    """
    lock = _read_lock()
    if lock is None:
        return False, (
            "verify_session: no pipeline lock at %s. This script is part of an "
            "orchestrated pipeline (setup → fetch → analyze → bundle → 3 "
            "sub-agents → 3 apply scripts → report) and must not be run "
            "standalone — its inputs would be a stale cache from a previous "
            "run. Re-run via the orchestrator (the slash command or a manual "
            "setup.py invocation)." % LOCK_PATH)
    if _is_stale(lock):
        return False, (
            "verify_session: pipeline lock at %s is stale (acquired %s for "
            "team %s, > %dh ago). The owning orchestrator likely crashed. "
            "Remove the lock and re-run setup.py." % (
                LOCK_PATH, lock.get("acquired_at", "?"), lock.get("team", "?"),
                STALE_LOCK_AFTER_SECONDS // 3600))
    try:
        with open(SETUP_PATH) as f:
            setup = json.load(f)
    except FileNotFoundError:
        return False, (
            "verify_session: lock exists but %s does not. The cache is in an "
            "inconsistent state. Remove %s and re-run setup.py." % (
                SETUP_PATH, LOCK_PATH))
    except (json.JSONDecodeError, OSError) as e:
        return False, (
            "verify_session: %s exists but is unreadable (%s). Remove %s and "
            "re-run setup.py." % (SETUP_PATH, e, LOCK_PATH))

    setup_session = (setup.get("session") or "").strip()
    lock_session = (lock.get("session") or "").strip()
    if not lock_session:
        return False, (
            "verify_session: lock at %s is from a pre-session-token run "
            "(legacy format). Remove the lock and re-run setup.py to "
            "regenerate." % LOCK_PATH)
    if not setup_session:
        return False, (
            "verify_session: %s does not contain a `session` token. Re-run "
            "setup.py to write a fresh one." % SETUP_PATH)
    if setup_session != lock_session:
        return False, (
            "verify_session: session mismatch between lock (%s…) and "
            "setup.json (%s…). The orchestrator likely restarted mid-run; "
            "your in-flight intermediate script is operating on stale state. "
            "Re-run from setup.py." % (lock_session[:8], setup_session[:8]))
    return True, "verify_session: ok (session %s, team %s)" % (
        lock_session[:8], lock.get("team", "?"))


# Files cleared by `clear_stale_state` between runs. Keeping a single tuple
# means the list is greppable from the rest of the codebase ("does this file
# survive across runs?") and a future addition is one edit.
_STALE_FILES_RELATIVE = (
    "bundle.json",
    "analysis.json",
    "data.json",
    "data_prior.json",
    "report.md",
    # v1-era artefacts that occasionally linger in /tmp/ when a v1 cache predates
    # a v2 install. Listed explicitly so a v2 run after a v1 run starts clean.
    "triage.json",
)
_STALE_DIRS_RELATIVE = (
    "themes",
    "support_feedback",
    "synthesise",
    # v1-era subdirs (charter agent, cluster fixture, old synthesis agent name,
    # triage cross-ref). Removing keeps the cache clean across version jumps.
    "charter",
    "clusters",
    "synthesis",
    "triage_crossref",
)
# Files explicitly KEPT across runs. Listed here so a future maintainer
# checking "where does the persistent vocabulary live?" can find it.
_KEPT_PATTERNS = (
    ".run.lock",                     # the lock itself; only release() removes it
    "themes_vocabulary",             # legacy + per-team vocab fallback (canonical lives in vault)
)


def clear_stale_state():
    """Wipe the cached intermediate files from the previous run.

    Called by setup.py immediately after a successful acquire(), before any
    other file write. Without this step, two consecutive pipeline runs share
    `/tmp/support_trends/` — and if Team B's themes sub-agent crashes before
    writing its results.json, apply_themes will pick up Team A's leftover
    results and silently merge them into Team B's analysis.json.

    Per-file rules:
      - Removes: bundle.json, analysis.json, data*.json, report.md
      - Removes: themes/, support_feedback/, synthesise/ subdirectories
        (each contains a single results.json from a sub-agent)
      - Keeps: the lock itself, JQL probe cache, themes vocabulary
        (these encode cross-run learning we want to preserve)

    Returns a count of removed files/dirs for logging.
    """
    if not os.path.isdir(CACHE_DIR):
        return 0
    removed = 0
    for name in _STALE_FILES_RELATIVE:
        path = os.path.join(CACHE_DIR, name)
        try:
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print("WARNING: could not remove stale %s (%s)" % (path, e), file=sys.stderr)
    for name in _STALE_DIRS_RELATIVE:
        path = os.path.join(CACHE_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as e:
            print("WARNING: could not remove stale dir %s (%s)" % (path, e), file=sys.stderr)
    return removed


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
