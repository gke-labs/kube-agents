"""Host tests for the KAGE_SLACK_UX status patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_status.py

The fixture carries upstream's ``_set_thread_status`` and the DM-title lines of
``_build_message_event`` verbatim (v2026.9.14). The tests apply the patch, exec
the patched and the unpatched fixture, and drive both with a stub client: with
the flag off the patched adapter must make exactly upstream's calls, which is
the flag-off identity for this surface; with it on, the runtime module takes
over. The plan is driven through the runtime module with a stub adapter.
"""

import asyncio
import importlib
import os
import shutil
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

import apply_slack_ux_status as applier
import kanban_progress_lines
import slack_status
import slack_ux_status as runtime
import verify_slack_ux_status as verifier

UPSTREAM = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
import enum
import logging

logger = logging.getLogger(__name__)


class MessageType(enum.Enum):
    TEXT = "text"
    COMMAND = "command"


def _sdk_supports_agent_sessions():
    return True


def _session_status_method(client):
    return client.agents_sessions_setStatus


def _session_title_method(client):
    return client.agents_sessions_rename


class SlackAdapter:
    def __init__(self, client):
        self.client = client
        self.titled = []

    def _get_client(self, chat_id, team_id=None):
        return self.client

    @staticmethod
    def _default_status_text(started):
        return "is thinking..."

    async def _set_assistant_thread_title(self, channel_id, thread_ts, text, team_id=None):
        self.titled.append((channel_id, thread_ts, text))

    async def _set_thread_status(
        self, chat_id: str, team_id: str, thread_ts: str, status: str, fail_label: str) -> None:
        """``assistant.threads.setStatus`` (empty ``status`` clears); failures are debug-logged."""
        try:
            _set_status = _session_status_method(self._get_client(chat_id, team_id=team_id))
            await _set_status(channel_id=chat_id, thread_ts=thread_ts, status=status)
        except Exception as e:
            logger.debug("[Slack] assistant.threads.setStatus %s: %s", fail_label, e)

    async def _build_message_event(
        self, *, text, original_text, channel_id, team_id, ts, thread_ts, is_dm, msg_type):
        """Resolve names, title the DM thread, and build the ``MessageEvent``."""
        if msg_type == MessageType.COMMAND:
            text = original_text
        # Best-effort: title the DM thread from the prompt for Slack's AI Agent Messages tab.
        if is_dm and thread_ts and msg_type != MessageType.COMMAND:
            await self._set_assistant_thread_title(
                channel_id, thread_ts, original_text or text, team_id=team_id)
        return text
