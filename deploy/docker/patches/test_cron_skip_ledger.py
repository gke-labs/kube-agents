#!/usr/bin/env python3
"""Host tests for cron_skip_ledger and its applier. No Hermes install required.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

The in-image gate (``verify_cron_skip_ledger.py``) drives the real scheduler
and is the authority on wiring. These cover what is cheaper and sharper to test
here: the migration's failure modes — the paths a healthy image never takes and
so never exercises — and the retention arithmetic, whose whole point is a
boundary.

The migration fixture is the DDL both live ledgers actually carry, copied out
of ``sqlite_master`` on 2026-08-08, because the migration derives the rebuilt
table from that text. A tidied-up fixture would be testing a different
migration than the one that ships.
"""

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cron_skip_ledger import (  # noqa: E402
    MAX_TERMINAL_EXECUTIONS,
    MAX_TERMINAL_EXECUTIONS_PER_JOB,
    MIN_TERMINAL_EXECUTIONS_PER_JOB,
    SKIP_ALREADY_RUNNING,
    SKIP_MISSED_WINDOW,
    SKIP_REASON_UNSPECIFIED,
    LedgerMigrationError,
    add_skip_reason_column,
    count_missed_occurrences,
    ensure_schema,
    migrate_executions_table,
    missed_window_detail,
    normalize_reason,
    prune_terminal_executions,
    status_check_allows_skipped,
)

# Verbatim from the live boards. Note the line break inside the CHECK and the
# two-space continuation indent: the regex has to survive both.
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

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
    "ON executions(job_id, claimed_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
    "ON executions(status, claimed_at DESC, id DESC)",
)

EPOCH = datetime(2026, 8, 7, tzinfo=timezone.utc)


