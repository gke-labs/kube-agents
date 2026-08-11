#!/usr/bin/env python3
"""Build gate for the cron skip-ledger patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_cron_skip_ledger.py``. The applier proves fourteen anchors matched and
that four files still parse; it proves nothing about whether a skip actually
reaches the ledger, and this patch fails silently in both directions if it does
not.

Two properties make a build-time gate non-optional here.

**The migration runs once, on a populated volume, in production.** The host
suite exercises it against a synthesised legacy database, but not through the
path that will invoke it: ``_initialize_schema``, inside ``_transaction``, with
WAL applied by ``hermes_state`` and ``PRAGMA synchronous=FULL``, and with the
two ``CREATE INDEX`` statements running after the table has been dropped and
rebuilt underneath them. So checks 2 and 3 lay down a byte-for-byte copy of the
schema the live boards carry and drive the real module over it.

**Every skip is written from a guard clause that must not raise.**
``record_skip`` swallows by design — a ledger write must never take a tick
down — so a mis-wired call site fails exactly like the bug it was meant to fix:
silently. Check 5 therefore drives ``tick`` and ``run_one_job`` themselves,
once per reason, and reads the ledger back.

The six checks:

1. SHAPE, ON THE REAL TREE. The prune is per job; ``_ensure_skip_schema`` runs
   before the indexes it invalidates; the ledger write in the in-process guard
   is outside ``_running_lock``; the cross-process guard releases the running
   slot before writing. Each is an ordering the applier's text match cannot see
   and a behavioural check would only catch under contention.
2. THE MIGRATION. Legacy DDL, rows in it, driven through ``list_executions``.
   Rows preserved, CHECK widened and still enforced, indexes back, scratch
   table gone, second pass a no-op.
3. THE WRITE PATH. ``record_skipped_execution`` and ``skip_execution``, and
   terminal-once still holding.
4. TELEMETRY. ``project_execution_event`` must not launder ``skipped`` into
   ``unknown`` — the state that means an owner process died mid-run.
5. THE FIVE REASONS, END TO END. Three ``tick`` guards, ``run_one_job``'s
   dispatch claim, and a genuinely stale job store through the real
   ``cron.jobs.get_due_jobs``.
6. RETENTION. A flood of one job's rows must not evict another job's, which is
   the live failure this half of the patch exists to fix.

Usage::

    cd /opt/hermes && python3 verify_cron_skip_ledger.py
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

# Must precede every Hermes import: cron/executions.py resolves EXECUTIONS_FILE
# from get_hermes_home() at module scope, so a HERMES_HOME set afterwards would
# point these checks at the image's real ledger.
HOME = Path(tempfile.mkdtemp(prefix="skip-ledger-verify-"))
os.environ["HERMES_HOME"] = str(HOME)
(HOME / "cron").mkdir(parents=True, exist_ok=True)
LEDGER = HOME / "cron" / "executions.db"

# The exact CREATE TABLE both live boards carry, read out of sqlite_master on
# 2026-08-08. Not a paraphrase: the migration derives the rebuilt table from
# this text by regex, so a tidied-up fixture would test a different migration
# than the one that ships.
LEGACY_DDL = """CREATE TABLE executions (
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

CHATTY_JOB = "profile-cron-tick"
QUIET_JOB = "compliance-audit"
LEGACY_ROWS = 40

FAILURES: list = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# --- 1. shape, on the real tree ---------------------------------------------


def function_named(path: Path, name: str):
    return next(
        (
            n
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.FunctionDef) and n.name == name
        ),
        None,
    )