'''

CHANNEL = "C1"
THREAD = "111.000"
TEAM = "T1"
PLAN_TS = "222.000"
PHRASE = "is thinking..."
FLAG_OFF_VALUES = (None, "", "0", "false")


def _run(coro):
    return asyncio.run(coro)


class _Client:
    """A Slack client that records the calls this patch makes."""

    def __init__(self, fail=(), error="refused"):
        self.calls = []
        self.fail = set(fail)
        self.error = error

    async def _record(self, name, value):
        self.calls.append((name, value))
        if name in self.fail:
            raise RuntimeError(f"{name} {self.error}")
        return {"ok": True, "ts": PLAN_TS}

    async def agents_sessions_setStatus(self, **kw):
        return await self._record("setStatus", kw["status"])

    async def agents_sessions_rename(self, **kw):
        return await self._record("rename", kw["title"])

    async def chat_postMessage(self, **kw):
        return await self._record("post", kw["blocks"])

    async def chat_update(self, **kw):
        return await self._record("update", kw["blocks"])


class _SlowClient(_Client):
    """A client whose next ``chat_update`` waits ``slow`` seconds first."""

    slow = 0.0

    async def chat_update(self, **kw):
        delay, self.slow = self.slow, 0.0
        await asyncio.sleep(delay)
        return await super().chat_update(**kw)


class _Root:
    """A throwaway Hermes root holding the fixture adapter and the runtime module."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        adapter = self.dir / applier.RELATIVE
        adapter.parent.mkdir(parents=True)
        adapter.write_text(UPSTREAM)
        gateway = self.dir / "gateway"
        gateway.mkdir()
        shutil.copy(HERE / "slack_ux_status.py", gateway / "slack_ux_status.py")
        (gateway / "__init__.py").write_text("")

    def load(self, name):
        """Exec the (possibly patched) fixture adapter with ``gateway`` importable."""
        sys.path.insert(0, str(self.dir))
        try:
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_status", None)
            namespace = {"__name__": name}
            exec(compile((self.dir / applier.RELATIVE).read_text(), name, "exec"), namespace)  # noqa: S102
            return namespace
        finally:
            sys.path.remove(str(self.dir))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        sys.modules.pop("gateway", None)
        sys.modules.pop("gateway.slack_ux_status", None)


def _flag(value):
    env = mock.patch.dict(os.environ, {} if value is None else {"KAGE_SLACK_UX": value})
    env.start()
    if value is None:
        os.environ.pop("KAGE_SLACK_UX", None)
    return env


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        text = (self.root.dir / applier.RELATIVE).read_text()
        self.assertEqual(text.count(applier.BUILD_MARKER), 1)
        self.assertIn("_kage_slack_status.note_ask(", text)
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drifted_docstring_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        drifted = UPSTREAM.replace("failures are debug-logged", "failures are logged")
        path.write_text(drifted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("_set_thread_status docstring", str(caught.exception))
        self.assertEqual(path.read_text(), drifted)

    def test_drifted_title_block_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        path.write_text(UPSTREAM.replace("original_text or text, team_id", "text, team_id"))
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("DM thread title", str(caught.exception))

    def test_verifier_passes_on_patched_tree(self):
        applier.apply(self.root.dir)
        env = _flag(None)
        self.addCleanup(env.stop)
        verifier.main(self.root.dir)

    def test_verifier_refuses_a_guard_name_upstream_renamed(self):
        renames = (
            ("def _session_title_method(", "def _session_rename_method("),
            ("def _sdk_supports_agent_sessions(", "def _sdk_has_agent_sessions("),
            ("team_id, ts, thread_ts", "team_id, message_ts, thread_ts"),
            # Renamed, but still assigned under a branch that may not run.
            ("self, *, text, original_text,", "self, *, body, original_text,"),
        )
        env = _flag(None)
        self.addCleanup(env.stop)
        path = self.root.dir / applier.RELATIVE
        for old, new in renames:
            with self.subTest(old):
                path.write_text(UPSTREAM)
                applier.apply(self.root.dir)
                path.write_text(path.read_text().replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn("no longer binds", str(caught.exception))

    def test_verifier_refuses_an_adapter_member_in_another_shape(self):
        reshapes = (
            ("def _get_client(self, chat_id, team_id=None):", "def _get_client(self, chat_id, team=None):"),
            ("def _get_client(self, chat_id, team_id=None):", "async def _get_client(self, chat_id, team_id=None):"),
            ("def _default_status_text(started):", "def _default_status_text():"),
            ("def _default_status_text(started):", "def _default_status_text(*, started):"),
            ("    @staticmethod\n    def _default_status_text", "    def _default_status_text"),
        )
        env = _flag(None)
        self.addCleanup(env.stop)
        path = self.root.dir / applier.RELATIVE
        for old, new in reshapes:
            with self.subTest(new):
                path.write_text(UPSTREAM)
                applier.apply(self.root.dir)
                patched = path.read_text()
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn(new.split("(")[0].split()[-1], str(caught.exception))

    def test_verifier_reads_the_adapter_class_only(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        path.write_text(path.read_text() + (
            "\n\nclass OtherAdapter:\n"
            "    async def _set_thread_status(self, thread_ts, chat_id):\n"
            "        return None\n"
        ))
        env = _flag(None)
        self.addCleanup(env.stop)
        verifier.main(self.root.dir)

    def test_verifier_refuses_unpatched_tree(self):
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)


class FlagOffIdentityTest(unittest.TestCase):
    """With KAGE_SLACK_UX off the patched adapter makes exactly upstream's calls."""

    STATUSES = (PHRASE, PHRASE, "still working… (31s)", "")

    def _calls(self, namespace):
        client = _Client()
        adapter = namespace["SlackAdapter"](client)
        for status in self.STATUSES:
            _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, status, "failed"))
        text_type = namespace["MessageType"].TEXT
        for is_dm in (True, False):
            _run(adapter._build_message_event(
                text="why is payments slow?", original_text="", channel_id=CHANNEL,
                team_id=TEAM, ts=THREAD, thread_ts=THREAD, is_dm=is_dm, msg_type=text_type,
            ))
        return client.calls, adapter.titled

    def test_identical_to_upstream(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        upstream = root.load("upstream_fixture")
        applier.apply(root.dir)
        patched = root.load("patched_fixture")
        module = sys.modules["gateway.slack_ux_status"]
        for value in FLAG_OFF_VALUES:
            with self.subTest(flag=value):
                env = _flag(value)
                try:
                    self.assertEqual(self._calls(patched), self._calls(upstream))
                    self.assertFalse(module._asks)
                finally:
                    env.stop()

    def test_upstream_sends_the_phrase_slack_refuses(self):
        # The #576 bug the flag fixes: free text to an enum-only method.
        root = _Root()
        self.addCleanup(root.cleanup)
        calls, _titled = self._calls(root.load("upstream_fixture"))
        self.assertFalse(all(value in slack_status.SESSION_STATUSES for _name, value in calls))

    def test_flag_on_hands_over(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        applier.apply(root.dir)
        patched = root.load("patched_fixture_on")
        env = _flag("1")
        self.addCleanup(env.stop)
        calls, titled = self._calls(patched)
        # The ask arrives after this turn's statuses, so no rename yet.
        self.assertEqual(calls, [("setStatus", "processing"), ("setStatus", "closed")])
        self.assertEqual(titled, [(CHANNEL, THREAD, "why is payments slow?")])
        # The channel ask was kept for the title the next processing sets.
        module = sys.modules["gateway.slack_ux_status"]
        self.assertEqual(module._asks[(CHANNEL, THREAD)], "why is payments slow?")


class _Adapter:
    """The patched adapter as the runtime sees it: a client and the status setter."""

    def __init__(self, client=None):
        self.client = client or _Client()

    def _get_client(self, chat_id, team_id=None):
        return self.client

    async def _set_thread_status(self, chat_id, team_id, thread_ts, status, fail_label):
        await runtime.set_thread_status(
            self, chat_id, team_id, thread_ts, status, fail_label,
            lambda c: c.agents_sessions_setStatus, lambda c: c.agents_sessions_rename,
        )

    @staticmethod
    def _default_status_text(started):
        return PHRASE

    @property
    def calls(self):
        return self.client.calls


class _RuntimeCase(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)
        env = _flag("1")
        self.addCleanup(env.stop)


class SessionTest(_RuntimeCase):
    def _status(self, adapter, status, thread=THREAD):
        _run(adapter._set_thread_status(CHANNEL, TEAM, thread, status, "failed"))

    def test_the_phrase_is_processing_and_the_clear_is_closed(self):
        self.assertEqual(slack_status.session_status(PHRASE), "processing")
        self.assertEqual(slack_status.session_status("still working… (2m03s)"), "processing")
        self.assertEqual(slack_status.session_status(""), "closed")
        self.assertEqual(slack_status.session_status(None), "closed")
        self.assertEqual(slack_status.session_status("suspended"), "suspended")

    def test_only_enum_values_reach_slack(self):
        adapter = _Adapter()
        for status in (PHRASE, "still working… (31s)", "", PHRASE, ""):
            self._status(adapter, status)
        self.assertTrue(adapter.calls)
        self.assertTrue(all(value in slack_status.SESSION_STATUSES for _n, value in adapter.calls))

    def test_an_unchanged_status_is_sent_once(self):
        adapter = _Adapter()
        for _ in range(30):
            self._status(adapter, PHRASE)
        self._status(adapter, "")
        self._status(adapter, "")
        self.assertEqual(adapter.calls, [("setStatus", "processing"), ("setStatus", "closed")])

    def test_processing_is_refreshed_after_a_minute(self):
        adapter = _Adapter()
        with mock.patch.object(runtime.time, "monotonic", return_value=1000.0):
            self._status(adapter, PHRASE)
        with mock.patch.object(runtime.time, "monotonic", return_value=1059.0):
            self._status(adapter, PHRASE)
        with mock.patch.object(runtime.time, "monotonic", return_value=1061.0):
            self._status(adapter, PHRASE)
        self.assertEqual(adapter.calls, [("setStatus", "processing")] * 2)

    def test_a_refused_status_is_retried_next_time(self):
        adapter = _Adapter(_Client(fail={"setStatus"}))
        self._status(adapter, PHRASE)
        adapter.client.fail.clear()
        self._status(adapter, PHRASE)
        self.assertEqual(adapter.calls, [("setStatus", "processing")] * 2)

    def test_a_client_getter_in_another_shape_is_logged_not_raised(self):
        adapter = _Adapter()
        adapter._get_client = lambda chat_id: adapter.client
        self._status(adapter, PHRASE)
        self.assertEqual(adapter.calls, [])

    def test_threads_are_tracked_apart(self):
        adapter = _Adapter()
        self._status(adapter, PHRASE, "1.0")
        self._status(adapter, PHRASE, "2.0")
        self.assertEqual(len(adapter.calls), 2)

    def test_the_ask_titles_the_session_after_it_opens(self):
        adapter = _Adapter()
        runtime.note_ask(CHANNEL, THREAD, "<@U1> why is <#C9|payments> slow: see <https://x.io|dash>")
        self._status(adapter, PHRASE)
        self._status(adapter, "")
        self._status(adapter, PHRASE)
        self.assertEqual(
            adapter.calls,
            [
                ("setStatus", "processing"),
                ("rename", "why is payments slow, see dash"),
                ("setStatus", "closed"),
                ("setStatus", "processing"),
            ],
        )
        self.assertEqual(runtime._titles[(CHANNEL, THREAD)], "why is payments slow, see dash")

    def test_a_follow_up_ask_keeps_the_title(self):
        adapter = _Adapter()
        runtime.note_ask(CHANNEL, THREAD, "why is payments slow?")
        self._status(adapter, PHRASE)
        self._status(adapter, "")
        runtime.note_ask(CHANNEL, THREAD, "and checkout?")
        self._status(adapter, PHRASE)
        self.assertEqual([v for n, v in adapter.calls if n == "rename"], ["why is payments slow?"])

    def test_a_second_ask_before_the_session_opens_keeps_the_first(self):
        adapter = _Adapter()
        runtime.note_ask(CHANNEL, THREAD, "why is payments slow?")
        runtime.note_ask(CHANNEL, THREAD, "never mind")
        self._status(adapter, PHRASE)
        self.assertEqual(runtime._titles[(CHANNEL, THREAD)], "why is payments slow?")

    def test_no_ask_no_rename(self):
        adapter = _Adapter()
        self._status(adapter, PHRASE)
        self.assertEqual(adapter.calls, [("setStatus", "processing")])

    def test_a_refused_rename_leaves_no_title(self):
        adapter = _Adapter(_Client(fail={"rename"}))
        runtime.note_ask(CHANNEL, THREAD, "scale it")
        self._status(adapter, PHRASE)
        self.assertNotIn((CHANNEL, THREAD), runtime._titles)

    def test_a_refused_rename_is_a_warning_once(self):
        adapter = _Adapter(_Client(fail={"rename"}, error="invalid_name"))
        runtime.note_ask(CHANNEL, THREAD, "scale it")
        with self.assertLogs(runtime.logger, "DEBUG") as logs:
            self._status(adapter, PHRASE)
            self._status(adapter, "")
            self._status(adapter, PHRASE)
        warnings = [line for line in logs.output if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, logs.output)
        self.assertIn("agents.sessions.rename refused", warnings[0])

    def test_a_rename_that_fails_otherwise_is_debug(self):
        adapter = _Adapter(_Client(fail={"rename"}, error="not_found"))
        runtime.note_ask(CHANNEL, THREAD, "scale it")
        with self.assertLogs(runtime.logger, "DEBUG") as logs:
            self._status(adapter, PHRASE)
        self.assertFalse([line for line in logs.output if line.startswith("WARNING")], logs.output)

    def test_a_refused_rename_keeps_the_first_ask_for_the_next_session(self):
        adapter = _Adapter(_Client(fail={"rename"}))
        runtime.note_ask(CHANNEL, THREAD, "why is payments slow?")
        self._status(adapter, PHRASE)
        self._status(adapter, "")
        runtime.note_ask(CHANNEL, THREAD, "and checkout?")
        adapter.client.fail.clear()
        self._status(adapter, PHRASE)
        self.assertEqual(
            [v for n, v in adapter.calls if n == "rename"], ["why is payments slow?", "why is payments slow?"],
        )
        self.assertEqual(runtime._titles[(CHANNEL, THREAD)], "why is payments slow?")
        self.assertNotIn((CHANNEL, THREAD), runtime._asks)

    def test_flag_off_keeps_no_ask(self):
        env = _flag("")
        self.addCleanup(env.stop)
        runtime.note_ask(CHANNEL, THREAD, "scale it")
        self.assertFalse(runtime._asks)


class TitleTest(unittest.TestCase):
    def test_refused_characters_are_replaced(self):
        title = slack_status.session_title("a: b / c · d")
        for refused in (":", "/", "·"):
            self.assertNotIn(refused, title)

    def test_clipped_to_the_limit_on_a_word(self):
        title = slack_status.session_title("word " * 40)
        self.assertLessEqual(len(title), slack_status.TITLE_MAX)
        self.assertTrue(title.endswith("word" + slack_status.TITLE_ELLIPSIS))

    def test_blank_is_empty(self):
        self.assertEqual(slack_status.session_title("<@U1>"), "")


def _sub(task="t_a", thread=THREAD):
    return {"platform": "slack", "chat_id": CHANNEL, "thread_id": thread, "task_id": task}


class PlanTest(_RuntimeCase):
    def _note(self, adapter, event_id, line, task="t_a", title="check payments"):
        return _run(runtime.deliver_row(adapter, _sub(task), event_id, title, line))

    def _kinds(self, adapter):
        return [name for name, _value in adapter.calls]

    def _sent(self, adapter):
        return [value for name, value in adapter.calls if name == "setStatus"]

    def test_the_first_note_posts_the_plan_and_opens_the_session(self):
        adapter = _Adapter()
        self.assertTrue(self._note(adapter, 1, "reading logs"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])
        blocks = adapter.calls[0][1]
        self.assertEqual(blocks[0]["type"], "plan")
        self.assertEqual(blocks[0]["title"], "check payments")
        self.assertEqual(blocks[0]["tasks"][0]["status"], "in_progress")
        self.assertEqual(len(blocks), 1, "the plan carries no Stop")
        self.assertEqual(adapter.calls[1], ("setStatus", "processing"))

    def test_later_notes_edit_the_plan(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        self._note(adapter, 2, "reading metrics")
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "update"])
        steps = adapter.calls[2][1][0]["tasks"][0]["details"]["elements"][0]["elements"]
        texts = [step["elements"][0]["text"] for step in steps]
        self.assertEqual(texts, ["✓ reading logs", "◌ reading metrics"])

    def test_one_row_per_card(self):
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a", title="check seeded-a")
        self._note(adapter, 2, "b", task="t_b", title="check seeded-b")
        self._note(adapter, 3, "a2", task="t_a", title="check seeded-a")
        plan = adapter.calls[-1][1][0]
        self.assertEqual([t["task_id"] for t in plan["tasks"]], ["t_a", "t_b"])
        self.assertEqual(plan["title"], "2 cards")
        self.assertEqual(self._kinds(adapter).count("post"), 1)

    def test_the_session_title_names_the_plan(self):
        adapter = _Adapter()
        runtime.note_ask(CHANNEL, THREAD, "is the fleet healthy?")
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "failed"))
        self._note(adapter, 1, "a", task="t_a")
        self._note(adapter, 2, "b", task="t_b")
        self.assertEqual(adapter.calls[-1][1][0]["title"], "is the fleet healthy?")

    def test_a_replay_is_delivered_but_not_appended(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        self.assertTrue(self._note(adapter, 1, "reading logs"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])

    def test_row_lines_are_bounded(self):
        adapter = _Adapter()
        for event_id in range(1, 20):
            self._note(adapter, event_id, f"note {event_id}")
        row = runtime._plans[(CHANNEL, THREAD)].rows["t_a"]
        self.assertEqual(len(row.lines), slack_status.STEPS_MAX)

    def test_a_refused_post_falls_back_to_the_progress_line(self):
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        adapter.client.fail.clear()
        self.assertFalse(self._note(adapter, 2, "reading metrics"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])
        self.assertEqual(self._sent(adapter), ["processing"])

    def test_a_phrase_getter_in_another_shape_keeps_the_note_on_the_plan(self):
        adapter = _Adapter()
        adapter._default_status_text = lambda: PHRASE
        self.assertTrue(self._note(adapter, 1, "reading logs"))
        self.assertEqual(self._kinds(adapter), ["post"])

    def test_a_refused_edit_falls_back_and_restores_the_row(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        adapter.client.fail.add("update")
        self.assertFalse(self._note(adapter, 2, "reading metrics"))
        row = runtime._plans[(CHANNEL, THREAD)].rows["t_a"]
        self.assertEqual((row.lines, row.steps, row.note, row.last_event_id), (["reading logs"], 1, "reading logs", 1))

    def test_a_fallen_back_plan_is_retried_once_its_cards_settle(self):
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        adapter.client.fail.clear()
        self.assertTrue(self._note(adapter, 2, "next card", task="t_b"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "setStatus", "post", "setStatus"])
        self.assertEqual(self._sent(adapter), ["processing", "closed", "processing"])

    def test_a_wait_after_a_refused_plan_post_suspends_and_completing_clears(self):
        # A plan refused on its first post must still clear or suspend the
        # session when its cards settle.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"])
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "closed"])

    def test_two_cards_rolling_after_fallback_one_blocking_suspends_when_running_finishes(self):
        # When A and B roll after a refused post, B blocking keeps processing
        # while A still runs; A completing suspends while B waits, and B
        # completing closes the session.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "a", task="t_a"))
        adapter.client.fail.clear()
        self.assertFalse(self._note(adapter, 2, "b", task="t_b"))
        _run(runtime.settle_row(adapter, _sub("t_b"), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing"], "t_a is still rolling")
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"], "t_b is waiting on user")
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "closed"])

    def test_a_wait_after_a_refused_plan_post_lapses_and_completing_clears(self):
        # A card blocking on an unposted plan suspends, sits past PLAN_HOLD_SECONDS
        # into _lapsed, and completing without unblock still clears the session.
        async def scenario(adapter):
            self.assertFalse(await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs"))
            await runtime.settle_row(adapter, _sub(), "blocked")
            self.assertEqual(self._sent(adapter), ["processing", "suspended"])
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            await runtime.settle_row(adapter, _sub(), "completed")
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "closed"])

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_a_wait_after_a_refused_plan_post_lapses_and_archiving_clears(self):
        # A card blocking on an unposted plan suspends, sits past PLAN_HOLD_SECONDS
        # into _lapsed, and archiving by hand still clears the session.
        async def scenario(adapter):
            self.assertFalse(await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs"))
            await runtime.settle_row(adapter, _sub(), "blocked")
            self.assertEqual(self._sent(adapter), ["processing", "suspended"])
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            await runtime.settle_row(adapter, _sub(), "archived")
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "closed"])

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_an_unblocked_card_on_unposted_plan_sends_processing_and_clears_on_completion(self):
        # When a card blocks on an unposted plan, it suspends. When it unblocks,
        # it sends processing. When it completes, the session clears to closed.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"])
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing"])
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "closed"])

    def test_a_blocked_card_on_unposted_plan_resuming_by_note_sends_processing_and_completing_clears(self):
        # A card blocking on an unposted plan suspends. Resuming via progress note
        # moves the session back to processing, and completing clears it to closed.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"])
        self.assertFalse(self._note(adapter, 2, "resumed working"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing"])
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "closed"])

    def test_two_cards_on_unposted_plan_one_blocked_sibling_starting_by_note_sends_processing(self):
        # Card A blocks on an unposted plan, sending suspended. Sibling card B
        # starting with a progress note transitions session to processing while A waits.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "a", task="t_a"))
        _run(runtime.settle_row(adapter, _sub("t_a"), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"])
        adapter.client.fail.clear()
        self.assertFalse(self._note(adapter, 2, "b", task="t_b"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing"], "t_b is rolling while t_a waits")
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "suspended"], "t_a is still waiting")
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "suspended", "closed"])

    def test_a_blocked_card_resuming_by_first_refused_note_sends_processing_and_completing_clears(self):
        # A card blocking after restart sets orphan suspended status. Its first note
        # creates a plan whose post is refused; the refused-first-post arm in deliver_row
        # transitions session status to processing, and completing clears it to closed.
        adapter = _Adapter(_Client(fail={"post"}))
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(self._sent(adapter), ["suspended"])
        self.assertFalse(self._note(adapter, 1, "resumed"))
        self.assertEqual(self._sent(adapter), ["suspended", "processing"])
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["suspended", "processing", "closed"])

    def test_a_closed_thread_starting_a_rolling_card_after_refused_post_sends_processing(self):
        # When a turn ends and clears the session to closed, a subsequent card whose
        # plan post is refused transitions from closed to processing upon delivering its note.
        adapter = _Adapter(_Client(fail={"post"}))
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self.assertEqual(self._sent(adapter), ["closed"])
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        self.assertEqual(self._sent(adapter), ["closed", "processing"])
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["closed", "processing", "closed"])

    def test_an_unblocked_card_on_unposted_plan_sends_default_text_to_legacy_setter(self):
        # On a client without Agent Sessions, an unblocked card on an unposted plan
        # passes adapter._default_status_text(None) to _set_thread_status, not literal "processing".
        raw_statuses = []
        adapter = _Adapter(_Client(fail={"post"}))
        async def custom_setter(chat_id, team_id, thread_ts, status, fail_label):
            raw_statuses.append(status)
            await runtime.set_thread_status(
                adapter, chat_id, team_id, thread_ts, status, fail_label,
                lambda c: c.agents_sessions_setStatus, lambda c: c.agents_sessions_rename,
            )
        adapter._set_thread_status = custom_setter
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(raw_statuses, [PHRASE, "", PHRASE, ""])

    def test_an_unblocked_card_on_unposted_plan_lapses_and_completing_clears(self):
        # When an unposted plan's unblocked card runs quietly past PLAN_HOLD_SECONDS
        # and lapses, its session clears, and late completion leaves it closed.
        async def scenario(adapter):
            self.assertFalse(await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs"))
            await runtime.settle_row(adapter, _sub(), "blocked")
            self.assertEqual(self._sent(adapter), ["processing", "suspended"])
            await runtime.settle_row(adapter, _sub(), "unblocked")
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing"])
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "closed"])
            await runtime.settle_row(adapter, _sub(), "completed")
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "closed"])

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_an_unposted_suspended_plan_closes_on_keep_eviction(self):
        # An unposted plan holding suspended must close its session if evicted at PLANS_MAX.
        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLANS_MAX", 1):
            self.assertFalse(self._note(adapter, 1, "a", task="t_a"))
            _run(runtime.settle_row(adapter, _sub("t_a"), "blocked"))
            self.assertEqual(self._sent(adapter), ["processing", "suspended"])
            _run(runtime.deliver_row(adapter, _sub("t_b", thread="2.0"), 2, "two", "b"))
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "closed", "processing"])

    def test_an_unposted_suspended_plan_closes_on_lapsed_eviction(self):
        # An unposted plan holding suspended that lapsed into _lapsed must close
        # its session if evicted from _lapsed at PLANS_MAX.
        async def scenario(adapter):
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_a", thread="1.0"), 1, "a", "logs"))
            await runtime.settle_row(adapter, _sub("t_a", thread="1.0"), "blocked")
            self.assertEqual(self._sent(adapter), ["processing", "suspended"])
            await asyncio.sleep(0.1)
            self.assertIn((CHANNEL, "1.0"), runtime._lapsed)
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b", thread="2.0"), 2, "b", "logs"))
            await asyncio.sleep(0.1)
            self.assertNotIn((CHANNEL, "1.0"), runtime._lapsed)
            self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing", "closed", "closed"])

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLANS_MAX", 1), mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_posted_plan_lapsed_and_subsequent_unposted_plan_blocking_suspends_and_completing_clears(self):
        # A posted plan lapses, and a subsequent unposted plan on the same thread
        # blocks: the session transitions from closed to processing, suspended, and completing clears.
        async def scenario(adapter):
            self.assertTrue(await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "reading logs"))
            self.assertEqual(self._sent(adapter), ["processing"])
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            self.assertEqual(self._sent(adapter), ["processing", "closed"])

            adapter.client.fail.add("post")
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b"), 2, "check payments", "checking pods"))
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            self.assertEqual(self._sent(adapter), ["processing", "closed", "processing", "suspended"])
            await runtime.settle_row(adapter, _sub("t_b"), "completed")
            self.assertEqual(self._sent(adapter), ["processing", "closed", "processing", "suspended", "closed"])

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_no_thread_or_no_client_is_not_taken(self):
        self.assertFalse(_run(runtime.deliver_row(_Adapter(), _sub(thread=""), 1, "t", "x")))
        self.assertFalse(_run(runtime.deliver_row(SimpleNamespace(), _sub(), 1, "t", "x")))

    def test_settling_the_last_row_closes_and_forgets(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "update", "setStatus"])
        blocks = adapter.calls[2][1]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["tasks"][0]["status"], "complete")
        self.assertEqual(adapter.calls[3], ("setStatus", "closed"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)

    def test_a_row_reads_as_its_current_step_then_its_result(self):
        adapter = _Adapter()
        last = slack_status.STEPS_MAX + 2
        for event_id in range(1, last + 1):
            self._note(adapter, event_id, f"note {event_id}")
        running = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"][0]
        self.assertEqual(running["title"], f"note {last} · step {last} ▸")
        _run(runtime.settle_row(adapter, _sub(), "completed", "both pods are up"))
        done = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"][0]
        self.assertEqual((done["status"], done["title"]), ("complete", "both pods are up"))

    def test_a_waiting_row_keeps_the_plan(self):
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a")
        self._note(adapter, 2, "b", task="t_b")
        _run(runtime.settle_row(adapter, _sub("t_a"), "gave_up"))
        _run(runtime.settle_row(adapter, _sub("t_b"), "blocked"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["error", "pending"])
        self.assertIn((CHANNEL, THREAD), runtime._plans)

    def _tasks(self, adapter):
        return [v for n, v in adapter.calls if n in ("post", "update")][-1][0]["tasks"]

    def test_a_card_that_completes_without_a_note_gets_its_row(self):
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "completed", "1.33.4 = default", "seeded-a"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])
        [task] = self._tasks(adapter)
        self.assertEqual((task["status"], task["title"]), ("complete", "1.33.4 = default"))
        self.assertEqual(self._sent(adapter), ["closed"])
        self.assertNotIn((CHANNEL, THREAD), runtime._plans, "settled, so forgotten")

    def test_a_card_with_no_note_joins_the_plan_beside_a_running_one(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading version", title="seeded-c")
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed", "1.33.4 = default", "seeded-a"))
        self.assertEqual(self._kinds(adapter).count("post"), 1, "edited into the plan, not a second one")
        self.assertEqual(
            [(t["status"], t["title"]) for t in self._tasks(adapter)],
            [("in_progress", "seeded-c · reading version"), ("complete", "seeded-a · 1.33.4 = default")],
        )
        self.assertIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(self._sent(adapter), ["processing"])

    def test_a_card_that_blocks_without_a_note_waits_on_you(self):
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "blocked", "", "seeded-a"))
        [task] = self._tasks(adapter)
        self.assertEqual((task["status"], task["title"]), ("pending", slack_status.WAITING_ON_YOU))
        self.assertIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(self._sent(adapter), ["suspended"])
        _run(runtime.settle_row(adapter, _sub(), "completed", "1.33.4 = default", "seeded-a"))
        self.assertEqual(self._kinds(adapter).count("post"), 1, "the answer settles the same row")
        [task] = self._tasks(adapter)
        self.assertEqual((task["status"], task["title"]), ("complete", "1.33.4 = default"))
        self.assertEqual(self._sent(adapter), ["suspended", "closed"])

    def test_a_card_that_gives_up_without_a_note_is_set_aside_for_its_unblock(self):
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "gave_up", "", "seeded-z"))
        [task] = self._tasks(adapter)
        self.assertEqual((task["status"], task["title"]), ("error", "seeded-z"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(len(runtime._lapsed[(CHANNEL, THREAD)]), 1)
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(self._tasks(adapter)[0]["status"], "in_progress")

    def test_a_replayed_terminal_event_opens_no_second_row(self):
        adapter = _Adapter()
        for _ in range(2):
            _run(runtime.settle_row(adapter, _sub(), "completed", "1.33.4 = default", "seeded-a"))
        self.assertEqual(self._kinds(adapter).count("post"), 1)

    def test_a_card_whose_row_was_dropped_opens_no_new_one(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        runtime._plans.clear()  # as a lapse and expiry would leave it
        _run(runtime.settle_row(adapter, _sub(), "completed", "both pods are up"))
        self.assertEqual(self._kinds(adapter).count("post"), 1)

    def test_a_kind_that_runs_or_is_silent_opens_no_row(self):
        adapter = _Adapter()
        for kind in ("crashed", "timed_out", "unblocked", "archived", "commented"):
            _run(runtime.settle_row(adapter, _sub(), kind, "", "seeded-a"))
        self.assertNotIn("post", self._kinds(adapter))

    def test_a_card_with_no_note_on_a_fallen_back_plan_opens_no_row(self):
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        posts = self._kinds(adapter).count("post")
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed", "1.33.4 = default", "seeded-a"))
        self.assertEqual(self._kinds(adapter).count("post"), posts)

    def test_a_settle_says_whether_the_plan_shows_the_card_complete(self):
        # True lets a fanned-out card's report fold into its row (kanban_progress_lines).
        adapter = _Adapter()
        self._note(adapter, 1, "reading version", title="seeded-c")
        self.assertTrue(_run(runtime.settle_row(adapter, _sub("t_b"), "completed", "1.33.4 = default", "seeded-a")))
        self.assertTrue(_run(runtime.settle_row(adapter, _sub(), "completed", "1.32.9", "seeded-c")))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans, "every row settled, so forgotten")

    def test_a_failed_card_settles_its_row_failed_and_does_not_fold(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading version", title="seeded-c")
        self.assertFalse(_run(runtime.settle_row(adapter, _sub(), "gave_up", "", "seeded-c")))
        [task] = self._tasks(adapter)
        self.assertEqual(task["status"], slack_status.TASK_ERROR)

    def test_a_settle_on_a_fallen_back_plan_does_not_fold(self):
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        self.assertFalse(_run(runtime.settle_row(adapter, _sub(), "completed", "done")))
        self.assertFalse(_run(runtime.settle_row(adapter, _sub("t_b"), "completed", "1.33.4 = default", "seeded-a")))

    def test_a_kind_that_moves_nothing_leaves_the_row(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "commented"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])

    def test_an_unblocked_card_runs_again(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["in_progress"])

    def test_a_card_that_gave_up_leaves_the_next_card_a_fresh_plan(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs", task="t_a")
        _run(runtime.settle_row(adapter, _sub("t_a"), "gave_up"))
        self._note(adapter, 2, "checking pods", task="t_b")
        self.assertEqual(self._kinds(adapter).count("post"), 2)
        self.assertEqual(list(runtime._plans[(CHANNEL, THREAD)].rows), ["t_b"])

    def test_archiving_a_card_that_gave_up_lets_its_plan_go(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "gave_up"))
        _run(runtime.settle_row(adapter, _sub(), "archived"))
        self.assertEqual((runtime._plans, runtime._lapsed), ({}, {}))

    def test_a_lone_card_that_gave_up_runs_again_when_unblocked(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "gave_up"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["in_progress"])
        self.assertEqual(self._kinds(adapter).count("post"), 1)

    def test_a_card_that_gave_up_runs_again_when_unblocked(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        self._note(adapter, 2, "b", task="t_b")
        _run(runtime.settle_row(adapter, _sub(), "gave_up"))
        _run(runtime.settle_row(adapter, _sub("t_b"), "blocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["in_progress", "pending"])

    def _move(self, adapter, event_id, moved, task="t_a"):
        return _run(runtime.deliver_row(adapter, _sub(task), event_id, "check payments", f"→ {moved}", moved))

    def test_a_move_opens_no_row(self):
        adapter = _Adapter()
        self.assertTrue(self._move(adapter, 1, "ready"))
        self.assertEqual((adapter.calls, runtime._plans), ([], {}))

    def test_a_move_is_never_the_rows_title_whatever_its_wording(self):
        # A status event with no status falls back to upstream's own move line, which has no "→ ".
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.deliver_row(adapter, _sub(), 2, "check payments", "🔄 moved", ""))
        self._move(adapter, 3, "todo")
        task = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"][0]
        self.assertEqual(task["title"], "reading logs")
        self._note(adapter, 4, "→ rolling back the node pool")
        task = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"][0]
        self.assertEqual(task["title"], "→ rolling back the node pool · step 2 ▸")

    def test_a_move_joins_the_trail_and_leaves_a_settled_row_settled(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs", task="t_a")
        self._note(adapter, 2, "reading metrics", task="t_b")
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        self.assertTrue(self._move(adapter, 3, "ready", task="t_a"))
        task = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"][0]
        self.assertEqual(task["status"], "complete")
        steps = task["details"]["elements"][0]["elements"]
        self.assertEqual([step["elements"][0]["text"] for step in steps], ["✓ reading logs", "✓ → ready"])

    def test_a_move_into_review_waits_on_the_user(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        self.assertTrue(self._move(adapter, 2, "review"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["status"] for t in tasks], ["pending"])
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))

    def test_a_move_into_review_whose_edit_is_refused_goes_to_the_rolling_message(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        adapter.client.fail.add("update")
        self.assertFalse(self._move(adapter, 2, "review"))
        row = runtime._plans[(CHANNEL, THREAD)].rows["t_a"]
        self.assertEqual((row.lines, row.steps, row.note, row.last_event_id), (["reading logs"], 1, "reading logs", 1))

    def test_a_move_for_a_card_rolling_on_a_set_aside_plan_goes_to_its_rolling_message(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "a")
            adapter.client.fail.add("update")
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "b"))
            adapter.client.fail.clear()
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertFalse(
                await runtime.deliver_row(adapter, _sub("t_b"), 3, "check checkout", "→ ready", "ready"),
            )

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_an_archived_card_shows_as_failed_with_a_note(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs", task="t_a")
        self._note(adapter, 1, "reading metrics", task="t_b")
        _run(runtime.settle_row(adapter, _sub("t_a"), "archived"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([(t["task_id"], t["status"]) for t in tasks], [("t_a", "error"), ("t_b", "in_progress")])
        self.assertEqual(runtime._plans[(CHANNEL, THREAD)].rows["t_a"].lines[-1], runtime.ARCHIVED_NOTE)
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_archiving_the_last_running_card_edits_the_plan_and_closes(self):
        # The credential proxy refuses chat.delete, so the plan is edited, never deleted.
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "archived"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "update", "setStatus"])
        self.assertEqual(adapter.calls[2][1][0]["tasks"][0]["status"], "error")
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_refused_edit_on_archive_still_closes(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        adapter.client.fail.add("update")
        _run(runtime.settle_row(adapter, _sub(), "archived"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_card_the_dispatcher_retries_keeps_running(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        for kind in ("crashed", "timed_out"):
            _run(runtime.settle_row(adapter, _sub(), kind))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])
        self.assertEqual(runtime._plans[(CHANNEL, THREAD)].rows["t_a"].status, "in_progress")
        # The retried worker's next note lands on the same plan.
        self._note(adapter, 2, "retrying")
        self.assertEqual(self._kinds(adapter).count("post"), 1)

    def test_a_block_loop_waits_on_the_user(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "block_loop_detected"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        self.assertIn((CHANNEL, THREAD), runtime._plans)

    def test_a_settle_after_a_restart_clears_the_working_status_once(self):
        # setUp's reload is the restart: Slack still shows the Working… the old process set.
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"], "the card's row, then the clear")
        self.assertEqual(self._sent(adapter), ["closed"])
        _run(runtime.settle_row(adapter, _sub(), "gave_up"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"], "sent once")

    def test_an_archive_after_a_restart_sends_no_status(self):
        # A card archived long after it finished; the thread may still hold another, waiting card.
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "archived"))
        self.assertEqual(adapter.calls, [])

    def test_a_wait_after_a_restart_suspends_and_its_answer_settles(self):
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["suspended", "closed"])

    def test_a_restart_leaves_running_and_retried_cards_alone(self):
        adapter = _Adapter()
        for kind in ("crashed", "timed_out", "unblocked", "heartbeat", "commented"):
            _run(runtime.settle_row(adapter, _sub(), kind))
        self.assertEqual(adapter.calls, [])

    def test_a_settle_with_no_thread_sends_no_status(self):
        adapter = _Adapter()
        _run(runtime.settle_row(adapter, _sub(thread=""), "completed"))
        self.assertEqual(adapter.calls, [])

    def test_a_settle_after_a_restart_leaves_a_running_turn_working(self):
        adapter = _Adapter()
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(self._sent(adapter), ["processing"])

    def test_archiving_a_card_whose_plan_never_posted_frees_the_thread(self):
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "reading logs"))
        self.assertEqual(runtime._plans[(CHANNEL, THREAD)].rows, {})
        _run(runtime.settle_row(adapter, _sub(), "archived"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        adapter.client.fail.clear()
        self.assertTrue(self._note(adapter, 2, "next card", task="t_b"))

    def test_a_card_rolling_after_the_fallback_keeps_the_plan(self):
        # A's post is refused; B's first note rolls. A finishing must not
        # forget the plan while B still runs on its rolling line.
        adapter = _Adapter(_Client(fail={"post"}))
        self.assertFalse(self._note(adapter, 1, "a", task="t_a"))
        adapter.client.fail.clear()
        self.assertFalse(self._note(adapter, 2, "b", task="t_b"))
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        self.assertIn((CHANNEL, THREAD), runtime._plans)
        self.assertFalse(self._note(adapter, 3, "b2", task="t_b"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])
        self.assertEqual(self._sent(adapter), ["processing"])
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed"))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "setStatus"])
        self.assertEqual(self._sent(adapter), ["processing", "closed"])

    def test_a_quiet_fallen_back_plan_is_forgotten(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await asyncio.sleep(0.2)

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)

    def test_a_move_for_a_card_rolling_after_its_never_posted_plan_lapsed_goes_to_its_rolling_message(self):
        async def scenario(adapter):
            self.assertFalse(await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs"))
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            self.assertFalse(await runtime.deliver_row(adapter, _sub(), 2, "check payments", "→ ready", "ready"))

        adapter = _Adapter(_Client(fail={"post"}))
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "setStatus"], "lapsed unposted plan clears session")
        self.assertEqual(self._sent(adapter), ["processing", "closed"])

    def test_eviction_takes_the_least_active_thread(self):
        adapter = _Adapter()
        with mock.patch.object(runtime, "PLANS_MAX", 2):
            self._note(adapter, 1, "a", task="t_a")
            _run(runtime.deliver_row(adapter, _sub("t_b", thread="2.0"), 2, "two", "b"))
            self._note(adapter, 3, "a again", task="t_a")
            _run(runtime.deliver_row(adapter, _sub("t_c", thread="3.0"), 4, "three", "c"))
        self.assertEqual(list(runtime._plans), [(CHANNEL, THREAD), (CHANNEL, "3.0")])

    def test_an_evicted_plan_closes_its_session(self):
        adapter = _Adapter()
        with mock.patch.object(runtime, "PLANS_MAX", 1):
            self._note(adapter, 1, "a", task="t_a", title="one")
            _run(runtime.deliver_row(adapter, _sub("t_b", thread="2.0"), 2, "two", "b"))
        self.assertEqual(list(runtime._plans), [(CHANNEL, "2.0")])
        self.assertEqual(
            [v for n, v in adapter.calls if n == "setStatus"], ["processing", "closed", "processing"],
        )

    def test_a_settle_after_a_refused_edit_still_edits_the_plan(self):
        # A rate-limited edit falls back; the settle must not leave the row running.
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        adapter.client.fail.add("update")
        self.assertFalse(self._note(adapter, 2, "reading metrics"))
        adapter.client.fail.clear()
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual(tasks[0]["status"], "complete")
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)

    def test_a_card_archived_after_it_finished_keeps_its_row(self):
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a")
        self._note(adapter, 2, "b", task="t_b")
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        calls = len(adapter.calls)
        _run(runtime.settle_row(adapter, _sub("t_a"), "archived"))
        self.assertEqual(len(adapter.calls), calls)
        self.assertEqual(list(runtime._plans[(CHANNEL, THREAD)].rows), ["t_a", "t_b"])

    def test_a_redelivered_batch_leaves_an_archived_row_archived(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        self._note(adapter, 2, "b", task="t_b")
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        for _ in range(3):
            for kind in ("unblocked", "blocked", "archived"):
                _run(runtime.settle_row(adapter, _sub(), kind))
        row = runtime._plans[(CHANNEL, THREAD)].rows["t_a"]
        self.assertEqual((row.lines, row.status), (["reading logs", "Archived"], "error"))

    def test_an_unblocked_replay_leaves_a_running_row(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "unblocked"))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus"])

    def test_the_plan_and_the_turn_share_the_session_entry(self):
        # The subscription carries no team; the Planning Agent's turn does.
        adapter = _Adapter()
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))
        # A woken turn starts within the minute; Working… must come back.
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))

    def test_a_planning_turn_ending_leaves_working_while_rows_run(self):
        # The Planning Agent's turn ends with a clear while its cards still run.
        adapter = _Adapter()
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        self._note(adapter, 1, "reading logs")
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self._note(adapter, 2, "reading metrics")
        self.assertNotIn(("setStatus", "closed"), adapter.calls)
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_stale_plan_stops_holding_the_session(self):
        # A card whose terminal event never reaches the thread never settles its row.
        adapter = _Adapter()
        with mock.patch.object(runtime.time, "monotonic", return_value=1000.0):
            self._note(adapter, 1, "reading logs")
        later = 1000.0 + runtime.PLAN_HOLD_SECONDS + 1
        with mock.patch.object(runtime.time, "monotonic", return_value=later):
            _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_note_after_the_lapse_opens_the_session_again(self):
        adapter = _Adapter()
        with mock.patch.object(runtime.time, "monotonic", return_value=1000.0):
            self._note(adapter, 1, "reading logs")
        later = 1000.0 + runtime.PLAN_HOLD_SECONDS + 1
        with mock.patch.object(runtime.time, "monotonic", return_value=later):
            _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
            self._note(adapter, 2, "still reading")
        self.assertEqual(
            [v for n, v in adapter.calls if n == "setStatus"], ["processing", "closed", "processing"],
        )

    def test_a_refused_plan_status_is_retried_on_the_next_note(self):
        adapter = _Adapter(_Client(fail={"setStatus"}))
        self._note(adapter, 1, "reading logs")
        adapter.client.fail.clear()
        self._note(adapter, 2, "reading metrics")
        self.assertEqual([v for n, v in adapter.calls if n == "setStatus"], ["processing", "processing"])

    def test_a_quiet_plan_closes_the_session_itself(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "reading logs")
            await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "reading logs")
            await runtime.settle_row(adapter, _sub("t_b"), "completed")
            # Each touch re-arms the timer, so only one lapse fires.
            await asyncio.sleep(0.2)

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual([v for n, v in adapter.calls if n == "setStatus"], ["processing", "closed"])
        self.assertNotIn((CHANNEL, THREAD), runtime._plans)

    def test_a_lapse_due_during_a_notes_render_keeps_the_plan(self):
        # The note lands within one chat.update of the hold running out.
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "reading logs")
            await asyncio.sleep(0.1)
            adapter.client.slow = 0.6  # the hold runs out 0.2 s into this render
            await runtime.deliver_row(adapter, _sub("t_a"), 2, "check payments", "reading metrics")
            await runtime.deliver_row(adapter, _sub("t_a"), 3, "check payments", "restarting")

        adapter = _Adapter(_SlowClient())
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.3):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter).count("post"), 1)
        self.assertNotIn((CHANNEL, THREAD), runtime._lapsed)

    def test_an_expiry_due_while_an_unblocked_row_renders_keeps_its_plan(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "asking")
            await runtime.settle_row(adapter, _sub("t_a"), "blocked")
            await asyncio.sleep(0.4)  # set aside at 0.1 s; its expiry is due at 0.5 s
            adapter.client.slow = 0.25
            await runtime.settle_row(adapter, _sub("t_a"), "unblocked")
            await runtime.deliver_row(adapter, _sub("t_a"), 2, "check payments", "restarting")

        adapter = _Adapter(_SlowClient())
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.1), mock.patch.object(
            runtime, "SET_ASIDE_MAX_SECONDS", 0.4,
        ):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter).count("post"), 1)
        self.assertIn((CHANNEL, THREAD), runtime._plans)

    def test_a_card_after_a_lapse_starts_a_new_plan(self):
        # t_a's terminal event was lost; t_b must not land on its stale plan.
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "reading logs")
            await asyncio.sleep(0.2)
            await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "reading logs")
            await runtime.settle_row(adapter, _sub("t_b"), "completed")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter).count("post"), 2)
        self.assertEqual(
            [v for n, v in adapter.calls if n == "setStatus"], ["processing", "closed", "processing", "closed"],
        )

    def test_a_plan_that_settles_leaves_no_timer(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            plan = runtime._plans[(CHANNEL, THREAD)]
            self.assertIsNotNone(plan.lapse)
            await runtime.settle_row(adapter, _sub(), "completed")
            self.assertIsNone(plan.lapse)
            await asyncio.sleep(0.2)

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual([v for n, v in adapter.calls if n == "setStatus"], ["processing", "closed"])

    def test_a_late_terminal_event_settles_a_lapsed_plans_row(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await asyncio.sleep(0.2)
            await runtime.settle_row(adapter, _sub(), "completed", "both pods are up")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter), ["post", "setStatus", "setStatus", "update"])
        task = adapter.calls[-1][1][0]["tasks"][0]
        self.assertEqual((task["status"], task["title"]), ("complete", "both pods are up"))
        self.assertEqual(adapter.calls[2], ("setStatus", "closed"))
        self.assertEqual(runtime._lapsed, {})

    def test_a_card_on_a_lapsed_and_a_new_plan_settles_both(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await asyncio.sleep(0.2)
            await runtime.deliver_row(adapter, _sub(), 2, "check payments", "still reading")
            await runtime.settle_row(adapter, _sub(), "completed")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        updates = [v for n, v in adapter.calls if n == "update"]
        self.assertEqual([u[0]["tasks"][0]["status"] for u in updates], ["complete", "complete"])
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_card_waiting_past_the_lapse_holds_suspended(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await runtime.settle_row(adapter, _sub(), "blocked")
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._plans)
            # A turn asking the user ends with a clear; the wait still holds.
            await adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn")
            await adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn")
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            self.assertNotIn(("setStatus", "closed"), adapter.calls)
            # Answered: the card runs on its plan again, which takes its next note.
            await runtime.settle_row(adapter, _sub(), "unblocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
            self.assertIn((CHANNEL, THREAD), runtime._plans)
            await runtime.deliver_row(adapter, _sub(), 3, "check payments", "retrying")
            await runtime.settle_row(adapter, _sub(), "completed")
            self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter).count("post"), 1)
        updates = [v for n, v in adapter.calls if n == "update"]
        self.assertEqual(
            [u[0]["tasks"][0]["status"] for u in updates], ["pending", "in_progress", "in_progress", "complete"],
        )
        self.assertEqual(runtime._lapsed, {})

    def test_a_set_aside_plan_with_only_a_running_row_expires(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await asyncio.sleep(0.1)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            await asyncio.sleep(0.3)
            self.assertEqual(runtime._lapsed, {})
            sent = len(adapter.calls)
            await runtime.settle_row(adapter, _sub(), "completed")
            self.assertEqual(len(adapter.calls), sent, "a terminal event after the expiry found the plan")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05), mock.patch.object(
            runtime, "SET_ASIDE_MAX_SECONDS", 0.1,
        ):
            _run(scenario(adapter))
        self.assertEqual(self._sent(adapter)[-1], "closed")

    def test_a_set_aside_plan_with_a_card_waiting_outlives_the_expiry(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await runtime.settle_row(adapter, _sub(), "blocked")
            await asyncio.sleep(0.4)
            self.assertIn((CHANNEL, THREAD), runtime._lapsed)
            await adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn")
            await adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn")
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            await runtime.settle_row(adapter, _sub(), "unblocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05), mock.patch.object(
            runtime, "SET_ASIDE_MAX_SECONDS", 0.1,
        ):
            _run(scenario(adapter))

    def test_a_card_answered_beside_a_newer_plan_holds_working(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_w"), 1, "check payments", "asking")
            await runtime.settle_row(adapter, _sub("t_w"), "blocked")
            await asyncio.sleep(0.2)
            await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "reading logs")
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            self.assertEqual(self._sent(adapter)[-1], "suspended")
            await runtime.settle_row(adapter, _sub("t_w"), "unblocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
            await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"), "the answered card's hold ran out")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_a_card_answered_beside_a_newer_plan_ends_on_its_own_timer(self):
        # The answered plan is armed well after the newer plan, so the newer
        # plan's lapse is not what ends its processing. Every step sits at
        # least 150 ms from the timer it must precede or follow.
        hold = 0.5

        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_w"), 1, "check payments", "asking")
            await runtime.settle_row(adapter, _sub("t_w"), "blocked")
            await asyncio.sleep(hold * 2)
            await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "reading logs")
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            await asyncio.sleep(hold * 0.5)
            await runtime.settle_row(adapter, _sub("t_w"), "unblocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
            await asyncio.sleep(hold * 0.7)
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"), "the newer plan lapsed")
            await asyncio.sleep(hold * 1.5)
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", hold):
            _run(scenario(adapter))

    def test_a_rolling_card_answered_after_the_lapse_resumes_its_plan(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "a")
            adapter.client.fail.add("update")
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "b"))
            adapter.client.fail.clear()
            await runtime.settle_row(adapter, _sub("t_a"), "completed")
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            await runtime.settle_row(adapter, _sub("t_b"), "unblocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
            self.assertIn((CHANNEL, THREAD), runtime._plans)
            await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))

    def test_the_per_thread_cap_drops_a_quiet_plan_before_an_answered_one(self):
        async def scenario(adapter):
            answered, quiet, newest = runtime._Plan(TEAM), runtime._Plan(TEAM), runtime._Plan(TEAM)
            for plan, card in ((answered, "t_r"), (quiet, "t_q"), (newest, "t_n")):
                plan.ts = PLAN_TS
                plan.rows[card] = runtime._Row(card, card)
                plan.rows[card].status = slack_status.TASK_RUNNING
            quiet.touched -= 10
            newest.touched -= 10
            runtime._lapsed[(CHANNEL, THREAD)] = [answered, quiet]
            await runtime._set_aside(adapter, (CHANNEL, THREAD), newest)
            return answered, newest

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 1.0), mock.patch.object(
            runtime, "LAPSED_PER_THREAD", 2,
        ):
            answered, newest = _run(scenario(adapter))
        self.assertEqual(runtime._lapsed[(CHANNEL, THREAD)], [answered, newest])

    def test_the_per_thread_cap_drops_a_quiet_plan_before_a_waiting_one(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_w"), 1, "check payments", "asking")
            await runtime.settle_row(adapter, _sub("t_w"), "blocked")
            await asyncio.sleep(0.2)
            for event_id, card in ((2, "t_1"), (3, "t_2")):
                await runtime.deliver_row(adapter, _sub(card), event_id, "check checkout", "reading logs")
                await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            await runtime.settle_row(adapter, _sub("t_w"), "completed")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05), mock.patch.object(
            runtime, "LAPSED_PER_THREAD", 2,
        ):
            _run(scenario(adapter))
        last = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([(t["task_id"], t["status"]) for t in last], [("t_w", "complete")])

    def test_a_set_aside_thread_evicted_at_the_cap_sends_its_session(self):
        other = "7.7"

        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "asking")
            await runtime.settle_row(adapter, _sub(), "blocked")
            await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            await runtime.deliver_row(adapter, _sub("t_b", thread=other), 2, "check checkout", "reading logs")
            await asyncio.sleep(0.2)
            self.assertNotIn((CHANNEL, THREAD), runtime._lapsed)
            self.assertEqual(self._sent(adapter).count("closed"), 2, "both threads, the evicted one too")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05), mock.patch.object(runtime, "PLANS_MAX", 1):
            _run(scenario(adapter))

    def test_a_note_after_the_lapse_starts_a_new_plan_beside_a_waiting_card(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "reading logs")
            await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "reading logs")
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            await asyncio.sleep(0.2)
            await runtime.deliver_row(adapter, _sub("t_a"), 3, "check payments", "still reading")
            self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
            await runtime.settle_row(adapter, _sub("t_a"), "completed")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(self._kinds(adapter).count("post"), 2)
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        self.assertNotIn(("setStatus", "closed"), adapter.calls)

    def test_a_card_quiet_twice_settles_both_set_aside_rows(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub(), 1, "check payments", "reading logs")
            await asyncio.sleep(0.2)
            await runtime.deliver_row(adapter, _sub(), 2, "check payments", "still reading")
            await asyncio.sleep(0.2)
            await runtime.settle_row(adapter, _sub(), "completed")

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        updates = [v for n, v in adapter.calls if n == "update"]
        self.assertEqual([u[0]["tasks"][0]["status"] for u in updates], ["complete", "complete"])
        self.assertEqual(runtime._lapsed, {})

    def test_the_cap_drops_settled_rows_before_a_live_one(self):
        adapter = _Adapter()
        with mock.patch.object(slack_status, "ROWS_MAX", 2):
            self._note(adapter, 1, "a", task="t_a")
            for event_id, card in ((2, "t_b"), (3, "t_c")):
                self._note(adapter, event_id, "x", task=card)
                _run(runtime.settle_row(adapter, _sub(card), "completed"))
            self._note(adapter, 4, "d", task="t_d")
        tasks = [v for n, v in adapter.calls if n == "update"][-1][0]["tasks"]
        self.assertEqual([t["task_id"] for t in tasks], ["t_a", "t_d"])

    def test_a_fallen_back_plan_holds_working_for_its_rolling_cards(self):
        # A's plan posted; B's first note is refused and rolls. A finishing
        # must not close the session while B still runs.
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a")
        adapter.client.fail.add("update")
        self.assertFalse(self._note(adapter, 2, "b", task="t_b"))
        adapter.client.fail.clear()
        _run(runtime.settle_row(adapter, _sub("t_a"), "completed"))
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self.assertNotIn(("setStatus", "closed"), adapter.calls)
        _run(runtime.settle_row(adapter, _sub("t_b"), "completed"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_rolling_card_answered_beside_a_newer_plan_closes_when_it_finishes(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "a")
            adapter.client.fail.add("update")
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "b"))
            adapter.client.fail.clear()
            await runtime.settle_row(adapter, _sub("t_a"), "completed")
            await asyncio.sleep(0.2)
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            await runtime.deliver_row(adapter, _sub("t_c"), 3, "check orders", "c")
            await runtime.settle_row(adapter, _sub("t_b"), "unblocked")
            await runtime.settle_row(adapter, _sub("t_c"), "completed")
            self.assertEqual(self._sent(adapter)[-1], "processing")
            await runtime.settle_row(adapter, _sub("t_b"), "completed")
            self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(runtime._lapsed, {})

    def test_a_rolling_card_blocking_after_the_lapse_holds_suspended(self):
        async def scenario(adapter):
            await runtime.deliver_row(adapter, _sub("t_a"), 1, "check payments", "a")
            adapter.client.fail.add("update")
            self.assertFalse(await runtime.deliver_row(adapter, _sub("t_b"), 2, "check checkout", "b"))
            adapter.client.fail.clear()
            await runtime.settle_row(adapter, _sub("t_a"), "completed")
            await asyncio.sleep(0.2)
            self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))
            await runtime.settle_row(adapter, _sub("t_b"), "blocked")
            self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
            await runtime.settle_row(adapter, _sub("t_b"), "completed")
            self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

        adapter = _Adapter()
        with mock.patch.object(runtime, "PLAN_HOLD_SECONDS", 0.05):
            _run(scenario(adapter))
        self.assertEqual(runtime._lapsed, {})

    def test_rolling_cards_waiting_on_the_user_suspend_the_session(self):
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a")
        adapter.client.fail.add("update")
        self.assertFalse(self._note(adapter, 2, "a2", task="t_a"))
        self.assertFalse(self._note(adapter, 3, "b", task="t_b"))
        adapter.client.fail.clear()
        _run(runtime.settle_row(adapter, _sub("t_a"), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing"], "t_b still runs")
        _run(runtime.settle_row(adapter, _sub("t_b"), "blocked"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended"])
        self.assertFalse(self._note(adapter, 4, "b2", task="t_b"))
        self.assertEqual(self._sent(adapter), ["processing", "suspended", "processing"])

    def test_a_turn_ending_without_a_plan_still_closes(self):
        adapter = _Adapter()
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_rows_waiting_on_the_user_suspend_the_session(self):
        adapter = _Adapter()
        self._note(adapter, 1, "reading logs")
        _run(runtime.settle_row(adapter, _sub(), "blocked"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        # A turn asking the user ends with a clear; the session stays suspended.
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "turn"))
        _run(adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "turn"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "suspended"))
        # The card runs again once answered, then finishes.
        self._note(adapter, 2, "resuming")
        self.assertEqual(adapter.calls[-1], ("setStatus", "processing"))
        _run(runtime.settle_row(adapter, _sub(), "completed"))
        self.assertEqual(adapter.calls[-1], ("setStatus", "closed"))

    def test_a_running_row_keeps_working_beside_a_waiting_one(self):
        adapter = _Adapter()
        self._note(adapter, 1, "a", task="t_a")
        self._note(adapter, 2, "b", task="t_b")
        _run(runtime.settle_row(adapter, _sub("t_b"), "blocked"))
        self.assertEqual([v for n, v in adapter.calls if n == "setStatus"], ["processing"])


class EnabledTest(unittest.TestCase):
    def test_missing_renderer_reads_as_off(self):
        with mock.patch.object(runtime, "_status", None), mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            self.assertFalse(runtime.enabled())

    def test_flag_values(self):
        for value, expected in (("1", True), ("true", True), ("", False), ("0", False)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"KAGE_SLACK_UX": value}):
                self.assertEqual(runtime.enabled(), expected)


class SettlingKindsTest(unittest.TestCase):
    def test_the_kinds_that_overtake_an_unblock_are_the_ones_that_settle_a_row(self):
        settling = {kind for kind, status in slack_status.TASK_STATUS_BY_KIND.items() if status != slack_status.TASK_RUNNING}
        self.assertEqual(set(kanban_progress_lines.SETTLING_KINDS), settling | {"archived"})


if __name__ == "__main__":
    unittest.main()
