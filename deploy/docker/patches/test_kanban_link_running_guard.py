"""Unit tests for the kanban_link guard installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import apply_kanban_link_running_guard as applier
import kanban_link_running_guard as guard

# The #3248 shape (ka-today-rehearsal, 2026-10-15): the Platform Agent's card
# fanned out a child, the dispatcher claimed the child (run 33), and then the
# parent linked it to itself.
PARENT = "t_05ea923d"
CHILD = "t_bf2da8bf"

# The subset of hermes_cli/kanban_db.py's schema the guard reads.
BOARD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, claim_lock TEXT);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                        status TEXT NOT NULL, started_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
                         PRIMARY KEY (parent_id, child_id));
"""

# _handle_link as pinned Hermes v2026.9.14 ships it (tools/kanban_tools.py),
# with the helpers it calls stubbed to upstream's behaviour.
UPSTREAM_TOOLS = (
    "import contextlib\n"
    "import json\n"
    "\n"
    "def tool_error(message):\n"
    "    return json.dumps({'error': message})\n"
    "\n"
    "class _Reject(Exception):\n"
    "    def __init__(self, message):\n"
    "        super().__init__(tool_error(message))\n"
    "\n"
    "def _check(cond, message):\n"
    "    if not cond:\n"
    "        raise _Reject(message)\n"
    "\n"
    "def _kanban_handler(tool_name):\n"
    "    def deco(fn):\n"
    "        def wrapper(args, **kw):\n"
    "            try:\n"
    "                return fn(args, **kw)\n"
    "            except _Reject as e:\n"
    "                return e.args[0]\n"
    "            except Exception as e:\n"
    "                return tool_error(f'{tool_name}: {e}')\n"
    "        return wrapper\n"
    "    return deco\n"
    "\n"
    "def _reject_delegated_child_mutation(tool_name):\n"
    "    pass\n"
    "\n"
    "@contextlib.contextmanager\n"
    "def _board(board):\n"
    "    yield BOARD\n"
    "\n"
    "def _ok(**fields):\n"
    "    return json.dumps({'ok': True, **fields})\n"
    "\n"
    "@_kanban_handler(\"kanban_link\")\n"
    "def _handle_link(args: dict, **kw) -> str:\n"
    '    """Add a parent→child dependency edge after the fact (cycles/self-links → ValueError)."""\n'
    '    _reject_delegated_child_mutation("kanban_link")\n'
    '    parent_id = args.get("parent_id")\n'
    '    child_id = args.get("child_id")\n'
    '    _check(parent_id and child_id, "both parent_id and child_id are required")\n'
    '    with _board(args.get("board")) as (kb, conn):\n'
    "        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)\n"
    "        return _ok(parent_id=parent_id, child_id=child_id)\n"
)


class FakeKB:
    """The three hermes_cli.kanban_db calls the patched handler makes,
    against the schema above, with upstream's semantics."""

    @staticmethod
    def get_task(conn, task_id):
        row = conn.execute(
            "SELECT id, status, claim_lock FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return types.SimpleNamespace(**dict(row)) if row else None

    @staticmethod
    def latest_run(conn, task_id):
        row = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def link_tasks(conn, parent_id, child_id):
        if parent_id == child_id:
            raise ValueError("a task cannot depend on itself")
        conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent_id, child_id))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (child_id,))
        conn.commit()


class Board(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(BOARD_SCHEMA)
        self.card(PARENT, "running", claim_lock="gw:1", run="running")

    def card(self, task_id, status, claim_lock=None, run=None):
        self.conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (task_id, status, claim_lock))
        if run:
            self.conn.execute("INSERT INTO task_runs (task_id, status) VALUES (?, ?)", (task_id, run))
        self.conn.commit()

    def links(self):
        return [tuple(r) for r in self.conn.execute("SELECT parent_id, child_id FROM task_links")]

    def status(self, task_id):
        return self.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]


