"""Unit tests for the ended-run patch installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kanban_ended_run as ker

# The #2947 rehearsal card.
TID = "t_43bd650c"
PID = 4242
LOCK = "agent-pod:1"

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, current_run_id INTEGER,
                    worker_pid INTEGER, claim_lock TEXT);
"""


class FakeKb:
    """The slice of hermes_cli.kanban_db the wrapper touches."""

    def __init__(self):
        self.events, self.comments = [], []

    @contextlib.contextmanager
    def write_txn(self, conn):
        yield

    def _append_event(self, conn, task_id, kind, payload, *, run_id=None):
        self.events.append((task_id, kind, payload, run_id))

    def add_comment(self, conn, task_id, author, body):
        self.comments.append((task_id, author, body))


class Board(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(str(Path(tempfile.mkdtemp()) / "kanban.db"))
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.kb = FakeKb()
        self.terminated = []
        self.block_calls = []
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
            os.environ.pop(var, None)
        latch = mock.patch.object(ker, "_LATCHED", None)
        latch.start()
        self.addCleanup(latch.stop)

    def card(self, status="running", run=2, pid=PID, lock=LOCK):
        self.conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", (TID, status, run, pid, lock))
        self.conn.commit()

    def status(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id = ?", (TID,)).fetchone()[0]

    def block_task(self, conn, task_id, *, reason=None, kind=None, expected_run_id=None):
        """Upstream's transition, as far as the wrapper can tell."""
        self.block_calls.append(expected_run_id)
        sql = "UPDATE tasks SET status = 'blocked', worker_pid = NULL WHERE id = ? AND status IN ('running', 'ready')"
        params = [task_id]
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(expected_run_id)
        return conn.execute(sql, params).rowcount == 1

    def terminate(self, pid, lock):
        self.terminated.append((pid, lock))
        return {"prev_pid": pid, "host_local": True, "termination_attempted": True,
                "terminated": True, "sigkill": False}

    def wrapped(self, terminate=None):
        return ker.stop_worker_on_external_block(
            self.kb, self.block_task, terminate=terminate or self.terminate)


class TestAnOutsideBlock(Board):
    def test_stops_the_running_worker_and_says_so(self):
        self.card()
        self.assertTrue(self.wrapped()(self.conn, TID, reason="operator"))
        self.assertEqual(self.status(), "blocked")
        self.assertEqual(self.terminated, [(PID, LOCK)])
        (event,) = self.kb.events
        self.assertEqual(event[:2], (TID, ker.TERMINATION_EVENT))
        self.assertEqual(event[3], 2)
        self.assertEqual(event[2]["blocked_by"], "the kanban CLI or dashboard")
        (comment,) = self.kb.comments
        self.assertIn("while run 2 was still working", comment[2])
        self.assertIn("cut short", comment[2])

    def test_is_pinned_to_the_run_it_saw(self):
        self.card()
        self.wrapped()(self.conn, TID, reason="operator")
        self.assertEqual(self.block_calls, [2])

    def test_a_card_that_moved_meanwhile_is_blocked_without_a_stop(self):
        """The run seen ended before the write: block what's there, stop nothing."""
        self.card()
        real = self.block_task

        def moved(conn, task_id, **kw):
            if kw.get("expected_run_id") is not None:
                conn.execute("UPDATE tasks SET current_run_id = 3 WHERE id = ?", (task_id,))
            return real(conn, task_id, **kw)

        self.block_task = moved
        self.assertTrue(self.wrapped()(self.conn, TID, reason="operator"))
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.kb.events, [])

    def test_a_failed_stop_still_blocks_and_is_recorded(self):
        self.card()

        def boom(pid, lock):
            raise OSError("no such process group")

        self.assertTrue(self.wrapped(boom)(self.conn, TID, reason="operator"))
        self.assertEqual(self.kb.events[0][2]["error"], "no such process group")
        self.assertIn("failed", self.kb.comments[0][2])

    def test_another_hosts_worker_is_named_as_not_stopped(self):
        self.card(lock="other-pod:1")

        def remote(pid, lock):
            return {"prev_pid": pid, "host_local": False, "terminated": False}

        self.wrapped(remote)(self.conn, TID, reason="operator")
        self.assertIn("runs on another host", self.kb.comments[0][2])

    def test_a_ready_card_has_no_worker_to_stop(self):
        self.card(status="ready", run=None, pid=None, lock=None)
        self.assertTrue(self.wrapped()(self.conn, TID, reason="operator"))
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.block_calls, [None])

    def test_a_refused_block_stops_nothing(self):
        self.card(status="done")
        self.assertFalse(self.wrapped()(self.conn, TID, reason="operator"))
        self.assertEqual(self.terminated, [])


