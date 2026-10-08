"""Unit tests for user cards ahead of background triage (kanban_priority.py).

Covers ``kanban_priority.py`` (the runtime), edits 7-10 of
``apply_kanban_scheduling.py`` (driven through an upstream-shaped dispatcher
on a real sqlite board) and ``apply_kanban_priority.py`` (on synthetic
pristine source, like its siblings).

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import sqlite3
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apply_kanban_priority as applier
import apply_kanban_scheduling as scheduling
import apply_kanban_wake_nudge as wake_nudge
import kanban_children_settled
import kanban_priority as kp
import kanban_progress_lines

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SESSION_KV_SERVER = REPO / "agents" / "platform" / "scripts" / "session_kv_server.py"
DOCKERFILE = HERE.parent / "Dockerfile"

QUEUED_LINE = "⏳ Queued: the system is busy. Your request will start when a worker frees up."


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


GATEWAY_SESSION = "20261008_101500_ab12cd34"
EVENT_SESSION = "k8s-evt-0a1b2c3d"


def stamp(requested, parent=None, origin=""):
    return kp.stamp_priority(requested, parent, origin_session=origin)


class StampTest(unittest.TestCase):
    def test_a_chat_users_card_is_raised_to_the_user_floor(self):
        # A Slack/Chat turn has no api_server origin session.
        self.assertEqual(stamp(0), kp.USER_PRIORITY)

    def test_an_api_door_card_is_a_user_card(self):
        self.assertEqual(stamp(0, origin=GATEWAY_SESSION), kp.USER_PRIORITY)

    def test_a_user_asking_for_more_keeps_it(self):
        self.assertEqual(stamp(250), 250)

    def test_event_triage_and_cron_relays_are_background(self):
        self.assertEqual(stamp(0, origin=EVENT_SESSION), 0)
        self.assertEqual(stamp(0, origin="cron-platform-stall-watch-20261008"), 0)

    def test_a_model_cannot_promote_triage(self):
        self.assertEqual(stamp(500, origin=EVENT_SESSION), kp.USER_PRIORITY - 1)
        self.assertEqual(stamp(kp.USER_PRIORITY, origin="cron-x-20261008"), kp.USER_PRIORITY - 1)

    def test_a_low_background_request_is_kept(self):
        self.assertEqual(stamp(5, origin=EVENT_SESSION), 5)

    def test_a_user_cards_child_inherits_the_user_class(self):
        parent = SimpleNamespace(priority=kp.USER_PRIORITY, session_id=GATEWAY_SESSION)
        self.assertEqual(stamp(0, parent), kp.USER_PRIORITY)
        bumped = SimpleNamespace(priority=180, session_id=None)
        self.assertEqual(stamp(0, bumped), 180)

    def test_a_triage_cards_child_stays_background(self):
        parent = SimpleNamespace(priority=0, session_id=EVENT_SESSION)
        self.assertEqual(stamp(300, parent), kp.USER_PRIORITY - 1)
        self.assertEqual(stamp(0, parent), 0)

    def test_a_child_of_any_background_priority_parent_stays_background(self):
        """The parent's own priority is trusted context too: a card below the
        floor is background whatever session it carries."""
        parent = SimpleNamespace(priority=0, session_id=GATEWAY_SESSION)
        self.assertEqual(stamp(0, parent), 0)

    def test_an_operator_promoted_triage_parents_child_is_still_clamped(self):
        parent = SimpleNamespace(priority=150, session_id=EVENT_SESSION)
        self.assertEqual(stamp(0, parent), kp.USER_PRIORITY - 1)

    def test_a_parent_without_a_priority_is_ignored(self):
        self.assertEqual(stamp(0, SimpleNamespace()), kp.USER_PRIORITY)

    def test_an_unreadable_parent_falls_back_to_the_request(self):
        class Bad:
            session_id = None

            @property
            def priority(self):
                raise RuntimeError("boom")

        self.assertEqual(stamp(3, Bad()), 3)

    def test_the_origin_is_read_from_the_runtime_when_not_given(self):
        with mock.patch.object(kp, "trusted_origin_session", return_value=EVENT_SESSION):
            self.assertEqual(kp.stamp_priority(500), kp.USER_PRIORITY - 1)
        with mock.patch.object(kp, "trusted_origin_session", return_value=""):
            self.assertEqual(kp.stamp_priority(0), kp.USER_PRIORITY)

    def test_the_runtime_origin_fails_toward_no_session(self):
        # No tools.async_delegation on the host: the read must not raise.
        self.assertEqual(kp.trusted_origin_session(), "")

    def test_the_class_boundary_is_the_floor(self):
        self.assertTrue(kp.is_user_priority(kp.USER_PRIORITY))
        self.assertFalse(kp.is_user_priority(kp.USER_PRIORITY - 1))
        self.assertFalse(kp.is_user_priority(None))
        self.assertFalse(kp.is_user_priority("garbage"))


class UntrustedSessionTest(unittest.TestCase):
    """The ``session_id`` a model passes to kanban_create never sets the class.

    An automated security review found the first version classified by the
    handler's ``session_id`` local, which prefers ``args["session_id"]``: a
    triage worker could name a user-looking session and take the user slot.
    """

    def test_the_stamp_takes_no_session_argument_from_the_handler(self):
        self.assertNotIn("session_id", applier.PRIORITY_PATCHED)
        self.assertIn("self_task", applier.PRIORITY_PATCHED)

    def test_background_origin_with_a_user_looking_args_session_stays_background(self):
        out, created = HandlerHarness(self).create(
            {"title": "Triage x", "priority": 500, "session_id": GATEWAY_SESSION},
            origin=EVENT_SESSION,
        )
        self.assertEqual(created["session_id"], GATEWAY_SESSION, "upstream still stores what it was given")
        self.assertEqual(created["priority"], kp.USER_PRIORITY - 1)

    def test_a_background_cards_child_with_a_user_looking_session_stays_background(self):
        parent = SimpleNamespace(priority=0, session_id=EVENT_SESSION)
        out, created = HandlerHarness(self).create(
            {"title": "Triage sub-step", "priority": 300, "session_id": GATEWAY_SESSION},
            origin="", self_task=parent,
        )
        self.assertEqual(created["priority"], kp.USER_PRIORITY - 1)

    def test_a_worker_naming_another_board_cannot_file_a_user_card(self):
        """A triage worker passes board="x", so its own card is not found there.
        A worker with no readable card of its own fails closed to background."""
        parent = SimpleNamespace(priority=0, session_id=EVENT_SESSION)
        out, created = HandlerHarness(self).create(
            {"title": "Spoofed", "priority": 300, "board": "x"},
            origin="", self_task=parent, worker=True,
        )
        self.assertEqual(created["priority"], kp.USER_PRIORITY - 1)

    def test_a_worker_whose_card_is_missing_files_background(self):
        self.assertEqual(
            kp.stamp_priority(0, None, worker_task_id="t_gone", origin_session=""), 0
        )
        self.assertEqual(
            kp.stamp_priority(500, None, worker_task_id="t_gone", origin_session=""),
            kp.USER_PRIORITY - 1,
        )

    def test_a_user_turn_naming_a_triage_session_is_not_demoted_by_it_either(self):
        out, created = HandlerHarness(self).create(
            {"title": "Question", "session_id": EVENT_SESSION}, origin="",
        )
        self.assertEqual(created["priority"], kp.USER_PRIORITY)


class SessionPrefixPinTest(unittest.TestCase):
    """The prefixes are session_kv_server's, read from its source.

    A renamed prefix there would silently turn every triage card into user
    work (the classification fails toward the user), so it fails here instead.
    Same idea as ``apply_kanban_scheduling._reconcile_with_the_writer``.
    """

    MINTS = {"create_session": "session_id", "_cron_report_session_id": None}

    def _minted_prefixes(self):
        tree = ast.parse(SESSION_KV_SERVER.read_text())
        found = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in self.MINTS:
                continue
            target = self.MINTS[node.name]
            for inner in ast.walk(node):
                value = None
                if target and isinstance(inner, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == target for t in inner.targets):
                        value = inner.value
                elif target is None and isinstance(inner, ast.Return):
                    value = inner.value
                if isinstance(value, ast.JoinedStr) and value.values:
                    head = value.values[0]
                    if isinstance(head, ast.Constant) and isinstance(head.value, str):
                        found.setdefault(node.name, set()).add(head.value)
        return found

    def test_each_background_prefix_is_minted_by_session_kv_server(self):
        minted = self._minted_prefixes()
        self.assertEqual(set(minted), set(self.MINTS), f"mint sites moved: {minted}")
        heads = set().union(*minted.values())
        for prefix in kp.BACKGROUND_SESSION_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, heads)

    def test_every_minted_prefix_is_classified_background(self):
        for heads in self._minted_prefixes().values():
            for head in heads:
                with self.subTest(head=head):
                    self.assertTrue(kp.is_background_session(head + "0a1b2c3d"))

    def test_a_gateway_session_id_is_not_background(self):
        self.assertFalse(kp.is_background_session("20261008_101500_ab12cd34"))
        self.assertFalse(kp.is_background_session(None))


# ---------------------------------------------------------------------------
# The dispatcher, driven through edits 7-10 on an upstream-shaped tick
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL, started_at INTEGER,
    claim_lock TEXT, session_id TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
"""

