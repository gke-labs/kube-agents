"""Host tests for the KAGE_SLACK_UX reactions patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_reactions.py

The fixture carries upstream's two reaction hooks verbatim (v2026.9.14). The
tests apply the patch, exec both the patched and the unpatched fixture, and
drive them with a stub adapter: with the flag off the patched hooks must make
exactly the calls upstream makes, which is the flag-off identity for this
surface; with it on, the runtime module takes over.
"""

import asyncio
import enum
import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_reactions as applier
import slack_ux_reactions as runtime
import verify_slack_ux_reactions as verifier

UPSTREAM = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
import enum


class ProcessingOutcome(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class SlackAdapter:
    def __init__(self):
        self.calls = []
        self._reacting_message_ids = {"m"}
        self.reactions = True

    def _reactions_enabled(self):
        return self.reactions

    def _workspace_message_marker(self, team_id, ts):
        return "m"

    async def _react(self, channel, timestamp, emoji, team_id, *, remove):
        self.calls.append((channel, timestamp, emoji, team_id, remove))
        return True

    def _reacting_target(self, event):
        """``(ts, team_id, marker)`` when reactions are on and ``event`` is being tracked."""
        if not self._reactions_enabled():
            return None
        ts = getattr(event, "message_id", None)
        team_id = str(getattr(event.source, "scope_id", "") or "")
        marker = self._workspace_message_marker(team_id, ts) if ts else None
        return (ts, team_id, marker) if ts and marker in self._reacting_message_ids else None

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        target = self._reacting_target(event)
        if target is None:
            return
        ts, team_id, _marker = target
        channel_id = getattr(event.source, "chat_id", None)
        if channel_id:
            await self._react(channel_id, ts, "eyes", team_id, remove=False)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction."""
        target = self._reacting_target(event)
        if target is None:
            return
        ts, team_id, marker = target
        self._reacting_message_ids.discard(marker)
        channel_id = getattr(event.source, "chat_id", None)
        if not channel_id:
            return
        await self._react(channel_id, ts, "eyes", team_id, remove=True)
        final = {ProcessingOutcome.SUCCESS: "white_check_mark", ProcessingOutcome.FAILURE: "x"}
        if outcome in final:
            await self._react(channel_id, ts, final[outcome], team_id, remove=False)
'''

CHANNEL = "C1"
THREAD = "111.000"
ASK = "111.000"
TEAM = "T1"


def _event(text, thread=THREAD):
    return SimpleNamespace(
        text=text,
        message_id=ASK,
        source=SimpleNamespace(chat_id=CHANNEL, thread_id=thread, scope_id=TEAM),
    )


def _run(coro):
    return asyncio.run(coro)


class _Root:
    """A throwaway Hermes root holding the fixture adapter and the runtime module."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        adapter = self.dir / applier.RELATIVE
        adapter.parent.mkdir(parents=True)
        adapter.write_text(UPSTREAM)
        gateway = self.dir / "gateway"
        gateway.mkdir()
        shutil.copy(HERE / "slack_ux_reactions.py", gateway / "slack_ux_reactions.py")
        (gateway / "__init__.py").write_text("")

    def load(self, name):
        """Exec the (possibly patched) fixture adapter with ``gateway`` importable."""
        sys.path.insert(0, str(self.dir))
        try:
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_reactions", None)
            namespace = {"MessageEvent": object, "__name__": name}
            exec(compile((self.dir / applier.RELATIVE).read_text(), name, "exec"), namespace)  # noqa: S102
            return namespace
        finally:
            sys.path.remove(str(self.dir))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        text = (self.root.dir / applier.RELATIVE).read_text()
        self.assertEqual(text.count(applier.BUILD_MARKER), 2)
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drifted_docstring_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        path.write_text(UPSTREAM.replace("when message processing begins", "on start"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("on_processing_start docstring", str(caught.exception))
        self.assertEqual(path.read_text(), UPSTREAM.replace("when message processing begins", "on start"))

    def test_verifier_passes_on_patched_tree(self):
        applier.apply(self.root.dir)
        with mock.patch.dict(os.environ, {}, clear=False):
            verifier.main(self.root.dir)

    def test_verifier_refuses_unpatched_tree(self):
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)


class FlagOffIdentityTest(unittest.TestCase):
    """With KAGE_SLACK_UX off the patched hooks make exactly upstream's calls."""

    SCENARIOS = (
        ("fix it", "SUCCESS"),
        ("is it down?", "FAILURE"),
        ("checkout is down", "CANCELLED"),
    )

    def _calls(self, namespace, text, outcome):
        adapter = namespace["SlackAdapter"]()
        event = _event(text)
        _run(adapter.on_processing_start(event))
        _run(adapter.on_processing_complete(event, namespace["ProcessingOutcome"][outcome]))
        untracked = namespace["SlackAdapter"]()
        untracked._reacting_message_ids = set()
        _run(untracked.on_processing_start(event))
        return adapter.calls + untracked.calls

    def test_identical_to_upstream(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        upstream = root.load("upstream_fixture")
        applier.apply(root.dir)
        patched = root.load("patched_fixture")
        for value in (None, "", "0", "false"):
            env = {} if value is None else {"KAGE_SLACK_UX": value}
            with mock.patch.dict(os.environ, env, clear=False):
                if value is None:
                    os.environ.pop("KAGE_SLACK_UX", None)
                for text, outcome in self.SCENARIOS:
                    with self.subTest(flag=value, text=text, outcome=outcome):
                        self.assertEqual(
                            self._calls(patched, text, outcome), self._calls(upstream, text, outcome)
                        )

    def test_flag_on_hands_over(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        applier.apply(root.dir)
        patched = root.load("patched_fixture_on")
        reactions = sys.modules["gateway.slack_ux_reactions"]
        adapter = patched["SlackAdapter"]()
        event = _event("fix it")
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}), mock.patch.object(
            reactions, "open_cards", mock.AsyncMock(return_value=frozenset())
        ):
            _run(adapter.on_processing_start(event))
            _run(adapter.on_processing_complete(event, patched["ProcessingOutcome"].SUCCESS))
        self.assertEqual(
            adapter.calls,
            [(CHANNEL, ASK, "hammer_and_wrench", TEAM, False), (CHANNEL, ASK, "white_check_mark", TEAM, False)],
        )


class _Stub:
    def __init__(self):
        self.calls = []
        self._reacting_message_ids = {"m"}

    def _reacting_target(self, event):
        return (ASK, TEAM, "m") if "m" in self._reacting_message_ids else None

    async def _react(self, channel, ts, emoji, team_id, *, remove):
        self.calls.append((emoji, remove))
        return True


class RuntimeTest(unittest.TestCase):
    """The runtime module with the flag on, the board faked."""

    def setUp(self):
        importlib.reload(runtime)
        patcher = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.boards = []
        self.board_args = []

        async def open_cards(chat_id, thread_id, board=None):
            self.board_args.append((chat_id, thread_id, board))
            return self.boards.pop(0)

        cards = mock.patch.object(runtime, "open_cards", open_cards)
        cards.start()
        self.addCleanup(cards.stop)

    def _turn(self, text, before, after, outcome="success"):
        adapter = _Stub()
        self.boards[:] = [before, after]
        _run(runtime.on_processing_start(adapter, _event(text)))
        _run(runtime.on_processing_complete(adapter, _event(text), SimpleNamespace(value=outcome)))
        return adapter

    def _sub(self, task="t_a", platform="slack"):
        return {"platform": platform, "chat_id": CHANNEL, "thread_id": THREAD, "task_id": task}

    def test_direct_answer_settles_now(self):
        adapter = self._turn("board", frozenset(), frozenset())
        self.assertEqual(adapter.calls, [("clipboard", False), ("white_check_mark", False)])

    def test_failed_turn(self):
        adapter = self._turn("checkout is down", frozenset(), frozenset(), "failure")
        self.assertEqual(adapter.calls, [("rotating_light", False), ("x", False)])

    def test_cancelled_turn_adds_nothing_more(self):
        adapter = _Stub()
        self.boards[:] = [frozenset()]
        _run(runtime.on_processing_start(adapter, _event("hi")))
        _run(runtime.on_processing_complete(adapter, _event("hi"), SimpleNamespace(value="cancelled")))
        self.assertEqual(adapter.calls, [("eyes", False)])

    def test_untracked_event_is_left_alone(self):
        adapter = _Stub()
        adapter._reacting_message_ids = set()
        _run(runtime.on_processing_start(adapter, _event("fix it")))
        _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [])

    def test_old_open_card_does_not_defer(self):
        # A card from an earlier ask is still running; this turn answered directly.
        adapter = self._turn("why?", frozenset({"t_old"}), frozenset({"t_old"}))
        self.assertEqual(adapter.calls, [("eyes", False), ("white_check_mark", False)])

    def test_unreadable_board_settles_now(self):
        adapter = self._turn("why?", None, frozenset({"t_a"}))
        self.assertEqual(adapter.calls, [("eyes", False), ("white_check_mark", False)])

    def test_delegated_settles_when_the_last_card_does(self):
        adapter = self._turn("fix it", frozenset(), frozenset({"t_a"}))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        # A child finishes while the coordinator still runs: nothing yet.
        self.boards[:] = [frozenset({"t_a"})]
        _run(runtime.settle_delegated(adapter, self._sub("t_child"), "completed", board="b1"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.assertEqual(self.board_args[-1], (CHANNEL, THREAD, "b1"))
        # The coordinator blocks on the user: ⏸️ at once.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        self.assertEqual(adapter.calls[-1], ("double_vertical_bar", False))
        # Crashes are retried; status is bookkeeping.
        for kind in ("crashed", "timed_out", "status", "unblocked"):
            _run(runtime.settle_delegated(adapter, self._sub("t_a"), kind))
        self.assertEqual(len(adapter.calls), 2)
        # It completes and nothing else is open: ✅, and the ask is forgotten.
        self.boards[:] = [frozenset({"t_a"})]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("white_check_mark", False))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(len(adapter.calls), 3)

    def test_delegated_failure(self):
        adapter = self._turn("fix it", frozenset(), frozenset({"t_a"}))
        self.boards[:] = [frozenset()]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls[-1], ("x", False))

    def test_settle_ignores_other_platforms_and_unknown_threads(self):
        adapter = self._turn("fix it", frozenset(), frozenset({"t_a"}))
        _run(runtime.settle_delegated(adapter, self._sub(platform="google_chat"), "completed"))
        other = dict(self._sub(), thread_id="999.000")
        _run(runtime.settle_delegated(adapter, other, "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_nothing_is_ever_removed(self):
        adapter = self._turn("fix it", frozenset(), frozenset({"t_a"}))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        self.boards[:] = [frozenset()]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertTrue(adapter.calls)
        self.assertTrue(all(remove is False for _emoji, remove in adapter.calls))

    def test_flag_off_settle_is_inert(self):
        adapter = self._turn("fix it", frozenset(), frozenset({"t_a"}))
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": ""}):
            _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])


class OpenCardsQueryTest(unittest.TestCase):
    """The SQL against the two tables it reads, in upstream's column names."""

    def test_query(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT);"
            "CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT);"
            "INSERT INTO tasks VALUES ('a','running'),('b','blocked'),('c','done'),('d','archived'),('e','ready');"
            "INSERT INTO kanban_notify_subs VALUES"
            " ('a','slack','C1','111.000'),('b','slack','C1','111.000'),('c','slack','C1','111.000'),"
            " ('d','slack','C1','111.000'),('e','slack','C1','222.000'),('a','telegram','C1','111.000');"
        )
        rows = conn.execute(runtime.OPEN_CARDS_SQL, ("slack", "C1", "111.000")).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["a", "b"])

    def test_read_failure_is_none(self):
        with mock.patch.object(runtime, "_query_open_cards", side_effect=RuntimeError("locked")):
            self.assertIsNone(_run(runtime.open_cards("C1", "111.000")))


class MissingPresenterTest(unittest.TestCase):
    def test_treated_as_off(self):
        with mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
            os.environ, {"KAGE_SLACK_UX": "1"}
        ):
            self.assertFalse(runtime.enabled())


# Keep the enum import used: the fixture's ProcessingOutcome is exec'd, this
# module compares outcomes by value only.
assert enum.Enum


if __name__ == "__main__":
    unittest.main()
