"""Tests for kanban_chat_notify and its applier, with the Hermes modules stubbed.

The build runs verify_kanban_chat_notify.py against the real patched tree; this
file runs on every pull request, where Hermes is not installed.
"""

import asyncio
import enum
import json
import sqlite3
import os
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _install_hermes_stubs() -> None:
    gateway = types.ModuleType("gateway")
    config = types.ModuleType("gateway.config")
    platforms = types.ModuleType("gateway.platforms")
    base = types.ModuleType("gateway.platforms.base")

    class Platform(enum.Enum):
        API_SERVER = "api_server"
        GOOGLE_CHAT = "google_chat"
        SLACK = "slack"

    @dataclass
    class PlatformConfig:
        enabled: bool = False

    @dataclass
    class SendResult:
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config, self.platform, self._message_handler = config, platform, None

        def set_message_handler(self, handler):
            self._message_handler = handler

    config.Platform, config.PlatformConfig = Platform, PlatformConfig
    base.BasePlatformAdapter, base.SendResult = BasePlatformAdapter, SendResult
    for name, module in (("gateway", gateway), ("gateway.config", config),
                         ("gateway.platforms", platforms), ("gateway.platforms.base", base)):
        sys.modules.setdefault(name, module)


_install_hermes_stubs()

import apply_kanban_chat_notify  # noqa: E402
import kanban_chat_notify  # noqa: E402
from gateway.config import Platform  # noqa: E402

ROUTED = {kanban_chat_notify.NOTIFY_PLATFORM_ENV: "google_chat"}
UNROUTED = {kanban_chat_notify.NOTIFY_PLATFORM_ENV: ""}


class _Runner:
    config = types.SimpleNamespace(multiplex_profiles=False)

    def _primary_message_handler(self):
        async def handler(event):
            return None
        return handler