# Upstream's tick at v2026.9.14 reduced to what edits 7-10 splice into: the
# result dataclass, the cap branch of ``_tick_spawn_budget``, the early return
# and the ready loop of ``_dispatch_once_locked``. Assembled from the applier's
# own anchors, so a drift between this and the applier fails the splice.
TICK_SOURCE = (
    "from dataclasses import dataclass, field\n"
    "from typing import Optional\n\n\n"
    "@dataclass\n"
    "class DispatchResult:\n"
    "    spawned: list = field(default_factory=list)\n"
    "    skipped_unassigned: list = field(default_factory=list)\n"
    "    auto_assigned_default: list = field(default_factory=list)\n"
    "    skipped_nonspawnable: list = field(default_factory=list)\n"
    "    skipped_per_profile_capped: list = field(default_factory=list)\n"
    "    respawn_guarded: list = field(default_factory=list)\n"
    + scheduling.RESULT_FIELDS_ANCHOR
    + "\n\n"
    "def _tick_spawn_budget(conn, result, *, max_spawn, max_in_progress, board):\n"
    "    running_count = count_running_tasks(conn)\n"
    "    spawn_budget = None\n"
    "    if max_in_progress is not None:\n"
    + scheduling.SATURATION_ANCHOR
    + "        remaining = max_in_progress - total_running\n"
    "        if spawn_budget is None or spawn_budget > remaining:\n"
    "            spawn_budget = remaining\n"
    "    if memory_pressure() == 'elevated':\n"
    "        result.memory_pressure = 'elevated'\n"
    "        spawn_budget = 1\n"
    "    return True, spawn_budget\n"
    "\n\n"
    "def _lane_rows(conn, status):\n"
    "    return conn.execute(\n"
    '        "SELECT id, assignee FROM tasks "\n'
    "        f\"WHERE status = '{status}' AND claim_lock IS NULL \"\n"
    '        "ORDER BY priority DESC, created_at ASC"\n'
    "    ).fetchall()\n"
    "\n\n"
    "def _dispatch_once_locked(conn, *, spawn_fn=None, dry_run=False, max_spawn=None,\n"
    "                          max_in_progress=None, board=None, default_assignee=None):\n"
    "    result = DispatchResult()\n"
    + scheduling.CAPPED_ANCHOR
    + '    ready_rows = _lane_rows(conn, "ready")\n'
    "    ready_budget = spawn_budget\n"
    "    lane_kwargs = dict(dry_run=dry_run)\n"
    + scheduling.RESERVE_HEAD_ANCHOR
    + '        row_assignee = row["assignee"]\n'
    "        if not row_assignee:\n"
    '            result.skipped_unassigned.append(row["id"])\n'
    "            continue\n"
    + scheduling.RESERVE_SPAWN_ANCHOR
    + "    return result\n"
)

NEW_EDITS = {
    label: (anchor, patched)
    for _file, label, anchor, patched in scheduling.EDITS
    if label in (
        "DispatchResult reservation fields",
        "_tick_spawn_budget saturation record",
        "capped tick queued notice",
        "ready loop reserved slot",
        "ready loop spawn accounting",
    )
}


class Board:
    """A sqlite board plus the patched tick, with the late-bound helpers kanban_priority reads."""

    def __init__(self, testcase):
        # Autocommit with explicit BEGIN IMMEDIATE, as kanban_db_connect opens boards.
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        testcase.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)
        for ddl in kanban_children_settled._TABLE_DDL:
            self.conn.execute(ddl)
        self.clock = 1_000
        self.pressure = "ok"
        source = TICK_SOURCE
        testcase.assertEqual(len(NEW_EDITS), 5)
        for label, (anchor, patched) in NEW_EDITS.items():
            testcase.assertEqual(source.count(anchor), 1, label)
            source = source.replace(anchor, patched)
        compile(source, "<tick>", "exec")
        self.ns = {
            "count_running_tasks": self.count_running,
            "count_running_tasks_other_boards": lambda board=None: 0,
            "memory_pressure": lambda: self.pressure,
            "_dispatch_lane_task": self.lane_task,
            "_kanban_record_saturation": kp.record_saturation,
            "_kanban_note_waiting": kp.note_waiting,
            "_kanban_reserved_slot": kp.reserved_slot,
        }
        exec(source, self.ns)
        kbd = SimpleNamespace(
            count_running_tasks=self.count_running,
            count_running_tasks_other_boards=lambda board=None: 0,
        )
        kb = SimpleNamespace(
            write_txn=self.write_txn,
            _append_event=self.append_event,
            list_boards=lambda include_archived=False: [],
        )
        for name, value in (("_kbd", lambda: kbd), ("_kb", lambda: kb)):
            patcher = mock.patch.object(kp, name, value)
            patcher.start()
            testcase.addCleanup(patcher.stop)

    # -- what the real engine provides ---------------------------------------

    def count_running(self, conn):
        """Upstream's count with edit 6's discount, as the patched module has it."""
        from kanban_scheduling import count_waiting_on_children

        raw = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
        return max(0, raw - count_waiting_on_children(conn))

    @contextlib.contextmanager
    def write_txn(self, conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def append_event(self, conn, task_id, kind, payload=None, *, run_id=None):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (task_id, run_id, kind, json.dumps(payload) if payload is not None else None, self.clock),
        )

    def lane_task(self, conn, row, assignee, result, *, lane, dry_run):
        if not dry_run:
            conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = 'me', started_at = ? WHERE id = ?",
                (self.clock, row["id"]),
            )
            self.append_event(conn, row["id"], "claimed")
        result.spawned.append((row["id"], assignee, ""))
        return True

    # -- the board ---------------------------------------------------------

    def card(self, tid, priority=0, status="ready", session_id=None, assignee="cluster-dev"):
        self.clock += 1
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at, started_at, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, tid, assignee, status, priority, self.clock,
             self.clock if status == "running" else None, session_id),
        )
        return tid

    def child_of(self, creator, tid, priority=0):
        self.card(tid, priority)
        self.conn.execute(
            f"INSERT INTO {kanban_children_settled.CHILDREN_TABLE} (child_id, creator_id, created_at) VALUES (?, ?, ?)",
            (tid, creator, self.clock),
        )
        # What upstream create_task writes for a worker's child.
        self.append_event(self.conn, tid, "created", {"creator_task_id": creator, "parents": []})
        return tid

    def linked_child_of(self, parent, tid, priority=0):
        """A card created with ``parents=[parent]`` (a dependency, no creator)."""
        self.card(tid, priority)
        self.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
        self.append_event(self.conn, tid, "created", {"creator_task_id": None, "parents": [parent]})
        return tid

    def requeue(self, tid):
        self.conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?", (tid,)
        )

    def tick(self, cap, dry_run=False):
        res = self.ns["_dispatch_once_locked"](self.conn, max_in_progress=cap, dry_run=dry_run)
        return [tid for tid, _a, _w in res.spawned], res

    def queued(self, tid):
        return [
            json.loads(r["payload"]) for r in self.conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'queued' ORDER BY id",
                (tid,),
            )
        ]


