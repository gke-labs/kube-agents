"""Unit tests for the wake-turn guard on kanban_create installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import asyncio
import contextvars
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import kanban_wake_guard as guard

# The #2610 shape: periodic run 2109358269314109440, front-door session whose
# wake turn filed the card that opened #210.
FRONT_DOOR = "a2a-ctx-ba0e5ccf90bd164ea10f7d31"
EVENT_WATCHER = "k8s-evt-13f95359"


COMPRESSED_TIP = "20261013_160000_ab12cd"


class _DB:
    def __init__(self, rows):
        self.rows = rows

    def get_session(self, sid):
        return self.rows.get(sid)


class _Adapter:
    def __init__(self, rows=None, broken=False):
        self.rows, self.broken = rows or {}, broken

    async def _ensure_session_db_async(self):
        if self.broken:
            raise RuntimeError("store unavailable")
        return _DB(self.rows)


def run_wrapped(headers, adapter=None, raises=False):
    seen = {}

    async def handler(self_, request):
        seen["wake"] = guard.is_wake_turn()
        if raises:
            raise RuntimeError("boom")
        return "ok"

    wrapped = guard.wake_marked(handler)
    request = types.SimpleNamespace(headers=headers)
    try:
        asyncio.run(wrapped(adapter or _Adapter(), request))
    except RuntimeError:
        pass
    return seen.get("wake")


MARK = {guard.WAKE_HEADER: "1"}


class TestTheHandlerWrapper(unittest.TestCase):
    def test_a_marked_front_door_request_is_a_wake_turn(self):
        self.assertIs(run_wrapped({**MARK, guard.SESSION_HEADER: FRONT_DOOR}), True)

    def test_a_compressed_continuation_of_a_front_door_session_is_too(self):
        rows = {COMPRESSED_TIP: {"parent_session_id": "20261013_150000_ffff"},
                "20261013_150000_ffff": {"parent_session_id": FRONT_DOOR}}
        self.assertTrue(run_wrapped({**MARK, guard.SESSION_HEADER: COMPRESSED_TIP}, _Adapter(rows)))

    def test_a_lineage_loop_ends_the_walk(self):
        rows = {"s1": {"parent_session_id": "s2"}, "s2": {"parent_session_id": "s1"}}
        self.assertIs(run_wrapped({**MARK, guard.SESSION_HEADER: "s1"}, _Adapter(rows)), False)

    def test_an_unreadable_store_answers_from_the_id_alone(self):
        self.assertIs(run_wrapped({**MARK, guard.SESSION_HEADER: COMPRESSED_TIP}, _Adapter(broken=True)), False)
        self.assertTrue(run_wrapped({**MARK, guard.SESSION_HEADER: FRONT_DOOR}, _Adapter(broken=True)))

    def test_an_unmarked_request_is_not(self):
        self.assertIs(run_wrapped({guard.SESSION_HEADER: FRONT_DOOR}), False)

    def test_an_event_watcher_wake_is_out_of_scope(self):
        self.assertIs(run_wrapped({**MARK, guard.SESSION_HEADER: EVENT_WATCHER}), False)

    def test_a_marked_request_with_no_session_is_not(self):
        self.assertIs(run_wrapped(MARK), False)

    def test_a_raising_handler_still_ran_marked(self):
        self.assertIs(run_wrapped({**MARK, guard.SESSION_HEADER: FRONT_DOOR}, raises=True), True)


class TestTheCarry(unittest.TestCase):
    def on_executor(self, marked, carry):
        async def go():
            token = guard._WAKE_TURN.set(marked)
            try:
                fn = guard.is_wake_turn
                return await asyncio.get_running_loop().run_in_executor(
                    None, guard.carry_wake_mark(fn) if carry else fn)
            finally:
                guard._WAKE_TURN.reset(token)
        return asyncio.run(go())

    def test_the_carry_sets_the_mark_on_the_executor_thread(self):
        self.assertTrue(self.on_executor(True, carry=True))

    def test_without_it_the_thread_has_no_mark(self):
        self.assertIs(self.on_executor(True, carry=False), False)

    def test_an_unmarked_turn_is_passed_through_unchanged(self):
        fn = lambda: None  # noqa: E731
        self.assertIs(guard.carry_wake_mark(fn), fn)


class TestTheCreateGate(unittest.TestCase):
    def test_a_marked_turn_is_refused(self):
        token = guard._WAKE_TURN.set(True)
        try:
            refusal = guard.refuse_create_on_wake()
        finally:
            guard._WAKE_TURN.reset(token)
        self.assertIsNotNone(refusal)
        self.assertIn("wait for the person", refusal)

    def test_an_unmarked_turn_is_not(self):
        self.assertIsNone(guard.refuse_create_on_wake())

    def test_another_context_is_not_marked_by_this_one(self):
        token = guard._WAKE_TURN.set(True)
        try:
            other = contextvars.Context().run(guard.refuse_create_on_wake)
        finally:
            guard._WAKE_TURN.reset(token)
        self.assertIsNone(other)


class TestTheApplier(unittest.TestCase):
    """Drive apply_kanban_wake_guard against a fixture tree."""

    def setUp(self):
        import apply_kanban_wake_guard as applier

        self.applier = applier
        self.tmp = Path(tempfile.mkdtemp())
        files = {
            applier.WAKE_RELATIVE: (
                "async def _self_post_chat_completion(adapter, *, text, session_id):\n"
                "    api_key = 'k'\n"
                + applier.HEADERS_ANCHOR
                + "    return headers\n"
            ),
            applier.API_RELATIVE: (
                "from __future__ import annotations\n"
                + applier.API_IMPORT_ANCHOR
                + "\nclass APIServerAdapter(OpenAICompatRoutesMixin):\n"
                + applier.HANDLER_ANCHOR
                + "\n    async def _run_agent(self):\n"
                "        def _run():\n"
                "            return 1\n"
                "        if True:\n"
                + applier.EXECUTOR_ANCHOR
            ),
            applier.TOOLS_RELATIVE: (
                "def _handle_create(args, **kw):\n"
                + applier.CREATE_ANCHOR
                + "    title = args['title']\n"
                "    return title\n"
            ),
        }
        for rel, text in files.items():
            target = self.tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.tmp / rel).read_text(encoding="utf-8")

    def test_all_three_hooks_land_and_the_files_still_parse(self):
        import ast

        self.applier.apply(self.tmp)
        wake = self.read(self.applier.WAKE_RELATIVE)
        api = self.read(self.applier.API_RELATIVE)
        tools = self.read(self.applier.TOOLS_RELATIVE)
        self.assertIn("headers[_KANBAN_WAKE_HEADER] = \"1\"", wake)
        self.assertIn("_kanban_wake_marked(OpenAICompatRoutesMixin._handle_chat_completions)", api)
        self.assertIn("_kanban_refuse_create_on_wake()", tools)
        self.assertIn("run_in_executor(None, _kanban_wake_carry(_run))", api)
        for text in (wake, api, tools):
            ast.parse(text)

    def test_the_handler_import_comes_before_the_class(self):
        self.applier.apply(self.tmp)
        api = self.read(self.applier.API_RELATIVE)
        self.assertLess(api.index("wake_marked as _kanban_wake_marked,"), api.index("class APIServerAdapter"))

    def test_the_create_gate_runs_before_any_argument_is_read(self):
        self.applier.apply(self.tmp)
        tools = self.read(self.applier.TOOLS_RELATIVE)
        self.assertLess(tools.index("_kanban_refuse_create_on_wake()"), tools.index("title = args"))

    def test_a_second_run_refuses(self):
        self.applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_missing_anchor_fails_the_build_and_writes_nothing(self):
        tools = self.tmp / self.applier.TOOLS_RELATIVE
        tools.write_text("def _handle_create(args):\n    pass\n", encoding="utf-8")
        before = self.read(self.applier.WAKE_RELATIVE)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)
        self.assertEqual(self.read(self.applier.WAKE_RELATIVE), before)


if __name__ == "__main__":
    unittest.main()
