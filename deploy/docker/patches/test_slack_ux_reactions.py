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


def _cards(*ids, board="default", status="running", creator=None, gave_up=False):
    """A board read: open cards as ``{(board, id): _Card}``."""
    return {(board, task): runtime._Card(status, creator, gave_up) for task in ids}


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
            "target order": ("return (ts, team_id, marker) if", "return (team_id, ts, marker) if"),
            "react order": ("_react(self, channel, timestamp, emoji, team_id, *", "_react(self, channel, emoji, timestamp, team_id, *"),
            "tracked set": ('self._reacting_message_ids = {"m"}', "self._reacting_message_ids = []"),
            "target gone": ("def _reacting_target(self, event):", "def _target(self, event):"),
            "second target shape": ("            return None\n", "            return (ts, marker)\n"),
            "target not a tuple": ("return (ts, team_id, marker) if", "return [ts, team_id, marker] if"),
            "set reassigned": (
                'self._reacting_message_ids = {"m"}',
                'self._reacting_message_ids = {"m"}\n        self._reacting_message_ids = []',
            ),
            "hook parameter renamed": (
                "event: MessageEvent, outcome: ProcessingOutcome)",
                "event: MessageEvent, result: ProcessingOutcome)",
            ),
        }
        for name, (old, new) in drifts.items():
            with self.subTest(drift=name):
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit):
                    verifier.check_adapter(self.root.dir)
        path.write_text(patched)
        verifier.check_adapter(self.root.dir)

    def test_verifier_reads_the_set_off_the_adapter_class(self):
        # A set assigned in another class does not make the adapter's a set.
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        patched = path.read_text()
        drifted = patched.replace('self._reacting_message_ids = {"m"}', "self._reacting_message_ids = []")
        drifted += "\n\nclass Other:\n    def __init__(self):\n        self._reacting_message_ids = set()\n"
        path.write_text(drifted)
        with self.assertRaises(SystemExit):
            verifier.check_adapter(self.root.dir)

    def test_verifier_reads_the_members_off_the_adapter_class(self):
        # A mixin above the adapter keeps the expected shape; the adapter's own
        # _react swaps timestamp and emoji, which the runtime would call wrong.
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        patched = path.read_text()
        mixin = "class _Mixin:\n    async def _react(self, channel, timestamp, emoji, team_id, *, remove):\n        pass\n\n\n"
        drifted = patched.replace(
            "async def _react(self, channel, timestamp, emoji, team_id, *, remove):",
            "async def _react(self, channel, emoji, timestamp, team_id, *, remove):",
        ).replace("class SlackAdapter", mixin + "class SlackAdapter", 1)
        self.assertNotEqual(drifted, patched)
        path.write_text(drifted)
        with self.assertRaises(SystemExit):
            verifier.check_adapter(self.root.dir)

    def test_verifier_reads_the_hooks_off_the_adapter_class(self):
        # Guarded hooks on another class do not guard the adapter's own, which
        # the runtime calls. Placed before the adapter they catch a module walk
        # that takes the first match; after it, one that takes the last.
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        other = path.read_text().replace("class SlackAdapter", "class Other", 1)
        for name, source in (("other first", other + "\n\n" + UPSTREAM), ("other last", UPSTREAM + "\n\n" + other)):
            with self.subTest(name):
                path.write_text(source)
                with self.assertRaises(SystemExit):
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
        for value in ("", "0", "false"):
            with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
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
            reactions, "open_cards", mock.AsyncMock(return_value={})
        ):
            _run(adapter.on_processing_start(event))
            _run(adapter.on_processing_complete(event, patched["ProcessingOutcome"].SUCCESS))
            adapter._reacting_message_ids = {"m"}
            _run(adapter.on_processing_start(event))
            _run(adapter.on_processing_complete(event, patched["ProcessingOutcome"].FAILURE))
        kind = (CHANNEL, ASK, "hammer_and_wrench", TEAM)
        self.assertEqual(
            adapter.calls,
            [(*kind, False), (*kind, True), (*kind, False), (CHANNEL, ASK, "x", TEAM, False), (*kind, True)],
        )