U = kp.USER_PRIORITY


class ReservedSlotTest(unittest.TestCase):
    def test_two_background_cards_at_cap_2_spawn_one_and_hold_one(self):
        b = Board(self)
        b.card("bg1")
        b.card("bg2")
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, ["bg1"])
        self.assertEqual(res.skipped_reserved, ["bg2"])
        self.assertIsNotNone(res.saturation, "a held-back tick must not read as stuck")
        self.assertEqual(res.saturation["reserved"], 1)
        self.assertEqual(res.queued_noticed, [])

    def test_a_user_card_then_takes_the_reserved_slot(self):
        b = Board(self)
        b.card("bg1")
        b.card("bg2")
        b.tick(cap=2)
        b.card("user", U)
        spawned, _res = b.tick(cap=2)
        self.assertEqual(spawned, ["user"])

    def test_a_user_card_filed_after_triage_sorts_first(self):
        b = Board(self)
        b.card("bg1")
        b.card("user", U)
        spawned, _ = b.tick(cap=4)
        self.assertEqual(spawned, ["user", "bg1"])

    def test_cap_4_gives_background_three_slots(self):
        b = Board(self)
        for n in range(5):
            b.card(f"bg{n}")
        spawned, res = b.tick(cap=4)
        self.assertEqual(spawned, ["bg0", "bg1", "bg2"])
        self.assertEqual(res.skipped_reserved, ["bg3", "bg4"])

    def test_a_cap_of_1_reserves_nothing(self):
        b = Board(self)
        b.card("bg1")
        b.card("bg2")
        spawned, res = b.tick(cap=1)
        self.assertEqual(spawned, ["bg1"])
        self.assertEqual(res.skipped_reserved, [])

    def test_no_cap_reserves_nothing(self):
        b = Board(self)
        for n in range(3):
            b.card(f"bg{n}")
        spawned, res = b.tick(cap=None)
        self.assertEqual(len(spawned), 3)
        self.assertEqual(res.skipped_reserved, [])
        self.assertIsNone(res.saturation)

    def test_cap_2_gives_one_slot_to_each_class(self):
        b = Board(self)
        b.card("u1", U)
        b.card("u2", U)
        b.card("bg1")
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, ["u1", "bg1"])
        self.assertEqual(res.skipped_reserved, ["u2"])

    def test_cap_6_holds_the_sixth_user_card_and_triage_still_runs(self):
        b = Board(self)
        for n in range(6):
            b.card(f"u{n}", U)
        b.card("bg1")
        spawned, res = b.tick(cap=6)
        self.assertEqual(spawned, ["u0", "u1", "u2", "u3", "u4", "bg1"])
        self.assertEqual(res.skipped_reserved, ["u5"])

    def test_user_cards_alone_leave_triages_slot_free_and_say_so(self):
        b = Board(self)
        for n in range(6):
            b.card(f"u{n}", U)
        spawned, res = b.tick(cap=6)
        self.assertEqual(spawned, [f"u{n}" for n in range(5)])
        self.assertEqual(res.skipped_reserved, ["u5"])
        self.assertEqual(res.saturation["reserved_user"], 1)
        self.assertEqual(res.saturation["reserved_background"], 0)
        self.assertEqual(res.queued_noticed, ["u5"], "a user held for triage's slot is told it waits")

    def test_a_cap_of_1_holds_no_user_card(self):
        b = Board(self)
        b.card("u1", U)
        spawned, res = b.tick(cap=1)
        self.assertEqual(spawned, ["u1"])
        self.assertEqual(res.skipped_reserved, [])

    def test_running_background_counts_against_the_share(self):
        b = Board(self)
        b.card("running", status="running")
        b.card("bg1")
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, [])
        self.assertEqual(res.skipped_reserved, ["bg1"])

    def test_a_waiting_background_coordinator_is_discounted(self):
        b = Board(self)
        b.card("coord", status="running")
        b.child_of("coord", "kid1")
        b.child_of("coord", "kid2")
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, ["kid1"])
        self.assertEqual(res.skipped_reserved, ["kid2"])

    def test_two_waiters_one_of_each_class_release_both_slots_at_cap_2(self):
        """verify_kanban_scheduling E5's shape: both waiters are discounted from
        their class's share, so one child of each class runs."""
        b = Board(self)
        b.card("coord-u", U, status="running")
        b.card("coord-b", status="running")
        b.child_of("coord-u", "kid-u1", U)
        b.child_of("coord-u", "kid-u2", U)
        b.child_of("coord-b", "kid-b1")
        b.child_of("coord-b", "kid-b2")
        spawned, res = b.tick(cap=2)
        self.assertEqual(sorted(spawned), ["kid-b1", "kid-u1"])
        # kid-u2 is held for triage's slot; kid-b2 is never reached, the budget
        # of two being spent once kid-b1 runs.
        self.assertEqual(res.skipped_reserved, ["kid-u2"])

    def test_two_user_waiters_at_cap_2_get_one_slot_by_design(self):
        """The case the first E5 used, which the per-class floor changed: at cap
        2 user cards may hold one slot, so only one user child runs even though
        both waiting coordinators are discounted (budget is 2, not 0)."""
        b = Board(self)
        b.card("coord-1", U, status="running")
        b.card("coord-2", U, status="running")
        for n in range(3):
            b.child_of("coord-1" if n < 2 else "coord-2", f"kid{n}", U)
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, ["kid0"])
        self.assertEqual(res.skipped_reserved, ["kid1", "kid2"])
        # At cap 3 the user share is 2, and the discount gives both to children.
        b2 = Board(self)
        b2.card("coord-1", U, status="running")
        b2.card("coord-2", U, status="running")
        for n in range(3):
            b2.child_of("coord-1" if n < 2 else "coord-2", f"kid{n}", U)
        spawned, _ = b2.tick(cap=3)
        self.assertEqual(spawned, ["kid0", "kid1"])

    def test_an_unassigned_background_row_is_reported_unassigned_not_reserved(self):
        b = Board(self)
        b.card("running", status="running")
        b.card("orphan", assignee=None)
        _, res = b.tick(cap=2)
        self.assertEqual(res.skipped_unassigned, ["orphan"])
        self.assertEqual(res.skipped_reserved, [])

    def test_a_broken_share_read_turns_the_reservation_off_not_dispatch(self):
        b = Board(self)
        b.card("bg1")
        b.card("bg2")
        with mock.patch.object(kp, "count_running_background_host", side_effect=RuntimeError("x")):
            spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, ["bg1", "bg2"])
        self.assertEqual(res.skipped_reserved, [])