def check_shape() -> None:
    print("patched shape (orderings a text match cannot see):")

    executions = HERMES / "cron" / "executions.py"
    prune = function_named(executions, "_prune_unlocked")
    check("_prune_unlocked still exists", prune is not None)
    if prune is not None:
        src = ast.unparse(prune)
        check(
            "the prune is per job",
            "_prune_terminal_executions(conn)" in src,
            f"body is still {src!r} — the global cap survived, so the busiest "
            "job goes on evicting every other job's evidence",
        )
        check(
            "the global DELETE is gone",
            "LIMIT -1 OFFSET" not in src,
            "the upstream statement is still there alongside the new one",
        )

    init = function_named(executions, "_initialize_schema")
    check("_initialize_schema still exists", init is not None)
    if init is not None:
        src = ast.unparse(init)
        migrate = src.find("_ensure_skip_schema(conn)")
        index = src.find("idx_executions_job_claimed")
        check("the migration is invoked from schema init", migrate >= 0)
        check(
            "it runs before the indexes are recreated",
            0 <= migrate < index,
            "the rebuild drops both indexes with the old table; recreating "
            "them first leaves the ledger unindexed until the next restart",
        )

    scheduler = HERMES / "cron" / "scheduler.py"
    guard = function_named(scheduler, "_submit_with_guard")
    check("_submit_with_guard still wraps the dispatch", guard is not None)
    if guard is not None:
        under_lock = [
            ast.unparse(n)
            for n in ast.walk(guard)
            if isinstance(n, ast.With)
            and "_running_lock" in ast.unparse(n.items[0].context_expr)
        ]
        check(
            "no ledger write happens under _running_lock",
            not any("record_skip" in w for w in under_lock),
            "every dispatching thread in the tick takes that lock; a SQLite "
            "write behind a 5s busy timeout inside it serialises the whole "
            "dispatch pass",
        )
        src = ast.unparse(guard)
        released = src.find("_running_job_ids.discard(job_id)")
        recorded = src.find("SKIP_ALREADY_RUNNING_ELSEWHERE")
        check(
            "the running slot is released before the cross-process skip is written",
            0 <= released < recorded,
            "a slow ledger write would leave the job wedged in the running "
            "set, suppressing it on the next tick as well",
        )


# --- 2 + 3. the migration and the write path --------------------------------


def seed_legacy_ledger() -> None:
    """A pre-patch ledger with rows in it, the way a live volume has one."""
    conn = sqlite3.connect(LEDGER)
    try:
        conn.execute(LEGACY_DDL)
        conn.execute(
            "CREATE INDEX idx_executions_job_claimed "
            "ON executions(job_id, claimed_at DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX idx_executions_status_claimed "
            "ON executions(status, claimed_at DESC, id DESC)"
        )
        for i in range(LEGACY_ROWS):
            stamp = f"2026-08-07T{i // 60:02d}:{i % 60:02d}:00+00:00"
            conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, "
                "status, claimed_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    CHATTY_JOB if i % 4 else QUIET_JOB,
                    "builtin",
                    "legacy",
                    4242,
                    "completed",
                    stamp,
                    stamp,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def ledger_sql() -> str:
    conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def ledger_objects(kind: str) -> set:
    conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type=? "
                "AND name NOT LIKE 'sqlite_%'",
                (kind,),
            )
        }
    finally:
        conn.close()


def skips_for(reason: str) -> list:
    conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM executions WHERE status='skipped' AND skip_reason=?",
                (reason,),
            )
        ]
    finally:
        conn.close()


def check_migration(ex) -> None:
    print("migration (a populated pre-patch ledger, through the real module):")
    before = ex.list_executions(limit=500)
    check(
        "every legacy row survived the rebuild",
        len(before) == LEGACY_ROWS,
        f"{len(before)} of {LEGACY_ROWS} rows",
    )
    sql = ledger_sql()
    check("the CHECK now admits skipped", "'skipped'" in sql, sql)
    check("the four upstream statuses are still constrained", "'unknown'" in sql, sql)
    check("the skip_reason column exists", "skip_reason" in sql, sql)
    check(
        "both indexes were recreated after the drop",
        ledger_objects("index")
        == {"idx_executions_job_claimed", "idx_executions_status_claimed"},
        f"found {sorted(ledger_objects('index'))} — the rebuild dropped them "
        "with the old table and nothing put them back",
    )
    check(
        "the scratch table was not left behind",
        "executions_kubeagents_migrating" not in ledger_objects("table"),
        f"found {sorted(ledger_objects('table'))}",
    )

    sql_after_first = ledger_sql()
    ex.list_executions(limit=1)
    check(
        "a second pass is a no-op",
        ledger_sql() == sql_after_first,
        "the migration is not idempotent, so every connection rebuilds the "
        "table — on a 1000-row ledger, on every tick",
    )
    check(
        "and did not disturb the rows",
        len(ex.list_executions(limit=500)) == LEGACY_ROWS,
    )

    conn = sqlite3.connect(LEDGER)
    refused = False
    try:
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "status, claimed_at) VALUES ('x','j','builtin','p',1,'bogus','t')"
        )
    except sqlite3.IntegrityError:
        refused = True
    finally:
        conn.close()
    check(
        "the rebuilt CHECK still refuses an unknown status",
        refused,
        "the rebuild widened the constraint into uselessness",
    )