class TestThePatchedHandler(Board):
    """The applier's edit, applied to upstream's _handle_link and run."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        target = self.tmp / applier.RELATIVE
        target.parent.mkdir(parents=True)
        target.write_text(UPSTREAM_TOOLS, encoding="utf-8")
        applier.apply(self.tmp)
        self.patched_source = target.read_text(encoding="utf-8")
        tools_pkg = types.ModuleType("tools")
        modules = {"tools": tools_pkg, "tools.kanban_link_running_guard": guard}
        stub = mock.patch.dict(sys.modules, modules)
        stub.start()
        self.addCleanup(stub.stop)
        self.ns = {"BOARD": (FakeKB, self.conn)}
        exec(compile(self.patched_source, str(target), "exec"), self.ns)

    def link(self, parent=PARENT, child=CHILD):
        return json.loads(self.ns["_handle_link"]({"parent_id": parent, "child_id": child}))

    def running_text(self, parent=PARENT):
        return (
            f"kanban_link refused: {CHILD} is already running and reports back when it "
            f"finishes; a link now would make it wait for {parent} and strand its run. "
            "No link is needed: wait for its result."
        )

    def started_text(self, status):
        return (
            f"kanban_link refused: {CHILD} has already started (status: {status}); a "
            f"link now would gate it behind {PARENT}. Leave its dependencies as they "
            "are; if it is blocked, it needs an unblock, not a link."
        )

    def test_a_running_child_is_refused_with_the_exact_text(self):
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        out = self.link()
        self.assertEqual(
            out.get("error"),
            "kanban_link refused: t_bf2da8bf is already running and reports back "
            "when it finishes; a link now would make it wait for t_05ea923d and "
            "strand its run. No link is needed: wait for its result.",
        )
        self.assertEqual(self.links(), [])
        self.assertEqual(self.status(CHILD), "running")

    def test_the_callers_own_card_as_child_is_told_to_poll_the_parent(self):
        # Round 3 of review: kanban_link(parent=<helper>, child=<own card>)
        # is refused like any running child, but "wait for its result" would
        # tell the worker to wait for itself.
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        with mock.patch.dict(os.environ, {"HERMES_KANBAN_TASK": CHILD}):
            out = self.link()
        self.assertEqual(
            out.get("error"),
            f"kanban_link refused: {CHILD} is your own card and it is already running, "
            f"so a link would make it wait for {PARENT} and refuse your own completion. "
            f"To wait for {PARENT}, keep your card open and check it with kanban_show "
            "until it settles.",
        )
        self.assertEqual(self.links(), [])
        self.assertEqual(self.status(CHILD), "running")

    def test_a_worker_linking_someone_elses_running_card_gets_the_running_text(self):
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        for env in ({"HERMES_KANBAN_TASK": PARENT}, {}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=False):
                if not env:
                    os.environ.pop("HERMES_KANBAN_TASK", None)
                self.assertEqual(self.link().get("error"), self.running_text())

    def test_a_claimed_child_not_yet_running_is_refused(self):
        self.card(CHILD, "ready", claim_lock="gw:1")
        self.assertEqual(self.link().get("error"), self.running_text())
        self.assertEqual(self.links(), [])
        self.assertEqual(self.status(CHILD), "ready")

    def test_running_status_alone_is_enough(self):
        self.card(CHILD, "running")
        self.assertEqual(self.link().get("error"), self.running_text())
        self.assertEqual(self.links(), [])

    def test_a_child_with_a_finished_run_is_refused(self):
        self.card(CHILD, "ready", run="timed_out")
        self.assertEqual(self.link().get("error"), self.started_text("ready"))
        self.assertEqual(self.links(), [])
        self.assertEqual(self.status(CHILD), "ready")

    def test_a_done_child_is_refused(self):
        self.card(CHILD, "done", run="done")
        self.assertEqual(self.link().get("error"), self.started_text("done"))

    def test_a_blocked_child_is_told_to_unblock_not_to_wait(self):
        """A blocked card won't move until someone unblocks it, so "wait for
        its result" would leave the caller polling a card that never moves."""
        self.card(CHILD, "blocked", run="blocked")
        out = self.link().get("error")
        self.assertEqual(out, self.started_text("blocked"))
        self.assertNotIn("wait for its result", out)
        self.assertEqual(self.links(), [])
        self.assertEqual(self.status(CHILD), "blocked")

    def test_a_requeued_child_in_todo_is_refused(self):
        self.card(CHILD, "todo", run="crashed")
        self.assertEqual(self.link().get("error"), self.started_text("todo"))

    def test_the_parent_does_not_matter(self):
        self.card("t_other", "todo")
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        self.assertEqual(self.link(parent="t_other").get("error"), self.running_text("t_other"))
        self.assertEqual(self.links(), [])

    def test_a_ready_never_started_child_is_linked(self):
        self.card(CHILD, "ready")
        self.assertEqual(self.link(), {"ok": True, "parent_id": PARENT, "child_id": CHILD})
        self.assertEqual(self.links(), [(PARENT, CHILD)])
        self.assertEqual(self.status(CHILD), "todo")

    def test_a_todo_never_started_child_is_linked(self):
        self.card(CHILD, "todo")
        self.assertTrue(self.link().get("ok"))
        self.assertEqual(self.links(), [(PARENT, CHILD)])

    def test_upstream_still_answers_a_self_link(self):
        out = self.link(parent=PARENT, child=PARENT)
        self.assertEqual(out.get("error"), "kanban_link: a task cannot depend on itself")

    def test_the_gate_runs_before_the_link(self):
        self.assertLess(
            self.patched_source.index("_kanban_refuse_started_child_link("),
            self.patched_source.index("kb.link_tasks("),
        )
        ast.parse(self.patched_source)

    def test_only_the_link_handler_changes(self):
        self.assertEqual(self.patched_source.count("_kanban_refuse_started_child_link("), 1)
        self.assertNotIn("_handle_create", self.patched_source)