class SaturationAndQueuedTest(unittest.TestCase):
    def _full(self):
        b = Board(self)
        b.card("bg-run", status="running", session_id="k8s-evt-0a1b2c3d")
        b.card("u-run", U, status="running", session_id="20261008_101500_ab12cd34")
        b.card("waiting", U)
        b.card("bg-wait")
        return b

    def test_a_full_cap_records_what_holds_the_slots(self):
        b = self._full()
        spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, [])
        sat = res.saturation
        self.assertEqual((sat["running"], sat["limit"]), (2, 2))
        self.assertEqual((sat["background_running"], sat["user_running"]), (1, 1))
        self.assertEqual([c["id"] for c in sat["cards"]], ["bg-run", "u-run"])
        self.assertEqual((sat["user_waiting"], sat["background_waiting"]), (1, 1))

    def test_a_waiting_user_card_is_queued_once_per_wait(self):
        b = self._full()
        _, res = b.tick(cap=2)
        self.assertEqual(res.queued_noticed, ["waiting"])
        events = b.queued("waiting")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["note"], kp.QUEUED_TEXT)
        self.assertEqual(events[0]["ahead"], 0)
        _, res = b.tick(cap=2)
        self.assertEqual(res.queued_noticed, [])
        self.assertEqual(len(b.queued("waiting")), 1, "every tick must not repeat it")

    def test_a_new_wait_after_a_claim_is_noticed_again(self):
        b = self._full()
        b.tick(cap=2)
        b.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'u-run'")
        spawned, _ = b.tick(cap=2)
        self.assertEqual(spawned, ["waiting"])
        b.requeue("waiting")  # the worker crashed and the card went back to ready
        b.card("u-run-2", U, status="running")
        _, res = b.tick(cap=2)
        self.assertEqual(res.queued_noticed, ["waiting"])
        self.assertEqual(len(b.queued("waiting")), 2)

    def test_a_user_cards_children_are_never_told_they_are_queued(self):
        """The person's request is already running; its fan-out waiting for a
        slot is not news for their thread."""
        b = self._full()
        # Their creator has finished, so it is not a waiting coordinator and the
        # board stays full; the children carry its creator_task_id all the same.
        b.card("u-done", U, status="done")
        b.child_of("u-done", "kid1", U)
        b.child_of("u-done", "kid2", U)
        _, res = b.tick(cap=2)
        self.assertEqual(res.queued_noticed, ["waiting"])
        self.assertEqual(b.queued("kid1"), [])
        self.assertEqual(b.queued("kid2"), [])

    def test_a_card_with_a_parent_link_is_not_told_either(self):
        b = self._full()
        b.linked_child_of("u-run", "follow-up", U)
        _, res = b.tick(cap=2)
        self.assertNotIn("follow-up", res.queued_noticed)

    def test_a_user_child_held_for_triages_slot_is_not_told(self):
        b = Board(self)
        b.card("coord", U, status="running")
        for n in range(6):
            b.child_of("coord", f"kid{n}", U)
        _, res = b.tick(cap=6)
        self.assertEqual(res.queued_noticed, [])

    def test_a_background_card_left_waiting_is_never_queued(self):
        b = self._full()
        b.tick(cap=2)
        self.assertEqual(b.queued("bg-wait"), [])

    def test_the_second_user_card_in_line_knows_one_is_ahead(self):
        b = self._full()
        b.card("waiting-2", U)
        b.tick(cap=2)
        self.assertEqual(b.queued("waiting-2")[0]["ahead"], 1)

    def test_a_ready_loop_that_runs_out_of_budget_queues_the_rest(self):
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("u1", U)
        b.card("u2", U)
        b.card("u3", U)
        spawned, res = b.tick(cap=3)
        self.assertEqual(spawned, ["u1", "u2"])
        self.assertEqual(res.queued_noticed, ["u3"])
        self.assertEqual(res.saturation["running"], 3)
        self.assertEqual(res.ready_left, 1)

    def test_rows_skipped_for_their_own_reasons_are_not_left_waiting(self):
        b = Board(self)
        b.card("orphan", assignee=None)
        _, res = b.tick(cap=4)
        self.assertEqual(res.skipped_unassigned, ["orphan"])
        self.assertEqual(res.ready_left, 0, "an unassigned row is not waiting for a slot")

    def test_memory_pressure_with_slots_free_is_not_reported_as_queued(self):
        b = Board(self)
        b.pressure = "elevated"
        b.card("u1", U)
        b.card("u2", U)
        spawned, res = b.tick(cap=4)
        self.assertEqual(spawned, ["u1"])
        self.assertEqual(res.queued_noticed, [])
        self.assertIsNone(res.saturation)

    def test_dry_run_writes_no_event(self):
        b = self._full()
        _, res = b.tick(cap=2, dry_run=True)
        self.assertEqual(b.queued("waiting"), [])
        self.assertEqual(res.queued_noticed, [])

    def test_an_idle_board_reports_nothing_left(self):
        b = Board(self)
        b.card("u1", U)
        spawned, res = b.tick(cap=4)
        self.assertEqual(spawned, ["u1"])
        self.assertEqual(res.ready_left, 0)
        self.assertIsNone(res.saturation)

    def test_a_failing_note_never_breaks_the_tick(self):
        b = self._full()
        with mock.patch.object(kp, "_queued_owed", side_effect=sqlite3.OperationalError("x")):
            spawned, res = b.tick(cap=2)
        self.assertEqual(spawned, [])
        self.assertIsNotNone(res.saturation)


