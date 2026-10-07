"""Wait until no run of the named audits is going on the Platform Agent. Runs in the agent container.

Usage: python3 - <home> <bound-seconds> <poll-seconds> <audit-id>... < ci_platform_runs.py

hack/ci-eval-pr.sh pipes this into the gateway before a unit on those audit streams
runs (wait_platform_runs). A run the install started for itself -- a scheduled one,
or one the `oobe` stage marked due -- writes the stream's ledger issue like the
unit's own, so the unit waits for it rather than resetting the ledger under it.

Counts a run claimed or running, and an audit marked due (or overdue) and not yet
claimed, since the next profile-cron-tick starts it. A row claimed more than
``STALE_SECONDS`` ago is not counted: a gateway restart leaves a cut-off run's row
at running for good, and no audit takes that long. Anything it cannot read counts
as busy, so a failed read is waited out rather than taken as idle.

While the Chat Agent's `oobe` stage is pending, the audit it marks next counts
too, before it is marked: a unit that started then would have its own worker's
`start` refused once the stage's run took the stream's in-flight note. Only the
next one: waiting out the whole chain would hold every audit case on those streams
for most of an hour. While the oobe-first-run-audits stack has the stage armed (its
state file is there: the chain overran or the teardown could not disarm), every
audit the stage has still to run counts. An audit has had its turn once the stage
has marked it and its run has ended, or it was held or given up on. The stage's
audit list and order are read from the image's copy of oobe.py, not run.

Prints one line: how long it waited and what was still going when it stopped.
"""

import ast
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

home, bound, poll, audits = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4:]
LEDGER = os.path.join(home, "profiles", "platform", "cron", "executions.db")
ROSTER = os.path.join(home, "profiles", "platform", "cron", "jobs.json")
CHAT_ROSTER = os.path.join(home, "cron", "jobs.json")
STAGE_MARKER = os.path.join(home, ".oobe_audits_fired")
# bench/tf/prebuilt/oobe-first-run-audits/arm.py writes it; disarm.py removes it.
STACK_STATE = os.path.join(home, ".bench-oobe.json")
# The image's copy, not the volume's: the volume is the agent's to write.
STAGE_SOURCE = os.environ.get("OOBE_STAGE_SOURCE", "/opt/defaults/scripts/oobe.py")
STAGE_JOB = "oobe"
STAGE_AUDITS = "FIRST_RUN_AUDITS"
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
        jobs = jobs_in(ROSTER)
    except FileNotFoundError:
        return set()
    found = set()
    for job in jobs:
        if job.get("id") not in audits:
            continue
        if not runnable(job):
            continue
        next_run = job.get("next_run_at")
        if not isinstance(next_run, str) or not next_run:
            continue
        when = stamp(next_run)
        if when is None or when <= now:
            found.add(job["id"])
    return found


def jobs_in(path):
    with open(path, encoding="utf-8") as fh:
        stored = json.load(fh)
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: its jobs are not a list")
    return [job for job in jobs if isinstance(job, dict)]


def stage_audits():
    tree = ast.parse(open(STAGE_SOURCE, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == STAGE_AUDITS for t in node.targets):
            return list(ast.literal_eval(node.value))
    raise ValueError(f"{STAGE_SOURCE} names no {STAGE_AUDITS}")


def runnable(job):
    return job.get("enabled", True) and job.get("state") != PAUSED_STATE and not job.get("paused_at")


def stage_pending():
    """The audits asked about that a pending `oobe` stage holds, each with why."""
    try:
        if not any(job.get("id") == STAGE_JOB and runnable(job) for job in jobs_in(CHAT_ROSTER)):
            return set()
    except FileNotFoundError:
        return set()
    try:
        with open(STAGE_MARKER, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        state = {}
    if state.get("done"):
        return set()
    current = (state.get("current") or {}).get("job")
    had_turn = (set(state.get("fired", [])) | set(state.get("held", {})) | set(state.get("gave_up", []))) - {current}
    remaining = [audit for audit in stage_audits() if audit not in had_turn]
    if os.path.exists(STACK_STATE):
        return {f"{audit} (armed oobe stage)" for audit in remaining if audit in audits}
    return {f"{audit} (oobe stage, next)" for audit in remaining[:1] if audit in audits}


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
        return due(now) | running(now) | stage_pending()
    except Exception as exc:  # noqa: BLE001 - any failed read is not "nothing going"
        return {f"{UNREADABLE} ({exc})"}


start = time.monotonic()
going = busy()
while going and time.monotonic() - start + poll <= bound:
    time.sleep(poll)
    going = busy()
waited = int(time.monotonic() - start)
if going:
    print(f"still going after {waited}s, the run goes ahead: {', '.join(sorted(going))}")
elif waited:
    print(f"ended after {waited}s")
else:
    print("none going")