class DisplayTest(unittest.TestCase):
    """What an ask shows: its kind until it is answered, ⏸️ while it waits on you, ❌ if it failed."""

    SUB = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": "t_a"}

    def setUp(self):
        importlib.reload(runtime)
        patcher = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.boards = []

        async def open_cards(chat_id, thread_id):
            return self.boards.pop(0) if self.boards else {}

        for name, fake in (("open_cards", open_cards), ("thread_lineage", mock.AsyncMock(return_value={}))):
            patched = mock.patch.object(runtime, name, fake)
            patched.start()
            self.addCleanup(patched.stop)

    def _turn(self, text, after, outcome, adapter=None):
        adapter = adapter or _Stub()
        self.boards[:] = [{}, after]
        _run(runtime.on_processing_start(adapter, _event(text)))
        _run(runtime.on_processing_complete(adapter, _event(text), SimpleNamespace(value=outcome)))
        return adapter

    def test_the_arrival_reaction_is_the_asks_kind(self):
        for text, kind in (
            ("why is checkout restarting?", "mag"),
            ("which clusters have pods restarting right now?", "globe_with_meridians"),
            ("what version is seeded-b on?", "arrow_up"),
            ("what did seeded-a cost last month?", "moneybag"),
            ("run a security audit on seeded-b", "shield"),
            ("fix it", "hammer_and_wrench"),
            ("board", "clipboard"),
            ("payments-api is down", "rotating_light"),
            ("thanks", "eyes"),
        ):
            with self.subTest(text=text):
                adapter = _Stub()
                self.boards[:] = [{}]
                _run(runtime.on_processing_start(adapter, _event(text)))
                self.assertEqual(adapter.calls, [(kind, False)])

    def test_a_direct_answer_takes_the_arrival_off_and_leaves_x_only_on_failure(self):
        for text, kind in (("is seeded-a healthy?", "eyes"), ("fix it", "hammer_and_wrench")):
            with self.subTest(text=text):
                self.assertEqual(self._turn(text, {}, "success").calls, [(kind, False), (kind, True)])
                self.assertEqual(
                    self._turn(text, {}, "failure").calls, [(kind, False), ("x", False), (kind, True)],
                )
                self.assertEqual(self._turn(text, {}, "cancelled").calls, [(kind, False), (kind, True)])

    def test_a_delegated_answer_keeps_the_arrival_until_its_final_answer(self):
        card = (runtime.DEFAULT_BOARD, "t_a")
        arrival = ("hammer_and_wrench", False)
        for kinds, settled in (
            (("completed",), [("hammer_and_wrench", True)]),
            (("gave_up",), [("x", False), ("hammer_and_wrench", True)]),
        ):
            with self.subTest(kinds=kinds):
                runtime._deferred.clear()
                adapter = self._turn("fix it", {card: runtime._Card("running")}, "success")
                # The acknowledgement posted; the work has not answered yet.
                self.assertEqual(adapter.calls, [arrival])
                for kind in kinds:
                    _run(runtime.settle_delegated(adapter, self.SUB, kind))
                self.assertEqual(adapter.calls, [arrival, *settled])

    def test_pause_goes_on_while_a_card_waits_on_you_and_off_when_you_answer(self):
        card = (runtime.DEFAULT_BOARD, "t_a")
        adapter = self._turn("fix it", {card: runtime._Card("running")}, "success")
        _run(runtime.settle_delegated(adapter, self.SUB, "blocked"))
        # A replayed or repeated block does not stack a second ⏸️.
        _run(runtime.settle_delegated(adapter, self.SUB, "review_requested"))
        self.assertEqual(adapter.calls[1:], [("double_vertical_bar", False)])
        _run(runtime.settle_delegated(adapter, self.SUB, "unblocked"))
        self.assertEqual(adapter.calls[2:], [("double_vertical_bar", True)])
        _run(runtime.settle_delegated(adapter, self.SUB, "completed"))
        self.assertEqual(adapter.calls[3:], [("hammer_and_wrench", True)])

    def test_a_card_that_finishes_while_paused_takes_the_pause_off_with_the_arrival(self):
        card = (runtime.DEFAULT_BOARD, "t_a")
        adapter = self._turn("fix it", {card: runtime._Card("running")}, "success")
        _run(runtime.settle_delegated(adapter, self.SUB, "blocked"))
        _run(runtime.settle_delegated(adapter, self.SUB, "gave_up"))
        self.assertEqual(
            adapter.calls,
            [
                ("hammer_and_wrench", False), ("double_vertical_bar", False),
                ("x", False), ("double_vertical_bar", True), ("hammer_and_wrench", True),
            ],
        )

    def test_the_pause_stays_while_another_card_of_the_ask_waits_on_you(self):
        adapter = self._turn("fix it", _cards("t_a", "t_b"), "success")
        _run(runtime.settle_delegated(adapter, self.SUB, "blocked"))
        _run(runtime.settle_delegated(adapter, {**self.SUB, "task_id": "t_b"}, "blocked"))
        _run(runtime.settle_delegated(adapter, self.SUB, "unblocked"))
        self.assertEqual(adapter.calls[1:], [("double_vertical_bar", False)])
        _run(runtime.settle_delegated(adapter, {**self.SUB, "task_id": "t_b"}, "unblocked"))
        self.assertEqual(adapter.calls[2:], [("double_vertical_bar", True)])

    def test_a_card_waiting_on_you_from_the_turn_or_a_follow_up_pauses_the_ask(self):
        # Both other ways an ask can wait on you: a card that blocks before the
        # turn ends, then a follow-up its worker files that blocks on you too.
        adapter = _Stub()
        self.boards[:] = [{}]
        _run(runtime.on_processing_start(adapter, _event("fix it")))

        async def block_then_read(chat_id, thread_id):
            await runtime.settle_delegated(adapter, self.SUB, "blocked")
            return _cards("t_a", status="blocked")

        with mock.patch.object(runtime, "open_cards", block_then_read):
            _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("double_vertical_bar", False)])
        self.boards[:] = [_cards("t_b", status="blocked", creator="t_a")]
        _run(runtime.settle_delegated(adapter, self.SUB, "completed"))
        # t_a finished, t_b waits on you now: the pause stays on.
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].blocked, {("default", "t_b")})
        _run(runtime.settle_delegated(adapter, {**self.SUB, "task_id": "t_b"}, "gave_up"))
        self.assertEqual(
            adapter.calls[2:], [("x", False), ("double_vertical_bar", True), ("hammer_and_wrench", True)],
        )

    def test_a_removal_that_is_refused_leaves_the_reaction_and_the_answer_goes_on(self):
        adapter = _Stub(refuse_removal=True)
        with self.assertLogs(runtime.logger, "INFO") as logs:
            self._turn("fix it", {}, "failure", adapter=adapter)
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False), ("hammer_and_wrench", True)])
        self.assertIn("leaving it on", logs.output[0])

    def test_an_arrival_that_never_went_on_is_not_removed(self):
        adapter = _Stub(refuse_add=True)
        self._turn("fix it", {}, "success", adapter=adapter)
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])