class QueueFieldsTest(unittest.TestCase):
    def test_a_user_card_into_a_full_cap_is_queued(self):
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("u-run", U, status="running")
        b.card("new", U)
        out = kp.queue_fields(b.conn, "new", cap_reader=lambda: 2)
        self.assertEqual(out, {"queued": True, "queue": {"running": 2, "limit": 2, "ahead": 0}})

    def test_a_user_card_with_the_reserved_slot_free_is_not_queued(self):
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("new", U)
        self.assertEqual(kp.queue_fields(b.conn, "new", cap_reader=lambda: 2), {})

    def test_a_background_card_beyond_its_share_is_queued(self):
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("new")
        self.assertTrue(kp.queue_fields(b.conn, "new", cap_reader=lambda: 2).get("queued"))

    def test_cards_ahead_of_it_count(self):
        b = Board(self)
        b.card("u-run", U, status="running")
        b.card("ahead", U)
        b.card("new", U)
        self.assertTrue(kp.queue_fields(b.conn, "new", cap_reader=lambda: 2).get("queued"))
        self.assertEqual(kp.queue_fields(b.conn, "new", cap_reader=lambda: 4), {})

    def test_a_user_card_beyond_the_user_share_is_queued(self):
        b = Board(self)
        b.card("u-run", U, status="running")
        b.card("new", U)
        self.assertTrue(kp.queue_fields(b.conn, "new", cap_reader=lambda: 2).get("queued"))

    def test_a_started_or_uncapped_card_gets_nothing(self):
        b = Board(self)
        b.card("run", U, status="running")
        self.assertEqual(kp.queue_fields(b.conn, "run", cap_reader=lambda: 1), {})
        b.card("new", U)
        self.assertEqual(kp.queue_fields(b.conn, "new", cap_reader=lambda: None), {})
        self.assertEqual(kp.queue_fields(b.conn, "missing", cap_reader=lambda: 1), {})

    def test_a_failure_returns_nothing_rather_than_raising(self):
        def broken():
            raise RuntimeError("config unreadable")

        b = Board(self)
        b.card("new", U)
        self.assertEqual(kp.queue_fields(b.conn, "new", cap_reader=broken), {})

    def test_the_return_carries_no_text_for_the_model_to_relay(self):
        """The thread hears about a wait once, from the dispatcher's event."""
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("u-run", U, status="running")
        b.card("new", U)
        out = kp.queue_fields(b.conn, "new", cap_reader=lambda: 2)
        self.assertNotIn("queue_note", out)
        self.assertFalse(any(isinstance(v, str) for v in out["queue"].values()))
        self.assertFalse(hasattr(kp, "QUEUE_NOTE"))

    def test_a_dispatcher_worker_gets_nothing(self):
        """A worker's profile has no kanban key, so its cap is not the dispatcher's."""
        b = Board(self)
        b.card("bg-run", status="running")
        b.card("u-run", U, status="running")
        b.card("new", U)
        with mock.patch.dict("os.environ", {"HERMES_KANBAN_TASK": "t_parent"}):
            self.assertEqual(kp.queue_fields(b.conn, "new", cap_reader=lambda: 2), {})


# ---------------------------------------------------------------------------
# The notifier
# ---------------------------------------------------------------------------


class Adapter:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)
        return SimpleNamespace(success=True, message_id=f"m{len(self.sent)}")

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append(text)
        return SimpleNamespace(success=True, message_id=message_id)


class NoticeTest(unittest.TestCase):
    EV = SimpleNamespace(id=7, kind="queued", payload={"note": kp.QUEUED_TEXT, "running": 2})
    N = SimpleNamespace(progress_header="@cluster-dev ", head="@cluster-dev Kanban t_1")
    SUB = {"task_id": "t_1", "platform": "google_chat", "chat_id": "spaces/0", "thread_id": "T1"}

    def test_the_line_is_bnaylors_wording_exactly(self):
        msg, handoff, review = kp.format_queued(self.EV, self.N)
        self.assertEqual(msg, QUEUED_LINE)
        self.assertIsNone(handoff)
        self.assertIsNone(review)

    def test_a_noteless_event_is_silent(self):
        ev = SimpleNamespace(id=8, kind="queued", payload=None)
        self.assertEqual(kp.format_queued(ev, self.N), (None, None, None))

    def test_queued_rolls_and_takes_its_note(self):
        self.assertIn("queued", kanban_progress_lines.ROLLING_KINDS)
        self.assertEqual(
            kanban_progress_lines.rolling_line("queued", self.EV.payload), kp.QUEUED_TEXT
        )

    def test_it_is_posted_bare_and_the_first_note_joins_it(self):
        adapter, watcher = Adapter(), SimpleNamespace()
        msg = kp.format_queued(self.EV, self.N)[0]

        async def run():
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "queued", self.EV, msg, {}, header="@cluster-dev ",
            )
            note = SimpleNamespace(id=9, kind="heartbeat", payload={"note": "Reading events"})
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "heartbeat", note, "⏳ @cluster-dev Reading events",
                {}, header="@cluster-dev ",
            )

        asyncio.run(run())
        self.assertEqual(adapter.sent, [QUEUED_LINE])
        self.assertEqual(
            adapter.edits,
            [f"⏳ @cluster-dev\n• {kp.QUEUED_TEXT}\n• Reading events"],
        )

    def test_a_queued_card_that_settles_without_a_note_drops_the_queued_text(self):
        """The reviewer's simulation: queued, then completed with no heartbeat
        between. The rolling message must not settle as "✓ @… Queued: …"."""
        for kind in ("completed", "crashed"):
            with self.subTest(kind=kind):
                adapter, watcher = Adapter(), SimpleNamespace()
                done = SimpleNamespace(id=10, kind=kind, payload={"summary": "All good"})

                async def run():
                    await kanban_progress_lines.deliver(
                        watcher, adapter, self.SUB, "queued", self.EV, QUEUED_LINE, {},
                        header="@platform ",
                    )
                    await kanban_progress_lines.deliver(
                        watcher, adapter, self.SUB, kind, done, "✔ @platform done: All good", {},
                        header="@platform ",
                    )

                asyncio.run(run())
                self.assertEqual(adapter.sent, [QUEUED_LINE, "✔ @platform done: All good"])
                self.assertEqual(len(adapter.edits), 1)
                self.assertNotIn("Queued", adapter.edits[0])
                marker = "✓" if kind == "completed" else "⏹"
                self.assertEqual(adapter.edits[0], f"{marker} @platform")

    def test_a_queued_card_with_notes_settles_on_its_notes(self):
        adapter, watcher = Adapter(), SimpleNamespace()
        note = SimpleNamespace(id=9, kind="heartbeat", payload={"note": "Reading events"})
        done = SimpleNamespace(id=10, kind="completed", payload={"summary": "ok"})

        async def run():
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "queued", self.EV, QUEUED_LINE, {}, header="@platform ",
            )
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "heartbeat", note, "", {}, header="@platform ",
            )
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "completed", done, "✔ done", {}, header="@platform ",
            )

        asyncio.run(run())
        self.assertEqual(adapter.edits[-1], "✓ @platform Reading events")

    def test_a_card_already_rolling_keeps_its_header(self):
        adapter, watcher = Adapter(), SimpleNamespace()

        async def run():
            note = SimpleNamespace(id=3, kind="heartbeat", payload={"note": "Started"})
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "heartbeat", note, "", {}, header="@cluster-dev ",
            )
            await kanban_progress_lines.deliver(
                watcher, adapter, self.SUB, "queued", self.EV, QUEUED_LINE, {}, header="@cluster-dev ",
            )

        asyncio.run(run())
        self.assertEqual(adapter.edits, [f"⏳ @cluster-dev\n• Started\n• {kp.QUEUED_TEXT}"])


# ---------------------------------------------------------------------------
# The watcher's warning
# ---------------------------------------------------------------------------


class Log:
    def __init__(self):
        self.lines = []

    def warning(self, fmt, *args):
        self.lines.append(fmt % args)


def board_result(saturation=None, reserved=(), ready_left=1):
    return SimpleNamespace(saturation=saturation, skipped_reserved=list(reserved), ready_left=ready_left)