class TestTheGate(Board):
    def test_the_refusal_never_suggests_another_card(self):
        """A refused model takes a suggestion literally: pointing it at
        kanban_create or parents= gets the running child's work filed twice."""
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        self.card("t_blocked", "blocked", run="blocked")
        running = guard.refuse_started_child_link(FakeKB, self.conn, PARENT, CHILD)
        stopped = guard.refuse_started_child_link(FakeKB, self.conn, PARENT, "t_blocked")
        self.assertIn("wait for its result", running)
        for out in (running, stopped):
            for steer in ("parents", "kanban_create", "create"):
                self.assertNotIn(steer, out)

    def test_unknown_ids_are_left_to_upstream(self):
        self.card(CHILD, "running", claim_lock="gw:1", run="running")
        self.assertIsNone(guard.refuse_started_child_link(FakeKB, self.conn, "t_gone", CHILD))
        self.assertIsNone(guard.refuse_started_child_link(FakeKB, self.conn, PARENT, "t_gone"))

    def test_blank_ids_are_left_to_upstream(self):
        self.assertIsNone(guard.refuse_started_child_link(FakeKB, self.conn, "", CHILD))
        self.assertIsNone(guard.refuse_started_child_link(FakeKB, self.conn, PARENT, None))

    def test_an_unreadable_board_fails_open_and_says_so(self):
        broken = mock.Mock(get_task=mock.Mock(side_effect=sqlite3.OperationalError("locked")))
        with self.assertLogs(guard.logger, level="WARNING") as logs:
            self.assertIsNone(guard.refuse_started_child_link(broken, self.conn, PARENT, CHILD))
        self.assertTrue(any("unguarded" in line for line in logs.output))


class TestCreationTimeParents(unittest.TestCase):
    """kanban_create's parents=[...] never goes through _handle_link, so the
    guard can't touch it; the applier must not grow an edit there."""

    def test_the_applier_edits_only_the_link_call(self):
        self.assertNotIn("create", applier.LINK_ANCHOR)
        self.assertIn("kb.link_tasks(", applier.LINK_ANCHOR)


class TestTheApplier(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(UPSTREAM_TOOLS, encoding="utf-8")

    def test_a_second_run_refuses(self):
        applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            applier.apply(self.tmp)

    def test_a_missing_anchor_fails_the_build(self):
        self.target.write_text(
            UPSTREAM_TOOLS.replace("kb.link_tasks(conn,", "kb.link_tasks_v2(conn,"), encoding="utf-8"
        )
        with self.assertRaises(SystemExit):
            applier.apply(self.tmp)


if __name__ == "__main__":
    unittest.main()
