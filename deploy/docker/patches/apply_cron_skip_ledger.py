#!/usr/bin/env python3
"""Wire tools/cron_skip_ledger.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Fourteen anchored
replacements across four files, with the same guarantee as every other patch in
that Dockerfile: each anchor must be found the exact number of times expected,
each edited file must still parse, and anything else fails the build loudly
rather than shipping a half-patched image.

**Must run after ``apply_cron_tick_lock_scope.py``.** Two of the anchors here
are text that patch inserts — the cross-process ``_job_locks.claim`` guard in
``tick`` — so applying this one first would fail on a missing anchor rather
than silently mis-apply, but the ordering is still load-bearing and the
Dockerfile records it.

Why each edit is needed is documented in the module docstring of
``deploy/docker/patches/cron_skip_ledger.py``. Usage::

    python3 apply_cron_skip_ledger.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

# Two of the anchors below are text that apply_cron_tick_lock_scope.py inserts,
# so "not found" here has a second, likelier cause than upstream drift: the
# Dockerfile ran the two patches out of order. Say so in the failure.
DRIFT_NOTE = (
    "Upstream Hermes changed (or apply_cron_tick_lock_scope.py has not run "
    "yet) — re-derive the anchor before bumping the base image."
)

# --- cron/executions.py: a fifth terminal status, and per-job retention -----

EXEC_CONSTANTS = '''MAX_TERMINAL_EXECUTIONS = 1000
_TERMINAL_STATES = ("completed", "failed", "unknown")
'''

EXEC_CONSTANTS_PATCHED = '''# kube-agents patch: retention is per job rather than global, and the ledger
# has a fifth terminal status. Both live in tools/cron_skip_ledger.py; the
# names are imported rather than restated so the prune SQL, the CHECK
# constraint and these module-level constants cannot drift apart.
from tools.cron_skip_ledger import (  # noqa: E402
    MAX_TERMINAL_EXECUTIONS,
    MAX_TERMINAL_EXECUTIONS_PER_JOB,
    MIN_TERMINAL_EXECUTIONS_PER_JOB,
    TERMINAL_STATES as _TERMINAL_STATES,
    ensure_schema as _ensure_skip_schema,
    normalize_reason as _normalize_skip_reason,
    prune_terminal_executions as _prune_terminal_executions,
)

'''

EXEC_CREATE_TABLE = '''    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
'''

# The CREATE TABLE gains 'skipped' and skip_reason so a fresh ledger is born
# migrated. ensure_schema() then sits between the table and the indexes on
# purpose: it only fires on a ledger that predates this patch, and its rebuild
# drops the indexes along with the old table, so the CREATE INDEX statements
# below must run after it.
EXEC_CREATE_TABLE_PATCHED = '''    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown','skipped')),
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT,
             skip_reason TEXT
           )"""
    )
    # kube-agents patch: bring a pre-existing ledger up to the schema above.
    # SQLite cannot alter a CHECK constraint in place, so this rebuilds the
    # table; it is idempotent, it never raises, and it must run before the
    # indexes below because its rebuild drops them with the old table. See
    # tools/cron_skip_ledger.py.
    _ensure_skip_schema(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
'''

EXEC_PRUNE = '''def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
    )
'''

EXEC_PRUNE_PATCHED = '''def _prune_unlocked(conn: sqlite3.Connection) -> None:
    """Retain history per job, not per board.

    kube-agents patch. The upstream body kept the newest
    MAX_TERMINAL_EXECUTIONS rows across ALL jobs, so the busiest job evicted
    every other job's evidence: on the live Chat Agent board the cap was
    saturated at 1000/1000 with 980 of those rows belonging to one minute-ly
    ticker, leaving a 16.7-hour horizon for the daily watchdogs sharing it.
    See tools/cron_skip_ledger.py for the replacement rule and the reasoning
    behind its three limits.
    """
    _prune_terminal_executions(conn)
