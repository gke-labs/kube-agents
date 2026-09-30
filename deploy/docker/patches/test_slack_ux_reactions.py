"""Host tests for the KAGE_SLACK_UX reactions patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_reactions.py

The fixture carries upstream's two reaction hooks verbatim (v2026.9.14). The
tests apply the patch, exec both the patched and the unpatched fixture, and
drive them with a stub adapter: with the flag off the patched hooks must make
exactly the calls upstream makes, which is the flag-off identity for this
surface; with it on, the runtime module takes over.
"""

import asyncio
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


def _cards(*ids, board="default"):
    """A board read: open cards as ``(board, id)``."""
    return frozenset((board, task) for task in ids)


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
        # The board read needs the image's hermes_cli; the image build runs it.
        applier.apply(self.root.dir)
        with mock.patch.dict(os.environ, {}, clear=False):
            verifier.main(self.root.dir, board_read=False)

    def test_verifier_refuses_a_drifted_adapter_member(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        patched = path.read_text()
        drifts = {
            "react keyword": ("team_id, *, remove):", "team_id, remove=False):"),
            "target arity": ("return (ts, team_id, marker) if", "return (ts, marker) if"),
            "tracked set": ('self._reacting_message_ids = {"m"}', "self._reacting_message_ids = []"),
            "target gone": ("def _reacting_target(self, event):", "def _target(self, event):"),
        }
        for name, (old, new) in drifts.items():
            with self.subTest(drift=name):
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit):
                    verifier.check_adapter(self.root.dir)
        path.write_text(patched)
        verifier.check_adapter(self.root.dir)

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
    def __init__(self, ts=ASK, log=None):
        self.calls = []
        self.ts = ts
        self.log = log
        self._reacting_message_ids = {"m"}

    def _reacting_target(self, event):
        return (self.ts, TEAM, "m") if "m" in self._reacting_message_ids else None

    async def _react(self, channel, ts, emoji, team_id, *, remove):
        self.calls.append((emoji, remove))
        if self.log is not None:
            self.log.append((ts, emoji))
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

        async def open_cards(chat_id, thread_id):
            self.board_args.append((chat_id, thread_id))
            return self.boards.pop(0)

        cards = mock.patch.object(runtime, "open_cards", open_cards)
        cards.start()
        self.addCleanup(cards.stop)

    def _turn(self, text, before, after, outcome="success", adapter=None):
        adapter = adapter or _Stub()
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
        adapter = self._turn("why?", _cards("t_old"), _cards("t_old"))
        self.assertEqual(adapter.calls, [("eyes", False), ("white_check_mark", False)])

    def test_unreadable_board_settles_now(self):
        adapter = self._turn("why?", None, _cards("t_a"))
        self.assertEqual(adapter.calls, [("eyes", False), ("white_check_mark", False)])

    def test_delegated_settles_when_the_last_card_does(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        # A card this ask did not open finishes: nothing, and no board is read.
        reads = len(self.board_args)
        _run(runtime.settle_delegated(adapter, self._sub("t_child"), "completed", board="b1"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.assertEqual(len(self.board_args), reads)
        # The coordinator blocks on the user: ⏸️ at once.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        self.assertEqual(adapter.calls[-1], ("double_vertical_bar", False))
        # Crashes are retried; status is bookkeeping.
        for kind in ("crashed", "timed_out", "status", "unblocked"):
            _run(runtime.settle_delegated(adapter, self._sub("t_a"), kind))
        self.assertEqual(len(adapter.calls), 2)
        # It completes: ✅, and the ask is forgotten.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("white_check_mark", False))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(len(adapter.calls), 3)

    def test_delegated_failure(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls[-1], ("x", False))

    def test_fan_out_with_a_give_up_settles_once_as_failed(self):
        # Hermes leaves a gave-up card at status 'blocked', so it still reads as
        # open on the board; the ask must settle on the event, not the board.
        adapter = self._turn("fix it", frozenset(), _cards("t_a", "t_b"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(len(adapter.calls), 2)

    def test_a_stale_blocked_card_in_the_thread_does_not_hold_the_settle(self):
        adapter = self._turn("fix it", _cards("t_old"), _cards("t_old", "t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("white_check_mark", False))
        # The stale card finishing later touches nothing.
        _run(runtime.settle_delegated(adapter, self._sub("t_old"), "gave_up"))
        self.assertEqual(len(adapter.calls), 2)

    def test_two_asks_in_one_thread_settle_on_their_own_cards(self):
        log = []
        first = self._turn("fix it", frozenset(), _cards("t_a"), adapter=_Stub("111.001", log))
        second = self._turn("scale it", _cards("t_a"), _cards("t_a", "t_b"), adapter=_Stub("111.002", log))
        _run(runtime.settle_delegated(second, self._sub("t_b"), "blocked"))
        self.assertEqual(log[-1], ("111.002", "double_vertical_bar"))
        _run(runtime.settle_delegated(second, self._sub("t_b"), "completed"))
        self.assertEqual(log[-1], ("111.002", "white_check_mark"))
        _run(runtime.settle_delegated(first, self._sub("t_a"), "gave_up"))
        self.assertEqual(log[-1], ("111.001", "x"))
        self.assertEqual(len(log), 5)

    def test_a_card_that_finishes_before_the_ask_is_deferred(self):
        # The notifier delivers the card's final event between the turn's board
        # read and its deferral: the turn settles at once instead of waiting.
        adapter = _Stub()
        self.boards[:] = [frozenset()]
        _run(runtime.on_processing_start(adapter, _event("fix it")))

        async def read_then_race(chat_id, thread_id):
            await runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up")
            return _cards("t_a")

        with mock.patch.object(runtime, "open_cards", read_then_race):
            _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False)])
        self.assertFalse(runtime._deferred)

    def test_a_card_on_another_board_settles_on_that_board_only(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a", board="b2"))
        # The same id on the default board is a different card.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed", board="b2"))
        self.assertEqual(adapter.calls[-1], ("white_check_mark", False))

    def test_a_thread_keeps_only_its_newest_deferred_asks(self):
        for n in range(runtime.DEFERRED_PER_THREAD + 2):
            self._turn("fix it", frozenset(), _cards(f"t_{n}"), adapter=_Stub(f"111.{n:03d}"))
        asks = runtime._deferred[(CHANNEL, THREAD)]
        self.assertEqual(len(asks), runtime.DEFERRED_PER_THREAD)
        self.assertEqual(asks[0].ts, "111.002")

    def test_settle_ignores_other_platforms_and_unknown_threads(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub(platform="google_chat"), "completed"))
        other = dict(self._sub(), thread_id="999.000")
        _run(runtime.settle_delegated(adapter, other, "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_nothing_is_ever_removed(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        self.boards[:] = [frozenset()]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertTrue(adapter.calls)
        self.assertTrue(all(remove is False for _emoji, remove in adapter.calls))

    def test_flag_off_settle_is_inert(self):
        adapter = self._turn("fix it", frozenset(), _cards("t_a"))
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

    def _fake_hermes(self, paths, rows, reads, broken=()):
        """``hermes_cli`` modules listing ``paths`` as boards, each database returning ``rows``."""

        class Conn:
            def __init__(self, path):
                self.path = path

            def execute(self, sql, params):
                reads.append((self.path, params))
                return SimpleNamespace(fetchall=lambda: rows[self.path])

            def close(self):
                pass

        def connect(board=None):
            if board in broken:
                raise RuntimeError("database is locked")
            return Conn(paths[board])

        kb = SimpleNamespace(
            DEFAULT_BOARD="default",
            list_boards=lambda include_archived: [{"slug": s, "db_path": p} for s, p in paths.items()],
            kanban_db_path=lambda slug: Path(paths[slug]),
        )
        connector = SimpleNamespace(connect=connect)
        hermes = SimpleNamespace(kanban_db=kb, kanban_db_connect=connector)
        return {"hermes_cli": hermes, "hermes_cli.kanban_db": kb, "hermes_cli.kanban_db_connect": connector}

    def _databases(self, *names):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        return [str((tmp / name).resolve()) for name in names]

    def test_reads_every_live_board_once(self):
        # Two boards share a database, as a board listed twice would: one read
        # under the first slug. A third board has its own database.
        default, b2 = self._databases("default.db", "b2.db")
        Path(default).touch()
        Path(b2).touch()
        paths = {"default": default, "alias": default, "b2": b2}
        rows = {default: [("t_a",)], b2: [("t_b",), ("t_c",)]}
        reads = []
        with mock.patch.dict(sys.modules, self._fake_hermes(paths, rows, reads)):
            found = runtime._query_open_cards("C1", "111.000")
        self.assertEqual(found, {("default", "t_a"), ("b2", "t_b"), ("b2", "t_c")})
        self.assertEqual([path for path, _ in reads], [default, b2])
        self.assertEqual(reads[0][1], ("slack", "C1", "111.000"))

    def test_an_unreadable_or_missing_board_skips_only_itself(self):
        good, bad, absent = self._databases("good.db", "bad.db", "absent.db")
        Path(good).touch()
        Path(bad).touch()
        paths = {"default": good, "b2": bad, "b3": absent}
        reads = []
        with mock.patch.dict(sys.modules, self._fake_hermes(paths, {good: [("t_a",)]}, reads, broken={"b2"})):
            found = runtime._query_open_cards("C1", "111.000")
        self.assertEqual(found, {("default", "t_a")})
        # b3 has no database: never connected to, so never created.
        self.assertEqual([path for path, _ in reads], [good])

    def test_read_failure_is_none(self):
        with mock.patch.object(runtime, "_query_open_cards", side_effect=RuntimeError("locked")):
            self.assertIsNone(_run(runtime.open_cards("C1", "111.000")))


class MissingPresenterTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)

    def test_treated_as_off(self):
        with mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
            os.environ, {"KAGE_SLACK_UX": "1"}
        ):
            self.assertFalse(runtime.enabled())

    def test_warns_only_when_the_flag_is_on(self):
        for value, warns in (("0", False), ("false", False), ("on", True)):
            importlib.reload(runtime)
            with self.subTest(flag=value), mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
                os.environ, {"KAGE_SLACK_UX": value}
            ), mock.patch.object(runtime.logger, "warning") as warning:
                runtime.enabled()
                self.assertEqual(warning.called, warns)


if __name__ == "__main__":
    unittest.main()
