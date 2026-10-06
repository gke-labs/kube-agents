#!/usr/bin/env python3
"""Dispatcher for the ``oobe`` cron job: the work an install does once, on first boot.

The design is ``docs/designs/oobe.md``. Today the job has one stage, the first-run
audits: once the onboarding inventory scan has settled, start the four fleet audits
that would otherwise wait for their schedules (the next 06:20 UTC, the next Monday
for cost). The bootstrap scan and delivery jobs still run beside it.

The stage fires when the scan's ranking card has finished, read from the board. It
does not wait for delivery, which needs a human message, and it does not look for
the report file, which is on the sandbox's volume when the sandbox is on. A scan
that has not settled ``FALLBACK_SECONDS`` after its sweep was filed fires anyway.

Each audit is started with ``hermes cron run`` on the Platform Agent's roster, which
marks it due for the next ``profile-cron-tick``; the audit then runs through its
schedule's own path. ``.oobe_audits_fired`` records each one started, so a retry
starts only the ones still missing: marking an audit due again after it has run
starts a second full run. With no GitOps repository configured every audit fails
before it reads anything, so the stage records the skip and starts none.

`hermes cron run` also sets a job's `enabled` back to true, so an audit an operator
has disabled or paused is left alone rather than started.

Once the stage is done, the next run removes the job. Stdout stays empty: the job
delivers locally and never speaks to the user.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import profile_cron_tick  # beside this script in the pod

OOBE_JOB_ID = "oobe"
AUDITS_MARKER = ".oobe_audits_fired"
# Written by bootstrap_scan_gate.py when it files the sweep: `task_id=` and `filed_at=` lines.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
MARKER_TASK_ID = "task_id"
MARKER_FILED_AT = "filed_at"

# The four audits #1866 names, by their ids in agents/platform/cron/jobs.json.
FIRST_RUN_AUDITS = (
    "compliance-audit",
    "obtainability-audit",
    "fleet-wide-cost-analysis",
    "stockout-prevention",
)
PLATFORM_PROFILE = "platform"
PROFILES_DIR = "profiles"
CRON_DIR = "cron"
ROSTER_FILE = "jobs.json"
# Hermes' pause marker on a job record (cron.jobs: is_job_runnable).
PAUSED_STATE = "paused"

# The ranking card the scan files last. A retry or a hand re-run uses this key with a
# suffix (inventory.md, step 5; bootstrap_onboarding/README.md), so the prefix counts too.
PRIORITIZE_KEY = "bootstrap-inventory-prioritize"
PRIORITIZE_RETRY_PATTERN = PRIORITIZE_KEY + "-%"
# Statuses a card does not leave on its own. Blocked and triage are not here: a person
# may unblock the card, and the fallback covers one nobody does.
FINISHED_STATUSES = ("done", "failed", "cancelled", "archived")
BOARD_FILE = "kanban.db"
SQLITE_BUSY_TIMEOUT_SECONDS = 10

# Past the hand-off's own one-hour deadline for the per-cluster cards, plus time for
# the ranking card. Also covers a sweep that audited no cluster, which files no ranking card.
FALLBACK_SECONDS = 90 * 60
CRON_RUN_TIMEOUT_SECONDS = 30
# A job id the roster does not have, or one disabled by hand, fails every attempt;
# without a bound the stage would never finish and the job never leave.
MAX_TRIGGER_ATTEMPTS = 5

STATE_DONE = "done"
STATE_FIRED = "fired"
STATE_ATTEMPTS = "attempts"
STATE_GAVE_UP = "gave_up"
STATE_HELD = "held"
STATE_SKIPPED = "skipped"
STATE_REASON = "reason"
STATE_AT = "at"
SKIP_NO_REPOSITORY = "no GitOps repository is configured"
TMP_SUFFIX = ".tmp"


def _log(message: str) -> None:
    sys.stderr.write(f"oobe: {message}\n")


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


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
    """The sweep card's id and when it was filed, or None before the scan has started."""
    try:
        text = (data_dir / SCAN_FILED_MARKER).read_text(encoding="utf-8")
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    task_id = fields.get(MARKER_TASK_ID, "").strip()
    try:
        filed_at = float(fields.get(MARKER_FILED_AT, ""))
    except ValueError:
        # Hand-written or truncated: the marker's own age is the next best clock.
        filed_at = (data_dir / SCAN_FILED_MARKER).stat().st_mtime
    return task_id, filed_at


def board_path(data_dir: Path) -> Path:
    try:
        from hermes_cli.kanban_db import kanban_db_path

        return Path(kanban_db_path())
    except Exception:  # noqa: BLE001 - outside the pod, or an older board
        return data_dir / BOARD_FILE