class LedgerFixture(unittest.TestCase):
    """A populated pre-patch ledger in a temp file, as a live PVC holds one."""

    ddl = LEGACY_DDL

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "executions.db"
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute(self.ddl)
        for statement in INDEXES:
            self.conn.execute(statement)
        self.conn.commit()

    def insert(self, job_id, *, count=1, status="completed", offset=0, reason=None):
        columns = "id, job_id, source, process_id, pid, status, claimed_at"
        values = "?, ?, 'builtin', 'p', 1, ?, ?"
        if reason is not None:
            columns += ", skip_reason"
            values += ", ?"
        for i in range(count):
            claimed = (EPOCH + timedelta(seconds=offset + i)).isoformat()
            row = [uuid.uuid4().hex, job_id, status, claimed]
            if reason is not None:
                row.append(reason)
            self.conn.execute(
                "INSERT INTO executions ({}) VALUES ({})".format(columns, values),
                row,
            )
        self.conn.commit()

    def schema(self):
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()
        return row[0] if row else None

    def tables(self):
        return {
            r[0]
            for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    def count(self, job_id=None):
        if job_id is None:
            return self.conn.execute("SELECT count(*) FROM executions").fetchone()[0]
        return self.conn.execute(
            "SELECT count(*) FROM executions WHERE job_id=?", (job_id,)
        ).fetchone()[0]


class MigrationTest(LedgerFixture):
    """The rebuild has to be survivable on a board that already has rows."""

    def test_it_rebuilds_a_legacy_table_without_losing_a_row(self):
        self.insert("profile-cron-tick", count=200)
        self.insert("compliance-audit", count=3, offset=1000)
        self.assertEqual(migrate_executions_table(self.conn), "rebuilt")
        self.assertEqual(self.count(), 203)
        self.assertIn("'skipped'", self.schema())
        self.assertIn("skip_reason", self.schema())

    def test_the_rebuilt_check_still_rejects_a_status_it_never_knew(self):
        """The constraint is the point. A rebuild that drops it is not a fix."""
        migrate_executions_table(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO executions (id, job_id, source, process_id, pid, "
                "status, claimed_at) VALUES ('x','j','builtin','p',1,'nope','t')"
            )

    def test_it_accepts_the_status_it_was_written_for(self):
        migrate_executions_table(self.conn)
        self.insert("compliance-audit", status="skipped", reason=SKIP_ALREADY_RUNNING)
        self.assertEqual(
            self.conn.execute(
                "SELECT skip_reason FROM executions WHERE status='skipped'"
            ).fetchone()[0],
            SKIP_ALREADY_RUNNING,
        )

    def test_it_is_idempotent(self):
        """Every connection runs _initialize_schema; only the first may rebuild."""
        self.insert("profile-cron-tick", count=10)
        self.assertEqual(migrate_executions_table(self.conn), "rebuilt")
        after_first = self.schema()
        for _ in range(3):
            self.assertEqual(migrate_executions_table(self.conn), "current")
        self.assertEqual(self.schema(), after_first)
        self.assertEqual(self.count(), 10)

    def test_a_second_connection_sees_the_migration_already_done(self):
        """The read-then-upgrade race between two `hermes cron tick` processes."""
        self.insert("profile-cron-tick", count=5)
        other = sqlite3.connect(self.path)
        self.addCleanup(other.close)
        stale_ddl = self.schema()  # what `other` would have read before the lock
        self.assertFalse(status_check_allows_skipped(stale_ddl))
        self.assertEqual(migrate_executions_table(self.conn), "rebuilt")
        # `other` re-reads inside its own BEGIN IMMEDIATE and backs off.
        self.assertEqual(migrate_executions_table(other), "current")
        self.assertEqual(self.count(), 5)

    def test_it_carries_a_column_upstream_added_since_this_patch(self):
        """Derived DDL, never an enumerated column list — that is the whole trick."""
        self.conn.execute("ALTER TABLE executions ADD COLUMN attempt INTEGER")
        self.conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, "
            "claimed_at, attempt) VALUES ('a','j','builtin','p',1,'completed','t',7)"
        )
        self.conn.commit()
        self.assertEqual(migrate_executions_table(self.conn), "rebuilt")
        self.assertIn("attempt", self.schema())
        self.assertEqual(
            self.conn.execute("SELECT attempt FROM executions").fetchone()[0], 7
        )

    def test_an_interrupted_rebuild_does_not_poison_the_next_attempt(self):
        self.insert("profile-cron-tick", count=4)
        self.conn.execute("CREATE TABLE executions_kubeagents_migrating (junk TEXT)")
        self.conn.commit()
        self.assertEqual(migrate_executions_table(self.conn), "rebuilt")
        self.assertNotIn("executions_kubeagents_migrating", self.tables())
        self.assertEqual(self.count(), 4)

    def test_there_is_nothing_to_migrate_before_the_table_exists(self):
        self.conn.execute("DROP TABLE executions")
        self.conn.commit()
        self.assertEqual(migrate_executions_table(self.conn), "absent")

    def test_add_column_is_idempotent_and_reports_who_added_it(self):
        self.assertTrue(add_skip_reason_column(self.conn))
        self.assertFalse(add_skip_reason_column(self.conn))

    def test_a_fresh_post_patch_ledger_needs_no_rebuild(self):
        """The CREATE TABLE the applier installs must satisfy the gate itself."""
        self.conn.execute("DROP TABLE executions")
        self.conn.execute(
            LEGACY_DDL.replace("'unknown')),", "'unknown','skipped')),").replace(
                "error TEXT\n", "error TEXT,\n             skip_reason TEXT\n"
            )
        )
        self.conn.commit()
        self.assertEqual(migrate_executions_table(self.conn), "current")


class MigrationRefusalTest(LedgerFixture):
    """When the schema is not the shape this knows, it must stop, not guess.

    The fixture is a table carrying a second, table-level ``CHECK(status IN
    ...)`` alongside the column one. Both are satisfied by the same values, so
    the ledger works normally — it is only the *rewrite* that is ambiguous, and
    that ambiguity is the thing under test. Rewriting the wrong one would
    produce a table that still silently refuses every skip row.
    """

    ddl = LEGACY_DDL.replace(
        "             error TEXT\n",
        "             error TEXT,\n"
        "             CHECK(status IN "
        "('claimed','running','completed','failed','unknown'))\n",
    )

    def refuse(self):
        """``ensure_schema`` on this fixture, with its ERROR captured.

        Captured rather than silenced: a migration that gives up has to say so
        at ERROR, since the only other symptom is skips quietly not being
        recorded — which is indistinguishable from having none.
        """
        with self.assertLogs("cron_skip_ledger", level="ERROR") as logs:
            outcome = ensure_schema(self.conn)
        self.assertTrue(any("could not migrate" in line for line in logs.output))
        return outcome

    def test_two_status_checks_raise_rather_than_rewrite_the_wrong_one(self):
        with self.assertRaises(LedgerMigrationError):
            migrate_executions_table(self.conn)

    def test_a_refused_migration_leaves_the_rows_and_the_constraint_alone(self):
        """Rolled back, not half-applied: the ledger is exactly as it was.

        ``skip_reason`` does survive — it is added by ``ADD COLUMN`` before the
        rebuild is attempted, deliberately, so the rebuild can carry it. An
        unused nullable column is inert, and it is the CHECK, not the column,
        that decides whether a skip can be written.
        """
        self.insert("profile-cron-tick", count=12)
        self.assertEqual(self.refuse(), "failed")
        self.assertEqual(self.count(), 12)
        self.assertNotIn("'skipped'", self.schema())
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("profile-cron-tick", status="skipped")

    def test_ensure_schema_never_raises(self):
        """A ledger that cannot record skips must still record everything else."""
        self.assertEqual(self.refuse(), "failed")
        self.insert("profile-cron-tick", count=1)
        self.assertEqual(self.count(), 1)


class SchemaGateTest(unittest.TestCase):
    """Gating on the DDL text, because neither live board has a version counter."""

    def test_it_reads_the_live_ddl_rather_than_a_counter(self):
        self.assertFalse(status_check_allows_skipped(LEGACY_DDL))
        self.assertTrue(
            status_check_allows_skipped(
                LEGACY_DDL.replace("'unknown')", "'unknown','skipped')")
            )
        )

    def test_a_table_with_no_status_check_is_not_mistaken_for_migrated(self):
        self.assertFalse(
            status_check_allows_skipped("CREATE TABLE executions (id TEXT)")
        )

    def test_the_word_skipped_elsewhere_in_the_ddl_does_not_count(self):
        """Only the CHECK list decides — a column named skipped_at must not."""
        self.assertFalse(
            status_check_allows_skipped(LEGACY_DDL.replace("error TEXT", "skipped_at TEXT"))
        )


class RetentionTest(LedgerFixture):
    """Per-job retention exists so a chatty job stops evicting a quiet one."""

    def setUp(self):
        super().setUp()
        migrate_executions_table(self.conn)

    def test_a_chatty_job_no_longer_evicts_a_quiet_one(self):
        """The live failure: 980 ticker rows, 17 hourly rows, one 1000-row board."""
        self.insert("compliance-audit", count=5)
        self.insert(
            "profile-cron-tick", count=MAX_TERMINAL_EXECUTIONS_PER_JOB + 400, offset=10_000
        )
        prune_terminal_executions(self.conn)
        self.assertEqual(self.count("compliance-audit"), 5)
        self.assertEqual(
            self.count("profile-cron-tick"), MAX_TERMINAL_EXECUTIONS_PER_JOB
        )

    def test_the_cap_keeps_the_newest_rows(self):
        self.insert("profile-cron-tick", count=20, offset=0)
        prune_terminal_executions(self.conn, per_job=5, floor=0)
        kept = [
            r[0]
            for r in self.conn.execute("SELECT claimed_at FROM executions ORDER BY claimed_at")
        ]
        self.assertEqual(
            kept, [(EPOCH + timedelta(seconds=i)).isoformat() for i in range(15, 20)]
        )

    def test_a_running_row_is_never_pruned(self):
        """Only terminal rows are retention-managed; an in-flight run is state."""
        self.insert("profile-cron-tick", count=3, status="running")
        self.insert("profile-cron-tick", count=3, status="claimed", offset=100)
        self.insert("profile-cron-tick", count=50, status="completed", offset=200)
        prune_terminal_executions(self.conn, per_job=1, floor=0)
        self.assertEqual(self.count(), 3 + 3 + 1)

    def test_a_skip_row_is_prunable_immediately(self):
        """It is terminal on arrival — the occurrence cannot start happening later."""
        self.insert("compliance-audit", count=40, status="skipped", reason=SKIP_MISSED_WINDOW)
        prune_terminal_executions(self.conn, per_job=10, floor=0)
        self.assertEqual(self.count(), 10)

    def test_the_global_ceiling_bounds_the_file_against_job_id_churn(self):
        """A per-job rule alone is unbounded on the axis that actually grows."""
        for n in range(30):
            self.insert("scaffolded-cluster-%d" % n, count=100, offset=n * 1000)
        self.assertEqual(self.count(), 3000)
        prune_terminal_executions(self.conn, total=1000, floor=0)
        self.assertEqual(self.count(), 1000)

    def test_the_floor_outranks_the_ceiling(self):
        """Evidence beats a size target: the newest 50 per job are never swept.

        The quiet job's rows are all older than the chatty job's, so the
        ceiling alone would take every one of them — the live crowd-out,
        reproduced. The floor keeps 50, and the board is deliberately left over
        its ceiling as a result: the real bound is the ceiling plus a floor per
        job id, which the module docstring states.
        """
        self.insert("compliance-audit", count=60)
        self.insert("profile-cron-tick", count=900, offset=100_000)
        prune_terminal_executions(self.conn, total=100, floor=MIN_TERMINAL_EXECUTIONS_PER_JOB)
        self.assertEqual(self.count("compliance-audit"), MIN_TERMINAL_EXECUTIONS_PER_JOB)
        self.assertEqual(self.count("profile-cron-tick"), 100)
        self.assertEqual(self.count(), 100 + MIN_TERMINAL_EXECUTIONS_PER_JOB)

    def test_the_defaults_are_the_ones_the_reasoning_was_done_against(self):
        """These three numbers are argued in the module docstring; pin them."""
        self.assertEqual(MAX_TERMINAL_EXECUTIONS_PER_JOB, 1000)
        self.assertEqual(MAX_TERMINAL_EXECUTIONS, 5000)
        self.assertEqual(MIN_TERMINAL_EXECUTIONS_PER_JOB, 50)
        # A minute-ly job's horizon must not regress from what the board has now.
        self.assertGreaterEqual(MAX_TERMINAL_EXECUTIONS_PER_JOB / 1440.0, 0.69)

    def test_pruning_an_empty_board_is_not_an_error(self):
        prune_terminal_executions(self.conn)
        self.assertEqual(self.count(), 0)


class ReasonTest(unittest.TestCase):
    def test_a_known_reason_passes_through(self):
        self.assertEqual(normalize_reason(SKIP_ALREADY_RUNNING), SKIP_ALREADY_RUNNING)

    def test_an_empty_reason_becomes_unspecified_rather_than_null(self):
        for value in (None, "", "   "):
            self.assertEqual(normalize_reason(value), SKIP_REASON_UNSPECIFIED)

    def test_an_unknown_reason_is_kept_not_dropped(self):
        """A skip under a bad label is still evidence; silence is the bug."""
        self.assertEqual(normalize_reason("Something-New"), "something-new")

    def test_an_unbounded_reason_is_clipped(self):
        self.assertEqual(len(normalize_reason("x" * 500)), 64)


class MissedWindowTest(unittest.TestCase):
    """The count on a catch-up row, since the row itself stands for a whole gap."""

    now = EPOCH + timedelta(hours=5)

    def test_an_interval_schedule_counts_the_lost_occurrences(self):
        schedule = {"kind": "interval", "minutes": 30}
        self.assertEqual(count_missed_occurrences(schedule, EPOCH, self.now), 10)

    def test_a_gap_shorter_than_the_period_lost_nothing(self):
        schedule = {"kind": "interval", "minutes": 1440}
        self.assertEqual(count_missed_occurrences(schedule, EPOCH, self.now), 0)

    def test_a_clock_that_went_backwards_reports_zero_not_a_negative(self):
        schedule = {"kind": "interval", "minutes": 30}
        self.assertEqual(count_missed_occurrences(schedule, self.now, EPOCH), 0)

    def test_a_runaway_gap_is_capped(self):
        schedule = {"kind": "interval", "minutes": 1}
        far = EPOCH + timedelta(days=365)
        self.assertEqual(count_missed_occurrences(schedule, EPOCH, far, cap=500), 500)

    def test_an_uncomputable_schedule_admits_it_rather_than_inventing_a_number(self):
        for schedule in ({}, {"kind": "cron"}, {"kind": "sunrise"}, None,
                         {"kind": "interval", "minutes": 0},
                         {"kind": "interval", "minutes": "soon"}):
            self.assertEqual(count_missed_occurrences(schedule, EPOCH, self.now), -1)

    @unittest.skipUnless(
        __import__("importlib").util.find_spec("croniter"),
        "croniter is only in the image venv",
    )
    def test_a_cron_schedule_counts_the_lost_occurrences(self):
        self.assertEqual(
            count_missed_occurrences({"kind": "cron", "expr": "0 * * * *"}, EPOCH, self.now),
            5,
        )

    def test_the_detail_carries_the_window_and_the_count(self):
        detail = missed_window_detail(
            {"kind": "interval", "minutes": 30}, EPOCH, self.now, 7200
        )
        self.assertIn("10 occurrence(s)", detail)
        self.assertIn(str(EPOCH), detail)
        self.assertIn(str(self.now), detail)
        self.assertIn("7200s", detail)

    def test_the_detail_says_unknown_rather_than_guessing(self):
        detail = missed_window_detail({"kind": "sunrise"}, EPOCH, self.now, 120)
        self.assertIn("an unknown number of", detail)


# --- the applier ------------------------------------------------------------
# Minimal stand-ins for the four Hermes modules: only the anchored regions are
# reproduced, at their real indentation, wrapped in just enough scaffolding to
# parse. Whether the anchors match the real tree is settled at build time by
# the applier's own count check; what is worth testing here is that a matched
# patch produces working code and that a second application fails loudly.

from apply_cron_skip_ledger import (  # noqa: E402
    EXEC_CONSTANTS,
    EXEC_CREATE_TABLE,
    EXEC_PRUNE,
    HEALTH_ERROR_CLASS,
    HEALTH_FLUSH,
    HEALTH_IMPORT,
    HEALTH_STATUSES,
    JOBS_CATCH_UP,
    PATCHES,
    SCHED_DISPATCH_CLAIM,
    SCHED_IMPORT,
    SCHED_JOB_LOCK_GUARD,
    SCHED_RUNNING_GUARD,
    SCHED_SHUTDOWN_GUARD,
    TOOLS_IMPORT,
    TOOLS_LOCK_GUARD,
    TOOLS_REGISTER_GUARD,
    apply,
)

FAKE_EXECUTIONS = (
    "import sqlite3\n\n"
    + EXEC_CONSTANTS
    + "\n\ndef _initialize_schema(conn):\n"
    + EXEC_CREATE_TABLE
    + '        "ON executions(job_id, claimed_at DESC, id DESC)"\n    )\n\n\n'
    + EXEC_PRUNE
    + "\n\ndef recover_interrupted_executions() -> int:\n    return 0\n"
)

FAKE_SCHEDULER = (
    SCHED_IMPORT
    + "\n\n\ndef tick():\n    def _submit_with_guard(job):\n"
    "        job_id = job[\"id\"]\n        if True:\n"
    + SCHED_SHUTDOWN_GUARD
    + SCHED_RUNNING_GUARD
    + SCHED_JOB_LOCK_GUARD
    + "            return True\n"
    "    return _submit_with_guard\n\n\n"
    "def run_one_job(job):\n    execution_id = \"x\"\n    for _ in (1,):\n"
    "        if not claim_dispatch(job[\"id\"]):\n"
    + SCHED_DISPATCH_CLAIM
    + "    return True\n"
)

FAKE_JOBS = (
    "import logging\n\nlogger = logging.getLogger(__name__)\n\n\n"
    "def get_due_jobs():\n    due = []\n    for job in ():\n"
    "        if True:\n            if True:\n                if True:\n"
    "                    if True:\n"
    + JOBS_CATCH_UP
    + "        due.append(job)\n    return due\n"
)

FAKE_HEALTH = (
    HEALTH_IMPORT
    + "\n\n"
    + HEALTH_STATUSES
    + "\n\n\ndef project_execution_event(record, status=None):\n"
    "    return CronExecutionEvent(\n        status=status,\n"
    + HEALTH_ERROR_CLASS
    + "    )\n\n\ndef _emit(event, target):\n    if target is not None:\n"
    + HEALTH_FLUSH
)

# _run_claimed_job as it stands *after* apply_cron_tick_lock_scope.py has run:
# the flock guard is that patch's insertion, and this applier anchors on it. A
# fixture written against pristine upstream would test an ordering the image
# never has.
FAKE_CRONJOB_TOOLS = (
    "from cron.jobs import (\n"
    + TOOLS_IMPORT
    + "\n\ndef _run_claimed_job(job):\n"
    '    job_id = job["id"]\n'
    "    _registered = False\n"
    "    _run_lock = None\n"
    "    try:\n"
    "        from cron.scheduler import (\n"
    "            _job_locks,\n"
    "            release_running_job,\n"
    "            run_one_job,\n"
    "            try_register_running_job,\n"
    "        )\n\n"
    + TOOLS_REGISTER_GUARD
    + '                "claimed": True,\n'
    '                "success": False,\n'
    '                "error": "already running",\n'
    "            }\n"
    "        _registered = True\n"
    + TOOLS_LOCK_GUARD
    + '                "claimed": True,\n'
    '                "success": False,\n'
    '                "error": "already running in another process",\n'
    "            }\n"
    "        return run_one_job(job)\n"
    "    finally:\n"
    "        if _run_lock is not None:\n"
    "            _run_lock.release()\n"
)

FAKE_TREE = {
    "cron/executions.py": FAKE_EXECUTIONS,
    "cron/scheduler.py": FAKE_SCHEDULER,
    "cron/jobs.py": FAKE_JOBS,
    "agent/monitoring/cron_health.py": FAKE_HEALTH,
    "tools/cronjob_tools.py": FAKE_CRONJOB_TOOLS,
}


def apply_quietly(root):
    """``apply`` with its per-file build log swallowed.

    That log belongs in ``docker build`` output, not interleaved with a test
    summary — a suite whose last five lines are patch chatter hides its own
    verdict.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        apply(root)


class ApplierTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for relative, body in FAKE_TREE.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

    def read(self, relative):
        return (self.root / relative).read_text()

    def test_the_fixtures_are_the_shape_the_applier_expects(self):
        """Guards the fixtures themselves: an anchor must appear exactly once."""
        for relative, edits in PATCHES:
            body = FAKE_TREE[relative]
            for anchor, _, expected in edits:
                self.assertEqual(
                    body.count(anchor), expected, "%s: %r" % (relative, anchor[:60])
                )

    def test_it_patches_every_file(self):
        apply_quietly(self.root)
        executions = self.read("cron/executions.py")
        self.assertIn("'claimed','running','completed','failed','unknown','skipped'", executions)
        self.assertIn("skip_reason TEXT", executions)
        self.assertIn("_ensure_skip_schema(conn)", executions)
        self.assertIn("def record_skipped_execution(", executions)
        self.assertIn("def skip_execution(", executions)
        self.assertIn("_prune_terminal_executions(conn)", executions)
        self.assertIn("def recover_interrupted_executions() -> int:", executions)

        scheduler = self.read("cron/scheduler.py")
        for reason in (
            "SKIP_INTERPRETER_SHUTDOWN",
            "SKIP_ALREADY_RUNNING",
            "SKIP_ALREADY_RUNNING_ELSEWHERE",
            "SKIP_DISPATCH_CLAIM_REJECTED",
        ):
            self.assertIn(reason, scheduler)
        self.assertNotIn("finish_execution(\n                execution_id,", scheduler)

        self.assertIn("SKIP_MISSED_WINDOW", self.read("cron/jobs.py"))
        health = self.read("agent/monitoring/cron_health.py")
        self.assertIn('"unknown", "skipped"}', health)
        self.assertIn("_normalize_skip_reason(record.get(\"skip_reason\"))", health)

        tools = self.read("tools/cronjob_tools.py")
        self.assertIn("from tools.cron_skip_ledger import (", tools)
        for reason in ("SKIP_ALREADY_RUNNING,", "SKIP_ALREADY_RUNNING_ELSEWHERE,"):
            self.assertIn("reason=%s" % reason.rstrip(","), tools)

    def test_both_dispatch_refusals_record_a_skip(self):
        """Each guard in _run_claimed_job drops an occurrence, so each writes.

        The pair mirrors tick's: an in-process register refusal and a
        cross-process flock refusal, taking the same two reasons. What makes
        them recordable at all is that both sit after ``claim_job_for_fire`` has
        advanced ``next_run_at`` — before v2026.8.13 the flock was taken ahead
        of the claim and a refusal cost nothing, so recording one would have
        manufactured a false skip.
        """
        apply_quietly(self.root)
        tools = self.read("tools/cronjob_tools.py")
        register = tools.split("if not try_register_running_job(job_id):", 1)
        self.assertEqual(len(register), 2, "the in-flight guard is not where it was")
        self.assertLess(
            register[1].index("reason=SKIP_ALREADY_RUNNING,"),
            register[1].index("return {"),
            "the skip is recorded after the guard has already returned",
        )
        flock = tools.split("if _run_lock is None:", 1)
        self.assertEqual(len(flock), 2, "the flock guard is not where it was")
        self.assertLess(
            flock[1].index("reason=SKIP_ALREADY_RUNNING_ELSEWHERE,"),
            flock[1].index("return {"),
            "the skip is recorded after the guard has already returned",
        )

    def test_the_dispatched_job_is_released_before_its_skip_is_recorded(self):
        """Same constraint as the scheduler's flock guard, one file over.

        ``record_skip`` is a SQLite write on a PVC; holding the job in the
        scheduler's running set across it would wedge the job for as long as
        the write takes, on the path where nothing is waiting to notice.
        """
        apply_quietly(self.root)
        guard = self.read("tools/cronjob_tools.py").split("if _run_lock is None:", 1)[1]
        self.assertLess(
            guard.index("release_running_job(job_id)"),
            guard.index("SKIP_ALREADY_RUNNING_ELSEWHERE"),
        )

    def test_the_ledger_write_stays_out_of_the_running_lock(self):
        """A SQLite write inside _running_lock would serialise the dispatch pass.

        v2026.8.13 moved the lock inside ``try_register_running_job``, so this
        patch no longer has to restructure the guard to get the write out of the
        critical section — but the constraint is the same and the day upstream
        inlines the guard again this has to fail rather than pass silently. So
        it asserts the property — the lock is still encapsulated, and the write
        happens after the call that holds it has returned — rather than the
        restructuring it used to have to perform.
        """
        apply_quietly(self.root)
        scheduler = self.read("cron/scheduler.py")
        self.assertNotIn("with _running_lock:", scheduler)
        guard = scheduler.split("if not try_register_running_job(job_id):", 1)
        self.assertEqual(len(guard), 2, "the in-flight guard is not where it was")
        self.assertIn("SKIP_ALREADY_RUNNING,", guard[1])

    def test_the_job_is_released_before_the_cross_process_skip_is_recorded(self):
        """Otherwise a slow ledger write leaves the job wedged in the running set."""
        apply_quietly(self.root)
        guard = self.read("cron/scheduler.py").split("_job_lock is None:", 1)[1]
        self.assertLess(
            guard.index("release_running_job(job_id)"),
            guard.index("SKIP_ALREADY_RUNNING_ELSEWHERE"),
        )

    def test_the_migration_runs_before_the_indexes_are_recreated(self):
        """The rebuild drops them with the old table; order is load-bearing."""
        apply_quietly(self.root)
        executions = self.read("cron/executions.py")
        self.assertLess(
            executions.index("_ensure_skip_schema(conn)"),
            executions.index("CREATE INDEX IF NOT EXISTS idx_executions_job_claimed"),
        )

    def test_a_second_application_fails_loudly(self):
        """Half-patching an image is the failure mode worth being noisy about."""
        apply_quietly(self.root)
        with self.assertRaises(SystemExit):
            apply_quietly(self.root)

    def test_a_missing_file_fails_loudly(self):
        (self.root / "cron" / "jobs.py").unlink()
        with self.assertRaises(SystemExit):
            apply_quietly(self.root)

    def test_a_moved_anchor_fails_loudly_rather_than_mis_applying(self):
        """The base-image bump case: upstream changed, the patch must not guess."""
        path = self.root / "agent" / "monitoring" / "cron_health.py"
        path.write_text(path.read_text().replace(HEALTH_FLUSH, "        pass\n"))
        with self.assertRaises(SystemExit):
            apply_quietly(self.root)

    def test_the_patched_create_table_satisfies_the_migration_gate(self):
        """A ledger born from the patched DDL must never trigger a rebuild."""
        apply_quietly(self.root)
        ddl = (
            self.read("cron/executions.py")
            .split('"""CREATE TABLE IF NOT EXISTS executions (', 1)[1]
            .split('"""', 1)[0]
        )
        self.assertTrue(status_check_allows_skipped("CREATE TABLE executions (" + ddl))


if __name__ == "__main__":
    unittest.main()