SAT = {
    "running": 2, "limit": 2, "background_running": 2, "user_running": 0,
    "cards": [
        {"id": "t_ab12", "assignee": "cluster-prod", "priority": 0, "session_id": "k8s-evt-1", "started_at": 1000 - 14 * 60},
        {"id": "t_cd34", "assignee": "platform", "priority": 0, "session_id": "cron-x-1", "started_at": 1000 - 200},
    ],
    "user_waiting": 1, "background_waiting": 0, "reserved": 0,
}


class WarningTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(kp._WATCH, {"ticks": 0, "last_warn": 0.0})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_message_names_slots_holders_and_waiters(self):
        self.assertEqual(
            kp.saturation_message(SAT, now=1000),
            "kanban dispatcher saturated: 2/2 worker slots busy (2 background, 0 user: "
            "t_ab12 @cluster-prod 14m [k8s-evt-], t_cd34 @platform 3m [cron-]); "
            "1 user card(s) and 0 background card(s) waiting",
        )

    def test_a_held_back_tick_says_why(self):
        sat = dict(SAT, running=1, background_running=1, cards=SAT["cards"][:1],
                   user_waiting=0, background_waiting=1, reserved=1)
        self.assertTrue(
            kp.saturation_message(sat, now=1000).endswith(
                "; 1 background card(s) held back because one slot is reserved for user cards"
            )
        )
        # A free slot held for users is not called saturation (live run, ka-today-1).
        self.assertTrue(
            kp.saturation_message(sat, now=1000).startswith(
                "kanban dispatcher holding background cards: 1/2 worker slots busy"
            )
        )

    def test_a_tick_held_for_triage_says_user_cards_were_held(self):
        sat = dict(SAT, running=5, limit=6, reserved=1, reserved_user=1, reserved_background=0)
        text = kp.saturation_message(sat, now=1000)
        self.assertTrue(text.startswith("kanban dispatcher holding user cards: 5/6"), text)
        self.assertTrue(text.endswith(
            "; 1 user card(s) held back because one slot is reserved for background triage"
        ), text)

    def test_a_user_card_is_labelled_user(self):
        sat = dict(SAT, cards=[{"id": "t_1", "assignee": "a", "priority": U, "session_id": "2026_x", "started_at": 990}])
        self.assertIn("t_1 @a 10s [user]", kp.saturation_message(sat, now=1000))

    def test_saturation_waits_for_the_window_then_rate_limits(self):
        log = Log()
        results = [("default", board_result(SAT))]
        flags = [kp.saturation_tick(log, results, True, False, 6, now=10_000 + i) for i in range(6)]
        self.assertEqual(flags, [True] * 6)
        self.assertEqual(len(log.lines), 1)
        self.assertTrue(log.lines[0].startswith("kanban dispatcher saturated: 2/2"))
        for i in range(10):
            kp.saturation_tick(log, results, True, False, 6, now=10_010 + i)
        self.assertEqual(len(log.lines), 1, "rate-limited to one per interval")
        kp.saturation_tick(log, results, True, False, 6, now=10_000 + kp.WARN_INTERVAL_SECONDS + 5)
        self.assertEqual(len(log.lines), 2)

    def test_free_slots_and_nothing_spawned_stays_stuck(self):
        log = Log()
        results = [("default", board_result(None, ready_left=2))]
        self.assertFalse(kp.saturation_tick(log, results, True, False, 6, now=1))
        self.assertEqual(log.lines, [])

    def test_a_reserved_only_tick_is_saturation(self):
        sat = dict(SAT, reserved=1)
        self.assertTrue(kp.saturation_tick(Log(), [("d", board_result(sat, reserved=["x"]))], True, False, 6))

    def test_one_unexplained_board_makes_it_upstreams_case(self):
        results = [("a", board_result(SAT)), ("b", board_result(None, ready_left=3))]
        self.assertIsNone(kp.saturation_of(results))

    def test_an_empty_other_board_does_not_spoil_saturation(self):
        results = [("a", board_result(SAT)), ("b", board_result(None, ready_left=0)), ("c", None)]
        self.assertIsNotNone(kp.saturation_of(results))

    def test_boards_merge(self):
        other = dict(SAT, user_waiting=2, background_waiting=3, cards=SAT["cards"][:1])
        merged = kp.saturation_of([("a", board_result(SAT)), ("b", board_result(other))])
        self.assertEqual((merged["user_waiting"], merged["background_waiting"]), (3, 3))
        self.assertEqual(len(merged["cards"]), 3)

    def test_a_tick_that_spawned_or_had_nothing_ready_resets_the_window(self):
        log = Log()
        results = [("default", board_result(SAT))]
        for i in range(5):
            kp.saturation_tick(log, results, True, False, 6, now=i)
        self.assertFalse(kp.saturation_tick(log, results, True, True, 6, now=6))
        kp.saturation_tick(log, results, True, False, 6, now=7)
        self.assertEqual(log.lines, [], "the window restarted")

    def test_never_raises(self):
        class Boom:
            def warning(self, *a):
                raise RuntimeError("log down")

        with mock.patch.dict(kp._WATCH, {"ticks": 99, "last_warn": 0.0}):
            self.assertFalse(kp.saturation_tick(Boom(), [("d", board_result(SAT))], True, False, 1))


# ---------------------------------------------------------------------------
# apply_kanban_priority on synthetic pristine source
# ---------------------------------------------------------------------------

# ``_handle_create`` at v2026.9.14 around the two anchors, as the latency block
# leaves it: children_settled's and auto_subscribe's hooks sit on the
# ``landed = ...`` line and keep it, report_format has wrapped ``body=``.
TOOLS_SOURCE = '''\
import json
import os


def _ok(**fields):
    return json.dumps({"ok": True, **fields})


def _opt_int(value, default=None):
    return int(value) if value is not None else default


@_kanban_handler("kanban_create")
def _handle_create(args: dict, **kw) -> str:
    """Create a (child) task; orchestrator workers use this to fan out."""
    with _board(args.get("board")) as (kb, conn):
        self_tid = os.environ.get("HERMES_KANBAN_TASK")
        self_task = kb.get_task(conn, self_tid) if self_tid else None
        session_id = (args.get("session_id") or (self_task.session_id if self_task else None)
                      or _current_origin_session_id() or os.environ.get("HERMES_SESSION_ID"))
        new_tid = kb.create_task(
            conn, title=str(args["title"]).strip(), body=_with_report_format(args.get("body")),
            priority=_opt_int(args.get("priority"), 0),
            created_by=os.environ.get("HERMES_PROFILE") or "worker", session_id=session_id)
        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)
        _kanban_record_worker_child(conn, new_tid)
        _kanban_inherit_worker_subs(conn, new_tid)
        return _ok(task_id=new_tid, **landed, subscribed=_maybe_auto_subscribe(conn, new_tid))


@_kanban_handler("kanban_unblock")
def _handle_unblock(args: dict, **kw) -> str:
    with _board(args.get("board")) as (kb, conn):
        return _ok(task_id=args.get("task_id"))
'''