'''

EXEC_NEW_FUNCTIONS_ANCHOR = "def recover_interrupted_executions() -> int:"

EXEC_NEW_FUNCTIONS_PATCHED = '''def record_skipped_execution(
    job_id: str, *, source: str, reason: str, detail: str = "",
) -> Optional[Dict[str, Any]]:
    """Persist an occurrence that came due and did not run.

    kube-agents patch. Written straight into a terminal state because there is
    no claim to transition from: the decision not to run is taken before
    dispatch, which is precisely why upstream had nowhere to put it. See
    tools/cron_skip_ledger.py for the five ways this happens.

    ``pid`` and ``process_started_at`` describe the process that made the
    decision, not a runner — they are what tells two racing tickers apart once
    a board starts filling with ``already_running_elsewhere``.

    ``started_at`` stays NULL and ``finished_at`` equals ``claimed_at``, so
    ``cron_health._duration_ms`` reports 0 instead of inventing a runtime, and
    any reader keying off ``started_at`` can still see that nothing ran.
    """
    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    code = _normalize_skip_reason(reason)
    text = str(detail).strip() or f"Occurrence skipped: {code}."
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, finished_at, error, skip_reason)
               VALUES (?, ?, ?, ?, ?, ?, 'skipped', ?, ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), now, now, text, code),
        )
        _prune_unlocked(conn)
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
    record = _record(row)
    _emit_execution_state(record)
    return record


