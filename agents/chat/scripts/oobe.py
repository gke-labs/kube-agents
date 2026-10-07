#!/usr/bin/env python3
"""Dispatcher for the ``oobe`` cron job: the work an install does once, on first boot.

The design is ``docs/designs/oobe.md``. Today the job has one stage, the first-run
audits: once the onboarding inventory scan has settled, run the four fleet audits
that would otherwise wait for their schedules (the next 06:20 UTC, the next Monday
for cost), one after another. The bootstrap scan and delivery jobs still run beside it.

The stage fires when the scan's ranking card has finished, read from the board. It
does not wait for delivery, which needs a human message, and it does not look for
the report file, which is on the sandbox's volume when the sandbox is on. A scan
that has not settled by the hand-off's own deadline plus ``RANKING_ALLOWANCE_SECONDS``
after its sweep was filed fires anyway.

The audits run as a chain, in ``FIRST_RUN_AUDITS`` order: the next is marked due only
once the previous one's run has ended, so no two overlap. The schedule staggers them
for the same reason; four started together on a small install's gateway pod put it
under memory pressure for the best part of an hour. Each is marked due on the
Platform Agent's roster with Hermes' ``cron.jobs.trigger_job``, so the next
``profile-cron-tick`` runs it through its schedule's own path. Not ``hermes cron
run``: that CLI runs the whole job synchronously in the calling process.
``.oobe_audits_fired`` records each one before it is marked, and the mark awaiting
its run, so a run killed partway marks nothing twice: marking an audit due again after it has run
starts a second full run. With no GitOps repository configured every audit fails
before it reads anything, so the stage records the skip and marks none.

``trigger_job`` also sets a job's ``enabled`` back to true, so an audit an operator
has disabled or paused is left alone rather than started.

An install whose sweep was filed more than ``NEW_INSTALL_SECONDS`` ago while the
stage has started nothing is not new: it onboarded before this job existed but
never reached delivery, so the entrypoint could not tell. Its audits run on their
schedules. A scan still unsettled after a day (a fleet of hundreds of clusters)
is caught by the same rule.

Once the stage is done, the next run removes the job. Stdout stays empty: the job
delivers locally and never speaks to the user.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import bootstrap_handoff  # beside this script in the pod

OOBE_JOB_ID = "oobe"
AUDITS_MARKER = ".oobe_audits_fired"
# Written by bootstrap_scan_gate.py when it files the sweep: `task_id=` and `filed_at=` lines.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
MARKER_TASK_ID = "task_id"
MARKER_FILED_AT = "filed_at"
# The hand-off's marker (bootstrap_handoff._record): `sweep=`, and `task_id=` its ranking card.
HANDOFF_SWEEP = "sweep"

# The four audits #1866 names, by their ids in agents/platform/cron/jobs.json, in the
# order it chains them: cost, security, reliability, capacity.
FIRST_RUN_AUDITS = (
    "fleet-wide-cost-analysis",
    "compliance-audit",
    "obtainability-audit",
    "stockout-prevention",
)
EXECUTIONS_DB = "executions.db"
# A run row in either is still going; any other status has ended.
IN_FLIGHT_STATUSES = ("claimed", "running")
# The cron store's skip ledger (deploy/docker/patches/cron_skip_ledger.py) writes a skipped row with
# its reason. A mark skipped because the audit was already running is answered by that run; a skip
# for any other reason (a shutdown, a lost claim) ran nothing and is read as no claim at all.
SKIPPED_STATUS = "skipped"
SKIP_REASON_COLUMN = "skip_reason"
SKIP_ALREADY_RUNNING = "already_running_elsewhere"
PLATFORM_PROFILE = "platform"
PROFILES_DIR = "profiles"
CRON_DIR = "cron"
ROSTER_FILE = "jobs.json"
# Hermes' pause marker on a job record (cron.jobs: is_job_runnable).
PAUSED_STATE = "paused"

# Without the hand-off's own record of its ranking card (an install the hand-off has not reached,
# or the eval stack's stand-in), the card is found by key. Hermes retries it in place; a re-run by
# hand adds a suffix (bootstrap_onboarding/README.md), so the prefix counts too.
PRIORITIZE_RETRY_PATTERN = bootstrap_handoff.PRIORITIZE_KEY + "-%"
# The statuses the hand-off itself counts as settled, less blocked and triage: a person may still
# unblock a card, and the fallback covers one nobody does.
FINISHED_STATUSES = (
    bootstrap_handoff.DONE,
    bootstrap_handoff.FAILED,
    bootstrap_handoff.CANCELLED,
    bootstrap_handoff.ARCHIVED,
)

# The fallback waits out the hand-off's own deadline for this sweep's cluster cards
# (bootstrap_handoff.deadline), after which it files the ranking card, plus this long for the
# ranking card to finish. A shorter wait would start the audits beside the ranking card on a
# large fleet, which is what waiting for the scan avoids.
RANKING_ALLOWANCE_SECONDS = 30 * 60
# Far past any fallback: a sweep this old was filed before the job existed.
NEW_INSTALL_SECONDS = 24 * 60 * 60
SECONDS_PER_MINUTE = 60
TRIGGER_TIMEOUT_SECONDS = 30
# Marks one job due and exits non-zero when the store does not have it. Run with
# HERMES_HOME set to the Platform Agent's home, which is where cron.jobs finds its store.
TRIGGER_SCRIPT = "import sys\nfrom cron.jobs import trigger_job\nsys.exit(0 if trigger_job(sys.argv[1]) else 3)\n"
# A mark trigger_job refuses, or one never claimed, is retried this many times; without a
# bound the stage would never finish and the job never leave.
MAX_TRIGGER_ATTEMPTS = 5
# A marked audit is claimed on the next profile-cron-tick, a minute or two later; past this
# with no run, the mark is counted as a failed attempt and made again.
START_LIMIT_SECONDS = 10 * 60
# Several times the longest audit run (9-15 minutes, #985). A run still going past it, or a
# row a gateway restart left at running, does not hold the chain any longer.
RUN_LIMIT_SECONDS = 60 * 60

STATE_DONE = "done"
STATE_FIRED = "fired"
STATE_ATTEMPTS = "attempts"
STATE_GAVE_UP = "gave_up"
STATE_HELD = "held"
STATE_CURRENT = "current"
# When each audit was marked due, so the run that answers a mark can be told from a scheduled one.
STATE_MARKS = "marks"
CURRENT_JOB = "job"
CURRENT_MARKED_AT = "marked_at"
STATE_SKIPPED = "skipped"
STATE_REASON = "reason"
STATE_AT = "at"
SKIP_NO_REPOSITORY = "no GitOps repository is configured"
SKIP_NOT_NEW = "the onboarding sweep was filed before this job existed"
HOLD_MISSING = "not on the Platform Agent's roster"
HOLD_DISABLED = "disabled"
HOLD_PAUSED = "paused"
DEFAULT_HOME = "/opt/data"
TMP_SUFFIX = ".tmp"


def _log(message: str) -> None:
    sys.stderr.write(f"oobe: {message}\n")


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", DEFAULT_HOME))


def read_state(data_dir: Path) -> dict:
    try:
        state = json.loads((data_dir / AUDITS_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(data_dir: Path, state: dict) -> None:
    """Replace the marker whole, so a run killed mid-write leaves the previous one."""
    target = data_dir / AUDITS_MARKER
    tmp = target.with_name(target.name + TMP_SUFFIX)
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(target)


def scan_filed(data_dir: Path) -> tuple[str, float] | None:
    """The sweep card's id and when it was filed, or None before the scan has started.

    Read with the hand-off's own parser, which takes a marker typed by hand as `key = value`.
    """
    marker = data_dir / SCAN_FILED_MARKER
    if not marker.is_file():
        return None
    fields = bootstrap_handoff._read_marker(marker)
    try:
        filed_at = float(fields.get(MARKER_FILED_AT, ""))
    except ValueError:
        # Hand-written or truncated: the marker's own age is the next best clock.
        filed_at = marker.stat().st_mtime
    return fields.get(MARKER_TASK_ID, ""), filed_at


def board_path(data_dir: Path) -> Path:
    return bootstrap_handoff._board_path(data_dir)


def handoff_ranking(data_dir: Path, sweep_id: str) -> str | None:
    """The ranking card the hand-off filed for this sweep, its ``NO_RANKING``, or None if it has not."""
    fields = bootstrap_handoff._read_marker(data_dir / bootstrap_handoff.HANDOFF_MARKER)
    if not sweep_id or fields.get(HANDOFF_SWEEP) != sweep_id:
        return None
    return fields.get(MARKER_TASK_ID) or None


def read_scan(board: Path, sweep_id: str, ranking: str | None = None) -> tuple[bool, int] | None:
    """Whether this sweep's ranking cards have all finished, and how many cluster cards it has.

    ``ranking`` is the card the hand-off recorded for this sweep. Without it, every card under the
    ranking key created after the sweep card counts, so an earlier run's cards, left on the board
    after onboarding was re-armed, cannot fire this one. None when the board cannot say.
    """
    if not sweep_id:
        return None
    try:
        conn = sqlite3.connect(
            f"file:{board}?mode=ro", uri=True, timeout=bootstrap_handoff.SQLITE_BUSY_TIMEOUT_SECONDS
        )
    except sqlite3.Error as e:
        _log(f"cannot open the board: {e}")
        return None
    try:
        row = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (sweep_id,)).fetchone()
        if row is None:
            return None
        if ranking is not None:
            statuses = [status for (status,) in conn.execute("SELECT status FROM tasks WHERE id = ?", (ranking,))]
        else:
            statuses = [
                status
                for (status,) in conn.execute(
                    "SELECT status FROM tasks WHERE (idempotency_key = ? OR idempotency_key LIKE ?) "
                    "AND created_at >= ?",
                    (bootstrap_handoff.PRIORITIZE_KEY, PRIORITIZE_RETRY_PATTERN, row[0]),
                ).fetchall()
            ]
        (clusters,) = conn.execute(
            "SELECT count(*) FROM tasks WHERE idempotency_key LIKE ? AND created_at >= ?",
            (bootstrap_handoff.CLUSTER_KEY_PREFIX + "%", row[0]),
        ).fetchone()
    except sqlite3.Error as e:
        _log(f"cannot read the board: {e}")
        return None
    finally:
        conn.close()
    return bool(statuses) and all(status in FINISHED_STATUSES for status in statuses), clusters


def fallback_seconds(clusters: int) -> int:
    """How long after the sweep was filed the stage stops waiting for the ranking card."""
    hand_off = bootstrap_handoff.DEADLINE_SECONDS + bootstrap_handoff.DEADLINE_PER_CARD_SECONDS * clusters
    return hand_off + RANKING_ALLOWANCE_SECONDS


def scan_settled(data_dir: Path, now: float) -> bool:
    filed = scan_filed(data_dir)
    if filed is None:
        return False
    sweep_id, filed_at = filed
    ranking = handoff_ranking(data_dir, sweep_id)
    if ranking == bootstrap_handoff.NO_RANKING:
        # No cluster was audited: the hand-off wrote the report itself and filed no ranking card.
        return True
    scan = read_scan(board_path(data_dir), sweep_id, ranking)
    if scan is not None and scan[0]:
        return True
    wait = fallback_seconds(scan[1] if scan is not None else 0)
    if now - filed_at >= wait:
        _log(f"the scan has not settled {wait // SECONDS_PER_MINUTE} minutes after its sweep was filed; starting the audits anyway")
        return True
    return False


def managed_repositories() -> list[str]:
    """The GitOps repositories audits publish to. Raises when the list cannot be read."""
    import gitops_workspace  # beside this script in the pod

    return gitops_workspace.get_managed_github_repos()


def trigger(job_id: str, data_dir: Path) -> bool:
    """Mark one Platform Agent job due for the next profile-cron-tick.

    A subprocess, because cron.jobs takes its store from HERMES_HOME, which here is the
    Chat Agent's. The interpreter is the gateway's own, the one running this script.
    """
    env = {**os.environ, "HERMES_HOME": str(data_dir / PROFILES_DIR / PLATFORM_PROFILE)}
    try:
        done = subprocess.run(
            [sys.executable, "-c", TRIGGER_SCRIPT, job_id],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TRIGGER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"cannot start {job_id}: {e}")
        return False
    if done.returncode != 0:
        _log(f"cannot start {job_id} (exit {done.returncode}): {(done.stderr or done.stdout or '').strip()}")
        return False
    _log(f"marked {job_id} due")
    return True


def audit_holds(data_dir: Path) -> dict[str, str] | None:
    """Audits not to start, each with why: absent from the roster, disabled or paused.

    None when the Platform Agent's roster cannot be read.
    """
    roster = data_dir / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / ROSTER_FILE
    try:
        stored = json.loads(roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _log(f"cannot read {roster}: {e}")
        return None
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    by_id = {job.get("id"): job for job in jobs if isinstance(job, dict)}
    holds = {}
    for job_id in FIRST_RUN_AUDITS:
        job = by_id.get(job_id)
        if job is None:
            holds[job_id] = HOLD_MISSING
        elif not job.get("enabled", True):
            holds[job_id] = HOLD_DISABLED
        elif job.get("state") == PAUSED_STATE or job.get("paused_at"):
            holds[job_id] = HOLD_PAUSED
    return holds


def retire() -> None:
    """Remove this job in-process. Its runs print nothing, so no output is lost with it."""
    try:
        from cron.jobs import remove_job  # type: ignore import-not-found
    except Exception:  # noqa: BLE001 - outside the gateway
        return
    try:
        remove_job(OOBE_JOB_ID)
    except Exception as e:  # noqa: BLE001 - the next run tries again
        _log(f"could not remove the {OOBE_JOB_ID} job: {e}")


def _runs(data_dir: Path, jobs: tuple[str, ...]) -> list[tuple[str, str, float]]:
    """``(job, status, claimed_at)`` for every run of ``jobs`` in the Platform Agent's cron store.

    An unreadable store reads as no runs.
    """
    ledger = data_dir / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / EXECUTIONS_DB
    if not ledger.is_file():
        return []
    placeholders = ",".join("?" * len(jobs))
    try:
        conn = sqlite3.connect(
            f"file:{ledger}?mode=ro", uri=True, timeout=bootstrap_handoff.SQLITE_BUSY_TIMEOUT_SECONDS
        )
        try:
            has_reason = any(row[1] == SKIP_REASON_COLUMN for row in conn.execute("PRAGMA table_info(executions)"))
            rows = conn.execute(
                f"SELECT job_id, status, claimed_at, {SKIP_REASON_COLUMN if has_reason else 'NULL'} "
                f"FROM executions WHERE job_id IN ({placeholders}) AND claimed_at IS NOT NULL ORDER BY claimed_at",
                jobs,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"cannot read {ledger}: {e}")
        return []
    runs = []
    for job_id, status, claimed, skip_reason in rows:
        if status == SKIPPED_STATUS and skip_reason != SKIP_ALREADY_RUNNING:
            continue
        try:
            runs.append((job_id, status, datetime.fromisoformat(claimed).timestamp()))
        except (TypeError, ValueError):
            continue
    return runs


def run_status(data_dir: Path, job_id: str, since: float) -> str | None:
    """The status of the first run of ``job_id`` claimed at or after ``since``, or None if there is none."""
    after = [status for _job, status, claimed in _runs(data_dir, (job_id,)) if claimed >= since]
    return after[0] if after else None


def audits_in_flight(data_dir: Path, now: float) -> set[str]:
    """The first-run audits with a run still going, scheduled or marked, younger than the run limit."""
    return {
        job
        for job, status, claimed in _runs(data_dir, FIRST_RUN_AUDITS)
        if status in IN_FLIGHT_STATUSES and now - claimed < RUN_LIMIT_SECONDS
    }


def advance_chain(data_dir: Path, state: dict, now: float) -> dict:
    """Move the chain one step: wait on the audit in flight, or mark the next one due.

    The returned state is also written to the marker, and says whether the stage is done.
    """
    fired = list(state.get(STATE_FIRED, []))
    attempts = dict(state.get(STATE_ATTEMPTS, {}))
    gave_up = list(state.get(STATE_GAVE_UP, []))
    held = dict(state.get(STATE_HELD, {}))
    current = state.get(STATE_CURRENT)
    marks = dict(state.get(STATE_MARKS, {}))

    def save(done: bool = False) -> dict:
        new_state = {
            STATE_FIRED: fired, STATE_ATTEMPTS: attempts, STATE_GAVE_UP: gave_up, STATE_HELD: held,
            STATE_CURRENT: current, STATE_MARKS: marks, STATE_AT: now, STATE_DONE: done,
        }
        write_state(data_dir, new_state)
        return new_state

    pending = [a for a in FIRST_RUN_AUDITS if a not in fired and a not in gave_up and a not in held]
    if current:
        job_id, marked_at = current[CURRENT_JOB], current[CURRENT_MARKED_AT]
        status = run_status(data_dir, job_id, marked_at)
        if status is None:
            if now - marked_at < START_LIMIT_SECONDS:
                return save()
            # Never claimed: count it as a failed start and mark it again, or give up on it.
            attempts[job_id] = attempts.get(job_id, 0) + 1
            fired.remove(job_id)
            current = None
            if attempts[job_id] >= MAX_TRIGGER_ATTEMPTS:
                _log(f"giving up on {job_id}: never started after {MAX_TRIGGER_ATTEMPTS} marks; it runs on its own schedule")
                gave_up.append(job_id)
            return save()
        if not pending:
            # The last audit has started, or a scheduled run of it was already going; nothing is
            # left to mark.
            return save(done=True)
        current = None

    busy = audits_in_flight(data_dir, now)
    if busy:
        # One of the four has a run going, whatever started it, including the run a mark found
        # already going; the chain waits its turn, as the schedule does.
        return save()
    holds = audit_holds(data_dir)
    for job_id in pending:
        if job_id in marks and run_status(data_dir, job_id, marks[job_id]) is not None:
            # A mark the scheduler claimed only after it was counted as never started: that run
            # was this audit's, and marking it again would start a second.
            fired.append(job_id)
            continue
        if holds is not None and job_id in holds:
            _log(f"not starting {job_id}: {holds[job_id]}")
            held[job_id] = holds[job_id]
            continue
        if holds is not None:
            # Recorded before it is made: a restart between the two leaves a record of a mark that
            # may not exist, which the start limit makes again, rather than a mark with no record,
            # which would be made a second time once its run ended.
            fired.append(job_id)
            marks[job_id] = now
            current = {CURRENT_JOB: job_id, CURRENT_MARKED_AT: now}
            save()
            if trigger(job_id, data_dir):
                return save()
            # The time stays in `marks`: a mark refused only after the store took it is adopted
            # once its run turns up, by the check at the top of this loop.
            fired.remove(job_id)
            current = None
        attempts[job_id] = attempts.get(job_id, 0) + 1
        if attempts[job_id] >= MAX_TRIGGER_ATTEMPTS:
            _log(f"giving up on {job_id} after {MAX_TRIGGER_ATTEMPTS} attempts; it runs on its own schedule")
            gave_up.append(job_id)
            continue
        return save()
    return save(done=True)


def main(data_dir: Path | None = None, now: float | None = None) -> int:
    data_dir = data_dir or _data_dir()
    now = time.time() if now is None else now
    state = read_state(data_dir)
    if state.get(STATE_DONE):
        retire()
        return 0
    if not state:
        # Not started yet: the checks that decide whether, and when, the chain starts.
        filed = scan_filed(data_dir)
        if filed is not None and now - filed[1] >= NEW_INSTALL_SECONDS:
            _log(f"not starting the first-run audits: {SKIP_NOT_NEW}")
            write_state(data_dir, {STATE_DONE: True, STATE_SKIPPED: True, STATE_REASON: SKIP_NOT_NEW, STATE_AT: now})
            return 0
        if not scan_settled(data_dir, now):
            return 0
        try:
            repositories = managed_repositories()
        except Exception as e:  # noqa: BLE001 - an unreadable list is retried, not taken as empty
            _log(f"cannot read the managed repositories: {e}")
            return 0
        if not repositories:
            _log(f"not starting the first-run audits: {SKIP_NO_REPOSITORY}")
            write_state(data_dir, {STATE_DONE: True, STATE_SKIPPED: True, STATE_REASON: SKIP_NO_REPOSITORY, STATE_AT: now})
            return 0
    advance_chain(data_dir, state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