WATCHERS_SOURCE = (
    "import time\n\n_HEALTH_WINDOW = 6\n\n\n"
    "class GatewayKanbanWatchersMixin:\n"
    "    async def _kanban_dispatcher_watcher(self) -> None:\n"
    "        bad_ticks = 0\n"
    "        last_warn_at = 0\n"
    + wake_nudge.DISPATCHER_MONITOR_ANCHOR
    + "            try:\n"
    "                if not _kanban_dispatch_allowed():\n"
    "                    bad_ticks = 0\n"
    "                else:\n"
    "                    results = await _to_thread_process_service(dispatcher.tick_once)\n"
    "                    any_spawned = _log_spawn_results(results)\n"
    + applier.STUCK_ANCHOR
    + "                now = int(time.time())\n"
    "                if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:\n"
    "                    logger.warning(\n"
    '                        "kanban dispatcher stuck: ready queue non-empty for "\n'
    '                        "%d consecutive ticks but 0 workers spawned.", bad_ticks,\n'
    "                    )\n"
    "                    last_warn_at = now\n"
    "            except Exception:\n"
    + wake_nudge.DISPATCHER_SLEEP_ANCHOR
)

TERMINAL_UPSTREAM = (
    'TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out", '
    '"status", "archived", "unblocked", "block_loop_detected", "review_requested", '
    '"changes_requested")'
)
WAKE_LINE = (
    '_WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked", '
    '"review_requested", "changes_requested", "block_loop_detected")'
)


def notifier_source(widened_by_progress_lines=True):
    kinds = TERMINAL_UPSTREAM + (' + ("heartbeat",)' if widened_by_progress_lines else "")
    return (
        "from typing import Any, Callable\n\n"
        + kinds + "\n" + WAKE_LINE + "\n\n\n"
        "_EVENT_FORMATTERS: dict[str, Callable[[Any, Any], tuple]] = {\n"
        '    "completed": lambda ev, n: ("done", None, None),\n'
        "}\n"
    )


PRISTINE = {
    applier.TOOLS_RELATIVE: lambda: TOOLS_SOURCE,
    applier.WATCHERS_RELATIVE: lambda: WATCHERS_SOURCE,
    applier.NOTIFIER_RELATIVE: notifier_source,
}


class HandlerHarness:
    """Run the patched ``_handle_create`` fixture with stub collaborators.

    ``origin`` is what the runtime reports as the turn's own session (upstream
    reads it twice: once for the card's ``session_id``, and through
    ``kanban_priority.trusted_origin_session`` for the class).
    """

    def __init__(self, testcase):
        self.testcase = testcase
        root = Path(tempfile.mkdtemp())
        for relative, make in PRISTINE.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(make())
        applier.apply(root)
        self.source = (root / applier.TOOLS_RELATIVE).read_text().replace(
            "from hermes_cli.kanban_priority import", "from kanban_priority import"
        )

    def create(self, args, origin="", self_task=None, worker=None):
        """``worker`` defaults to "a dispatcher worker exactly when self_task is
        given". A worker's own card lives on the default board; asking another
        board (``board="x"``) finds no card, as upstream's get_task would."""
        created = {}
        worker = self_task is not None if worker is None else worker

        class Kb:
            def __init__(self, board):
                self.board = board

            def get_task(self, conn, tid):
                if self.board not in (None, "default"):
                    return None
                return self_task if tid == "t_parent" else None

            def create_task(self, conn, **kw):
                created.update(kw)
                return "t_new"

        @contextlib.contextmanager
        def _board(board=None):
            yield Kb(board), "conn"

        ns = {
            "_kanban_handler": lambda name: (lambda fn: fn),
            "_board": _board,
            "_current_origin_session_id": lambda: origin,
            "_with_report_format": lambda body: body,
            "_fields": lambda task, names: {"status": "ready"},
            "_CREATED_FIELDS": ("status",),
            "_kanban_record_worker_child": lambda conn, tid: None,
            "_kanban_inherit_worker_subs": lambda conn, tid: None,
            "_maybe_auto_subscribe": lambda conn, tid: True,
        }
        exec(compile(self.source, "<tools>", "exec"), ns)
        ns["_kanban_queue_fields"] = kp.queue_fields
        env = {"HERMES_KANBAN_TASK": "t_parent"} if worker else {}
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(kp, "trusted_origin_session", return_value=origin):
            if not worker:
                import os

                os.environ.pop("HERMES_KANBAN_TASK", None)
            out = json.loads(ns["_handle_create"](args))
        return out, created


class ApplierTest(unittest.TestCase):
    def _tree(self, bodies=None):
        root = Path(tempfile.mkdtemp())
        targets = {}
        for relative, make in PRISTINE.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (bodies or {}).get(relative)
            target.write_text(make() if body is None else body)
            targets[relative] = target
        return root, targets

    def _applied(self, bodies=None):
        root, targets = self._tree(bodies)
        applier.apply(root)
        return {relative: target.read_text() for relative, target in targets.items()}

    def test_the_fixtures_are_faithful_stand_ins(self):
        self.assertEqual(TOOLS_SOURCE.count(applier.PRIORITY_ANCHOR), 1)
        self.assertEqual(TOOLS_SOURCE.count(applier.RETURN_ANCHOR), 1)
        self.assertEqual(WATCHERS_SOURCE.count(applier.STUCK_ANCHOR), 1)
        for make in PRISTINE.values():
            ast.parse(make())

    def test_all_edits_land_and_every_file_compiles(self):
        out = self._applied()
        tools = out[applier.TOOLS_RELATIVE]
        self.assertIn(applier.PRIORITY_PATCHED, tools)
        self.assertIn('**_kanban_queue_fields(conn, new_tid, args.get("board")),', tools)
        self.assertEqual(tools.count("from hermes_cli.kanban_priority import"), 1)
        watchers = out[applier.WATCHERS_RELATIVE]
        self.assertIn("if _kanban_saturation_tick(", watchers)
        self.assertIn("kanban dispatcher stuck", watchers)
        notifier = out[applier.NOTIFIER_RELATIVE]
        self.assertIn('+ ("heartbeat",) + ("queued",)', notifier)
        self.assertIn('_EVENT_FORMATTERS["queued"] = _kanban_format_queued', notifier)
        self.assertIn(WAKE_LINE, notifier, "_WAKE_KINDS is untouched")
        for relative, text in out.items():
            with self.subTest(file=relative):
                compile(text, relative, "exec")

    def test_the_notifier_widens_a_bare_upstream_tuple_too(self):
        out = self._applied({applier.NOTIFIER_RELATIVE: notifier_source(False)})
        self.assertIn('"changes_requested") + ("queued",)', out[applier.NOTIFIER_RELATIVE])

    def test_the_patched_notifier_claims_and_formats_queued_without_waking(self):
        notifier = self._applied()[applier.NOTIFIER_RELATIVE].replace(
            "from hermes_cli.kanban_priority import", "from kanban_priority import"
        )
        ns: dict = {}
        exec(compile(notifier, "<notifier>", "exec"), ns)
        self.assertIn("queued", ns["TERMINAL_KINDS"])
        self.assertIn("heartbeat", ns["TERMINAL_KINDS"])
        self.assertNotIn("queued", ns["_WAKE_KINDS"])
        msg = ns["_EVENT_FORMATTERS"]["queued"](NoticeTest.EV, NoticeTest.N)[0]
        self.assertEqual(msg, QUEUED_LINE)

    def test_the_patched_handler_stamps_and_reports_the_queue(self):
        with mock.patch.object(kp, "queue_fields", return_value={"queued": True, "queue_note": "n"}) as qf:
            out, created = HandlerHarness(self).create(
                {"title": "Triage x", "priority": 500, "board": "b"}, origin=EVENT_SESSION,
            )
        self.assertEqual(created["priority"], kp.USER_PRIORITY - 1, "triage is clamped")
        self.assertEqual(created["session_id"], EVENT_SESSION)
        self.assertEqual(out["task_id"], "t_new")
        self.assertTrue(out["subscribed"])
        self.assertTrue(out["queued"])
        qf.assert_called_once_with("conn", "t_new", "b")

    def test_every_anchor_is_load_bearing(self):
        for relative, anchor in (
            (applier.TOOLS_RELATIVE, applier.PRIORITY_ANCHOR),
            (applier.TOOLS_RELATIVE, applier.RETURN_ANCHOR),
            (applier.WATCHERS_RELATIVE, applier.STUCK_ANCHOR),
        ):
            with self.subTest(anchor=anchor[:40]):
                root, targets = self._tree({relative: PRISTINE[relative]().replace(anchor, "", 1)})
                with self.assertRaises(SystemExit):
                    applier.apply(root)
                for rel, target in targets.items():
                    if rel != relative:
                        self.assertEqual(target.read_text(), PRISTINE[rel](), "nothing written")

    def test_a_priority_keyword_outside_the_create_handler_is_refused(self):
        moved = TOOLS_SOURCE.replace("            priority=_opt_int(args.get(\"priority\"), 0),\n", "") + (
            "\n\ndef _handle_other(args):\n"
            '    return dict(priority=_opt_int(args.get("priority"), 0),)\n'
        )
        root, _ = self._tree({applier.TOOLS_RELATIVE: moved})
        with self.assertRaises(SystemExit) as ctx:
            applier.apply(root)
        self.assertIn("inside _handle_create()", str(ctx.exception))

    def test_a_duplicated_anchor_fails(self):
        root, _ = self._tree({applier.WATCHERS_RELATIVE: WATCHERS_SOURCE + textwrap.dedent("") + WATCHERS_SOURCE})
        with self.assertRaises(SystemExit):
            applier.apply(root)

    def test_a_repurposed_terminal_filter_fails(self):
        body = notifier_source().replace('"gave_up", ', "")
        root, _ = self._tree({applier.NOTIFIER_RELATIVE: body})
        with self.assertRaises(SystemExit) as ctx:
            applier.apply(root)
        self.assertIn("gave_up", str(ctx.exception))

    def test_a_second_run_is_refused_and_writes_nothing(self):
        root, targets = self._tree()
        applier.apply(root)
        once = {rel: t.read_text() for rel, t in targets.items()}
        with self.assertRaises(SystemExit) as ctx:
            applier.apply(root)
        self.assertIn("already patched", str(ctx.exception))
        for rel, target in targets.items():
            self.assertEqual(target.read_text(), once[rel])

    def test_a_missing_file_fails(self):
        root, targets = self._tree()
        targets[applier.NOTIFIER_RELATIVE].unlink()
        with self.assertRaises(SystemExit):
            applier.apply(root)
        self.assertEqual(targets[applier.TOOLS_RELATIVE].read_text(), TOOLS_SOURCE)