class _Stub:
    def __init__(self, ts=ASK, log=None, refuse_removal=False, refuse_add=False):
        self.calls = []
        self.ts = ts
        self.log = log
        self.refuse_removal = refuse_removal
        self.refuse_add = refuse_add
        self._reacting_message_ids = {"m"}

    def _reacting_target(self, event):
        return (self.ts, TEAM, "m") if "m" in self._reacting_message_ids else None

    async def _react(self, channel, ts, emoji, team_id, *, remove):
        self.calls.append((emoji, remove))
        if self.log is not None:
            self.log.append((ts, f"-{emoji}" if remove else emoji))
        return not (self.refuse_removal if remove else self.refuse_add)


class RuntimeTest(unittest.TestCase):
    """The runtime module with the flag on and the board faked: which settle an ask reaches, and when.

    A settle shows as the arrival reaction coming off (``(kind, True)``), with ❌
    before it for a failure; ``DisplayTest`` checks the reactions themselves.
    """

    def setUp(self):
        importlib.reload(runtime)
        patcher = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.boards = []
        self.board_args = []

        async def open_cards(chat_id, thread_id):
            self.board_args.append((chat_id, thread_id))
            return self.boards.pop(0) if self.boards else {}

        cards = mock.patch.object(runtime, "open_cards", open_cards)
        cards.start()
        self.addCleanup(cards.stop)
        self.lineage = {}

        self.lineage_reads = 0

        async def thread_lineage(chat_id, thread_id):
            self.lineage_reads += 1
            return self.lineage

        lineage = mock.patch.object(runtime, "thread_lineage", thread_lineage)
        lineage.start()
        self.addCleanup(lineage.stop)
        # slack_ux_status, faked: test_slack_ux_status.py covers the hold itself.
        self.expected = []

        async def expect_cards(adapter, chat_id, team_id, thread_ts, cards):
            self.expected.append((chat_id, team_id, thread_ts, dict(sorted(cards.items()))))

        status = SimpleNamespace(expect_cards=expect_cards)
        modules = mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_status=status), "gateway.slack_ux_status": status})
        modules.start()
        self.addCleanup(modules.stop)

    def _turn(self, text, before, after, outcome="success", adapter=None):
        adapter = adapter or _Stub()
        self.boards[:] = [before, after]
        _run(runtime.on_processing_start(adapter, _event(text)))
        _run(runtime.on_processing_complete(adapter, _event(text), SimpleNamespace(value=outcome)))
        return adapter

    def _sub(self, task="t_a", platform="slack"):
        return {"platform": platform, "chat_id": CHANNEL, "thread_id": THREAD, "task_id": task}

    def test_direct_answer_settles_now(self):
        adapter = self._turn("board", {}, {})
        self.assertEqual(adapter.calls, [("clipboard", False), ("clipboard", True)])

    def test_failed_turn(self):
        adapter = self._turn("checkout is down", {}, {}, "failure")
        self.assertEqual(adapter.calls, [("rotating_light", False), ("x", False), ("rotating_light", True)])

    def test_cancelled_turn_adds_nothing_more(self):
        adapter = _Stub()
        self.boards[:] = [{}]
        _run(runtime.on_processing_start(adapter, _event("hi")))
        _run(runtime.on_processing_complete(adapter, _event("hi"), SimpleNamespace(value="cancelled")))
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_untracked_event_is_left_alone(self):
        adapter = _Stub()
        adapter._reacting_message_ids = set()
        _run(runtime.on_processing_start(adapter, _event("fix it")))
        _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [])

    def test_old_open_card_does_not_defer(self):
        # A card from an earlier ask is still running; this turn answered directly.
        adapter = self._turn("what now?", _cards("t_old"), _cards("t_old"))
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_unreadable_board_settles_now(self):
        adapter = self._turn("what now?", None, _cards("t_a"))
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_delegated_settles_when_the_last_card_does(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
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
        for kind in ("crashed", "timed_out", "status"):
            _run(runtime.settle_delegated(adapter, self._sub("t_a"), kind))
        self.assertEqual(len(adapter.calls), 2)
        # It completes: ⏸️ and the arrival reaction off, and the ask is forgotten.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-2:], [("double_vertical_bar", True), ("hammer_and_wrench", True)])
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(len(adapter.calls), 4)

    def test_a_failed_turn_that_delegated_settles_failed(self):
        adapter = self._turn("fix it", {}, _cards("t_a"), "failure")
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-2:], [("x", False), ("hammer_and_wrench", True)])

    def test_delegated_failure(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls[-2:], [("x", False), ("hammer_and_wrench", True)])

    def test_fan_out_with_a_give_up_settles_once_as_failed(self):
        # Hermes leaves a gave-up card at status 'blocked', so it still reads as
        # open on the board; the ask must settle on the event, not the board.
        adapter = self._turn("fix it", {}, _cards("t_a", "t_b"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False), ("hammer_and_wrench", True)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(len(adapter.calls), 3)

    def test_a_card_archived_by_hand_comes_off_its_ask_without_failing_it(self):
        # No later event names an archived card, so it cannot hold the ask open.
        adapter = self._turn("fix it", {}, _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "archived"))
        self.assertEqual(adapter.calls[-2:], [("double_vertical_bar", True), ("hammer_and_wrench", True)])
        self.assertNotIn(("x", False), adapter.calls)
        self.assertNotIn((CHANNEL, THREAD), runtime._deferred)

    def test_a_fan_out_with_an_archived_card_settles_on_the_rest(self):
        adapter = self._turn("fix it", {}, _cards("t_a", "t_b"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "archived"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])

    def test_a_stale_blocked_card_in_the_thread_does_not_hold_the_settle(self):
        adapter = self._turn("fix it", _cards("t_old"), _cards("t_old", "t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("hammer_and_wrench", True))
        # The stale card finishing later touches nothing.
        _run(runtime.settle_delegated(adapter, self._sub("t_old"), "gave_up"))
        self.assertEqual(len(adapter.calls), 2)

    def test_a_follow_up_the_cards_worker_files_after_the_turn_holds_the_settle(self):
        # t_a's worker files t_b with parents=[t_a]: absent at the turn's end,
        # open on the board when t_a completes.
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [{**_cards("t_b", status="todo", creator="t_a"), **_cards("t_other", creator="t_x")}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        # Another worker's card is not this ask's.
        _run(runtime.settle_delegated(adapter, self._sub("t_other"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "gave_up"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False), ("hammer_and_wrench", True)])

    def test_a_turn_that_opened_cards_holds_working_for_those_about_to_start(self):
        board = {
            **_cards("t_a"), **_cards("t_ready", status="ready"),
            **_cards("t_b", status="todo"), **_cards("t_later", status="scheduled"),
        }
        self._turn("fix it", _cards("t_old"), {**_cards("t_old"), **board})
        self.assertEqual(
            self.expected,
            [(CHANNEL, TEAM, THREAD, {"t_a": True, "t_b": False, "t_ready": True})],
            "t_b waits on its parents",
        )

    def test_a_direct_answer_holds_nothing(self):
        self._turn("what now?", _cards("t_old"), _cards("t_old"))
        self.assertEqual(self.expected, [])

    def test_a_follow_up_holds_working_until_it_starts(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [_cards("t_b", status="todo", creator="t_a")]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(self.expected, [(CHANNEL, TEAM, THREAD, {"t_a": True}), (CHANNEL, TEAM, THREAD, {"t_b": False})])

    def test_a_failed_hold_never_fails_the_turn(self):
        async def boom(*args):
            raise RuntimeError("slack down")

        sys.modules["gateway.slack_ux_status"].expect_cards = boom
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_a_follow_up_filed_under_a_completed_follow_up_holds_the_settle(self):
        # t_b, filed by t_a's worker, has completed; t_c, filed by t_b's worker,
        # is still open. Only the lineage read still carries t_b.
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.lineage = {("default", "t_a"): None, ("default", "t_b"): "t_a", ("default", "t_c"): "t_b"}
        self.boards[:] = [{**_cards("t_c", status="todo", creator="t_b"), **_cards("t_other", creator="t_x")}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_c"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])

    def test_a_card_under_a_parked_follow_up_does_not_hold_the_settle(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [{
            **_cards("t_b", status="blocked", creator="t_a", gave_up=True),
            **_cards("t_c", status="todo", creator="t_b"),
        }]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])
        self.assertEqual(runtime._deferred, {})

    def test_a_card_under_an_open_follow_up_waits_for_that_follow_up(self):
        # t_g, filed by t_b's worker, is t_b's to carry: t_a's completion holds
        # t_b alone, and t_g's give-up before t_b completes leaves t_g parked.
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [{**_cards("t_b", creator="t_a"), **_cards("t_g", creator="t_b")}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        _run(runtime.settle_delegated(adapter, self._sub("t_g"), "gave_up"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.boards[:] = [_cards("t_g", status="blocked", creator="t_b", gave_up=True)]
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])

    def test_a_card_under_an_open_follow_up_blocked_on_the_user_pauses_at_that_follow_up(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [{**_cards("t_b", creator="t_a"), **_cards("t_g", status="blocked", creator="t_b")}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.boards[:] = [_cards("t_g", status="blocked", creator="t_b")]
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("double_vertical_bar", False)])

    def test_a_completed_card_still_read_as_open_holds_its_follow_ups(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [{**_cards("t_a"), **_cards("t_b", status="todo", creator="t_a")}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_a_card_past_the_depth_cap_does_not_hold_the_settle(self):
        depth = runtime.LINEAGE_DEPTH
        adapter = self._turn("fix it", {}, _cards("t_0"))
        self.lineage = {("default", f"t_{n}"): f"t_{n - 1}" for n in range(1, depth + 2)}
        self.boards[:] = [_cards(f"t_{depth + 1}", status="todo", creator=f"t_{depth}")]
        _run(runtime.settle_delegated(adapter, self._sub("t_0"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])
        adapter = self._turn("fix it", {}, _cards("t_0"))
        self.boards[:] = [_cards(f"t_{depth}", status="todo", creator=f"t_{depth - 1}")]
        _run(runtime.settle_delegated(adapter, self._sub("t_0"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_an_unreadable_board_skips_the_lineage_read(self):
        reads = []

        async def thread_lineage(chat_id, thread_id):
            reads.append((chat_id, thread_id))
            return {}

        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [None]
        with mock.patch.object(runtime, "thread_lineage", thread_lineage):
            _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(reads, [])
        self.assertEqual(adapter.calls[-1], ("hammer_and_wrench", True))

    def test_the_lineage_walk_stops_at_its_depth_cap_and_on_a_cycle(self):
        chain = {("default", f"t_{n}"): f"t_{n - 1}" for n in range(1, runtime.LINEAGE_DEPTH + 2)}
        found = runtime._descendants(("default", "t_0"), chain)
        self.assertEqual(found, {("default", f"t_{n}") for n in range(1, runtime.LINEAGE_DEPTH + 1)})
        cycle = {("default", "t_a"): "t_b", ("default", "t_b"): "t_a"}
        self.assertEqual(runtime._descendants(("default", "t_a"), cycle), {("default", "t_a"), ("default", "t_b")})
        # Another board's card of the same id is not a descendant.
        self.assertEqual(runtime._descendants(("default", "t_a"), {("b2", "t_b"): "t_a"}), set())
        # Nor is anything under a card still open, which is itself still found.
        under = {("default", "t_b"): "t_a", ("default", "t_c"): "t_b"}
        self.assertEqual(runtime._descendants(("default", "t_a"), under, frozenset({("default", "t_b")})), {("default", "t_b")})

    def test_a_follow_up_blocked_on_the_user_holds_the_settle_and_pauses_it(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [_cards("t_b", status="blocked", creator="t_a")]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("double_vertical_bar", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "gave_up"))
        self.assertEqual(
            adapter.calls[-3:], [("x", False), ("double_vertical_bar", True), ("hammer_and_wrench", True)],
        )

    def test_a_follow_up_parked_by_a_give_up_does_not_hold_the_settle(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [_cards("t_b", status="blocked", creator="t_a", gave_up=True)]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("hammer_and_wrench", True)])

    def test_a_give_up_does_not_wait_on_a_follow_up_it_gates(self):
        # t_b waits on t_a reaching done, and a card that gave up is parked.
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [_cards("t_b", status="todo", creator="t_a")]
        reads = len(self.board_args)
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False), ("hammer_and_wrench", True)])
        self.assertEqual(len(self.board_args), reads)

    def test_a_follow_up_whose_creator_closed_within_the_turn_is_its(self):
        # t_a opened and completed before the notifier reported it: absent from
        # both reads and from the turn's finishes, but its follow-up is the turn's.
        adapter = self._turn("fix it", {}, _cards("t_b", creator="t_a"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_b"), "gave_up"))
        self.assertEqual(adapter.calls[-2:], [("x", False), ("hammer_and_wrench", True)])

    def test_an_unreadable_board_at_the_finish_settles_as_before(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        self.boards[:] = [None]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("hammer_and_wrench", True))

    def test_two_asks_in_one_thread_settle_on_their_own_cards(self):
        log = []
        first = self._turn("fix it", {}, _cards("t_a"), adapter=_Stub("111.001", log))
        second = self._turn("scale it", _cards("t_a"), _cards("t_a", "t_b"), adapter=_Stub("111.002", log))
        _run(runtime.settle_delegated(second, self._sub("t_b"), "blocked"))
        self.assertEqual(log[-1], ("111.002", "double_vertical_bar"))
        _run(runtime.settle_delegated(second, self._sub("t_b"), "completed"))
        self.assertEqual(log[-2:], [("111.002", "-double_vertical_bar"), ("111.002", "-hammer_and_wrench")])
        _run(runtime.settle_delegated(first, self._sub("t_a"), "gave_up"))
        self.assertEqual(log[-2:], [("111.001", "x"), ("111.001", "-hammer_and_wrench")])
        self.assertEqual(len(log), 7)

    def test_a_card_that_finishes_before_the_ask_is_deferred(self):
        # The notifier delivers the card's final event between the turn's board
        # read and its deferral: the turn settles at once instead of waiting.
        adapter = _Stub()
        self.boards[:] = [{}]
        _run(runtime.on_processing_start(adapter, _event("fix it")))

        async def read_then_race(chat_id, thread_id):
            await runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up")
            return _cards("t_a")

        with mock.patch.object(runtime, "open_cards", read_then_race):
            _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("x", False), ("hammer_and_wrench", True)])
        self.assertFalse(runtime._deferred)

    def _turn_racing(self, text, before, after, events, adapter):
        """A turn whose end-of-turn board read sees the notifier deliver ``events`` first."""
        self.boards[:] = [before]
        _run(runtime.on_processing_start(adapter, _event(text)))

        async def read_then_race(chat_id, thread_id):
            for task, kind in events:
                await runtime.settle_delegated(adapter, self._sub(task), kind)
            return after

        with mock.patch.object(runtime, "open_cards", read_then_race):
            _run(runtime.on_processing_complete(adapter, _event(text), SimpleNamespace(value="success")))

    def test_a_card_reopened_after_it_finished_waits_on_its_new_run(self):
        log = []
        first = self._turn("fix it", {}, _cards("t_a"), adapter=_Stub("111.001", log))
        _run(runtime.settle_delegated(first, self._sub("t_a"), "completed"))
        self.assertEqual(log[-1], ("111.001", "-hammer_and_wrench"))
        # The finished card is closed, so the next turn's start read misses it;
        # the turn reopens it. Its earlier finish does not settle this ask.
        self._turn("fix it again", {}, _cards("t_a"), adapter=_Stub("111.002", log))
        self.assertEqual([ts for ts, _ in log].count("111.002"), 1)
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].cards, {("default", "t_a")})
        _run(runtime.settle_delegated(first, self._sub("t_a"), "completed"))
        self.assertEqual(log[-1], ("111.002", "-hammer_and_wrench"))

    def test_a_card_unblocked_by_someone_else_during_a_direct_answer_is_not_its(self):
        # A CLI unblock lands while "what now?" is answered; nothing names who did it.
        adapter = _Stub()
        self._turn_racing("what now?", _cards("t_a", status="blocked"), _cards("t_a"), [], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])
        self.assertFalse(runtime._deferred)
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "gave_up"))
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_a_blocked_card_that_gives_up_again_during_the_turn_does_not_fail_it(self):
        # Unblocked and parked again, or a give-up from before the ask reported
        # late: either way the card's own ask carries the ❌.
        adapter = _Stub()
        blocked = _cards("t_a", status="blocked")
        self._turn_racing("try again", blocked, blocked, [("t_a", "gave_up")], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])
        self.assertFalse(runtime._deferred)

    def test_a_blocked_card_that_blocks_again_during_the_turn_does_not_pause_it(self):
        adapter = _Stub()
        blocked = _cards("t_a", status="blocked")
        self._turn_racing("yes, go ahead", blocked, blocked, [("t_a", "blocked")], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_a_card_an_earlier_asks_worker_creates_is_not_the_turns(self):
        # Ask 1's worker files a follow-up while "what now?" is answered; the
        # follow-up inherited the thread's subscription from t_a.
        adapter = _Stub()
        after = {**_cards("t_a"), **_cards("t_b", creator="t_a")}
        self._turn_racing("what now?", _cards("t_a"), after, [], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])
        self.assertFalse(runtime._deferred)

    def test_a_card_created_under_a_left_out_card_is_left_out_too(self):
        adapter = _Stub()
        after = {**_cards("t_a"), **_cards("t_b", creator="t_a"), **_cards("t_c", creator="t_b")}
        self._turn_racing("what now?", _cards("t_a"), after, [], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_a_card_under_a_left_out_cards_follow_up_that_closed_within_the_turn_is_left_out(self):
        # Ask 1's t_c is running when ask 2 arrives. During ask 2's turn t_c's
        # worker files t_a, t_a's worker files t_b, and t_a completes, so the
        # end read shows t_b under t_a and no t_a; the lineage links t_a to t_c.
        for events in ([], [("t_a", "completed")]):
            with self.subTest(delivered=bool(events)):
                runtime._deferred.clear()
                log = []
                first = self._turn("fix it", {}, _cards("t_c"), adapter=_Stub("111.001", log))
                self.lineage = {("default", "t_c"): None, ("default", "t_a"): "t_c", ("default", "t_b"): "t_a"}
                second = _Stub("111.002", log)
                self._turn_racing("what now?", _cards("t_c"), {**_cards("t_c"), **_cards("t_b", creator="t_a")}, events, second)
                self.assertEqual(second.calls, [("eyes", False), ("eyes", True)])
                # t_b is ask 1's: its follow-up's follow-up, held once t_c completes.
                self.boards[:] = [_cards("t_b", creator="t_a")]
                _run(runtime.settle_delegated(first, self._sub("t_c"), "completed"))
                self.assertNotIn(("111.001", "-hammer_and_wrench"), log)
                _run(runtime.settle_delegated(first, self._sub("t_b"), "completed"))
                self.assertEqual(log[-1], ("111.001", "-hammer_and_wrench"))
                self.assertEqual([ts for ts, _ in log].count("111.002"), 2)

    def test_an_earlier_asks_follow_up_that_gave_up_within_the_turn_does_not_fail_it(self):
        # t_c's worker files t_a while "what now?" is answered, after the end read's
        # snapshot, and t_a's give-up is delivered while that read awaits: t_a
        # is in neither read, so only the lineage shows its creator.
        adapter = _Stub()
        self.lineage = {("default", "t_c"): None, ("default", "t_a"): "t_c"}
        self._turn_racing("what now?", _cards("t_c"), _cards("t_c"), [("t_a", "gave_up")], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])
        self.assertEqual(self.lineage_reads, 1)

    def test_the_turn_reads_the_lineage_only_for_a_creator_neither_read_shows(self):
        self._turn("what now?", _cards("t_old"), _cards("t_old"))
        self._turn("fix it", {}, {**_cards("t_a"), **_cards("t_b", creator="t_a")})
        self.assertEqual(self.lineage_reads, 0)
        self._turn("fix it", {}, _cards("t_b", creator="t_a"))
        self.assertEqual(self.lineage_reads, 1)

    def test_a_card_the_turns_own_cards_worker_creates_is_its(self):
        adapter = _Stub()
        after = {**_cards("t_a"), **_cards("t_b", creator="t_a")}
        self._turn_racing("fix it", {}, after, [], adapter)
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].cards, {("default", "t_a"), ("default", "t_b")})

    def test_a_card_the_turn_creates_after_an_older_card_is_its(self):
        # "Once t_a finishes, run the smoke test": the turn names t_a as the
        # parent, which gives the new card no creator.
        adapter = _Stub()
        self._turn_racing("fix it", _cards("t_a"), _cards("t_a", "t_b"), [], adapter)
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].cards, {("default", "t_b")})

    def test_a_blocked_card_that_completes_during_the_turn_is_not_its(self):
        adapter = _Stub()
        self._turn_racing("yes, go ahead", _cards("t_a", status="blocked"), {}, [("t_a", "completed")], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])
        self.assertFalse(runtime._deferred)

    def test_a_card_that_blocks_before_the_turn_ends_pauses_the_ask(self):
        adapter = _Stub()
        self._turn_racing("fix it", {}, _cards("t_a", status="blocked"), [("t_a", "blocked")], adapter)
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False), ("double_vertical_bar", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls[-1], ("hammer_and_wrench", True))

    def test_a_card_that_blocks_and_resumes_within_the_turn_does_not_pause_the_ask(self):
        adapter = _Stub()
        self._turn_racing("fix it", {}, _cards("t_a"), [("t_a", "blocked")], adapter)
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].cards, {("default", "t_a")})

    def test_a_card_running_at_the_start_that_finishes_is_not_the_turns(self):
        adapter = _Stub()
        self._turn_racing("what now?", _cards("t_a"), {}, [("t_a", "gave_up")], adapter)
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_a_finish_in_another_thread_is_not_the_turns(self):
        adapter = _Stub()
        self.boards[:] = [{}]
        _run(runtime.on_processing_start(adapter, _event("fix it")))
        other = {**self._sub("t_a"), "thread_id": "999.000"}
        _run(runtime.settle_delegated(adapter, other, "completed"))
        self.boards[:] = [_cards("t_a")]
        _run(runtime.on_processing_complete(adapter, _event("fix it"), SimpleNamespace(value="success")))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        self.assertEqual(runtime._deferred[(CHANNEL, THREAD)][0].cards, {("default", "t_a")})

    def test_answering_a_blocked_card_settles_the_answer_and_leaves_the_card_to_its_ask(self):
        # Ask 1's card blocks on the user; ask 2 answers and unblocks it.
        log = []
        first = self._turn("fix it", {}, _cards("t_a"), adapter=_Stub("111.001", log))
        _run(runtime.settle_delegated(first, self._sub("t_a"), "blocked"))
        self._turn("yes, go ahead", _cards("t_a", status="blocked"), _cards("t_a"), adapter=_Stub("111.002", log))
        self.assertEqual(log[-2:], [("111.002", "eyes"), ("111.002", "-eyes")])
        _run(runtime.settle_delegated(first, self._sub("t_a"), "completed"))
        self.assertEqual(log[-2:], [("111.001", "-double_vertical_bar"), ("111.001", "-hammer_and_wrench")])
        self.assertEqual([ts for ts, _ in log].count("111.002"), 2)

    def test_a_retry_after_give_up_settles_on_its_own_turn(self):
        log = []
        first = self._turn("fix it", {}, _cards("t_a"), adapter=_Stub("111.001", log))
        _run(runtime.settle_delegated(first, self._sub("t_a"), "gave_up"))
        self.assertEqual(log[-2:], [("111.001", "x"), ("111.001", "-hammer_and_wrench")])
        # Hermes parks the gave-up card at blocked; "try again" unblocks it.
        self._turn("try again", _cards("t_a", status="blocked"), _cards("t_a"), adapter=_Stub("111.002", log))
        self.assertEqual(log[-1], ("111.002", "-eyes"))
        _run(runtime.settle_delegated(first, self._sub("t_a"), "completed"))
        self.assertEqual(len(log), 5)

    def test_a_card_still_blocked_after_the_turn_is_not_its(self):
        adapter = self._turn("what now?", _cards("t_a", status="blocked"), _cards("t_a", status="blocked"))
        self.assertEqual(adapter.calls, [("eyes", False), ("eyes", True)])

    def test_a_card_on_another_board_settles_on_that_board_only(self):
        adapter = self._turn("fix it", {}, _cards("t_a", board="b2"))
        # The same id on the default board is a different card.
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed", board="b2"))
        self.assertEqual(adapter.calls[-1], ("hammer_and_wrench", True))

    def test_a_thread_keeps_only_its_newest_deferred_asks(self):
        for n in range(runtime.DEFERRED_PER_THREAD + 2):
            self._turn("fix it", {}, _cards(f"t_{n}"), adapter=_Stub(f"111.{n:03d}"))
        asks = runtime._deferred[(CHANNEL, THREAD)]
        self.assertEqual(len(asks), runtime.DEFERRED_PER_THREAD)
        self.assertEqual(asks[0].ts, "111.002")

    def test_settle_ignores_other_platforms_and_unknown_threads(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub(platform="google_chat"), "completed"))
        other = dict(self._sub(), thread_id="999.000")
        _run(runtime.settle_delegated(adapter, other, "completed"))
        self.assertEqual(adapter.calls, [("hammer_and_wrench", False)])

    def test_only_reactions_this_process_put_on_are_removed(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "blocked"))
        self.boards[:] = [{}]
        _run(runtime.settle_delegated(adapter, self._sub("t_a"), "completed"))
        added = [emoji for emoji, remove in adapter.calls if not remove]
        removed = [emoji for emoji, remove in adapter.calls if remove]
        self.assertEqual(sorted(added), sorted(removed))

    def test_flag_off_settle_is_inert(self):
        adapter = self._turn("fix it", {}, _cards("t_a"))
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
            "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT);"
            "INSERT INTO task_events VALUES (1,'a','created','{\"creator_task_id\": null}'),"
            " (2,'b','created','{\"creator_task_id\": \"c\"}'),(3,'b','blocked','{\"creator_task_id\": \"x\"}'),"
            " (4,'f','blocked','{}'),(5,'f','gave_up','{}'),(6,'g','gave_up','{}'),(7,'g','blocked','{}'),"
            " (8,'h','gave_up','{}'),(9,'h','unblocked','{}'),(10,'i','gave_up','{}');"
            "INSERT INTO tasks VALUES ('a','running'),('b','blocked'),('c','done'),('d','archived'),('e','ready'),"
            " ('f','blocked'),('g','blocked'),('h','ready'),('i','running');"
            "INSERT INTO kanban_notify_subs VALUES"
            " ('a','slack','C1','111.000'),('b','slack','C1','111.000'),('c','slack','C1','111.000'),"
            " ('d','slack','C1','111.000'),('e','slack','C1','222.000'),('a','telegram','C1','111.000'),"
            " ('f','slack','C1','111.000'),('g','slack','C1','111.000'),('h','slack','C1','111.000'),"
            " ('i','slack','C1','111.000');"
        )
        rows = conn.execute(runtime.OPEN_CARDS_SQL, ("slack", "C1", "111.000")).fetchall()
        # f's latest stop is a give-up; g gave up, was retried, and blocked on the user;
        # h gave up and was unblocked; i gave up and was moved back to running without one.
        self.assertEqual(
            sorted(rows),
            [
                ("a", "running", None, 0), ("b", "blocked", "c", 0), ("f", "blocked", None, 1),
                ("g", "blocked", None, 0), ("h", "ready", None, 0), ("i", "running", None, 0),
            ],
        )

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
        rows = {
            default: [("t_a", "running", None, None)],
            b2: [("t_b", "running", None, None), ("t_c", "blocked", "t_b", 1)],
        }
        reads = []
        with mock.patch.dict(sys.modules, self._fake_hermes(paths, rows, reads)):
            found = runtime._query_open_cards("C1", "111.000")
        self.assertEqual(found, {**_cards("t_a"), **_cards("t_b", board="b2"), **_cards("t_c", board="b2", status="blocked", creator="t_b", gave_up=True)})
        self.assertEqual([path for path, _ in reads], [default, b2])
        self.assertEqual(reads[0][1], ("slack", "C1", "111.000"))

    def test_a_board_with_no_database_is_skipped(self):
        good, absent = self._databases("good.db", "absent.db")
        Path(good).touch()
        reads = []
        paths = {"default": good, "b3": absent}
        with mock.patch.dict(sys.modules, self._fake_hermes(paths, {good: [("t_a", "running", None, None)]}, reads)):
            found = runtime._query_open_cards("C1", "111.000")
        self.assertEqual(found, _cards("t_a"))
        # b3 has no database: never connected to, so never created.
        self.assertEqual([path for path, _ in reads], [good])

    def test_an_unreadable_board_fails_the_whole_read(self):
        # A partial read, compared with a whole one, would misattribute the
        # missing board's cards; the turn settles now instead.
        good, bad = self._databases("good.db", "bad.db")
        Path(good).touch()
        Path(bad).touch()
        paths, rows = {"default": good, "b2": bad}, {good: [("t_a", "running", None, None)], bad: []}
        # The same boards read whole: the None below is the broken board's.
        with mock.patch.dict(sys.modules, self._fake_hermes(paths, rows, [])):
            self.assertEqual(_run(runtime.open_cards("C1", "111.000")), _cards("t_a"))
        hermes = self._fake_hermes(paths, rows, [], broken={"b2"})
        with mock.patch.dict(sys.modules, hermes):
            self.assertIsNone(_run(runtime.open_cards("C1", "111.000")))
        hermes["hermes_cli.kanban_db"].list_boards = mock.Mock(side_effect=RuntimeError("locked"))
        with mock.patch.dict(sys.modules, hermes):
            self.assertIsNone(_run(runtime.open_cards("C1", "111.000")))

    def test_read_failure_is_none(self):
        with mock.patch.object(runtime, "_query_open_cards", side_effect=RuntimeError("locked")):
            self.assertIsNone(_run(runtime.open_cards("C1", "111.000")))

    def test_lineage_reads_closed_cards_and_a_failure_is_empty(self):
        good, = self._databases("good.db")
        Path(good).touch()
        reads = []
        rows = {good: [("t_a", None), ("t_b", "t_a")]}
        with mock.patch.dict(sys.modules, self._fake_hermes({"default": good}, rows, reads)):
            found = _run(runtime.thread_lineage("C1", "111.000"))
        self.assertEqual(found, {("default", "t_a"): None, ("default", "t_b"): "t_a"})
        self.assertEqual(reads, [(good, ("slack", "C1", "111.000"))])
        with mock.patch.object(runtime, "_query_thread_lineage", side_effect=RuntimeError("locked")):
            self.assertEqual(_run(runtime.thread_lineage("C1", "111.000")), {})


class MissingPresenterTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)

    def test_treated_as_off(self):
        with mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
            os.environ, {"KAGE_SLACK_UX": "1"}
        ):
            self.assertFalse(runtime.enabled())

    def test_warns_only_when_the_flag_is_on(self):
        for value, warns in ((None, True), ("0", False), ("false", False), ("on", True)):
            importlib.reload(runtime)
            env = {} if value is None else {"KAGE_SLACK_UX": value}
            with self.subTest(flag=value), mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
                os.environ, env
            ), mock.patch.object(runtime.logger, "warning") as warning:
                if value is None:
                    os.environ.pop("KAGE_SLACK_UX", None)
                runtime.enabled()
                self.assertEqual(warning.called, warns)


if __name__ == "__main__":
    unittest.main()