def skip_execution(
    execution_id: str, *, reason: str, detail: str = "",
) -> Optional[Dict[str, Any]]:
    """Close an already-claimed attempt as skipped rather than failed.

    kube-agents patch. For the one path that claims first and only then learns
    the occurrence must not run: a finite one-shot whose dispatch budget was
    already spent. Upstream closed that as ``failed``, which both inflates the
    failure rate cron_health derives and libels an at-most-once guarantee that
    is working exactly as designed. Terminal-once, like ``finish_execution``.
    """
    now = _hermes_now().isoformat()
    code = _normalize_skip_reason(reason)
    text = str(detail).strip() or f"Occurrence skipped: {code}."
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='skipped', finished_at=?, error=?,
                   skip_reason=?
               WHERE id=? AND status IN ('claimed','running')""",
            (now, text, code, execution_id),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        record = _record(conn.execute(
            "SELECT * FROM executions WHERE id=?", (execution_id,)
        ).fetchone())
    _emit_execution_state(record)
    return record


''' + EXEC_NEW_FUNCTIONS_ANCHOR

# --- cron/scheduler.py: record the four ways a due occurrence is dropped ----

SCHED_IMPORT = (
    "from cron.executions import create_execution, finish_execution, "
    "mark_execution_running"
)

SCHED_IMPORT_PATCHED = (
    "from cron.executions import (\n"
    "    create_execution,\n"
    "    finish_execution,\n"
    "    mark_execution_running,\n"
    "    skip_execution,\n"
    ")\n"
    "\n"
    "# kube-agents patch: a due occurrence that never ran used to leave nothing\n"
    "# behind but a log line. See tools/cron_skip_ledger.py.\n"
    "from tools.cron_skip_ledger import (\n"
    "    SKIP_ALREADY_RUNNING,\n"
    "    SKIP_ALREADY_RUNNING_ELSEWHERE,\n"
    "    SKIP_DISPATCH_CLAIM_REJECTED,\n"
    "    SKIP_INTERPRETER_SHUTDOWN,\n"
    "    record_skip,\n"
    ")"
)

# advance_next_runs() has already moved next_run_at for the whole due set by
# the time any of the three guards below runs, so each one drops the
# occurrence permanently rather than deferring it.
SCHED_SHUTDOWN_GUARD = '''            if _interpreter_shutting_down():
                logger.warning(
                    "Job '%s' not dispatched — interpreter is shutting down",
                    job.get("name", job_id),
                )
                return None
'''

SCHED_SHUTDOWN_GUARD_PATCHED = '''            if _interpreter_shutting_down():
                logger.warning(
                    "Job '%s' not dispatched — interpreter is shutting down",
                    job.get("name", job_id),
                )
                # kube-agents patch: next_run_at was advanced for this whole
                # due set before dispatch, so the occurrence is gone, not
                # deferred. See tools/cron_skip_ledger.py.
                record_skip(
                    job_id,
                    source="builtin",
                    reason=SKIP_INTERPRETER_SHUTDOWN,
                    detail=(
                        "Interpreter began finalizing before this occurrence "
                        "could be dispatched; the job was not run and its "
                        "schedule had already advanced."
                    ),
                )
                return None
'''

# The ledger write is deliberately moved OUT of the _running_lock critical
# section. That lock is held by every dispatching thread in the tick; a SQLite
# write behind a 5s busy timeout inside it would serialise the whole dispatch
# pass on the ledger.
SCHED_RUNNING_GUARD = '''            with _running_lock:
                if job_id in _running_job_ids:
                    logger.info("Job '%s' already running — skipping", job.get("name", job_id))
                    return None
                _running_job_ids.add(job_id)
'''

SCHED_RUNNING_GUARD_PATCHED = '''            with _running_lock:
                _already_running = job_id in _running_job_ids
                if not _already_running:
                    _running_job_ids.add(job_id)
            if _already_running:
                logger.info("Job '%s' already running — skipping", job.get("name", job_id))
                # kube-agents patch: recorded outside _running_lock on purpose
                # — every dispatching thread in this tick takes that lock, and
                # a ledger write behind a 5s busy timeout inside it would
                # serialise the whole dispatch pass. See
                # tools/cron_skip_ledger.py.
                record_skip(
                    job_id,
                    source="builtin",
                    reason=SKIP_ALREADY_RUNNING,
                    detail=(
                        "A previous run of this job was still in flight in "
                        "this process when the occurrence came due; the "
                        "occurrence was dropped, not queued."
                    ),
                )
                return None
'''

# Inserted by apply_cron_tick_lock_scope.py — this applier must run after it.
SCHED_JOB_LOCK_GUARD = '''            _job_lock = _job_locks.claim(job_id)
            if _job_lock is None:
                logger.info(
                    "Job '%s' already running in another process — skipping",
                    job.get("name", job_id),
                )
                with _running_lock:
                    _running_job_ids.discard(job_id)
                return None
'''

SCHED_JOB_LOCK_GUARD_PATCHED = '''            _job_lock = _job_locks.claim(job_id)
            if _job_lock is None:
                logger.info(
                    "Job '%s' already running in another process — skipping",
                    job.get("name", job_id),
                )
                with _running_lock:
                    _running_job_ids.discard(job_id)
                # kube-agents patch: distinct from SKIP_ALREADY_RUNNING because
                # the remedy is distinct — that one says the job outruns its
                # own period, this one says two tickers are racing for the same
                # profile. See tools/cron_skip_ledger.py.
                record_skip(
                    job_id,
                    source="builtin",
                    reason=SKIP_ALREADY_RUNNING_ELSEWHERE,
                    detail=(
                        "Another process held this job's run lock when the "
                        "occurrence came due; the occurrence was dropped, not "
                        "queued."
                    ),
                )
                return None
'''

SCHED_DISPATCH_CLAIM = '''            finish_execution(
                execution_id,
                success=False,
                error="Dispatch claim rejected; execution was not started.",
            )
            return True  # not an error — already handled/removed
'''

SCHED_DISPATCH_CLAIM_PATCHED = '''            # kube-agents patch: a one-shot that has already spent its dispatch
            # budget is an at-most-once guarantee working, not a failure. Closed
            # as failed it inflated the failure rate cron_health derives from
            # this ledger. See tools/cron_skip_ledger.py.
            skip_execution(
                execution_id,
                reason=SKIP_DISPATCH_CLAIM_REJECTED,
                detail="Dispatch claim rejected; execution was not started.",
            )
            return True  # not an error — already handled/removed
'''

# --- cron/jobs.py: the outage that hides itself -----------------------------

JOBS_CATCH_UP = '''                        record_catch_up_occurrence()
                        # Fall through to due.append(job) — execute once now
'''

JOBS_CATCH_UP_PATCHED = '''                        record_catch_up_occurrence()
                        # kube-agents patch: record_catch_up_occurrence() bumps
                        # one profile-wide integer in a file — no job id, no
                        # window, no count — so a gateway outage long enough to
                        # blow the grace window leaves a daily watchdog looking
                        # as though it ran on schedule. One ledger row per gap,
                        # not per lost occurrence: a three-hour outage of a
                        # minute-ly job would otherwise write 180 rows and evict
                        # the history somebody is reading. The catch-up fire
                        # itself is recorded separately by the ticker.
                        # See tools/cron_skip_ledger.py.
                        try:
                            from tools.cron_skip_ledger import (
                                SKIP_MISSED_WINDOW,
                                missed_window_detail,
                                record_skip,
                            )

                            record_skip(
                                job["id"],
                                source="builtin",
                                reason=SKIP_MISSED_WINDOW,
                                detail=missed_window_detail(
                                    schedule, next_run_dt, now, grace
                                ),
                            )
                        except Exception:
                            logger.debug(
                                "could not record missed window for job %s",
                                job.get("id"),
                                exc_info=True,
                            )
                        # Fall through to due.append(job) — execute once now
'''

# --- agent/monitoring/cron_health.py: do not launder a skip into "unknown" --

HEALTH_STATUSES = (
    '_KNOWN_STATUSES = {"claimed", "running", "completed", "failed", "unknown"}'
)

HEALTH_STATUSES_PATCHED = (
    "# kube-agents patch: without 'skipped' here, project_execution_event\n"
    "# coerces every skip to 'unknown' — the state that means an owner process\n"
    "# died mid-run. That corrupts a real signal on top of hiding a new one.\n"
    "# See tools/cron_skip_ledger.py.\n"
    '_KNOWN_STATUSES = {"claimed", "running", "completed", "failed", "unknown", "skipped"}'
)

HEALTH_IMPORT = "from cron.scheduler import get_running_job_ids"

HEALTH_IMPORT_PATCHED = (
    "from cron.scheduler import get_running_job_ids\n"
    "\n"
    "# kube-agents patch: see tools/cron_skip_ledger.py\n"
    "from tools.cron_skip_ledger import normalize_reason as _normalize_skip_reason"
)

HEALTH_ERROR_CLASS = '''        error_class=(
            classify_cron_error(record.get("error"))
            if status in {"failed", "unknown"}
            else None
        ),
'''

HEALTH_ERROR_CLASS_PATCHED = '''        error_class=(
            # kube-agents patch: a skip already carries a machine-readable,
            # content-free reason, so project that instead of running the
            # free-text classifier over it. See tools/cron_skip_ledger.py.
            _normalize_skip_reason(record.get("skip_reason"))
            if status == "skipped"
            else classify_cron_error(record.get("error"))
            if status in {"failed", "unknown"}
            else None
        ),
'''

HEALTH_FLUSH = '''        if event.status in {"completed", "failed", "unknown"}:
            target.flush(timeout=1.0)
'''

HEALTH_FLUSH_PATCHED = '''        # kube-agents patch: 'skipped' is terminal, so it crosses the queue
        # barrier synchronously like the other terminal states — a skip
        # recorded moments before the gateway exits is exactly the one worth
        # keeping. See tools/cron_skip_ledger.py.
        if event.status in {"completed", "failed", "unknown", "skipped"}:
            target.flush(timeout=1.0)
'''

# (relative path, [(anchor, replacement, expected occurrences)])
PATCHES = (
    (
        "cron/executions.py",
        (
            (EXEC_CONSTANTS, EXEC_CONSTANTS_PATCHED, 1),
            (EXEC_CREATE_TABLE, EXEC_CREATE_TABLE_PATCHED, 1),
            (EXEC_PRUNE, EXEC_PRUNE_PATCHED, 1),
            (EXEC_NEW_FUNCTIONS_ANCHOR, EXEC_NEW_FUNCTIONS_PATCHED, 1),
        ),
    ),
    (
        "cron/scheduler.py",
        (
            (SCHED_IMPORT, SCHED_IMPORT_PATCHED, 1),
            (SCHED_SHUTDOWN_GUARD, SCHED_SHUTDOWN_GUARD_PATCHED, 1),
            (SCHED_RUNNING_GUARD, SCHED_RUNNING_GUARD_PATCHED, 1),
            (SCHED_JOB_LOCK_GUARD, SCHED_JOB_LOCK_GUARD_PATCHED, 1),
            (SCHED_DISPATCH_CLAIM, SCHED_DISPATCH_CLAIM_PATCHED, 1),
        ),
    ),
    (
        "cron/jobs.py",
        ((JOBS_CATCH_UP, JOBS_CATCH_UP_PATCHED, 1),),
    ),
    (
        "agent/monitoring/cron_health.py",
        (
            (HEALTH_STATUSES, HEALTH_STATUSES_PATCHED, 1),
            (HEALTH_IMPORT, HEALTH_IMPORT_PATCHED, 1),
            (HEALTH_ERROR_CLASS, HEALTH_ERROR_CLASS_PATCHED, 1),
            (HEALTH_FLUSH, HEALTH_FLUSH_PATCHED, 1),
        ),
    ),
)


def apply(root: Path) -> None:
    """Apply every patch under ``root``, or raise SystemExit with the reason."""
    for relative, edits in PATCHES:
        patch = patchlib.Patch(
            root, relative, prefix="cron_skip_ledger", note=DRIFT_NOTE
        )
        for anchor, replacement, expected in edits:
            patch.substitute(anchor, replacement, expected=expected)
        patch.commit(f"{len(edits)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