class CompositionTest(unittest.TestCase):
    """The neighbours this applier shares files with."""

    def test_the_stuck_anchor_overlaps_no_wake_nudge_anchor_and_survives_it(self):
        for anchor in (wake_nudge.DISPATCHER_MONITOR_ANCHOR, wake_nudge.DISPATCHER_SLEEP_ANCHOR):
            self.assertNotIn(anchor, applier.STUCK_ANCHOR)
            self.assertNotIn(applier.STUCK_ANCHOR, anchor)
            self.assertNotIn(anchor, applier.STUCK_PATCHED)

    def test_no_other_tools_applier_claims_these_lines(self):
        others = (
            "apply_kanban_auto_subscribe.py",
            "apply_kanban_children_settled.py",
            "apply_kanban_comment_status.py",
            "apply_kanban_event_routing.py",
            "apply_kanban_report_format.py",
            "apply_kanban_result_required.py",
            "apply_kanban_worker_tools.py",
            "apply_cron_run_scope.py",
        )
        for name in others:
            src = (HERE / name).read_text()
            for line in (applier.PRIORITY_ANCHOR, applier.RETURN_ANCHOR.strip()):
                with self.subTest(applier=name, line=line[:30]):
                    self.assertNotIn(line, src)

    def test_the_patch_adds_nothing_cron_run_scope_counts(self):
        from apply_cron_run_scope import KANBAN_IMPORT_ANCHOR

        added = applier.PRIORITY_PATCHED + applier.RETURN_PATCHED + applier.TOOLS_TRAILER
        self.assertNotIn(KANBAN_IMPORT_ANCHOR, added)
        self.assertNotIn('"task_id is required (or set HERMES_KANBAN_TASK in the env)"', added)

    def test_the_wake_kinds_are_left_alone(self):
        self.assertNotIn("_WAKE_KINDS", applier.NOTIFIER_TRAILER)
        self.assertNotIn("_WAKE_KINDS =", applier.KINDS_COMMENT)

    def test_the_slack_plan_status_map_does_not_claim_queued(self):
        """TASK_PENDING means "waits on the user" in the Slack plan (it moves the
        thread to suspended), which a queued card does not. queued rolls, so the
        plan never consults this map for it; keeping it out keeps that true."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "slack_status_probe", REPO / "agents" / "platform" / "scripts" / "slack_status.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertNotIn("queued", module.TASK_STATUS_BY_KIND)


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        if not DOCKERFILE.exists():
            self.skipTest("Dockerfile not beside the patches")
        self.text = DOCKERFILE.read_text()

    def test_the_runtime_is_installed_before_the_scheduling_applier_runs(self):
        install = self.text.index(
            "install -m 0644 /tmp/kanban-scheduling/kanban_priority.py /opt/hermes/hermes_cli/kanban_priority.py"
        )
        self.assertLess(install, self.text.index("/tmp/kanban-scheduling/apply_kanban_scheduling.py /opt/hermes"))
        self.assertLess(install, self.text.index("/tmp/kanban-scheduling/verify_kanban_scheduling.py"))

    def test_the_applier_runs_after_wake_nudge_and_the_tools_appliers(self):
        at = self.text.index("/tmp/latency-patches/apply_kanban_priority.py /opt/hermes")
        for earlier in (
            "/tmp/latency-patches/apply_kanban_wake_nudge.py /opt/hermes",
            "/tmp/latency-patches/apply_kanban_children_settled.py /opt/hermes",
            "/tmp/kanban-progress/apply_kanban_progress_lines.py /opt/hermes",
        ):
            with self.subTest(earlier=earlier):
                self.assertLess(self.text.index(earlier), at)
        self.assertLess(at, self.text.index("/tmp/latency-patches/verify_kanban_priority.py"))

    def test_the_gates_grep_for_what_the_applier_writes(self):
        for marker in (
            applier.PRIORITY_PATCHED,
            '**_kanban_queue_fields(conn, new_tid, args.get("board")),',
            '+ ("heartbeat",) + ("queued",)',
            '_EVENT_FORMATTERS["queued"] = _kanban_format_queued',
            "_kanban_saturation_tick(",
        ):
            with self.subTest(marker=marker[:40]):
                self.assertIn(marker, self.text)

    def test_both_files_are_copied_into_the_latency_stage(self):
        for name in ("apply_kanban_priority.py", "verify_kanban_priority.py"):
            self.assertIn(f"deploy/docker/patches/{name} \\", self.text)


if __name__ == "__main__":
    unittest.main()
