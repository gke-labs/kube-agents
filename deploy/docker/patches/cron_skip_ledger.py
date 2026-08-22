"""Record cron occurrences that were due and did not run, and keep history per job.

Installed into the image at ``/opt/hermes/tools/cron_skip_ledger.py`` and wired
into ``cron/executions.py``, ``cron/scheduler.py``, ``cron/jobs.py``,
``tools/cronjob_tools.py`` and ``agent/monitoring/cron_health.py`` by
``deploy/docker/Dockerfile``.

Two independent holes in the execution ledger, both of which make a watchdog
that stopped firing look exactly like a watchdog with nothing to report. That
is not hypothetical here: six governance watchdogs went a whole release without
firing and the ledger showed nothing wrong, because it had nothing to show.

Hole 1 — a skipped occurrence leaves no trace
---------------------------------------------
Upstream's ``executions`` table constrains ``status`` to
``('claimed','running','completed','failed','unknown')``. Every one of those
means *a run existed*. An occurrence that came due and was then dropped has no
representable state, so the scheduler does the only thing it can: it writes a
log line and returns. The log is rotated out of the pod in hours; the ledger is
on a PVC and is what anybody actually queries.

Measured on the live Chat Agent board on 2026-08-08 (``/opt/data/cron/
executions.db``): ``profile-cron-tick`` is a ``* * * * *`` job, and over a
999-minute window it holds 980 rows in 18 runs of consecutive minutes, every
gap ~120s. **Twenty occurrences of a minute-ly job left no record of any
kind** — and the gaps cluster on ``:00`` and ``:30``, the minutes when the
hourly and half-hourly jobs fire, so they are contention, not noise. Every one
of the board's 1000 rows reads ``completed``. There is no query against the
current schema that can find the twenty.

The five ways an occurrence is lost, all now recorded with a distinguishing
reason (the codes below are the vocabulary, and they are content-free so they
can also be projected to monitoring):

``already_running``
    An in-process dedup guard refused: ``job_id`` is still in the scheduler's
    ``_running_job_ids`` from an earlier fire. The occurrence is genuinely
    lost, because the schedule has *already* moved past it — in ``tick``,
    ``advance_next_runs`` moved ``next_run_at`` for the whole due set before
    dispatch begins; in ``_run_claimed_job``, ``claim_job_for_fire`` moved it
    for this job before the guard ran. This is the common case above: a job
    whose run outlives its own period.

``already_running_elsewhere``
    The cross-process mirror of the same thing — ``_job_locks.claim`` refused,
    so another *process* holds the per-job flock (see
    ``tools/cron_tick_lock_scope.py``). Distinct from ``already_running``
    because the remedy is different: one says the job is too slow for its
    schedule, the other says two tickers are racing for the same profile.

Each of those two is recorded at both of its sites — ``tick``'s dispatch loop
and ``_run_claimed_job``, the run half a manual fire and a background dispatch
share. The second pair is new at v2026.8.13: the split that gave ``run_one_job``
four callers put both guards after the fire claim, so a refusal there now costs
an occurrence where it used to cost nothing.

``interpreter_shutdown``
    The gateway began finalizing between ``advance_next_runs`` and
    ``pool.submit``. ``next_run_at`` has moved, nothing was dispatched.
    Deliberately separated from a real dispatch failure: it is expected during
    a rollout and should not read as a fault.

``missed_window``
    The one that hides an outage. ``cron/jobs.py:get_due_jobs`` fast-forwards a
    job whose ``next_run_at`` is older than ``_compute_grace_seconds`` and
    fires it exactly once, deliberately collapsing every accumulated
    occurrence. Upstream's only trace is
    ``record_catch_up_occurrence()`` — a single integer in a file, profile-wide,
    with no job id, no timestamp and no count. A four-hour gateway outage
    therefore leaves a daily watchdog looking as though it ran on schedule. One
    row per gap, not one per lost occurrence: a three-hour outage of a
    minute-ly job would otherwise write 180 rows and evict the very history
    somebody is trying to read.

``dispatch_claim_rejected``
    A finite one-shot whose ``repeat.times`` was already consumed. Upstream
    already writes a row for this, but as ``failed`` — which is wrong twice
    over: it inflates the failure count that ``cron_health`` derives, and it
    describes a correctly-enforced at-most-once guarantee as a fault.

Two near-misses deliberately *not* recorded, because neither loses an
occurrence: the tick-lock contention path (``Tick skipped — another instance
holds the lock``) returns before ``get_due_jobs``, so the other tick fires
those jobs; and the ``can_dispatch()`` drain gate likewise leaves the due set
untouched for the next tick. Recording either would manufacture false skips.
The timezone-repair branch in ``get_due_jobs`` is also left alone: in the
common case it re-anchors onto the same wall-clock time later in the same
period and nothing is lost, so a row there would be a false positive in every
case but a rare DST collision.

Hole 2 — pruning is global, so a chatty job evicts a quiet one
---------------------------------------------------------------
``_prune_unlocked`` keeps the newest ``MAX_TERMINAL_EXECUTIONS`` terminal rows
**across all jobs**, so the horizon is the same 16.7 hours for every job on the
board and it is the busiest job that sets it. The live Chat Agent board was
saturated at 1000/1000: ``profile-cron-tick`` owned 980 of those rows,
``cluster-agent-reconcile`` (hourly) 16, ``github-issue-resolver`` 4. Replaying
one more day of both cadences against a copy of that board shows what the cap
does next — under the global rule the hourly job is left with 17 rows and
``github-issue-resolver`` with none at all; under the rule below the hourly job
keeps 40 and the half-hourly one keeps its 4. A daily watchdog on that board
retains at best a single row, and the first thing anybody asks about a watchdog
that went quiet is "when did it last succeed?".

Retention is therefore per job. The numbers, against the two real rosters
(``agents/chat/defaults/cron/jobs.json``, ``agents/platform/cron/jobs.json``):

* ``MAX_TERMINAL_EXECUTIONS_PER_JOB = 1000``. Deliberately the same 1000
  upstream already chose — the cap was scoped wrong, not sized wrong. It is
  set by the fastest job on either roster, ``profile-cron-tick`` at
  ``* * * * *`` (1440 rows/day): 1000 rows is a 16.7-hour horizon, which is
  *exactly* the whole-board horizon observed today, so the busiest job loses
  nothing and every other job stops paying for it. The same 1000 rows buy
  ``cluster-agent-reconcile`` (``11 * * * *``) 41 days, ``github-issue-resolver``
  (``*/30 * * * *``) 20.8 days, the three daily audits ~2.7 years, and the
  three weekly ones roughly 19 — i.e. a daily watchdog is never pruned in
  practice, which is the point.

* ``MAX_TERMINAL_EXECUTIONS = 5000``, retained as an absolute ceiling and
  raised from 1000. A per-job rule alone is unbounded on the one axis that
  actually grows here: *distinct job ids*. The Platform Agent creates and
  removes jobs at runtime (per-cluster scaffolding, one-shot follow-ups), and
  a deleted job's rows are never reclaimed by a per-job cap. 5000 is five
  minute-ly jobs' worth, which is more job ids than either roster carries, so
  the ceiling only engages once dead job ids accumulate. At the ~0.6 KB/row the
  live board measures (610,304 bytes for 1000 rows, indexes included) the
  ceiling is ~3 MB per profile.

* ``MIN_TERMINAL_EXECUTIONS_PER_JOB = 50``. The ceiling sweep may never take a
  job's newest 50 terminal rows. Without a floor, raising the global cap only
  moves the crowd-out threshold — the ticker would still be the last writer
  standing. Fifty rows is 50 days for a daily watchdog and 50 weeks for a
  weekly one, i.e. the floor alone already answers "when did it last run?".
  The floor outranks the ceiling: with more than ``5000/50`` job ids on one
  board the protected rows exceed the ceiling and the ceiling gives way.
  That is the intended precedence — evidence beats a size target — and the
  real bound is ``MAX_TERMINAL_EXECUTIONS + 50 x (job ids)``. Both boards
  carry fewer than twenty.

Why the schema change is a table rebuild
-----------------------------------------
``status`` is guarded by a ``CHECK`` constraint, and SQLite cannot alter a
``CHECK`` in place. The only options are rebuild-and-rename or dropping the
constraint, and dropping it would trade a real invariant for convenience.

Both live ledgers are populated (1000 and 99 rows), on a PVC, in WAL mode,
opened concurrently by the gateway and by spawned ``hermes cron tick``
processes, and carry ``PRAGMA user_version = 0`` with no metadata table — so
there is no version counter to gate on. ``migrate_executions_table`` therefore
gates on the schema itself: it reads the live ``CREATE TABLE`` text out of
``sqlite_master`` and no-ops if that text already admits ``'skipped'``. That is
idempotent by construction and needs nothing persisted.

The rebuild derives the new table's DDL *from the live table's own DDL* — one
anchored regex substitution on the ``CHECK(status IN (...))`` list, one on the
table name — rather than from a hardcoded schema. Two reasons. It cannot drop a
column an upstream release added, because it never enumerates columns; and
``INSERT INTO ... SELECT *`` is then trivially correct, because the column
order is identical by construction. If either substitution does not match
exactly once the migration raises instead of guessing, and the build-time
verify turns that into a failed image rather than a ledger that silently
refuses every skip row.

Atomicity is explicit. ``_initialize_schema`` runs its DDL in autocommit, so
the rebuild opens its own ``BEGIN IMMEDIATE``, re-reads the schema *inside* the
write lock (closing the read-then-upgrade race between two ticker processes),
compares row counts before and after the copy, and rolls the whole thing back
on any mismatch. A failed migration leaves the original table exactly as it
was; the caller keeps a working ledger for the four upstream statuses and only
loses the ability to record skips.

``skip_reason`` is added separately, by an idempotent ``ADD COLUMN``, because
``ADD COLUMN`` is the one schema change SQLite *can* do in place and pairing it
with the rebuild would make the rebuild's derived-DDL trick harder to reason
about. It runs first, so the rebuild carries it along. A machine-readable
column rather than a substring of ``error``: "which jobs are being skipped, and
for what" has to be a ``GROUP BY``, not a ``LIKE``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# --- the skip vocabulary ----------------------------------------------------
#: A due occurrence was dropped because the previous run of the same job was
#: still in flight in this process.
SKIP_ALREADY_RUNNING = "already_running"
#: ...or in another process, proved by the per-job flock refusing the claim.
SKIP_ALREADY_RUNNING_ELSEWHERE = "already_running_elsewhere"
#: The interpreter began finalizing between the schedule advance and dispatch.
SKIP_INTERPRETER_SHUTDOWN = "interpreter_shutdown"
#: get_due_jobs fast-forwarded past accumulated occurrences after an outage.
SKIP_MISSED_WINDOW = "missed_window"
#: A finite one-shot had already consumed its dispatch budget.
SKIP_DISPATCH_CLAIM_REJECTED = "dispatch_claim_rejected"

#: Every reason the ledger will accept. An unknown code is coerced rather than
#: rejected: a skip recorded under a bad label is still evidence, and dropping
#: it would reintroduce the exact silence this module exists to remove.
SKIP_REASONS = frozenset(
    {
        SKIP_ALREADY_RUNNING,
        SKIP_ALREADY_RUNNING_ELSEWHERE,
        SKIP_INTERPRETER_SHUTDOWN,
        SKIP_MISSED_WINDOW,
        SKIP_DISPATCH_CLAIM_REJECTED,
    }
)
SKIP_REASON_UNSPECIFIED = "unspecified"

#: The status added to the CHECK constraint.
SKIPPED_STATUS = "skipped"

#: States a row can never leave. ``skipped`` joins them: an occurrence that did
#: not happen cannot later start happening, so a skip row is immutable the
#: moment it is written and is eligible for pruning immediately.
TERMINAL_STATES = ("completed", "failed", "unknown", SKIPPED_STATUS)

# --- retention --------------------------------------------------------------
# The module docstring justifies all three against the real rosters.
MAX_TERMINAL_EXECUTIONS_PER_JOB = 1000
MAX_TERMINAL_EXECUTIONS = 5000
MIN_TERMINAL_EXECUTIONS_PER_JOB = 50

# Interpolated into the prune SQL. These are module constants, never input, so
# there is nothing here for a parameter marker to protect against — and the IN
# list has to be literal because it also has to be identical in three
# statements, which is exactly what a single derived constant guarantees.
_TERMINAL_IN = ", ".join("'{}'".format(state) for state in TERMINAL_STATES)


def terminal_states_sql() -> str:
    """The ``IN`` list the prune and any caller-side query must agree on."""
    return _TERMINAL_IN


# --- schema migration -------------------------------------------------------

#: The one thing the rebuild has to rewrite. Tolerant of whitespace and of the
#: order of the existing values, strict about there being exactly one match.
_STATUS_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*status\s+IN\s*\(\s*(?P<values>[^()]*?)\s*\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)

#: ``CREATE TABLE [IF NOT EXISTS] [schema.]"executions"``. SQLite strips
#: ``IF NOT EXISTS`` before storing DDL in ``sqlite_master``, but a ledger
#: created by some future code path may not have gone through that, so accept
#: both rather than fail a migration on a cosmetic difference.
_CREATE_TABLE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?executions[\"'`\]]?",
    re.IGNORECASE,
)

#: Scratch name for the rebuild. Prefixed rather than suffixed with ``_new`` so
#: it cannot collide with a real table, and dropped unconditionally first so a
#: process killed mid-rebuild does not poison the next attempt.
_SCRATCH_TABLE = "executions_kubeagents_migrating"


class LedgerMigrationError(RuntimeError):
    """The live schema is not the shape this migration knows how to rewrite."""


def _executions_ddl(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    if row is None:
        return None
    return str(row[0] or "")


def status_check_allows_skipped(ddl: str) -> bool:
    """Does this ``CREATE TABLE`` text already admit a ``skipped`` status?

    Read off the DDL rather than off a version counter. Both live ledgers have
    ``user_version = 0`` and no metadata table, and a counter that can disagree
    with the schema is worse than no counter — it is how a migration comes to
    be skipped on the one board that still needed it.
    """
    match = _STATUS_CHECK_RE.search(ddl)
    if match is None:
        return False
    return "'{}'".format(SKIPPED_STATUS) in match.group("values").replace('"', "'")


def _rewritten_ddl(ddl: str) -> str:
    """Derive the scratch table's DDL from the live table's own DDL.

    Never enumerates columns, so a column upstream added since this patch was
    written survives the rebuild and ``INSERT ... SELECT *`` stays correct.
    """
    check_matches = _STATUS_CHECK_RE.findall(ddl)
    if len(check_matches) != 1:
        raise LedgerMigrationError(
            "expected exactly one CHECK(status IN (...)) in the executions "
            "schema, found {}. Upstream Hermes changed the ledger; re-derive "
            "the migration before bumping the base image.\n{}".format(
                len(check_matches), ddl
            )
        )

    def _extend(match: "re.Match[str]") -> str:
        values = match.group("values").strip()
        return "CHECK(status IN ({}, '{}'))".format(values, SKIPPED_STATUS)

    rewritten = _STATUS_CHECK_RE.sub(_extend, ddl, count=1)
    rewritten, renamed = _CREATE_TABLE_RE.subn(
        "CREATE TABLE {}".format(_SCRATCH_TABLE), rewritten, count=1
    )
    if renamed != 1:
        raise LedgerMigrationError(
            "could not find the CREATE TABLE executions header to rename:\n" + ddl
        )
    return rewritten


def add_skip_reason_column(conn: sqlite3.Connection) -> bool:
    """``ADD COLUMN skip_reason``, idempotent and race-tolerant.

    Returns True when this call added it. Mirrors
    ``hermes_cli/sqlite_util.add_column_if_missing`` — reproduced rather than
    imported so this module stays importable by the local unit suite, which has
    no Hermes tree.
    """
    try:
        conn.execute("ALTER TABLE executions ADD COLUMN skip_reason TEXT")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        if "no such table" in str(exc).lower():
            return False
        raise


def migrate_executions_table(conn: sqlite3.Connection) -> str:
    """Make the live ``executions`` table accept ``skipped``. Idempotent.

    Returns ``"absent"`` (no table yet — the caller's ``CREATE TABLE IF NOT
    EXISTS`` owns that), ``"current"`` (already migrated, the steady state on
    every connection after the first) or ``"rebuilt"``.

    Safety, in the order it matters on a populated PVC:

    * The whole rebuild is one ``BEGIN IMMEDIATE`` transaction. The caller runs
      its schema DDL in autocommit, so relying on the caller's transaction
      would leave a ``DROP TABLE`` committed on its own if the process died
      between statements.
    * The schema is re-read *inside* the write lock. Two ticker processes can
      open this file at once; without the re-read both would pass the cheap
      pre-check and the second would rebuild a table that no longer needs it.
    * Row counts are compared across the copy and a mismatch raises, which
      rolls back. A rebuild that silently loses rows is strictly worse than the
      gap it is closing.
    * Nothing references ``executions`` — no views, no triggers, no foreign
      keys, and ``PRAGMA foreign_keys`` is off by default and never enabled by
      the ledger — so ``DROP TABLE`` cannot cascade. The two indexes go with
      the table and the caller's ``CREATE INDEX IF NOT EXISTS`` puts them back.
    """
    ddl = _executions_ddl(conn)
    if ddl is None:
        return "absent"
    add_skip_reason_column(conn)
    if status_check_allows_skipped(ddl):
        return "current"

    started_here = not conn.in_transaction
    if started_here:
        conn.execute("BEGIN IMMEDIATE")
    try:
        ddl = _executions_ddl(conn)
        if ddl is None:
            outcome = "absent"
        elif status_check_allows_skipped(ddl):
            outcome = "current"
        else:
            before = conn.execute("SELECT count(*) FROM executions").fetchone()[0]
            conn.execute("DROP TABLE IF EXISTS {}".format(_SCRATCH_TABLE))
            conn.execute(_rewritten_ddl(ddl))
            conn.execute(
                "INSERT INTO {} SELECT * FROM executions".format(_SCRATCH_TABLE)
            )
            after = conn.execute(
                "SELECT count(*) FROM {}".format(_SCRATCH_TABLE)
            ).fetchone()[0]
            if after != before:
                raise LedgerMigrationError(
                    "executions rebuild copied {} of {} rows; rolling back "
                    "rather than shipping a truncated ledger".format(after, before)
                )
            conn.execute("DROP TABLE executions")
            conn.execute(
                "ALTER TABLE {} RENAME TO executions".format(_SCRATCH_TABLE)
            )
            outcome = "rebuilt"
    except BaseException:
        if started_here:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                # SQLite already rolled back (EIO, lock loss). Never let a
                # spurious rollback error shadow the real one.
                pass
        raise
    if started_here:
        conn.execute("COMMIT")
    if outcome == "rebuilt":
        logger.info(
            "cron ledger: rebuilt executions table to record skipped occurrences"
        )
    return outcome


def ensure_schema(conn: sqlite3.Connection) -> str:
    """Migration entry point for ``cron/executions.py:_initialize_schema``.

    Never raises. A ledger that cannot be migrated must keep working for the
    four upstream statuses — losing every ``completed`` row because a skip row
    could not be represented would be a far larger regression than the gap.
    The failure is logged at ERROR and the build-time verify is what stops a
    broken migration reaching a cluster in the first place.
    """
    try:
        return migrate_executions_table(conn)
    except Exception:
        logger.error(
            "cron ledger: could not migrate the executions table to record "
            "skipped occurrences; skips will not be recorded on this profile",
            exc_info=True,
        )
        return "failed"


# --- retention --------------------------------------------------------------

_PRUNE_PER_JOB_SQL = """
    DELETE FROM executions WHERE id IN (
      SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
                     PARTITION BY job_id ORDER BY claimed_at DESC, id DESC
                   ) AS job_rank
        FROM executions WHERE status IN (%s)
      ) WHERE job_rank > ?
    )