class TestTheCardsOwnRun(Board):
    def test_a_block_naming_its_run_stops_nothing(self):
        self.card()
        self.assertTrue(self.wrapped()(self.conn, TID, reason="need input", expected_run_id=2))
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.block_calls, [2])

    def test_the_cards_own_worker_without_a_run_id_stops_nothing(self):
        """The rate-limit exit and the CLI inside a worker can block with no run id."""
        self.card()
        os.environ["HERMES_KANBAN_TASK"] = TID
        self.assertTrue(self.wrapped()(self.conn, TID, reason="429"))
        self.assertEqual(self.terminated, [])

    def test_a_caller_that_is_the_worker_process_stops_nothing(self):
        self.card(pid=os.getpid())
        self.wrapped()(self.conn, TID, reason="self")
        self.assertEqual(self.terminated, [])


class TestEndedRunRefusal(Board):
    def worker(self, run=2):
        os.environ["HERMES_KANBAN_TASK"] = TID
        os.environ["HERMES_KANBAN_RUN_ID"] = str(run)

    def test_a_current_run_is_not_refused(self):
        self.card()
        self.worker()
        self.assertIsNone(ker.ended_run_refusal(TID, self.conn))

    def test_a_blocked_card_is_over(self):
        self.card(status="blocked", run=None)
        self.worker()
        text = ker.ended_run_refusal(TID, self.conn)
        self.assertIn("no longer yours to finish", text)
        self.assertIn("it is blocked", text)
        self.assertIn("Stop now", text)

    def test_a_card_another_run_holds_is_over(self):
        self.card(run=3)
        self.worker(run=2)
        self.assertIn("another run (run 3)", ker.ended_run_refusal(TID, self.conn))

    def test_the_answer_is_latched(self):
        self.card(status="blocked", run=None)
        self.worker()
        first = ker.ended_run_refusal(TID, self.conn)
        self.conn.execute("UPDATE tasks SET status = 'running', current_run_id = 2")
        self.assertEqual(ker.ended_run_refusal(TID, self.conn), first)
        self.assertEqual(ker.refuse_ended_run(TID, self.fail_connect), first)

    def test_not_a_dispatcher_worker_is_never_refused(self):
        self.card(status="blocked")
        self.assertIsNone(ker.ended_run_refusal(TID, self.conn))
        self.assertIsNone(ker.refuse_ended_run(TID, self.fail_connect))

    def test_a_worker_for_another_card_is_never_refused(self):
        self.card(status="blocked")
        os.environ["HERMES_KANBAN_TASK"] = "t_other"
        os.environ["HERMES_KANBAN_RUN_ID"] = "2"
        self.assertIsNone(ker.ended_run_refusal(TID, self.conn))

    def test_an_unreadable_board_fails_open(self):
        self.worker()

        def broken():
            raise sqlite3.OperationalError("database is locked")

        self.assertIsNone(ker.refuse_ended_run(TID, broken))
        self.conn.execute("DROP TABLE tasks")
        self.assertIsNone(ker.ended_run_refusal(TID, self.conn))

    def test_refuse_ended_run_closes_what_it_opens(self):
        self.card(status="blocked", run=None)
        self.worker()
        opened = []

        class Conn:
            def __init__(self, real):
                self.real, self.closed = real, False

            def execute(self, *a):
                return self.real.execute(*a)

            def close(self):
                self.closed = True

        def connect():
            opened.append(Conn(self.conn))
            return opened[-1]

        self.assertIsNotNone(ker.refuse_ended_run(TID, connect))
        self.assertTrue(opened[0].closed)

    @staticmethod
    def fail_connect():
        raise AssertionError("opened the board")


