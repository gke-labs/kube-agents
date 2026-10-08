"""Unit tests for oobe.py, the first-boot job's first-run audits stage.

Run: python3 -m unittest agents/chat/scripts/test_oobe.py

oobe imports gitops_workspace from agents/platform/scripts, which the image copies
beside the chat scripts; the tests stand in for its repository list rather than
reading this machine's /etc/gitops.
"""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
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
# The hand-off's deadline for a sweep with no cluster cards, plus the ranking allowance.
FALLBACK_SECONDS = oobe.bootstrap_handoff.DEADLINE_SECONDS + oobe.RANKING_ALLOWANCE_SECONDS
NOW_PAST_FALLBACK = FILED_AT + FALLBACK_SECONDS
RANKING_ID = "t_rank"
REPOS = ["acme/gitops"]
FIRST = [oobe.FIRST_RUN_AUDITS[0]]
MINUTE = 60


def _board(path: Path, cards: list[tuple[str, str, str, int]]) -> None:
    """A board with the sweep card plus `cards` as (id, status, idempotency_key, created_at).

    The tables the hand-off's board read takes, in the board's own names (test_bootstrap_handoff._board).
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT, status TEXT, idempotency_key TEXT, title TEXT, created_at INTEGER, body TEXT);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, metadata TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);"
    )
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
        # The scan and delivery stages are their own modules, tested beside them; here they are stubs.
        self.scan = mock.Mock(return_value=0)
        self.deliver = mock.Mock(return_value=0)
        patches = [
            mock.patch.object(oobe, "board_path", lambda _d: self.board),
            mock.patch.object(oobe.subprocess, "run", self._run),
            mock.patch.object(oobe, "managed_repositories", self._repos),
            mock.patch.object(oobe.bootstrap_scan_gate, "main", self.scan),
            mock.patch.object(oobe.bootstrap_delivery, "main", self.deliver),
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

    def _file_scan(self, filed_at: int = FILED_AT, ranking: str | None = RANKING_ID) -> None:
        """The gate's sweep marker, and the hand-off's record of its ranking card unless ``ranking`` is None."""
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id={SWEEP_ID}\nfiled_at={filed_at}\n", encoding="utf-8")
        if ranking is not None:
            (self.d / oobe.bootstrap_handoff.HANDOFF_MARKER).write_text(
                f"sweep={SWEEP_ID}\ntask_id={ranking}\nfiled_at={filed_at}\n", encoding="utf-8"
            )

    def _main(self, now: float = NOW_SETTLED) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(oobe.main(self.d, now=now), 0)
        return out.getvalue()

    def _started_ids(self) -> list[str]:
        return [argv[-1] for argv, _env in self.started]

    def _ledger(
        self,
        job_id: str,
        status: str,
        claimed_at: float,
        replace: bool = True,
        skip_reason: str | None = None,
        finished_at: float | None = None,
    ) -> None:
        """A run row in the Platform Agent's cron store, as profile-cron-tick leaves one.

        The ``skip_reason`` and ``finished_at`` columns are added only for a row that carries one,
        so the other tests read a store without them.
        """
        db = self.d / "profiles" / "platform" / "cron" / oobe.EXECUTIONS_DB
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS executions (id INTEGER PRIMARY KEY, job_id TEXT, status TEXT, claimed_at TEXT)")
            columns = {"job_id": job_id, "status": status}
            if skip_reason is not None:
                columns["skip_reason"] = skip_reason
            if finished_at is not None:
                columns["finished_at"] = datetime.fromtimestamp(finished_at, timezone.utc).isoformat()
            for optional in ("skip_reason", "finished_at"):
                if optional in columns:
                    with contextlib.suppress(sqlite3.OperationalError):
                        conn.execute(f"ALTER TABLE executions ADD COLUMN {optional} TEXT")
            # A run's row is updated in place as it ends; a new status replaces the job's in-flight row.
            if replace:
                conn.execute("DELETE FROM executions WHERE job_id = ? AND status IN ('claimed', 'running')", (job_id,))
            columns["claimed_at"] = datetime.fromtimestamp(claimed_at, timezone.utc).isoformat()
            conn.execute(
                f"INSERT INTO executions ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(columns.values()),
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
        self._file_scan(ranking="t_real")
        _board(self.board, [_ranking("done", tid="t_stray"), _ranking("running", tid="t_real")])
        self._main()
        self.assertEqual(self.started, [])
        self.board.unlink()
        _board(self.board, [_ranking("running", tid="t_stray"), _ranking("done", tid="t_real")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_record_for_another_sweep_is_ignored(self):
        self._file_scan(ranking=None)
        _board(self.board, [_ranking("done")])
        (self.d / ".bootstrap_handoff_filed").write_text("sweep=t_older\ntask_id=none\nfiled_at=1\n")
        self._main()
        self.assertEqual(self.started, [])

    def test_a_scan_marker_typed_by_hand_is_read(self):
        # The re-arm runbook has an operator write this file; the hand-off accepts `key = value`.
        self._file_scan()
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id = {SWEEP_ID}\nfiled_at = {FILED_AT}\n")
        _board(self.board, [_ranking("done")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_scan_marker_the_hand_off_refuses_starts_nothing(self):
        # No readable task_id: the hand-off hands nothing off, so the stage must not fire either,
        # not even at the fallback.
        _board(self.board, [_ranking("done")])
        for text in ("task_id=\n", f"task_id = {SWEEP_ID} # re-armed\n", f"task={SWEEP_ID}\n"):
            with self.subTest(marker=text):
                (self.d / oobe.SCAN_FILED_MARKER).write_text(text + f"filed_at={FILED_AT}\n")
                self._main(now=NOW_PAST_FALLBACK)
                self.assertEqual(self.started, [])

    def test_a_refused_marker_is_given_up_on_after_a_day(self):
        _board(self.board, [])
        marker = self.d / oobe.SCAN_FILED_MARKER
        marker.write_text("task_id=\n")
        self._main(now=marker.stat().st_mtime + oobe.NEW_INSTALL_SECONDS - MINUTE)
        self.assertEqual(oobe.read_state(self.d), {})
        self._main(now=marker.stat().st_mtime + oobe.NEW_INSTALL_SECONDS)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_REASON], oobe.SKIP_NO_SWEEP)

    def test_a_finished_recorded_card_needs_no_sweep_row(self):
        # Archived cards purged from the board: the recorded ranking card still answers.
        self._file_scan()
        conn = sqlite3.connect(self.board)
        conn.executescript("CREATE TABLE tasks (id TEXT, status TEXT, idempotency_key TEXT, title TEXT, created_at INTEGER, body TEXT);")
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, 'done')", (RANKING_ID,))
        conn.commit()
        conn.close()
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_board_that_cannot_be_opened_waits(self):
        # The recorded ranking card cannot be read: not finished, and not past the fallback either.
        self._file_scan()
        self.board = self.d / "no-such-dir" / "kanban.db"
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])

    def test_a_sweep_not_on_the_board_waits_past_the_fallback(self):
        # The re-arm runbook's `task_id=pending` placeholder: the hand-off waits for it, and so does the stage.
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id=pending\nfiled_at={FILED_AT}\n")
        _board(self.board, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])

    def test_a_blocked_ranking_card_waits_for_the_fallback(self):
        # A card a person may still unblock.
        self._file_scan()
        _board(self.board, [_ranking("blocked")])
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_an_archived_recorded_card_counts(self):
        # How the eval stack presents a settled scan (bench/tf/prebuilt/oobe-first-run-audits).
        self._file_scan()
        _board(self.board, [_ranking("archived")])
        self._main()
        self.assertEqual(self._started_ids(), FIRST)

    def test_without_the_hand_offs_record_a_finished_card_under_the_key_does_not_fire(self):
        # A ranking card the hand-off did not file, such as one a sweep worker filed against a raw
        # file that did not exist yet: the hand-off archives it as stale, and the stage waits.
        self._file_scan(ranking=None)
        for status in ("done", "failed"):
            with self.subTest(status=status):
                self.board.unlink(missing_ok=True)
                _board(self.board, [_ranking(status, tid="t_worker")])
                self._main()
                self.assertEqual(self.started, [])

    def test_no_ranking_card_fires_at_the_fallback(self):
        # The hand-off never recorded a card: the stage stops waiting at the hand-off's deadline.
        self._file_scan(ranking=None)
        _board(self.board, [])
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_large_fleet_waits_out_the_hand_offs_deadline(self):
        # The hand-off files the ranking card only after its per-cluster deadline.
        clusters = [(f"t_c{i}", "running", f"bootstrap-inventory-cluster-c{i}", SWEEP_CREATED_AT + 1) for i in range(10)]
        self._file_scan(ranking=None)
        _board(self.board, clusters)
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK + 10 * oobe.bootstrap_handoff.DEADLINE_PER_CARD_SECONDS)
        self.assertEqual(self._started_ids(), FIRST)

    def test_the_fallback_is_the_hand_offs_deadline_for_the_cards_it_counts(self):
        # An archived cluster card is not one the hand-off waits for, so the stage does not either.
        per_card = oobe.bootstrap_handoff.DEADLINE_PER_CARD_SECONDS
        clusters = [(f"t_c{i}", "running", f"bootstrap-inventory-cluster-c{i}", SWEEP_CREATED_AT + 1) for i in range(3)]
        clusters.append(("t_c3", "archived", "bootstrap-inventory-cluster-c3", SWEEP_CREATED_AT + 1))
        self._file_scan(ranking=None)
        _board(self.board, clusters)
        self._main(now=NOW_PAST_FALLBACK + 3 * per_card - 1)
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK + 3 * per_card)
        self.assertEqual(self._started_ids(), FIRST)

    def test_an_unreadable_board_waits_past_the_fallback(self):
        # Not the shortest fallback, which on a large fleet starts the audits beside the scan; it
        # fires once the board reads, and a board that never does is ended by the not-new rule.
        self._file_scan(ranking=None)
        self.board.write_text("not a database")
        self._main()
        self.assertEqual(self.started, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self.started, [])
        self.board.unlink()
        _board(self.board, [])
        self._main(now=NOW_PAST_FALLBACK)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_marker_without_filed_at_falls_back_to_its_age(self):
        (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id={SWEEP_ID}\n", encoding="utf-8")
        _board(self.board, [])
        mtime = (self.d / oobe.SCAN_FILED_MARKER).stat().st_mtime
        self._main(now=mtime + 60)
        self.assertEqual(self.started, [])
        self._main(now=mtime + FALLBACK_SECONDS)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_filed_at_that_is_not_epoch_seconds_falls_back_to_its_age(self):
        # Milliseconds, nan and inf would each switch off the fallback and the not-new rule.
        _board(self.board, [])
        for value in (int(time.time() * 1000), "nan", "inf", "-5"):
            with self.subTest(filed_at=value):
                self.started.clear()
                (self.d / oobe.AUDITS_MARKER).unlink(missing_ok=True)
                (self.d / oobe.SCAN_FILED_MARKER).write_text(f"task_id={SWEEP_ID}\nfiled_at={value}\n", encoding="utf-8")
                mtime = (self.d / oobe.SCAN_FILED_MARKER).stat().st_mtime
                self._main(now=mtime + 60)
                self.assertEqual(self.started, [])
                self._main(now=mtime + FALLBACK_SECONDS)
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
        self._ledger(FIRST[0], "completed", NOW_SETTLED - oobe.NEW_INSTALL_SECONDS)
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

    def test_a_run_past_an_hour_still_holds_the_chain(self):
        # Single audit runs on CI have reached 46 minutes; a slow one at 90 still holds.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "running", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 90 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)

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

    def test_a_scheduled_run_completed_since_the_sweep_is_not_run_again(self):
        # The 06:20 compliance run went while the scan was still settling: when the chain reaches
        # compliance it passes over it and marks the audit after.
        self._ledger("compliance-audit", "completed", FILED_AT + MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = self._drive()
        self.assertNotIn("compliance-audit", self._started_ids())
        self.assertEqual(self._started_ids(), [a for a in oobe.FIRST_RUN_AUDITS if a != "compliance-audit"])
        state = oobe.read_state(self.d)
        self.assertIn("compliance-audit", state[oobe.STATE_FIRED])
        self.assertEqual(state[oobe.STATE_ADOPTED], ["compliance-audit"])

    def test_a_run_under_way_when_the_sweep_was_filed_is_adopted(self):
        # The 06:20 run claimed while the reconcile held the gate, completed during onboarding.
        self._ledger("compliance-audit", "completed", FILED_AT - 20 * MINUTE, finished_at=FILED_AT + 5 * MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertNotIn("compliance-audit", self._started_ids())

    def test_a_run_that_finished_before_the_sweep_was_filed_is_not_adopted(self):
        # Claimed inside the run limit, but it saw the fleet before onboarding.
        self._ledger("compliance-audit", "completed", FILED_AT - 20 * MINUTE, finished_at=FILED_AT - 5 * MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS))
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_ADOPTED], [])

    def test_a_run_from_before_the_sweep_is_not_adopted(self):
        # Yesterday's scheduled run is not this install's first run.
        self._ledger("compliance-audit", "completed", FILED_AT - oobe.RUN_LIMIT_SECONDS - MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS))

    def test_a_failed_run_since_the_sweep_is_run_again(self):
        self._ledger("compliance-audit", "failed", FILED_AT + MINUTE)
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._drive()
        self.assertIn("compliance-audit", self._started_ids())

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

    def test_a_claim_later_closed_as_skipped_is_made_again(self):
        # The store closes a claimed row as skipped in place when the worker loses the fire claim:
        # that audit ran nothing, so it is marked again rather than counted and passed over.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "claimed", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        self._ledger(FIRST[0], "skipped", NOW_SETTLED + MINUTE, skip_reason="fire_claim_lost")
        self._main(now=NOW_SETTLED + 3 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS)
        self._main(now=NOW_SETTLED + oobe.START_LIMIT_SECONDS + MINUTE)
        self.assertEqual(self._started_ids(), FIRST * 2)

    def test_the_last_audits_claim_later_closed_as_skipped_is_made_again(self):
        # Done only once the last audit is running: a lost claim on it is marked again.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = NOW_SETTLED
        last = oobe.FIRST_RUN_AUDITS[-1]
        for _ in range(40):
            self._main(now=now)
            current = oobe.read_state(self.d).get(oobe.STATE_CURRENT)
            if current and current[oobe.CURRENT_JOB] == last:
                break
            if current and current[oobe.CURRENT_MARKED_AT] == now:
                self._ledger(current[oobe.CURRENT_JOB], "completed", now + MINUTE)
            now += 2 * MINUTE
        else:
            self.fail("the chain did not reach the last audit")
        self._ledger(last, "claimed", now + MINUTE)
        self._main(now=now + 2 * MINUTE)
        self.assertFalse(oobe.read_state(self.d)[oobe.STATE_DONE])
        self._ledger(last, "skipped", now + MINUTE, skip_reason="fire_claim_lost")
        self._main(now=now + oobe.START_LIMIT_SECONDS)
        self._main(now=now + oobe.START_LIMIT_SECONDS + MINUTE)
        self.assertEqual(self._started_ids().count(last), 2)
        self._ledger(last, "running", now + oobe.START_LIMIT_SECONDS + 2 * MINUTE)
        self._main(now=now + oobe.START_LIMIT_SECONDS + 3 * MINUTE)
        self.assertTrue(oobe.read_state(self.d)[oobe.STATE_DONE])

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

    def test_a_scan_still_unsettled_after_a_day_says_so(self):
        # A board that never reads, or a sweep it does not have, is ended by the age rule with
        # that reason, not with "filed before this job existed".
        self._file_scan()
        self.board.write_text("not a database")
        self._main(now=FILED_AT + oobe.NEW_INSTALL_SECONDS)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_REASON], oobe.SKIP_UNSETTLED)

    def test_no_cluster_audited_a_day_ago_is_old_not_unsettled(self):
        # The hand-off finished the scan itself (`task_id=none`): it settled.
        self._file_scan(ranking=oobe.bootstrap_handoff.NO_RANKING)
        _board(self.board, [])
        self._main(now=FILED_AT + oobe.NEW_INSTALL_SECONDS)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_REASON], oobe.SKIP_NOT_NEW)

    def test_a_ranking_card_unfinished_after_a_day_is_unsettled_not_old(self):
        # Past the fallback, but the recorded card is still blocked, or none was recorded: the scan
        # did not finish, whatever the fallback says.
        for ranking, cards in (("t_rank", [_ranking("blocked")]), (None, [])):
            with self.subTest(ranking=ranking):
                (self.d / oobe.AUDITS_MARKER).unlink(missing_ok=True)
                (self.d / oobe.bootstrap_handoff.HANDOFF_MARKER).unlink(missing_ok=True)
                self.board.unlink(missing_ok=True)
                self._file_scan(ranking=ranking)
                _board(self.board, cards)
                self._main(now=FILED_AT + oobe.NEW_INSTALL_SECONDS)
                self.assertEqual(oobe.read_state(self.d)[oobe.STATE_REASON], oobe.SKIP_UNSETTLED)

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
            # pause_job's own shape: enabled false beside the pause markers, so not "disabled".
            {"id": "obtainability-audit", "enabled": False, "state": "paused", "paused_at": "2026-10-06T00:00:00"},
            {"id": "stockout-prevention", "enabled": True, "paused_at": "2026-10-06T00:00:00"},
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
            "stockout-prevention": "paused",
        })
        self.assertTrue(state[oobe.STATE_DONE])

    def test_an_unreadable_roster_starts_nothing_and_spends_no_attempt(self):
        roster = self.d / "profiles" / "platform" / "cron" / "jobs.json"
        good = roster.read_text()
        roster.write_text("{not json")
        self._file_scan()
        _board(self.board, [_ranking("done")])
        for minute in range(oobe.MAX_TRIGGER_ATTEMPTS + 1):
            self._main(now=NOW_SETTLED + minute * MINUTE)
        self.assertEqual(self.started, [])
        state = oobe.read_state(self.d)
        self.assertFalse(state[oobe.STATE_DONE])
        self.assertEqual((state[oobe.STATE_ATTEMPTS], state[oobe.STATE_GAVE_UP]), ({}, []))
        roster.write_text(good)
        self._main(now=NOW_SETTLED + 10 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)

    def test_a_roster_whose_jobs_are_not_a_list_is_unreadable(self):
        # Not "every audit is missing": that would hold all four and retire having started none.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        roster = self.d / "profiles" / "platform" / "cron" / "jobs.json"
        for shape in ({"jobs": {"compliance-audit": {}}}, "jobs"):
            with self.subTest(shape=shape):
                roster.write_text(json.dumps(shape))
                self._main()
                state = oobe.read_state(self.d)
                self.assertEqual(self.started, [])
                self.assertEqual((state[oobe.STATE_HELD], state[oobe.STATE_ATTEMPTS], state[oobe.STATE_DONE]), ({}, {}, False))

    def test_an_unreadable_ledger_is_not_nothing_running(self):
        # A read that fails must hold the chain, not mark compliance beside the live cost run.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._main(now=NOW_SETTLED)
        self._ledger(FIRST[0], "running", NOW_SETTLED + MINUTE)
        self._main(now=NOW_SETTLED + 2 * MINUTE)
        ledger = self.d / "profiles" / "platform" / "cron" / oobe.EXECUTIONS_DB
        rows = ledger.read_bytes()
        ledger.write_text("not a database")
        self._main(now=NOW_SETTLED + 3 * MINUTE)
        self.assertEqual(self._started_ids(), FIRST)
        self.assertEqual(oobe.read_state(self.d)[oobe.STATE_ATTEMPTS], {})
        ledger.write_bytes(rows)
        self._ledger(FIRST[0], "completed", NOW_SETTLED + 4 * MINUTE)
        self._main(now=NOW_SETTLED + 5 * MINUTE)
        self.assertEqual(self._started_ids(), list(oobe.FIRST_RUN_AUDITS[:2]))

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

    def _claim(self, at: float) -> None:
        completed = self.d / ".bootstrap_completed"
        completed.touch()
        os.utime(completed, (at, at))

    def _main_removing(self, now: float) -> list[str]:
        removed: list[str] = []
        jobs = types.ModuleType("cron.jobs")
        jobs.remove_job = removed.append
        with mock.patch.dict(sys.modules, {"cron": types.ModuleType("cron"), "cron.jobs": jobs}):
            self._main(now=now)
        return removed

    def test_once_every_stage_is_done_it_removes_itself_and_the_old_jobs(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = self._drive()
        self.started.clear()
        self.deliver.reset_mock()
        self._claim(now - oobe.bootstrap_delivery.RETIRE_AFTER_SECONDS)
        removed = self._main_removing(now)
        self.assertEqual(self.started, [])
        # The old entries first; removing oobe ends the run.
        self.assertEqual(
            removed, [oobe.bootstrap_delivery.SCAN_JOB_ID, oobe.bootstrap_delivery.DELIVERY_JOB_ID, oobe.OOBE_JOB_ID]
        )
        self.deliver.assert_not_called()

    def test_it_stays_until_the_report_is_claimed(self):
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = self._drive()
        self.assertEqual(self._main_removing(now + MINUTE), [])
        self.deliver.assert_called_with(self.d)

    def test_a_young_claim_keeps_it(self):
        # The run that claimed the report may still be posting it.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        now = self._drive()
        self._claim(now - oobe.bootstrap_delivery.RETIRE_AFTER_SECONDS + MINUTE)
        self.assertEqual(self._main_removing(now), [])

    def test_a_claimed_report_does_not_end_the_audits(self):
        # Delivery can land before the chain ends; the job stays for the rest of it.
        self._file_scan()
        _board(self.board, [_ranking("done")])
        self._claim(NOW_SETTLED - oobe.bootstrap_delivery.RETIRE_AFTER_SECONDS)
        self.assertEqual(self._main_removing(NOW_SETTLED), [])
        self.assertEqual(self._started_ids(), FIRST)

    # --- the stages ------------------------------------------------------------

    def test_the_stages_run_delivery_then_scan_then_audits(self):
        # Delivery first, within seconds of the scheduler's snapshot of the job's destination.
        order = []
        self.scan.side_effect = lambda _d: order.append("scan")
        self.deliver.side_effect = lambda _d: order.append("deliver") or 0
        with mock.patch.object(oobe, "first_run_audits", lambda _d, _now: order.append("audits")):
            self._main()
        self.assertEqual(order, ["deliver", "scan", "audits"])

    def test_the_exit_is_deliverys(self):
        # A report delivery cannot read fails the run, which the scheduler posts as an alert.
        self.deliver.return_value = 1
        self.assertEqual(oobe.main(self.d, now=NOW_SETTLED), 1)

    def test_a_failing_stage_still_delivers(self):
        self.scan.side_effect = RuntimeError("board locked")
        with mock.patch.object(oobe, "first_run_audits", mock.Mock(side_effect=RuntimeError("roster gone"))):
            self.assertEqual(oobe.main(self.d, now=NOW_SETTLED), 0)
        self.deliver.assert_called_once_with(self.d)

    def test_only_delivery_reaches_stdout(self):
        # Once oobe is linked to the chat, whatever it prints is posted there.
        def speaks(_d, *_rest):
            print("scan chatter")
            os.write(oobe.STDOUT_FD, b"subprocess chatter\n")

        def delivers(_d):
            sys.stdout.write("REPORT")
            return 0

        self.scan.side_effect = speaks
        self.deliver.side_effect = delivers
        with tempfile.TemporaryFile() as captured, mock.patch.object(oobe, "first_run_audits", speaks):
            saved = os.dup(oobe.STDOUT_FD)
            out = io.StringIO()
            try:
                os.dup2(captured.fileno(), oobe.STDOUT_FD)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    oobe.main(self.d, now=NOW_SETTLED)
            finally:
                os.dup2(saved, oobe.STDOUT_FD)
                os.close(saved)
            captured.seek(0)
            self.assertEqual(captured.read(), b"")
        self.assertEqual(out.getvalue(), "REPORT")

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