""" % _TERMINAL_IN

_PRUNE_TOTAL_SQL = """
    DELETE FROM executions WHERE id IN (
      SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                 ORDER BY claimed_at DESC, id DESC
               ) AS total_rank,
               ROW_NUMBER() OVER (
                 PARTITION BY job_id ORDER BY claimed_at DESC, id DESC
               ) AS job_rank
        FROM executions WHERE status IN (%s)
      ) WHERE total_rank > ? AND job_rank > ?
    )
""" % _TERMINAL_IN


def prune_terminal_executions(
    conn: sqlite3.Connection,
    *,
    per_job: Optional[int] = None,
    total: Optional[int] = None,
    floor: Optional[int] = None,
) -> None:
    """Two-pass retention: a per-job cap, then a global ceiling with a floor.

    Pass one bounds a single hyperactive job. Pass two bounds the file against
    job-id churn, and is the only pass that can touch a job which is already
    under its own cap — which is why it may never cross ``floor``. Ordered this
    way on purpose: the ceiling should only ever see rows the per-job rule
    already decided to keep.

    ``ROW_NUMBER() OVER (PARTITION BY ...)`` rather than a correlated subquery
    per job: one statement, one scan, and the ordering is the same
    ``claimed_at DESC, id DESC`` the ``idx_executions_job_claimed`` index and
    ``list_executions`` already use, so "the newest N" means the same thing to
    the prune as it does to every reader. Window functions need SQLite >= 3.25;
    the image ships 3.53.
    """
    per_job_limit = max(1, int(MAX_TERMINAL_EXECUTIONS_PER_JOB if per_job is None else per_job))
    total_limit = max(1, int(MAX_TERMINAL_EXECUTIONS if total is None else total))
    floor_limit = max(0, int(MIN_TERMINAL_EXECUTIONS_PER_JOB if floor is None else floor))
    conn.execute(_PRUNE_PER_JOB_SQL, (per_job_limit,))
    conn.execute(_PRUNE_TOTAL_SQL, (total_limit, floor_limit))


# --- call-site helpers ------------------------------------------------------


def normalize_reason(reason: Any) -> str:
    """Coerce a reason to the known vocabulary without ever dropping the row."""
    value = str(reason or "").strip().lower()
    if value in SKIP_REASONS:
        return value
    if value:
        logger.debug("cron ledger: unrecognised skip reason %r", value)
        return value[:64]
    return SKIP_REASON_UNSPECIFIED


def count_missed_occurrences(
    schedule: Dict[str, Any], stale: Any, now: Any, *, cap: int = 10000
) -> int:
    """How many occurrences the catch-up fast-forward is about to discard.

    ``stale`` is the ``next_run_at`` that went past its grace window; ``now``
    is when the ticker noticed. Every occurrence in ``[stale, now]`` is lost
    except the single catch-up fire that happens now, so the answer is the
    number of schedule steps strictly after ``stale`` and not after ``now``.

    Returns ``-1`` when it cannot be computed (no croniter, an unparseable
    expression, an unknown schedule kind). The caller reports "an unknown
    number of" rather than a wrong number — a fabricated count in an outage
    report is worse than an admission.
    """
    if not isinstance(schedule, dict):
        return -1
    try:
        gap = (now - stale).total_seconds()
    except Exception:
        return -1
    if gap <= 0:
        return 0

    kind = schedule.get("kind")
    if kind == "interval":
        try:
            minutes = float(schedule.get("minutes") or 0)
        except (TypeError, ValueError):
            return -1
        if minutes <= 0:
            return -1
        return min(cap, int(gap // (minutes * 60.0)))

    if kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            return -1
        try:
            from datetime import datetime

            from croniter import croniter

            steps = croniter(expr, stale)
            missed = 0
            while missed < cap:
                nxt = steps.get_next(datetime)
                if nxt > now:
                    break
                missed += 1
            return missed
        except Exception:
            return -1

    return -1


def missed_window_detail(
    schedule: Dict[str, Any], stale: Any, now: Any, grace_seconds: Any
) -> str:
    """The human half of a ``missed_window`` row.

    Carries the window and the count because the row itself carries neither:
    one row stands for the whole gap, so without this text an operator can see
    that *something* was collapsed but not how much or over what period.
    """
    missed = count_missed_occurrences(schedule, stale, now)
    how_many = "an unknown number of" if missed < 0 else str(missed)
    return (
        "Scheduler did not fire this job between {} and {} ({} occurrence(s) "
        "collapsed into a single catch-up run; grace {}s). The catch-up run "
        "itself is recorded separately.".format(stale, now, how_many, grace_seconds)
    )


def record_skip(
    job_id: Any,
    *,
    source: str,
    reason: str,
    detail: str = "",
) -> Optional[Dict[str, Any]]:
    """Best-effort ledger write for an occurrence that did not run. Never raises.

    The import is deferred: ``cron/executions.py`` imports *this* module at
    module scope, and ``cron/__init__.py`` eagerly pulls in ``cron.jobs`` and
    ``cron.scheduler``, so a module-scope import back into ``cron`` here would
    close the cycle.

    Swallowing is deliberate and is the whole reason the call sites do not each
    wrap this. Every caller is a guard clause in ``tick``'s dispatch path that
    must return promptly and must not raise: a ledger write that failed —
    because the migration did not land, because the PVC is full, because
    another process holds the write lock past the busy timeout — must degrade
    to exactly today's behaviour (a log line), not take the tick down with it.
    """
    try:
        from cron.executions import record_skipped_execution

        return record_skipped_execution(
            job_id, source=source, reason=reason, detail=detail
        )
    except Exception:
        logger.warning(
            "cron ledger: failed to record skipped occurrence for job %s (%s)",
            job_id,
            reason,
            exc_info=True,
        )
        return None
