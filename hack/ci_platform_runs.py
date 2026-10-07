"""Wait until no run of the named audits is going on the Platform Agent. Runs in the agent container.

Usage: python3 - <home> <bound-seconds> <poll-seconds> <audit-id>... < ci_platform_runs.py

hack/ci-eval-pr.sh pipes this into the gateway before a unit on those audit streams
runs (wait_platform_runs). A run the install started for itself -- a scheduled one,
or one the `oobe` stage marked due -- writes the stream's ledger issue like the
unit's own, so the unit waits for it rather than resetting the ledger under it.

Counts a run claimed or running, and an audit marked due (or overdue) and not yet
claimed, since the next profile-cron-tick starts it. A row claimed more than
``STALE_SECONDS`` ago is not counted: a gateway restart leaves a cut-off run's row
at running for good, and no audit takes that long. A store it cannot read counts as
busy, so a failed read is waited out rather than taken as idle.

Prints one line: how long it waited and what was still going when it stopped.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

home, bound, poll, audits = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]
LEDGER = os.path.join(home, "profiles", "platform", "cron", "executions.db")
ROSTER = os.path.join(home, "profiles", "platform", "cron", "jobs.json")
PAUSED_STATE = "paused"
SQLITE_BUSY_TIMEOUT_SECONDS = 10
IN_FLIGHT = ("claimed", "running")
# Several times the longest audit run (9-15 minutes, #985), so a live run always counts.
STALE_SECONDS = 60 * 60
UNREADABLE = "unreadable"


def stamp(value):
    """An ISO timestamp as an aware datetime: a naive one predates Hermes' offset-aware
    stamps and meant local time (profile_cron_tick.due_job_ids). None when it does not parse.
    """
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when.astimezone() if when.tzinfo is None else when


def due(now):
    """Audits whose next run is already due: marked, or overdue, and not yet claimed.

    A disabled or paused job is never claimed, so it is not waited on; one with no next
    run is not scheduled. A stamp that does not parse counts as due, as the tick reads it.
    """
    try:
        with open(ROSTER, encoding="utf-8") as fh:
            stored = json.load(fh)
    except FileNotFoundError:
        return set()
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    found = set()
    for job in jobs:
        if not isinstance(job, dict) or job.get("id") not in audits:
            continue
        if not job.get("enabled", True) or job.get("state") == PAUSED_STATE or job.get("paused_at"):
            continue
        next_run = job.get("next_run_at")
        if not isinstance(next_run, str) or not next_run:
            continue
        when = stamp(next_run)
        if when is None or when <= now:
            found.add(job["id"])
    return found


def running(now):
    if not os.path.exists(LEDGER):
        return set()
    placeholders = ",".join("?" * len(audits))
    since = now - timedelta(seconds=STALE_SECONDS)
    conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        rows = conn.execute(
            f"SELECT job_id, claimed_at FROM executions WHERE job_id IN ({placeholders}) AND status IN (?, ?)",
            (*audits, *IN_FLIGHT),
        ).fetchall()
    finally:
        conn.close()
    # A row whose stamp does not parse counts: it is claimed or running, and its age unknown.
    return {job for job, claimed in rows if (when := stamp(claimed)) is None or when >= since}


def busy():
    now = datetime.now(timezone.utc)
    try:
        return due(now) | running(now)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return {f"{UNREADABLE} ({exc})"}


waited = 0
going = busy()
while going and waited < bound:
    time.sleep(poll)
    waited += poll
    going = busy()
if going:
    print(f"still going after {waited}s, the run goes ahead: {', '.join(sorted(going))}")
elif waited:
    print(f"ended after {waited}s")
else:
    print("none going")