def ranking_settled(board: Path, sweep_id: str) -> bool:
    """True once this sweep's ranking cards exist and none can still change.

    Only cards created after the sweep card count, so an earlier run's ranking card,
    left on the board after onboarding was re-armed, cannot fire this one.
    """
    if not sweep_id:
        return False
    try:
        conn = sqlite3.connect(f"file:{board}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    except sqlite3.Error as e:
        _log(f"cannot open the board: {e}")
        return False
    try:
        row = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (sweep_id,)).fetchone()
        if row is None:
            return False
        statuses = [
            status
            for (status,) in conn.execute(
                "SELECT status FROM tasks WHERE (idempotency_key = ? OR idempotency_key LIKE ?) "
                "AND created_at >= ?",
                (PRIORITIZE_KEY, PRIORITIZE_RETRY_PATTERN, row[0]),
            ).fetchall()
        ]
    except sqlite3.Error as e:
        _log(f"cannot read the board: {e}")
        return False
    finally:
        conn.close()
    return bool(statuses) and all(status in FINISHED_STATUSES for status in statuses)


def scan_settled(data_dir: Path, now: float) -> bool:
    filed = scan_filed(data_dir)
    if filed is None:
        return False
    sweep_id, filed_at = filed
    if ranking_settled(board_path(data_dir), sweep_id):
        return True
    if now - filed_at >= FALLBACK_SECONDS:
        _log(f"the scan has not settled {FALLBACK_SECONDS // 60} minutes after its sweep was filed; starting the audits anyway")
        return True
    return False


def managed_repositories() -> list[str]:
    """The GitOps repositories audits publish to. Raises when the list cannot be read."""
    import gitops_workspace  # beside this script in the pod

    return gitops_workspace.get_managed_github_repos()


def trigger(job_id: str, data_dir: Path) -> bool:
    """Mark one Platform Agent job due, as `hermes cron run` does for an operator."""
    try:
        binary = profile_cron_tick.hermes_bin()
    except FileNotFoundError as e:
        _log(f"cannot start {job_id}: {e}")
        return False
    env = {**os.environ, "HERMES_HOME": str(data_dir / PROFILES_DIR / PLATFORM_PROFILE)}
    try:
        done = subprocess.run(
            [str(binary), "cron", "run", job_id],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=CRON_RUN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"cannot start {job_id}: {e}")
        return False
    if done.returncode != 0:
        _log(f"cannot start {job_id} (exit {done.returncode}): {(done.stderr or done.stdout or '').strip()}")
        return False
    _log(f"started {job_id}")
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
            holds[job_id] = "not on the Platform Agent's roster"
        elif not job.get("enabled", True):
            holds[job_id] = "disabled"
        elif job.get("state") == PAUSED_STATE or job.get("paused_at"):
            holds[job_id] = "paused"
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


def fire_audits(data_dir: Path, state: dict, now: float) -> dict:
    """Start every audit not yet started; the returned state says which and whether the stage is done."""
    fired = list(state.get(STATE_FIRED, []))
    attempts = dict(state.get(STATE_ATTEMPTS, {}))
    gave_up = list(state.get(STATE_GAVE_UP, []))
    held = dict(state.get(STATE_HELD, {}))
    holds = audit_holds(data_dir)

    def record() -> dict:
        return {STATE_FIRED: fired, STATE_ATTEMPTS: attempts, STATE_GAVE_UP: gave_up, STATE_HELD: held, STATE_AT: now}

    for job_id in FIRST_RUN_AUDITS:
        if job_id in fired or job_id in gave_up or job_id in held:
            continue
        if holds is not None and job_id in holds:
            _log(f"not starting {job_id}: {holds[job_id]}")
            held[job_id] = holds[job_id]
        elif holds is not None and trigger(job_id, data_dir):
            fired.append(job_id)
        else:
            attempts[job_id] = attempts.get(job_id, 0) + 1
            if attempts[job_id] >= MAX_TRIGGER_ATTEMPTS:
                _log(f"giving up on {job_id} after {MAX_TRIGGER_ATTEMPTS} attempts; it runs on its own schedule")
                gave_up.append(job_id)
        # After each audit, so a run killed partway does not start the same audit twice.
        write_state(data_dir, record())
    new_state = {**record(), STATE_DONE: all(job_id in fired or job_id in gave_up or job_id in held for job_id in FIRST_RUN_AUDITS)}
    write_state(data_dir, new_state)
    return new_state


def main(data_dir: Path | None = None, now: float | None = None) -> int:
    data_dir = data_dir or _data_dir()
    now = time.time() if now is None else now
    state = read_state(data_dir)
    if state.get(STATE_DONE):
        retire()
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
    fire_audits(data_dir, state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
