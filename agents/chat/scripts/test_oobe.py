"""Unit tests for oobe.py, the first-boot job's first-run audits stage.

Run: python3 -m unittest agents/chat/scripts/test_oobe.py

oobe imports gitops_workspace from agents/platform/scripts, which the image copies
beside the chat scripts; the tests stand in for its repository list rather than
reading this machine's /etc/gitops.
"""

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))

import oobe  # noqa: E402

SWEEP_ID = "t_sweep"
FILED_AT = 1_000_000
SWEEP_CREATED_AT = FILED_AT
NOW_SETTLED = FILED_AT + 600
NOW_PAST_FALLBACK = FILED_AT + oobe.fallback_seconds(0)
REPOS = ["acme/gitops"]
FIRST = [oobe.FIRST_RUN_AUDITS[0]]
MINUTE = 60


def _board(path: Path, cards: list[tuple[str, str, str, int]]) -> None:
    """A board with the sweep card plus `cards` as (id, status, idempotency_key, created_at)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, idempotency_key TEXT, title TEXT, created_at INTEGER, body TEXT)")
    rows = [(SWEEP_ID, "done", "bootstrap-inventory-scan", SWEEP_CREATED_AT)] + cards
    conn.executemany("INSERT INTO tasks (id, status, idempotency_key, created_at) VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _ranking(status: str, key: str = oobe.bootstrap_handoff.PRIORITIZE_KEY, created_at: int = SWEEP_CREATED_AT + 60, tid: str = "t_rank"):
    return (tid, status, key, created_at)


class StageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.board = self.d / "kanban.db"
        self.started: list[tuple[list[str], dict]] = []
        self.failing: set[str] = set()
        self.repos: list[str] | Exception = list(REPOS)
        self._roster([{"id": job_id, "enabled": True, "state": "scheduled"} for job_id in oobe.FIRST_RUN_AUDITS])
        patches = [
            mock.patch.object(oobe, "board_path", lambda _d: self.board),
            mock.patch.object(oobe.subprocess, "run", self._run),
            mock.patch.object(oobe, "managed_repositories", self._repos),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv, env=None, **_kwargs):
        self.started.append((argv, env))
        code = 1 if argv[-1] in self.failing else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="no such job" if code else "")

    def _repos(self):
        if isinstance(self.repos, Exception):
            raise self.repos
        return self.repos

    def _roster(self, jobs: list[dict]) -> None:
        cron = self.d / "profiles" / "platform" / "cron"
        cron.mkdir(parents=True, exist_ok=True)
        (cron / "jobs.json").write_text(json.dumps({"jobs": jobs, "updated_at": "x"}), encoding="utf-8")

    def _file_scan(self, filed_at: int = FILED_AT) -> None:
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id={SWEEP_ID}\nfiled_at={filed_at}\n", encoding="utf-8")

    def _main(self, now: float = NOW_SETTLED) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(oobe.main(self.d, now=now), 0)
        return out.getvalue()

    def _started_ids(self) -> list[str]:
        return [argv[-1] for argv, _env in self.started]

    def _ledger(
        self, job_id: str, status: str, claimed_at: float, replace: bool = True, skip_reason: str | None = None
    ) -> None:
        """A run row in the Platform Agent's cron store, as profile-cron-tick leaves one.

        The skip ledger's ``skip_reason`` column is added only for a row that carries one, so the
        other tests read a store without it.
        """
        db = self.d / "profiles" / "platform" / "cron" / oobe.EXECUTIONS_DB
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS executions (id INTEGER PRIMARY KEY, job_id TEXT, status TEXT, claimed_at TEXT)")
            if skip_reason is not None:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ALTER TABLE executions ADD COLUMN skip_reason TEXT")
            # A run's row is updated in place as it ends; a new status replaces the job's in-flight row.
            if replace:
                conn.execute("DELETE FROM executions WHERE job_id = ? AND status IN ('claimed', 'running')", (job_id,))
            claimed = datetime.fromtimestamp(claimed_at, timezone.utc).isoformat()
            if skip_reason is None:
                conn.execute("INSERT INTO executions (job_id, status, claimed_at) VALUES (?, ?, ?)", (job_id, status, claimed))
            else:
                conn.execute(
                    "INSERT INTO executions (job_id, status, claimed_at, skip_reason) VALUES (?, ?, ?, ?)",
                    (job_id, status, claimed, skip_reason),
                )

    def _drive(self, now: float = NOW_SETTLED, ticks: int = 20) -> float:
        """Tick the stage, completing each audit's run a minute after it is marked, until done."""
        for _ in range(ticks):
            self._main(now=now)
            state = oobe.read_state(self.d)
            if state.get(oobe.STATE_DONE):
                return now
            current = state.get(oobe.STATE_CURRENT)
            if current and current[oobe.CURRENT_MARKED_AT] == now:
                self._ledger(current[oobe.CURRENT_JOB], "completed", now + MINUTE)
            now += 2 * MINUTE
        self.fail("the chain did not finish")

    # --- when the stage fires -------------------------------------------------

    def test_nothing_before_the_scan_is_filed(self):
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])
        self.assertFalse((self.d / oobe.AUDITS_MARKER).exists())

    def test_fires_once_the_ranking_card_is_done(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)
        state = oobe.read_state(self.d)
        self.assertFalse(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_CURRENT][oobe.CURRENT_JOB], FIRST[0])

    def test_waits_while_the_ranking_card_runs(self):
        self._file_scan()
        _board(self.board, [_ranking("running")])
        self._main()
        self.assertEqual(self.started, [])

    def test_a_failed_or_cancelled_ranking_card_fires(self):
        # The hand-off counts both as settled, and so does the stage.
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.started.clear()
                (self.d / oobe.AUDITS_MARKER).unlink(missing_ok=True)
                self.board.unlink(missing_ok=True)
                self._file_scan()
                _board(self.board, [_ranking(status)])
                self._main()
                self.assertEqual(self._started_ids(), FIRST)

    def test_the_hand_offs_no_ranking_record_fires_at_once(self):
        # No cluster audited: the hand-off wrote the report itself and files no ranking card.
        self._file_scan()
        _board(self.board, [])
        (self.d / ".bootstrap_handoff_filed").write_text(f"sweep={SWEEP_ID}\ntask_id=none\nfiled_at={FILED_AT}\n")
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_the_hand_offs_recorded_card_decides(self):
        # Only the card the hand-off filed counts, whatever else sits under the key.
        (self.d / ".bootstrap_handoff_filed").write_text(f"sweep={SWEEP_ID}\ntask_id=t_real\nfiled_at={FILED_AT}\n")
        self._file_scan()
        _board(self.board, [_ranking("done", tid="t_stray"), _ranking("running", tid="t_real")])
        self._main()
        self.assertEqual(self.started, [])
        self.board.unlink()
        _board(self.board, [_ranking("running", tid="t_stray"), _ranking("done", tid="t_real")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_record_for_another_sweep_is_ignored(self):
        self._file_scan()
        _board(self.board, [_ranking("running")])
        (self.d / ".bootstrap_handoff_filed").write_text("sweep=t_older\ntask_id=none\nfiled_at=1\n")
        self._main()
        self.assertEqual(self.started, [])

    def test_a_scan_marker_typed_by_hand_is_read(self):
        # The re-arm runbook has an operator write this file; the hand-off accepts `key = value`.
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id = {SWEEP_ID}\nfiled_at = {FILED_AT}\n")
        _board(self.board, [_ranking("done")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_blocked_ranking_card_waits_for_the_fallback(self):
        # A card a person may still unblock.
        self._file_scan()
        _board(self.board, [_ranking("blocked")])
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_waits_while_a_retry_still_runs(self):
        self._file_scan()
        _board(self.board, [_ranking("done"), _ranking("running", key=oobe.bootstrap_handoff.PRIORITIZE_KEY + "-retry-1", tid="t_retry")])
        self._main()
        self.assertEqual(self.started, [])

    def test_a_finished_retry_counts(self):
        self._file_scan()
        _board(self.board, [_ranking("done", key=oobe.bootstrap_handoff.PRIORITIZE_KEY + "-retry-1")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_an_archived_ranking_card_counts(self):
        # How the eval stack presents a settled scan (bench/tf/prebuilt/oobe-first-run-audits).
        self._file_scan()
        _board(self.board, [_ranking("archived", key=oobe.bootstrap_handoff.PRIORITIZE_KEY + "-oobe-eval-20261006")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_an_earlier_runs_ranking_card_does_not_count(self):
        # Left on the board by a run before onboarding was re-armed.
        self._file_scan()
        _board(self.board, [_ranking("done", created_at=SWEEP_CREATED_AT - 3600)])
        self._main()
        self.assertEqual(self.started, [])

    def test_no_ranking_card_fires_at_the_fallback(self):
        # A sweep that audited no cluster files no ranking card.
        self._file_scan()
        _board(self.board, [])
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_large_fleet_waits_out_the_hand_offs_deadline(self):
        # The hand-off files the ranking card only after its per-cluster deadline.
        clusters = [(f"t_c{i}", "running", f"bootstrap-inventory-cluster-c{i}", SWEEP_CREATED_AT + 1) for i in range(10)]
        self._file_scan()
        _board(self.board, clusters)
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])
        self._main(now=FILED_AT + oobe.fallback_seconds(10))
        self.assertEqual(self._started_ids(), FIRST)

    def test_the_fallback_follows_the_hand_offs_deadline(self):
        handoff = oobe.bootstrap_handoff
        self.assertEqual(
            oobe.fallback_seconds(4),
            handoff.DEADLINE_SECONDS + 4 * handoff.DEADLINE_PER_CARD_SECONDS + oobe.RANKING_ALLOWANCE_SECONDS,
        )

    def test_an_unreadable_board_waits_for_the_fallback(self):
        self._file_scan()
        self.board.write_text("not a database")
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_marker_without_filed_at_falls_back_to_its_age(self):
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id={SWEEP_ID}\n", encoding="utf-8")
        _board(self.board, [])
        mtime = (self.d / oobe.SCAN_FILED_MARKER).stat().st_mtime
        self._main(now=mtime + 60)
        self.assertEqual(self.started, [])
        self._main(now=mtime + oobe.fallback_seconds(0))
        self.assertEqual(self._started_ids(), FIRST)

    # --- how it starts them ---------------------------------------------------

    def test_marks_each_audit_due_on_the_platform_roster(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS))
        for argv, env in self.started:
            self.assertEqual(argv[:3], [sys.executable, "-c", oobe.TRIGGER_SCRIPT])
            self.assertEqual(env["HERMES_HOME"], str(self.d / "profiles" / "platform"))

    def test_the_chain_follows_1866s_order(self):
        self.assertEqual(
            oobe.FIRST_RUN_AUDITS,
            ("fleet-wide-cost-analysis", "compliance-audit", "obtainability-audit", "stockout-prevention"),
        )

    def test_the_trigger_marks_the_job_due_and_does_not_run_it(self):
        # `hermes cron run` runs the job in the calling process; trigger_job only schedules it.
        self.assertIn("from cron.jobs import trigger_job", oobe.TRIGGER_SCRIPT)
        self.assertNotIn("cron run", oobe.TRIGGER_SCRIPT)

    def test_the_trigger_script_reports_an_unknown_job(self):
        cron = self.d / "stub" / "cron"
        cron.mkdir(parents=True)
        (cron / "__init__.py").write_text("")
        (cron / "jobs.py").write_text("def trigger_job(job_id):\n    return {'id': job_id} if job_id == 'known' else None\n")
        env = {"PYTHONPATH": str(self.d / "stub")}
        for job_id, code in (("known", 0), ("unknown", 3)):
            done = subprocess.call([sys.executable, "-c", oobe.TRIGGER_SCRIPT, job_id], env=env)
            self.assertEqual(done, code)

    def test_prints_nothing(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.assertEqual(self._main(), "")

    # --- the chain ------------------------------------------------------------

    def test_the_next_audit_waits_for_the_previous_run_to_end(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self.assertEqual(self._started_ids(), FIRST)
        # Not yet claimed by the scheduler: nothing new.
        self._main(now=NOW_SETTLED + MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        # Running: still nothing new.
        self._ledger(FIRST[0], "running", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        # Ended: the next one is marked.
        self._ledger(FIRST[0], "completed", NOW_SETTLED + 3 * MINUTE)
        self._main(now=NOW_SETTLED + 4 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_a_failed_run_still_moves_the_chain_on(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "failed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_a_run_from_before_the_mark_does_not_count(self):
        # Yesterday's scheduled run of the same audit is not this mark's.
        self._ledger(FIRST[0], "completed", NOW_SETTLED - 3600)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._main(now=NOW_SETTLED + MINUTE)
        self.assertEqual(self._started_ids(), FIRST)

    def test_it_is_done_once_the_last_audit_has_started(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        state = oobe.read_state(self.d)
        self.assertTrue(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_FIRED], list(oobe.FIRST_RUN_AUDITS))

    def test_done_while_the_last_audit_is_still_running(self):
        # Nothing is left to mark, so the stage need not wait for the last run to end.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = NOW_SETTLED
        for job_id in oobe.FIRST_RUN_AUDITS:
            self._main(now=now)
            status = "running" if job_id == oobe.FIRST_RUN_AUDITS[-1] else "completed"
            self._ledger(job_id, status, now + MINUTE)
            now += 2 * MINUTE
        self._main(now=now)
        self.assertTrue(oobe.read_state(self.d)[oobe.STATE_DONE])

    def test_a_mark_never_claimed_is_made_again(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS + MINUTE)
        self.assertEqual(self._started_ids(), FIRST * 2)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_ATTEMPTS], {FIRST[0]: 1})

    def test_a_run_that_never_ends_stops_holding_the_chain(self):
        # A row a gateway restart left at running.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "running", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + oobe.RUN_LIMIT_SECONDS)
        self.assertEqual(self._started_ids(), FIRST)
        self._main(now=NOW_SETTLED + MINUTE + oobe.RUN_LIMIT_SECONDS)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_a_claimed_run_holds_the_chain(self):
        # Claimed by the tick but not yet running is still in flight.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "claimed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self._ledger(FIRST[0], "completed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 3 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_the_first_mark_waits_for_a_scheduled_run(self):
        # The 06:20 compliance run is going when the scan settles.
        self._ledger("compliance-audit", "running", NOW_SETTLED - MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self.assertEqual(self.started, [])
        self._ledger("compliance-audit", "completed", NOW_SETTLED - MINUTE)
        self._main(now=NOW_SETTLED + MINUTE)
        self.assertEqual(self._started_ids(), FIRST)

    def test_the_next_mark_waits_for_a_scheduled_run(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "completed", NOW_SETTLED + MINUTE)
        # A scheduled stockout run starts before the chain reaches it.
        self._ledger("stockout-prevention", "running", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self._ledger("stockout-prevention", "completed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 3 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_a_skipped_mark_waits_for_the_scheduled_run_it_found(self):
        # Marked while its 06:20 run was going: the store records the mark as skipped.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "completed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        second = oobe.FIRST_RUN_AUDITS[1]
        self.assertEqual(self._started_ids()[-1], second)
        self._ledger(second, "running", NOW_SETTLED + MINUTE)
        self._ledger(second, "skipped", NOW_SETTLED + 3 * MINUTE, replace=False, skip_reason=oobe.SKIP_ALREADY_RUNNING)
        self._main(now=NOW_SETTLED + 4 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))
        self._ledger(second, "completed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 5 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:3]))

    def test_a_skip_for_another_reason_is_no_claim(self):
        # A mark skipped as the gateway shut down ran nothing; it is made again at the start limit.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "skipped", NOW_SETTLED + MINUTE, skip_reason="interpreter_shutdown")
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS + MINUTE)
        self.assertEqual(self._started_ids(), FIRST * 2)

    def test_a_mark_claimed_late_is_not_made_again(self):
        # The scheduler claims the mark after the start limit counted it as never started.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS)
        late = NOW_SETTLED + oobe.START_LIMIT_SECONDS + MINUTE
        self._ledger(FIRST[0], "running", late)
        self._main(now=late + MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self._ledger(FIRST[0], "completed", late)
        self._main(now=late + 2 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_the_mark_is_recorded_before_it_is_made(self):
        # A restart between the two must leave a record of the mark, not a mark with no record.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        seen = []
        run = self._run

        def recording(argv, env=None, **kwargs):
            seen.append(oobe.read_state(self.d).get(oobe.STATE_CURRENT))
            return run(argv, env=env, **kwargs)

        with mock.patch.object(oobe.subprocess, "run", recording):
            self._main()
        self.assertEqual(seen, [{oobe.CURRENT_JOB: FIRST[0], oobe.CURRENT_MARKED_AT: NOW_SETTLED}])

    def test_a_mark_refused_after_the_store_took_it_is_adopted(self):
        # The trigger reported a failure (a timeout, say) after committing the mark.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.failing.add(FIRST[0])
        self._main(now=NOW_SETTLED)
        state = oobe.read_state(self.d)
        self.assertNotIn(FIRST[0], state[oobe.STATE_FIRED])
        self.assertIsNone(state[oobe.STATE_CURRENT])
        self.failing.clear()
        self._ledger(FIRST[0], "completed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_each_mark_time_is_recorded(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_MARKS], {FIRST[0]: NOW_SETTLED})

    def test_retries_an_audit_that_failed_to_start(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.failing = {FIRST[0]}
        self._main()
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_FIRED], [])
        self.failing = set()
        self._main(now=NOW_SETTLED + MINUTE)
        self.assertEqual(self._started_ids(), FIRST * 2)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_FIRED], FIRST)

    def test_a_run_killed_partway_does_not_start_an_audit_twice(self):
        # Killed after marking, before the next tick: the mark is in the marker.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self.started.clear()
        self._main(now=NOW_SETTLED + MINUTE)
        self.assertEqual(self.started, [])

    def test_gives_up_on_an_audit_that_never_starts(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.failing = {"stockout-prevention"}
        self._drive()
        state = oobe.read_state(self.d)
        self.assertTrue(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_GAVE_UP], ["stockout-prevention"])
        self.assertEqual(self._started_ids().count("stockout-prevention"), oobe.MAX_TRIGGER_ATTEMPTS)

    def test_a_trigger_that_times_out_is_a_failed_start(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])

        def slow(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, oobe.TRIGGER_TIMEOUT_SECONDS)

        with mock.patch.object(oobe.subprocess, "run", slow):
            self._main()
        state = oobe.read_state(self.d)
        self.assertEqual(state[oobe.STATE_FIRED], [])
        self.assertEqual(state[oobe.STATE_ATTEMPTS], {FIRST[0]: 1})
        self.assertFalse(state[oobe.STATE_DONE])

    # --- an install that is not new -------------------------------------------

    def test_a_sweep_filed_before_the_job_existed_is_skipped(self):
        # Onboarded but never delivered, so the entrypoint could not tell it is not new.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=FILED_AT + oobe.NEW_INSTALL_SECONDS)
        self.assertEqual(self.started, [])
        state = oobe.read_state(self.d)
        self.assertTrue(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_REASON], oobe.SKIP_NOT_NEW)

    def test_a_chain_already_under_way_is_not_cut_off_by_age(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main()
        self._ledger(FIRST[0], "completed", NOW_SETTLED + MINUTE)
        self._main(now=FILED_AT + oobe.NEW_INSTALL_SECONDS)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

    def test_a_disabled_or_paused_audit_is_left_alone(self):
        # trigger_job would set enabled back to true.
        self._roster([
            {"id": "compliance-audit", "enabled": False},
            {"id": "obtainability-audit", "enabled": True, "state": "paused", "paused_at": "2026-10-06T00:00:00"},
            {"id": "fleet-wide-cost-analysis", "enabled": True},
        ])
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertEqual(self._started_ids(), ["fleet-wide-cost-analysis"])
        state = oobe.read_state(self.d)
        self.assertEqual(state[oobe.STATE_HELD], {
            "compliance-audit": "disabled",
            "obtainability-audit": "paused",
            "stockout-prevention": oobe.HOLD_MISSING,
        })
        self.assertTrue(state[oobe.STATE_DONE])

    def test_an_unreadable_roster_starts_nothing_and_counts_an_attempt(self):
        (self.d / "profiles" / "platform" / "cron" / "jobs.json").write_text("{not json")
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main()
        self.assertEqual(self.started, [])
        state = oobe.read_state(self.d)
        self.assertFalse(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_ATTEMPTS], {FIRST[0]: 1})

    # --- no GitOps repository -------------------------------------------------

    def test_no_repository_skips_and_finishes(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.repos = []
        self._main()
        self.assertEqual(self.started, [])
        state = oobe.read_state(self.d)
        self.assertTrue(state[oobe.STATE_DONE])
        self.assertEqual(state[oobe.STATE_REASON], oobe.SKIP_NO_REPOSITORY)

    def test_an_unreadable_repository_list_is_retried(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self.repos = RuntimeError("kubectl binary not found in PATH")
        self._main()
        self.assertEqual(self.started, [])
        self.assertFalse((self.d / oobe.AUDITS_MARKER).exists())
        self.repos = list(REPOS)
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    # --- once only ------------------------------------------------------------

    def test_once_done_it_removes_itself_and_starts_nothing(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = self._drive()
        self.started.clear()
        removed = []
        jobs = types.ModuleType("cron.jobs")
        jobs.remove_job = removed.append
        with mock.patch.dict(sys.modules, {"cron": types.ModuleType("cron"), "cron.jobs": jobs}):
            self._main(now=now + MINUTE)
        self.assertEqual(self.started, [])
        self.assertEqual(removed, [oobe.OOBE_JOB_ID])

    def test_a_corrupt_marker_is_read_as_not_started(self):
        (self.d / oobe.AUDITS_MARKER).write_text("{not json", encoding="utf-8")
        self.assertEqual(oobe.read_state(self.d), {})

    def test_the_marker_is_json(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main()
        state = json.loads((self.d / oobe.AUDITS_MARKER).read_text(encoding="utf-8"))
        self.assertEqual(state[oobe.STATE_FIRED], FIRST)

class RosterTest(unittest.TestCase):
    def test_every_audit_is_on_the_platform_roster(self):
        roster = Path(__file__).resolve().parents[2] / "platform" / "cron" / "jobs.json"
        ids = {job["id"] for job in json.loads(roster.read_text(encoding="utf-8"))["jobs"] if job.get("enabled")}
        self.assertLessEqual(set(oobe.FIRST_RUN_AUDITS), ids)

    def test_the_entrypoint_keeps_the_job_off_finished_installs(self):
        entrypoint = (Path(__file__).resolve().parents[3] / "deploy" / "shared" / "docker-entrypoint.sh").read_text()
        seeded = next(line for line in entrypoint.splitlines() if line.strip().startswith('ASSUME_RETIRED="bootstrap'))
        self.assertIn(oobe.OOBE_JOB_ID, seeded.split('"')[1].split(","))

    def test_the_job_is_on_the_chat_roster(self):
        roster = Path(__file__).resolve().parent.parent / "defaults" / "cron" / "jobs.json"
        jobs = {job["id"]: job for job in json.loads(roster.read_text(encoding="utf-8"))["jobs"]}
        job = jobs[oobe.OOBE_JOB_ID]
        self.assertEqual(job["script"], "oobe.py")
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["deliver"], "local")


if __name__ == "__main__":
    unittest.main()