class ResolveTest(unittest.TestCase):
    def setUp(self):
        # Pin the route probe up, so no test here runs whatever `a2a` is on the
        # developer's PATH. The probe's own tests stop this and patch the child.
        self.probe = mock.patch.object(kanban_chat_notify.ChatNotifyAdapter, "_probe", return_value=True)
        self.probe.start()
        self.addCleanup(mock.patch.stopall)

    def test_a_live_adapter_always_wins(self):
        live = object()
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIs(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, live), live)

    def test_the_routed_platform_gets_the_stand_in(self):
        runner = _Runner()
        with mock.patch.dict(os.environ, ROUTED):
            first = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            again = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
        self.assertIsInstance(first, kanban_chat_notify.ChatNotifyAdapter)
        self.assertIs(first, again)
        self.assertIsNotNone(first._message_handler)

    def test_nothing_else_gets_one(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.SLACK, None))
        with mock.patch.dict(os.environ, UNROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None))

    def test_a_subscription_without_a_thread_gets_none(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None, {"thread_id": ""}))
            self.assertIsNotNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None, {"thread_id": "spaces/H/threads/T"}))

    def test_no_stand_in_under_multiplex(self):
        runner = _Runner()
        runner.config = types.SimpleNamespace(multiplex_profiles=True)
        with mock.patch.dict(os.environ, ROUTED):
            self.assertIsNone(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None, {"thread_id": "t"}))

    def test_a_route_marked_down_resolves_to_none_until_the_backoff_ends(self):
        runner = _Runner()
        with mock.patch.dict(os.environ, ROUTED):
            stand_in = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            stand_in._route_down_until = kanban_chat_notify.time.monotonic() + 30
            self.assertIsNone(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None))
            stand_in._route_down_until = 0.0
            self.assertIs(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None), stand_in)

    def test_the_probe_decides_whether_a_routed_subscription_is_offered(self):
        self.probe.stop()
        runner = _Runner()
        refused = subprocess.CompletedProcess([], 1, b"", b"")
        with mock.patch.dict(os.environ, ROUTED):
            with mock.patch.object(kanban_chat_notify.subprocess, "run", return_value=refused):
                stand_in = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)  # an armed gateway's refusal: up
            for returncode, offered in ((kanban_chat_notify.NOTIFY_ROUTE_UNAVAILABLE, False), (1, True)):
                stand_in._route_down_until = 0.0
                stand_in._probed_at = float("-inf")
                done = subprocess.CompletedProcess([], returncode, b"", b"")
                with mock.patch.object(kanban_chat_notify.subprocess, "run", return_value=done) as run:
                    got = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
                self.assertEqual(got is stand_in, offered, returncode)
                self.assertEqual(run.call_args.args[0][-2:], ["--", ""])

    def test_the_probe_is_cached_and_never_runs_on_the_event_loop(self):
        self.probe.stop()
        runner = _Runner()
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(kanban_chat_notify.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 1, b"", b"")) as run:
            stand_in = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            self.assertEqual(run.call_count, 1, "a second resolve inside the TTL must not probe again")
            stand_in._probed_at = float("-inf")

            async def on_loop():
                return kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)

            self.assertIs(asyncio.run(on_loop()), stand_in)
            self.assertEqual(run.call_count, 1, "delivery on the event loop must use the cached answer")

    def test_an_up_answer_is_trusted_for_the_ttl_and_a_down_one_is_reprobed_after_its_backoff(self):
        self.probe.stop()
        runner = _Runner()
        clock = [1000.0]
        up = subprocess.CompletedProcess([], 1, b"", b"")
        down = subprocess.CompletedProcess([], kanban_chat_notify.NOTIFY_ROUTE_UNAVAILABLE, b"", b"")
        with mock.patch.dict(os.environ, ROUTED), \
                mock.patch.object(kanban_chat_notify.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(kanban_chat_notify.subprocess, "run", return_value=up) as run:
            stand_in = kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            clock[0] += kanban_chat_notify.ROUTE_PROBE_TTL_SECONDS - 1
            kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None)
            self.assertEqual(run.call_count, 1, "an up answer is trusted for the TTL")
            # A send meets an outage: the route is held down, then probed again
            # at once rather than trusting the old up answer for the rest of the TTL.
            stand_in.mark_route_down()
            self.assertIsNone(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None))
            clock[0] += kanban_chat_notify.ROUTE_DOWN_BACKOFF_SECONDS + 1
            run.return_value = down
            self.assertIsNone(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None))
            self.assertEqual(run.call_count, 2, "the backoff's end is probed")
            clock[0] += kanban_chat_notify.ROUTE_DOWN_BACKOFF_SECONDS + 1
            run.return_value = up
            self.assertIs(kanban_chat_notify.resolve(runner, Platform.GOOGLE_CHAT, None), stand_in)
            self.assertEqual(run.call_count, 3)

    def test_a_conversation_subscription_gets_the_conversation_stand_in(self):
        sub = {"thread_id": "gchat:spaces/A/threads/B", "chat_id": "a2a-ctx-1"}
        conv_only = {kanban_chat_notify.NOTIFY_PLATFORM_ENV: "", kanban_chat_notify.NOTIFY_CONVERSATIONS_ENV: "google_chat"}
        with mock.patch.dict(os.environ, conv_only):
            got = kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None, sub)
            self.assertIsInstance(got, kanban_chat_notify.ConversationNotifyAdapter)
            # A home-thread subscription is not served by the conversation switch.
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None, {"thread_id": "spaces/H/threads/T"}))
            self.assertEqual(kanban_chat_notify.active_platforms(set()), {"google_chat"})
        with mock.patch.dict(os.environ, ROUTED):
            # A2A_NOTIFY_PLATFORM alone does not route conversations.
            self.assertIsNone(kanban_chat_notify.resolve(_Runner(), Platform.GOOGLE_CHAT, None, sub))

    def test_active_platforms(self):
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(kanban_chat_notify.active_platforms({"api_server"}), {"api_server", "google_chat"})
        with mock.patch.dict(os.environ, UNROUTED):
            self.assertEqual(kanban_chat_notify.active_platforms({"api_server"}), {"api_server"})


class _Proc:
    def __init__(self, returncode, out=b"", err=b""):
        self.returncode, self._out, self._err = returncode, out, err

    async def communicate(self):
        return self._out, self._err