def check_write_path(ex, reasons):
    print("write path (cron/executions.py):")
    record = ex.record_skipped_execution(
        QUIET_JOB, source="builtin", reason=reasons.SKIP_ALREADY_RUNNING, detail="d"
    )
    check(
        "a skip row was written",
        (record or {}).get("status") == "skipped",
        repr(record),
    )
    check(
        "with its reason",
        (record or {}).get("skip_reason") == reasons.SKIP_ALREADY_RUNNING,
        repr(record),
    )
    check(
        "and nothing pretending to be a runtime",
        (record or {}).get("started_at") is None,
        "started_at is set, so cron_health will report a duration for a run "
        "that never started",
    )
    check("and a finish time", bool((record or {}).get("finished_at")))

    claimed = ex.create_execution(QUIET_JOB, source="direct")
    closed = ex.skip_execution(
        claimed["id"], reason=reasons.SKIP_DISPATCH_CLAIM_REJECTED
    )
    check(
        "a claimed attempt closes as skipped",
        (closed or {}).get("status") == "skipped",
        repr(closed),
    )
    check(
        "a terminal attempt cannot be rewritten",
        ex.skip_execution(claimed["id"], reason=reasons.SKIP_ALREADY_RUNNING) is None,
        "terminal-once no longer holds, so a finished attempt can be relabelled",
    )
    return record


# --- 4. telemetry ------------------------------------------------------------


def check_telemetry(record, reasons) -> None:
    print("telemetry (agent/monitoring/cron_health.py):")
    from agent.monitoring.cron_health import project_execution_event

    event = project_execution_event(record)
    check(
        "the status is not laundered into unknown",
        event.status == "skipped",
        f"status={event.status!r} — 'unknown' means an owner process died "
        "mid-run, so coercing to it corrupts a real signal too",
    )
    check(
        "the reason is projected",
        event.error_class == reasons.SKIP_ALREADY_RUNNING,
        f"error_class={event.error_class!r}",
    )
    check(
        "no runtime is invented",
        event.duration_ms == 0,
        f"duration_ms={event.duration_ms!r}",
    )


# --- 5. the five reasons, end to end -----------------------------------------


def check_tick_guards(sched, reasons) -> None:
    print("the tick guards (driven, then read back out of the ledger):")
    job = {"id": CHATTY_JOB, "name": "Named-Profile Cron Ticker"}
    sched.get_due_jobs = lambda: [dict(job)]
    sched.advance_next_runs = lambda ids: None

    sched._running_job_ids.add(CHATTY_JOB)
    try:
        sched.tick(verbose=False, sync=True)
    finally:
        sched._running_job_ids.discard(CHATTY_JOB)
    rows = skips_for(reasons.SKIP_ALREADY_RUNNING)
    check(
        "an in-process overlap is recorded",
        any(r["job_id"] == CHATTY_JOB for r in rows),
        "next_run_at was already advanced for the whole due set, so this "
        "occurrence is gone and nothing records it",
    )

    class RefusingLocks:
        def claim(self, job_id):
            return None

    real_locks = sched._job_locks
    sched._job_locks = RefusingLocks()
    try:
        sched.tick(verbose=False, sync=True)
    finally:
        sched._job_locks = real_locks
    check(
        "a cross-process overlap is recorded",
        bool(skips_for(reasons.SKIP_ALREADY_RUNNING_ELSEWHERE)),
        "two tickers racing for the same profile leave no trace",
    )
    check(
        "and the job is not left wedged in the running set",
        CHATTY_JOB not in sched.get_running_job_ids(),
    )

    real_shutting_down = sched._interpreter_shutting_down
    sched._interpreter_shutting_down = lambda *a, **k: True
    try:
        sched.tick(verbose=False, sync=True)
    finally:
        sched._interpreter_shutting_down = real_shutting_down
    check(
        "a dispatch dropped to interpreter shutdown is recorded",
        bool(skips_for(reasons.SKIP_INTERPRETER_SHUTDOWN)),
        "a rollout silently eats the occurrences that were mid-dispatch",
    )

    real_claim_dispatch = sched.claim_dispatch
    sched.claim_dispatch = lambda job_id: False
    try:
        processed = sched.run_one_job({"id": QUIET_JOB, "name": "Compliance Audit"})
    finally:
        sched.claim_dispatch = real_claim_dispatch
    check("run_one_job still reports the occurrence handled", processed is True)
    check(
        "a spent one-shot budget is recorded as skipped, not failed",
        len(skips_for(reasons.SKIP_DISPATCH_CLAIM_REJECTED)) >= 2,
        "an at-most-once guarantee working as designed is still inflating the "
        "failure rate cron_health derives",
    )