class TestCurrentRunFailure(unittest.TestCase):
    """The one last_failure_error the current run wrote: a PR-acceptance refusal."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            "CREATE TABLE tasks (id TEXT, current_run_id INTEGER, last_failure_error TEXT);"
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,"
            " kind TEXT, payload TEXT);")
        self.conn.execute("INSERT INTO tasks VALUES (?, 2, 'PR acceptance unmerged: fix the checks')", (TID,))

    def event(self, run, ok, kind="pr_acceptance"):
        self.conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                          (TID, run, kind, '{"ok": %s}' % ("true" if ok else "false")))

    def test_this_runs_failed_acceptance_is_reported(self):
        self.event(2, False)
        self.assertEqual(ker.current_run_failure(TID, self.conn), "PR acceptance unmerged: fix the checks")

    def test_an_earlier_runs_failure_is_not(self):
        self.event(1, False)
        self.assertIsNone(ker.current_run_failure(TID, self.conn))

    def test_a_passed_acceptance_is_not(self):
        self.event(2, True)
        self.assertIsNone(ker.current_run_failure(TID, self.conn))

    def test_no_acceptance_event_is_not(self):
        """The rehearsal: last_failure_error from run 1's exit, no acceptance at all."""
        self.assertIsNone(ker.current_run_failure(TID, self.conn))

    def test_an_unreadable_board_is_not(self):
        self.conn.execute("DROP TABLE task_events")
        self.assertIsNone(ker.current_run_failure(TID, self.conn))


class TestTheApplier(unittest.TestCase):
    TOOLS = (
        "def _handle_complete(args, **kw):\n"
        '    tid = _worker_guard("kanban_complete", args)\n'
        "    _children_err = _require_children_settled(tid, c, result)\n"
        "    with _board(None) as (kb, conn):\n"
        "        ok = kb.complete_task(conn, tid)\n"
        "        task = kb.get_task(conn, tid)\n"
        "        _check(ok, (task.last_failure_error if task else None) or\n"
        '               f"could not complete {tid} (unknown id, stale run, or already terminal)")\n'
        "\n"
        "def _handle_block(args, **kw):\n"
        '    tid = _worker_guard("kanban_block", args)\n'
        "    with _board(None) as (kb, conn):\n"
        "        ok = kb.block_task(conn, tid)\n"
        '        _check(ok, f"could not block {tid} (unknown id or not in running/ready)")\n'
    )

    def setUp(self):
        import apply_kanban_ended_run as applier

        self.applier = applier
        self.root = Path(tempfile.mkdtemp())
        self.db = self.root / applier.DB_RELATIVE
        self.tools = self.root / applier.TOOLS_RELATIVE
        self.db.parent.mkdir(parents=True)
        self.tools.parent.mkdir(parents=True)
        self.db.write_text("def block_task(conn, task_id, **kw):\n    return True\n")
        self.tools.write_text(self.TOOLS)

    def test_the_gates_come_first_and_the_stale_reason_is_gone(self):
        import ast

        self.applier.apply(self.root)
        out = self.tools.read_text()
        ast.parse(out)
        self.assertNotIn("last_failure_error", out)
        self.assertEqual(out.count("_kanban_refuse_ended_run(tid, _kanban_ended_run_connect)"), 2)
        self.assertLess(out.index("_kanban_refuse_ended_run("), out.index("_require_children_settled("))
        self.assertEqual(out.count("_kanban_ended_run_refusal(tid, conn) or"), 2)
        self.assertEqual(out.count("_kanban_current_run_failure(tid, conn) or"), 1)
        # The lookup latches, so it must only run on a refusal.
        self.assertEqual(out.count('_check(ok, "" if ok else ('), 2)
        db = self.db.read_text()
        ast.parse(db)
        self.assertIn("block_task = _kanban_stop_worker_on_external_block(", db)

    def test_a_second_run_refuses(self):
        self.applier.apply(self.root)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.root)

    def test_a_missing_anchor_fails_the_build_and_writes_nothing(self):
        self.tools.write_text(self.TOOLS.replace("last_failure_error", "failure"))
        before = self.db.read_text()
        with self.assertRaises(SystemExit):
            self.applier.apply(self.root)
        self.assertEqual(self.db.read_text(), before)


if __name__ == "__main__":
    unittest.main()