class SendTest(unittest.TestCase):
    def _send(self, returncode, out=b"", err=b"", metadata=None, text="- done"):
        adapter = kanban_chat_notify.ChatNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        calls = []

        async def fake_exec(*argv, **kwargs):
            calls.append((argv, kwargs))
            return _Proc(returncode, out, err)

        with mock.patch.object(kanban_chat_notify.asyncio, "create_subprocess_exec", fake_exec):
            result = asyncio.run(adapter.send("spaces/H", text, metadata=metadata))
        return result, calls

    def test_a_threaded_send_ends_the_flags_before_the_text(self):
        result, calls = self._send(0, b'{"message_id":"m1","thread_id":"t1"}', metadata={"thread_id": "spaces/H/threads/T"})
        argv, kwargs = calls[0]
        self.assertEqual(list(argv), ["a2a", "notify", "--platform", "google_chat", "--timeout", "60s",
                                      "--thread", "spaces/H/threads/T", "--", "- done"])
        self.assertIs(kwargs["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "m1")

    def test_a_gateway_conversation_is_sent_with_its_context(self):
        # The subscription is (platform, the bridge session holding the route,
        # the conversation key); the context id is read back from the route.
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        db = os.path.join(home.name, "session_kv.db")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            conn.execute("INSERT INTO session_metadata VALUES (?, ?)", ("a2a-ctx-1", json.dumps(
                {"conversation_route": {"platform": "google_chat", "conversation": "gchat:spaces/A/threads/B",
                                        "context_id": "ctx-1"}})))
        adapter = kanban_chat_notify.ConversationNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        calls = []

        async def fake_exec(*argv, **kwargs):
            calls.append(argv)
            return _Proc(0, b'{"message_id":"m"}')

        with mock.patch.dict(os.environ, {kanban_chat_notify.SESSION_KV_DB_ENV: db}), \
                mock.patch.object(kanban_chat_notify.asyncio, "create_subprocess_exec", fake_exec):
            result = asyncio.run(adapter.send("a2a-ctx-1", "- 3 nodes", metadata={"thread_id": "gchat:spaces/A/threads/B"}))
            missing = asyncio.run(adapter.send("a2a-ctx-gone", "- 3 nodes", metadata={"thread_id": "gchat:spaces/A/threads/B"}))
        self.assertTrue(result.success)
        self.assertEqual(list(calls[0]), ["a2a", "notify", "--platform", "google_chat", "--timeout", "60s",
                                          "--conversation", "gchat:spaces/A/threads/B", "--context", "ctx-1",
                                          "--", "- 3 nodes"])
        self.assertFalse(missing.success, "a session with no recorded route is a failed send, not a guess")
        self.assertEqual(len(calls), 1)

    def test_a_conversation_cards_wake_self_posts_into_the_bridge_session(self):
        # Push-capable, so the notifier still sends the report; but a wake is
        # self-posted into the bridge session (the source's chat_id) through
        # the API server, never run as a fresh-session turn.
        with mock.patch.dict(os.environ, {kanban_chat_notify.API_SERVER_KEY_ENV: "k"}):
            adapter = kanban_chat_notify.ConversationNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        self.assertTrue(getattr(adapter, "supports_async_delivery", True), "push-capable, or the report is skipped")
        self.assertEqual((adapter._host, adapter._port, adapter._api_key), ("127.0.0.1", 8642, "k"))
        posted = []

        async def self_post(a, *, text, session_id):
            posted.append((a, text, session_id))

        wake_mod = types.ModuleType("gateway.wake")
        wake_mod._self_post_chat_completion = self_post
        event = types.SimpleNamespace(internal=True, text="[kanban] t_1 blocked",
                                      source=types.SimpleNamespace(chat_id="a2a-ctx-1"))
        with mock.patch.dict(sys.modules, {"gateway.wake": wake_mod}):
            asyncio.run(adapter.handle_message(event))
        self.assertEqual(posted, [(adapter, "[kanban] t_1 blocked", "a2a-ctx-1")])
        self.assertTrue(event._gateway_accepted)

    def test_no_thread_is_a_new_thread(self):
        _, calls = self._send(0, b"{}")
        self.assertNotIn("--thread", calls[0][0])

    def test_a_refusal_is_a_failed_send(self):
        result, _ = self._send(1, err=b"a2a: notify: the gateway posted nothing")
        self.assertFalse(result.success)
        self.assertIn("posted nothing", result.error)

    def test_outcome_unknown_is_not_a_failure(self):
        result, _ = self._send(kanban_chat_notify.NOTIFY_OUTCOME_UNKNOWN)
        self.assertTrue(result.success)

    def test_route_unavailable_fails_and_marks_the_route_down(self):
        adapter = kanban_chat_notify.ChatNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())

        async def fake_exec(*argv, **kwargs):
            return _Proc(kanban_chat_notify.NOTIFY_ROUTE_UNAVAILABLE, err=b"not armed")

        with mock.patch.object(kanban_chat_notify.asyncio, "create_subprocess_exec", fake_exec):
            result = asyncio.run(adapter.send("spaces/H", "x"))
        self.assertFalse(result.success)
        self.assertTrue(adapter.route_down())

    def test_a_timed_out_child_is_killed(self):
        adapter = kanban_chat_notify.ChatNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        proc = mock.MagicMock()
        proc.returncode = None

        async def hang():
            await asyncio.sleep(10)

        async def wait():
            return 0

        proc.communicate = hang
        proc.wait = wait

        async def fake_exec(*argv, **kwargs):
            return proc

        with mock.patch.object(kanban_chat_notify.asyncio, "create_subprocess_exec", fake_exec), \
                mock.patch.object(kanban_chat_notify, "SEND_TIMEOUT_SECONDS", 0.05):
            result = asyncio.run(adapter.send("spaces/H", "x"))
        self.assertFalse(result.success)
        proc.kill.assert_called_once()

    def test_attachments_are_not_posted(self):
        adapter = kanban_chat_notify.ChatNotifyAdapter(Platform.GOOGLE_CHAT, _Runner())
        with mock.patch.object(adapter, "send") as send:
            result = asyncio.run(adapter._send_media_fallback_notice(
                "send_document", "document", "/opt/data/r.pdf", "spaces/H", None, None, None, file_name="r.pdf"))
        self.assertFalse(result.success)
        send.assert_not_called()


@dataclass
class _Event:
    id: int
    created_at: int


class FreshEventsTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = home.name
        patcher = mock.patch.dict(os.environ, {kanban_chat_notify.HERMES_HOME_ENV: self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        kanban_chat_notify._routed_since = None
        self.addCleanup(setattr, kanban_chat_notify, "_routed_since", None)

    def test_the_skip_is_once_per_install_not_a_standing_deadline(self):
        live = 1_000_000.0
        claim = {"sub": {"platform": "google_chat", "task_id": "t"}, "cursor": 9}
        backlog = _Event(1, int(live - kanban_chat_notify.STALE_EVENT_SECONDS - 1))
        with mock.patch.dict(os.environ, ROUTED):
            self.assertEqual(kanban_chat_notify.fresh_events(dict(claim, events=[backlog]), now=live)["events"], [])
            # A day later, after an outage: an event raised after routing went
            # live is delivered however old it is now.
            later = live + 24 * 3600
            held = _Event(2, int(live + 60))
            kept = kanban_chat_notify.fresh_events(dict(claim, events=[held]), now=later)
            self.assertEqual([ev.id for ev in kept["events"]], [2])
            # And a restart reads the recorded moment rather than starting over.
            kanban_chat_notify._routed_since = None
            kept = kanban_chat_notify.fresh_events(dict(claim, events=[held]), now=later)
            self.assertEqual([ev.id for ev in kept["events"]], [2])
        recorded = Path(self.home, kanban_chat_notify.ROUTED_SINCE_FILE).read_text().strip()
        self.assertEqual(float(recorded), live)

    def test_an_unreadable_record_falls_back_to_when_it_was_written(self):
        # A write cut short leaves an empty file, and nan, inf or a future time
        # is no go-live moment. Recording "now" instead would move the cutoff
        # forward and drop events that were not stale, so the record's mtime,
        # when it was written, stands in and is written back whole.
        path = Path(self.home, kanban_chat_notify.ROUTED_SINCE_FILE)
        live, now = 1_000_000.0, 1_000_000.0 + 24 * 3600
        for body in ("", "nan", "inf", "-inf", "1e400", str(now + 60), "garbage"):
            with self.subTest(body=body):
                path.write_text(body)
                os.utime(path, (live, live))
                kanban_chat_notify._routed_since = None
                self.assertEqual(kanban_chat_notify.routed_since(now), live)
                self.assertEqual(float(path.read_text().strip()), live)
                self.assertEqual(sorted(os.listdir(self.home)), [kanban_chat_notify.ROUTED_SINCE_FILE])

    def test_a_record_that_cannot_be_rewritten_still_yields_its_mtime(self):
        # The volume that cut the first write short may still be full or
        # read-only: the rewrite fails, and the mtime stands for this process
        # rather than the error escaping and halting routed delivery.
        path = Path(self.home, kanban_chat_notify.ROUTED_SINCE_FILE)
        live, now = 1_000_000.0, 1_000_000.0 + 3600
        path.write_text("")
        os.utime(path, (live, live))
        os.chmod(self.home, 0o500)
        self.addCleanup(os.chmod, self.home, 0o700)
        self.assertEqual(kanban_chat_notify.routed_since(now), live)

    def test_an_absent_record_is_written_whole_as_now(self):
        now = 1_000_000.0
        self.assertEqual(kanban_chat_notify.routed_since(now), now)
        self.assertEqual(os.listdir(self.home), [kanban_chat_notify.ROUTED_SINCE_FILE])
        self.assertEqual(float(Path(self.home, kanban_chat_notify.ROUTED_SINCE_FILE).read_text()), now)

    def test_stale_events_are_dropped_for_the_routed_platform_only(self):
        now = 1_000_000.0
        old, new = _Event(1, int(now - kanban_chat_notify.STALE_EVENT_SECONDS - 1)), _Event(2, int(now - 60))
        claim = {"sub": {"platform": "google_chat", "task_id": "t"}, "events": [old, new], "cursor": 2}
        with mock.patch.dict(os.environ, ROUTED):
            kept = kanban_chat_notify.fresh_events(claim, now=now)
            self.assertEqual([ev.id for ev in kept["events"]], [2])
            self.assertEqual(kept["cursor"], 2)
            other = dict(claim, sub={"platform": "slack", "task_id": "t"})
            self.assertEqual(len(kanban_chat_notify.fresh_events(other, now=now)["events"]), 2)
        self.assertIsNone(kanban_chat_notify.fresh_events(None))


class ContractTest(unittest.TestCase):
    """The notify contract has three spellings (Go, chat_notify.py, this module); hold the Python two together."""

    def test_the_python_copies_agree(self):
        scripts = Path(__file__).resolve().parents[3] / "agents" / "platform" / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            import chat_notify
        finally:
            sys.path.remove(str(scripts))
        self.assertEqual(kanban_chat_notify.NOTIFY_PLATFORM_ENV, chat_notify.NOTIFY_PLATFORM_ENV)
        self.assertEqual(kanban_chat_notify.A2A_CLI, chat_notify.A2A_CLI)
        self.assertEqual(kanban_chat_notify.NOTIFY_OUTCOME_UNKNOWN, chat_notify.NOTIFY_OUTCOME_UNKNOWN)
        self.assertEqual(kanban_chat_notify.NOTIFY_ROUTE_UNAVAILABLE, chat_notify.NOTIFY_ROUTE_UNAVAILABLE)
        self.assertEqual(kanban_chat_notify.NOTIFY_WAIT_SECONDS, chat_notify.NOTIFY_WAIT_SECONDS)
        self.assertEqual(kanban_chat_notify.NOTIFY_CONNECT_SECONDS, chat_notify.NOTIFY_CONNECT_SECONDS)
        self.assertEqual(kanban_chat_notify.NOTIFY_MARGIN_SECONDS, chat_notify.NOTIFY_MARGIN_SECONDS)
        self.assertEqual(kanban_chat_notify.SEND_TIMEOUT_SECONDS, chat_notify.NOTIFY_SUBPROCESS_TIMEOUT_SECONDS)

    def test_the_conversation_route_is_spelled_alike_where_it_is_written_and_read(self):
        # session_kv_server writes it, kanban_event_routing addresses the card
        # by it, and this module reads the context id back from it.
        import ast
        import kanban_event_routing
        scripts = Path(__file__).resolve().parents[3] / "agents" / "platform" / "scripts"
        tree = ast.parse((scripts / "session_kv_server.py").read_text())
        consts = {t.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                  for t in node.targets if isinstance(t, ast.Name)}
        self.assertEqual(ast.literal_eval(consts["CONVERSATION_ROUTE_KEY"]), kanban_chat_notify.CONVERSATION_ROUTE_KEY)
        self.assertEqual(kanban_event_routing.CONVERSATION_ROUTE_KEY, kanban_chat_notify.CONVERSATION_ROUTE_KEY)
        self.assertEqual(sorted(ast.literal_eval(consts["CONVERSATION_KEY_PREFIXES"]).values()),
                         sorted(kanban_chat_notify.CONVERSATION_KEY_PREFIXES))
        self.assertEqual(ast.literal_eval(consts["SESSION_KV_DB_PATH"].args[1]) if isinstance(consts["SESSION_KV_DB_PATH"], ast.Call)
                         else None, kanban_chat_notify.SESSION_KV_DEFAULT_DB)


# The upstream shapes the applier anchors on and wraps, as in v2026.9.14.
_NOTIFIER_SOURCE = '''
def _adapter_for_subscription(runner, platform, sub, owner_profile):
    return runner.adapters.get(platform)


class _Collector:
    def _claim_for_sub(self, conn, slug, sub):
        return None

    def __init__(self, runner):
        self.profile_adapters = {}
        self.active_platforms = _platform_names(runner.adapters).union(
            *(_platform_names(m) for m in self.profile_adapters.values()))
'''


class ApplierTest(unittest.TestCase):
    def test_it_wraps_the_resolver_and_the_filter_once(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / apply_kanban_chat_notify.NOTIFIER_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text(_NOTIFIER_SOURCE)
            apply_kanban_chat_notify.apply(Path(root))
            patched = target.read_text()
            self.assertIn("self.active_platforms = _kage_chat_notify_active(", patched)
            self.assertIn("_kage_upstream_adapter_for_subscription = _adapter_for_subscription", patched)
            compile(patched, str(target), "exec")
            with self.assertRaises(SystemExit):
                apply_kanban_chat_notify.apply(Path(root))

    def test_a_drifted_anchor_fails_the_build(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / apply_kanban_chat_notify.NOTIFIER_RELATIVE
            target.parent.mkdir(parents=True)
            target.write_text(_NOTIFIER_SOURCE.replace("self.active_platforms =", "self.served ="))
            with self.assertRaises(SystemExit):
                apply_kanban_chat_notify.apply(Path(root))



@dataclass
class _Ev:
    id: int
    kind: str
    created_at: int


class FoldFanoutTest(unittest.TestCase):
    """A fanned-out child's answer folds into its parent's on the routed path."""

    THREAD = {"platform": "google_chat", "chat_id": "spaces/H", "thread_id": "spaces/H/threads/T"}

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, completed_at INTEGER);"
            "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT, created_at INTEGER);"
            "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
            "CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT, last_event_id INTEGER);"
        )
        self.conn.execute("INSERT INTO kanban_worker_children VALUES ('t_child', 't_parent', 1)")
        for task in ("t_child", "t_parent"):
            self.conn.execute("INSERT INTO kanban_notify_subs VALUES (?, ?, ?, ?, 0)",
                              (task, self.THREAD["platform"], self.THREAD["chat_id"], self.THREAD["thread_id"]))
        self.rewinds = []
        self.rewind_ok = True
        patcher = mock.patch.object(kanban_chat_notify, "_rewind", side_effect=self._rewind)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, ROUTED)
        env.start()
        self.addCleanup(env.stop)

    def _rewind(self, conn, sub, claimed, to):
        self.rewinds.append((claimed, to))
        return self.rewind_ok

    def parent(self, status, completed_at=None):
        self.conn.execute("INSERT OR REPLACE INTO tasks VALUES ('t_parent', ?, ?)", (status, completed_at))

    def claim(self, *events, task="t_child"):
        return {"sub": dict(self.THREAD, task_id=task), "old_cursor": 0, "cursor": events[-1].id, "events": list(events)}

    def fold(self, claim, now=2000):
        return kanban_chat_notify.fold_fanout(self.conn, claim, now=now)

    def test_a_childs_answer_is_dropped_once_its_parent_has_answered(self):
        self.parent("done", completed_at=1500)
        self.assertIsNone(self.fold(self.claim(_Ev(7, "completed", 1400))))
        self.assertEqual(self.rewinds, [], "a dropped answer must leave the cursor advanced, or it posts later")

    def test_a_childs_answer_is_held_while_its_parent_works(self):
        self.parent("running")
        self.assertIsNone(self.fold(self.claim(_Ev(7, "completed", 1900))))
        self.assertEqual(self.rewinds, [(7, 6)], "the hold rewinds to just before the answer")

    def test_events_before_a_held_answer_still_deliver(self):
        self.parent("running")
        got = self.fold(self.claim(_Ev(5, "blocked", 1800), _Ev(7, "completed", 1900)))
        self.assertEqual([e.id for e in got["events"]], [5])
        self.assertEqual(got["cursor"], 6)
        self.assertEqual(self.rewinds, [(7, 6)])

    def test_a_parent_that_fails_releases_its_childs_answer(self):
        # Blocked is where a failed, gave_up, crashed or timed-out card lands.
        self.parent("blocked")
        claim = self.claim(_Ev(7, "completed", 1900))
        self.assertIs(self.fold(claim), claim)
        self.assertEqual(self.rewinds, [])

    def test_a_hold_that_runs_out_posts_the_answer(self):
        self.parent("running")
        created = 2000 - kanban_chat_notify.FOLD_HOLD_SECONDS - 1
        claim = self.claim(_Ev(7, "completed", created))
        self.assertIs(self.fold(claim), claim)

    def test_a_parent_that_finished_first_does_not_swallow_its_childs_answer(self):
        self.parent("done", completed_at=1000)
        claim = self.claim(_Ev(7, "completed", 1900))
        self.assertIs(self.fold(claim), claim)

    def test_a_continuation_child_is_never_folded(self):
        self.conn.execute("INSERT INTO task_links VALUES ('t_parent', 't_child')")
        self.parent("done", completed_at=1500)
        claim = self.claim(_Ev(7, "completed", 1400))
        self.assertIs(self.fold(claim), claim)

    def test_a_parent_on_another_thread_does_not_fold(self):
        self.conn.execute("UPDATE kanban_notify_subs SET thread_id = 'spaces/H/threads/OTHER' WHERE task_id = 't_parent'")
        self.parent("done", completed_at=1500)
        claim = self.claim(_Ev(7, "completed", 1400))
        self.assertIs(self.fold(claim), claim)

    def test_only_completed_events_fold(self):
        self.parent("done", completed_at=1500)
        claim = self.claim(_Ev(6, "gave_up", 1400))
        self.assertIs(self.fold(claim), claim)

    def test_a_failed_hold_delivers_rather_than_loses(self):
        self.parent("running")
        self.rewind_ok = False
        claim = self.claim(_Ev(7, "completed", 1900))
        self.assertEqual([e.id for e in self.fold(claim)["events"]], [7])

    def test_the_hermes_path_is_untouched(self):
        self.parent("done", completed_at=1500)
        claim = self.claim(_Ev(7, "completed", 1400))
        with mock.patch.dict(os.environ, UNROUTED):
            self.assertIs(self.fold(claim), claim)

    def test_no_children_table_delivers_as_upstream(self):
        self.conn.execute("DROP TABLE kanban_worker_children")
        claim = self.claim(_Ev(7, "completed", 1400))
        self.assertIs(self.fold(claim), claim)


if __name__ == "__main__":
    unittest.main()