def check_missed_window(reasons) -> None:
    print("the outage that hides itself (cron/jobs.py::get_due_jobs):")
    from hermes_time import now as hermes_now

    import cron.jobs as cj

    now = hermes_now()
    stale = (now - timedelta(hours=5)).isoformat()  # a daily job's grace is 7200s
    (HOME / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": QUIET_JOB,
                        "name": "Security & RBAC Posture Audit",
                        "schedule": {"kind": "cron", "expr": "20 6 * * *"},
                        "prompt": "",
                        "enabled": True,
                        "deliver": "local",
                        "next_run_at": stale,
                        "state": "scheduled",
                    }
                ],
                "updated_at": now.isoformat(),
            }
        )
    )
    due = cj.get_due_jobs()
    check(
        "the stale job still fires exactly once",
        len(due) == 1,
        f"{len(due)} due — the catch-up behaviour changed, which is not this "
        "patch's business to change",
    )
    missed = skips_for(reasons.SKIP_MISSED_WINDOW)
    check(
        "the collapsed window is recorded",
        len(missed) == 1,
        f"{len(missed)} rows — one row per gap, not per lost occurrence, and "
        "not zero: upstream's only trace is a profile-wide integer in a file",
    )
    if missed:
        check("against the job it belongs to", missed[0]["job_id"] == QUIET_JOB)
        check(
            "with a count rather than a shrug",
            "unknown number" not in (missed[0]["error"] or ""),
            f"detail={missed[0]['error']!r} — croniter could not walk the "
            "expression, so the row cannot say how much was lost",
        )


# --- 6. retention -------------------------------------------------------------


def check_retention() -> None:
    print("retention (a chatty job must not evict a quiet one):")
    from tools.cron_skip_ledger import (
        MAX_TERMINAL_EXECUTIONS_PER_JOB,
        prune_terminal_executions,
    )

    conn = sqlite3.connect(LEDGER)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        quiet_before = conn.execute(
            "SELECT count(*) FROM executions WHERE job_id=?", (QUIET_JOB,)
        ).fetchone()[0]
        overflow = MAX_TERMINAL_EXECUTIONS_PER_JOB + 500
        conn.executemany(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "status, claimed_at, finished_at) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    uuid.uuid4().hex,
                    CHATTY_JOB,
                    "builtin",
                    "flood",
                    1,
                    "completed",
                    # Newer than every quiet-job row, which is the point: under
                    # the global cap these are exactly what evicted them.
                    "2026-08-09T{:02d}:{:02d}:{:02d}+00:00".format(
                        i // 3600 % 24, i // 60 % 60, i % 60
                    ),
                    "2026-08-09T00:00:00+00:00",
                )
                for i in range(overflow)
            ],
        )
        conn.commit()
        prune_terminal_executions(conn)
        conn.commit()
        chatty_after = conn.execute(
            "SELECT count(*) FROM executions WHERE job_id=?", (CHATTY_JOB,)
        ).fetchone()[0]
        quiet_after = conn.execute(
            "SELECT count(*) FROM executions WHERE job_id=?", (QUIET_JOB,)
        ).fetchone()[0]
    finally:
        conn.close()

    check(
        "the chatty job is capped at its own limit",
        chatty_after == MAX_TERMINAL_EXECUTIONS_PER_JOB,
        f"{chatty_after} rows, cap {MAX_TERMINAL_EXECUTIONS_PER_JOB}",
    )
    check(
        "and the quiet job kept every row it had",
        quiet_after == quiet_before,
        f"{quiet_after} of {quiet_before} rows survived — the flood still "
        "evicted the evidence, which is the whole defect",
    )


if __name__ == "__main__":
    print("verify_cron_skip_ledger")
    check_shape()

    seed_legacy_ledger()
    check(
        "the fixture really is a pre-patch ledger",
        "'skipped'" not in ledger_sql(),
        "the fixture already admits skipped, so the migration is untested",
    )

    import cron.executions as executions_module
    import cron.scheduler as scheduler_module
    import tools.cron_skip_ledger as skip_reasons

    check_migration(executions_module)
    skip_record = check_write_path(executions_module, skip_reasons)
    check_telemetry(skip_record, skip_reasons)
    check_tick_guards(scheduler_module, skip_reasons)
    check_missed_window(skip_reasons)
    check_retention()

    print()
    if FAILURES:
        print(f"verify_cron_skip_ledger: {len(FAILURES)} FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("verify_cron_skip_ledger: all checks passed")
