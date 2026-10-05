"""Host tests for the KAGE_SLACK_UX button-click patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_ux_clicks.py

The fixture carries the shape of upstream's ``_register_bolt_handlers`` around
the anchor (v2026.9.14). The tests apply the patch, exec both the patched and
the unpatched fixture, and compare the listeners each wires: with the flag off
they must be identical, which is the flag-off identity for this surface. The
runtime module is driven with a stub adapter.
"""

import ast
import asyncio
import importlib
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_ux_clicks as applier
import slack_presenter as presenter
import slack_ux_clicks as runtime
import slack_ux_incident as incident
import verify_slack_ux_clicks as verifier

UPSTREAM = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
import re


def _slack_mention_detection_text(event):
    return event.get("text", "")


def _flag_getter(key):
    def getter(self):
        return False

    return getter


def _channel_set_getter(key):
    def getter(self):
        return set()

    return getter


class App:
    def __init__(self):
        self.listeners = []

    def action(self, matcher):
        def wire(handler):
            self.listeners.append(matcher)
            return handler

        return wire


class SlackAdapter:
    def __init__(self):
        self._app = App()
        self._bot_user_id: str = ""
        self._team_bot_user_ids, self._other = {}, {}

    def _handle_clarify_action(self, ack, body, action):
        return None

    def _register_plugin_action_handlers(self) -> None:
        """Wire plugin handlers."""
        self._app.action("plugin_action")(self._handle_clarify_action)

    def _wire_plugin_handlers(self, app):
        return None

    async def _begin_interaction(self, ack, body, action, kind, *, team_scoped=True):
        await ack()
        team_id = action_id = value = msg_ts = channel_id = user_name = user_id = ""
        message = {}
        return team_id, action_id, value, message, msg_ts, channel_id, user_name, user_id

    def _get_client(self, chat_id, team_id=None):
        return None

    def _client_for(self, chat_id, metadata):
        return None

    def _is_ignored_channel(self, channel_id):
        return False

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        return True

    def _event_declares_bot_sender(self, event: dict) -> bool:
        return False

    def _slack_message_matches_mention_patterns(self, text: str) -> bool:
        return False

    async def _channel_gate_allows(
        self, *, channel_id: str, routing_text: str, bot_uid: str, is_mentioned: bool,
        is_thread_reply: bool, event_thread_ts, user_id: str, team_id: str, is_dm: bool,
        force_process: bool) -> bool:
        return True

    async def _handle_slack_message(self, event, payload=None):
        return bool(event.get("_hermes_force_process"))

    _slack_disable_dms = _flag_getter("disable_dms")
    _slack_allowed_channels = _channel_set_getter("allowed_channels")

    def _register_bolt_handlers(self) -> None:
        """Wire every Bolt listener onto ``self._app``; must run before Socket Mode starts."""
        self._app.action(re.compile(r"^hermes_clarify_choice_\\d+$"))(self._handle_clarify_action)
        self._register_plugin_action_handlers()
        # ctx.register_platform_handler("slack", ...) factories get the full
        # AsyncApp surface (event/action/command), wired before Socket Mode starts.
        self._wire_plugin_handlers(self._app)
'''

CHANNEL = "C1"
TEAM = "T1"
USER = "U1"
MESSAGE_TS = "222.000"
THREAD = "111.000"
ACTION_TS = "333.000"
#: The longest text Slack keeps in a message; a typed reply cannot be longer.
SLACK_TEXT_LIMIT = 40000
#: How long matching a reply that long may take.
FAST_SECONDS = 0.25


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
        shutil.copy(HERE / "slack_ux_clicks.py", gateway / "slack_ux_clicks.py")
        (gateway / "__init__.py").write_text("")

    def load(self, name):
        """Exec the (possibly patched) fixture adapter with ``gateway`` importable."""
        sys.path.insert(0, str(self.dir))
        try:
            sys.modules.pop("gateway", None)
            sys.modules.pop("gateway.slack_ux_clicks", None)
            namespace = {"__name__": name}
            exec(compile((self.dir / applier.RELATIVE).read_text(), name, "exec"), namespace)  # noqa: S102
            return namespace
        finally:
            sys.path.remove(str(self.dir))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _listeners(namespace):
    adapter = namespace["SlackAdapter"]()
    adapter._register_bolt_handlers()
    return adapter._app.listeners


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        text = (self.root.dir / applier.RELATIVE).read_text()
        self.assertEqual(text.count(applier.BUILD_MARKER), 1)
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drifted_anchor_fails_loudly(self):
        path = self.root.dir / applier.RELATIVE
        drifted = UPSTREAM.replace("        self._register_plugin_action_handlers()\n", "")
        path.write_text(drifted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("plugin action handler wiring", str(caught.exception))
        self.assertEqual(path.read_text(), drifted)

    def test_verifier_passes_on_patched_tree(self):
        applier.apply(self.root.dir)
        verifier.main(self.root.dir)

    def test_verifier_refuses_unpatched_tree(self):
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)

    def test_verifier_refuses_an_adapter_missing_what_the_runtime_calls(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        patched = path.read_text()
        returns = "return team_id, action_id, value, message, msg_ts, channel_id, user_name, user_id"
        for old, new, named in (
            ("def _get_client(", "def _client_for(", "_get_client"),
            ("_slack_disable_dms = ", "_disable_dms = ", "_slack_disable_dms"),
            ('event.get("_hermes_force_process")', 'event.get("force")', "_hermes_force_process"),
            ("_slack_allowed_channels = ", "_allowed_channels = ", "_slack_allowed_channels"),
            ("def _handle_slack_message(", "def _handle_message(", "_handle_slack_message"),
            ("def _begin_interaction(", "def _start_interaction(", "_begin_interaction"),
            ("body, action, kind, *", "body, action, source, kind, *", "_begin_interaction takes"),
            (returns, returns.replace("channel_id, user_name", "user_name, channel_id"), "returns"),
            (returns, returns + ", None", "returns"),
            ("def _get_client(self, chat_id, team_id=None)", "def _get_client(self, chat_id, *, team=None)",
             "_get_client no longer accepts"),
            ("*, team_scoped=True)", "*, team_scoped)", "_begin_interaction requires a keyword"),
            ("def _client_for(", "def _workspace_client(", "_client_for"),
            ("def _client_for(self, chat_id, metadata)", "def _client_for(self, chat_id)",
             "_client_for no longer accepts"),
            ("    def _client_for(", "    async def _client_for(", "_client_for is now async"),
            ("def _is_ignored_channel(", "def _ignored(", "_is_ignored_channel"),
            ("def _is_interactive_user_authorized(", "def _is_user_authorized(", "_is_interactive_user_authorized"),
            ("user_name=None, team_id=\"\")", "user_name=None)", "_is_interactive_user_authorized no longer accepts"),
            ("def _channel_gate_allows(", "def _channel_gate(", "_channel_gate_allows"),
            ("    async def _channel_gate_allows(", "    def _channel_gate_allows(",
             "_channel_gate_allows is no longer async"),
            ("is_dm: bool,\n        force_process: bool)", "is_dm: bool)", "_channel_gate_allows no longer accepts"),
            ("def _slack_message_matches_mention_patterns(", "def _mention_patterns(",
             "_slack_message_matches_mention_patterns"),
            ("def _event_declares_bot_sender(", "def _declares_bot(", "_event_declares_bot_sender"),
            ("def _event_declares_bot_sender(self, event: dict)", "def _event_declares_bot_sender(self)",
             "_event_declares_bot_sender no longer accepts"),
            ("    def _event_declares_bot_sender(", "    async def _event_declares_bot_sender(",
             "_event_declares_bot_sender is now async"),
            ("def _slack_mention_detection_text(", "def _mention_text(", "_slack_mention_detection_text"),
            ("def _slack_mention_detection_text(event)", "def _slack_mention_detection_text(event, bot_uid)",
             "_slack_mention_detection_text no longer accepts"),
            ("def _slack_mention_detection_text(", "async def _slack_mention_detection_text(",
             "_slack_mention_detection_text is now async"),
            ("        self._bot_user_id: str = \"\"\n", "", "no longer sets _bot_user_id"),
            ("self._team_bot_user_ids, self._other = {}, {}", "self._bot_ids, self._other = {}, {}",
             "no longer sets _team_bot_user_ids"),
            ("def _is_ignored_channel(self, channel_id)", "def _is_ignored_channel(self)",
             "_is_ignored_channel no longer accepts"),
            ("    def _is_ignored_channel(", "    async def _is_ignored_channel(",
             "_is_ignored_channel is now async"),
            ("async def _handle_slack_message(", "def _handle_slack_message(",
             "_handle_slack_message is no longer async"),
            ("def _get_client(self, chat_id, team_id=None)", "def _get_client(self, chat_id, team_id=None, /)",
             "_get_client no longer accepts 1 positional argument(s) and ('team_id',)"),
            ("    def _get_client(", "    @property\n    def _get_client(", "_get_client is no longer a method"),
            ("def _handle_slack_message(self, event, payload=None)",
             "def _handle_slack_message(self, event, payload)", "_handle_slack_message no longer accepts"),
            ('_slack_disable_dms = _flag_getter("disable_dms")', "_slack_disable_dms = property(bool)",
             "_slack_disable_dms is no longer a method"),
            ("    def getter(self):\n        return False", "    def getter(self, extra):\n        return False",
             "_slack_disable_dms no longer accepts"),
            ("    def getter(self):\n        return False", "    async def getter(self):\n        return False",
             "_slack_disable_dms is now async"),
            ("    def getter(self):\n        return set()", "    async def getter(self):\n        return set()",
             "_slack_allowed_channels is now async"),
        ):
            with self.subTest(named=named, new=new):
                self.assertEqual(patched.count(old), 1, old)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn(named, str(caught.exception))

    def test_a_required_parameter_the_runtime_passes_by_keyword_is_accepted(self):
        args = ast.parse("def _get_client(self, chat_id, team_id): pass").body[0].args
        self.assertTrue(verifier._accepts(args, 1, ("team_id",)))
        self.assertFalse(verifier._accepts(args, 1, ()))
        posonly = ast.parse("def _get_client(self, chat_id, team_id=None, /): pass").body[0].args
        self.assertFalse(verifier._accepts(posonly, 1, ("team_id",)))
        swapped = ast.parse("def _get_client(self, team_id, chat_id=None, **kwargs): pass").body[0].args
        self.assertFalse(verifier._accepts(swapped, 1, ("team_id",)))
        swallowed = ast.parse("def _get_client(self, chat_id, team_id=None, /, **kwargs): pass").body[0].args
        self.assertFalse(verifier._accepts(swallowed, 1, ("team_id",)))
        annotated = ast.parse("def _get_client(self, chat_id: str, team_id: Opt[str] = UNSET): pass").body[0].args
        self.assertTrue(verifier._accepts(annotated, 1, ("team_id",)))
        self.assertTrue(verifier._accepts(annotated, 1, ()))

    def test_verifier_refuses_an_adapter_whose_bolt_app_is_renamed(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RELATIVE
        path.write_text(path.read_text().replace("self._app", "self._bolt_app"))
        with self.assertRaises(SystemExit) as caught:
            verifier.main(self.root.dir)
        self.assertIn("self._app", str(caught.exception))


class FlagOffIdentityTest(unittest.TestCase):
    """With KAGE_SLACK_UX off the patched adapter wires exactly upstream's listeners."""

    def test_identical_to_upstream(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        upstream = root.load("upstream_fixture")
        applier.apply(root.dir)
        patched = root.load("patched_fixture")
        for value in (None, "", "0", "false"):
            with self.subTest(flag=value), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("KAGE_SLACK_UX", None)
                if value is not None:
                    os.environ["KAGE_SLACK_UX"] = value
                self.assertEqual(_listeners(patched), _listeners(upstream))

    def test_flag_on_registers_after_the_plugin_handlers(self):
        root = _Root()
        self.addCleanup(root.cleanup)
        applier.apply(root.dir)
        patched = root.load("patched_fixture_on")
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"}):
            listeners = _listeners(patched)
        self.assertEqual(listeners[1], "plugin_action")
        self.assertEqual(
            listeners[2:],
            [presenter.CHOICE_ACTION_ID_PATTERN, presenter.LINK_ACTION_ID_PATTERN],
        )


class ActionIdTest(unittest.TestCase):
    def test_choice_pattern_matches_the_presenters_choice_buttons_only(self):
        ids = [
            f"triage.{presenter.LINK_ACTION}.0",
            f"triage.{presenter.CHOICE_ACTION}.0",
            f"triage.{presenter.CHOICE_ACTION}.1",
        ]
        self.assertEqual(
            [bool(presenter.CHOICE_ACTION_ID_PATTERN.search(i)) for i in ids], [False, True, True]
        )
        for other in ("hermes_clarify_choice_0", "hermes_approve_once"):
            self.assertIsNone(presenter.CHOICE_ACTION_ID_PATTERN.search(other))


class _Client:
    def __init__(self, log, fail=(), replies=()):
        self.log = log
        self.fail = fail
        self.replies = replies

    async def conversations_replies(self, **kwargs):
        if "conversations_replies" in self.fail:
            raise RuntimeError("read refused")
        self.log.append(("conversations_replies", kwargs))
        await asyncio.sleep(0)  # a real read yields, so a second click can land during it
        return {"ok": True, "messages": list(self.replies)}

    async def chat_update(self, **kwargs):
        if "chat_update" in self.fail:
            raise RuntimeError("update refused")
        self.log.append(("chat_update", kwargs))

    async def chat_postMessage(self, **kwargs):
        if "chat_postMessage" in self.fail:
            raise RuntimeError("post refused")
        self.log.append(("chat_postMessage", kwargs))


def _slack_mention_detection_text(event):
    """Stands in for adapter.py's module function, found through ``_Adapter``'s module: the flat
    text plus a mention only in the blocks."""
    def users(node):
        if isinstance(node, list):
            return [u for n in node for u in users(n)]
        if not isinstance(node, dict):
            return []
        own = [f"<@{node['user_id']}>"] if node.get("type") == "user" else []
        return own + users(node.get("elements", []))

    flat = event.get("text", "") or ""
    extra = [m for m in users(event.get("blocks") or []) if m not in flat]
    return (flat.strip() + "\n" + " ".join(extra)).strip() if extra else flat


class _Adapter:
    def __init__(
        self, authorized=True, fail=(), allowed_channels=(), disable_dms=False, ignored=(), replies=(), unlisted=(),
        unheard=(), ignore_other_user_mentions=False, broken=(), api_human_users=(),
    ):
        self.broken = set(broken)
        self.api_human_users = frozenset(api_human_users)
        self.ignore_other_user_mentions = ignore_other_user_mentions
        self.authorized = authorized
        self.unlisted = set(unlisted)
        self.asked = []
        self.unheard = set(unheard)
        self.gated = []
        self._bot_user_id = "U0BOT"
        self._team_bot_user_ids = {TEAM: "U0TEAMBOT"}
        self.ignored = set(ignored)
        self.replies = replies
        self.log = []
        self.acks = 0
        self.fail = fail
        self.allowed_channels = set(allowed_channels)
        self.disable_dms = disable_dms

    def _is_ignored_channel(self, channel_id):
        return channel_id in self.ignored

    def _slack_allowed_channels(self):
        return self.allowed_channels

    def _slack_disable_dms(self):
        return self.disable_dms

    async def _begin_interaction(self, ack, body, action, kind, *, team_scoped=True):
        await ack()
        self.kind = kind
        if not self.authorized:
            return None
        message = body.get("message", {})
        return (
            TEAM, action.get("action_id", ""), action.get("value", ""), message, message.get("ts", ""),
            CHANNEL, "someone", USER,
        )

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.log, self.fail, self.replies)

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        self.asked.append((user_id, channel_id, team_id))
        if "authorized" in self.broken and user_id != USER:
            raise RuntimeError("allowlist unreadable")
        return user_id not in self.unlisted

    def _event_declares_bot_sender(self, event):
        # Upstream's, with _slack_api_human_users read from api_human_users.
        if "bot" in self.broken:
            raise RuntimeError("bot check failed")
        if event.get("bot_id") or event.get("bot_profile") or event.get("subtype") == "bot_message":
            return True
        if (event.get("user_profile") or {}).get("is_bot"):
            return True
        if event.get("app_id") and not event.get("client_msg_id"):
            return event.get("user") not in self.api_human_users
        return False

    def _slack_message_matches_mention_patterns(self, text):
        if "patterns" in self.broken:
            raise RuntimeError("bad pattern")
        return "@kage" in text.lower()

    async def _channel_gate_allows(self, **gate):
        self.gated.append(gate)
        if "gate" in self.broken:
            raise RuntimeError("gate failed")
        # Upstream's ignore_other_user_mentions rule: un-mentioned and opening on someone else is not for us.
        lead = re.match(r"\s*<@([^>|\s]+)(?:\|[^>]*)?>", gate["routing_text"])
        if self.ignore_other_user_mentions and not gate["is_mentioned"] and lead and lead.group(1) != gate["bot_uid"]:
            return False
        return gate["user_id"] not in self.unheard

    async def _handle_slack_message(self, event, payload=None):
        self.log.append(("message", event))


def _message(thread=THREAD):
    message = {
        "ts": MESSAGE_TS,
        "text": "fallback",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Raise the limit?*"}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "action_id": "kage.link.0", "url": "https://x"},
                    {"type": "button", "action_id": "kage.choice.0", "value": "Raise to 512Mi"},
                    {"type": "button", "action_id": "kage.choice.1", "value": "Leave it"},
                ],
            },
        ],
    }
    if thread:
        message["thread_ts"] = thread
    return message


def _alert_choice(index, value):
    """A click on an incident alert's option, on the alert itself: the thread's parent."""
    return _choice(index, value, prefix=incident.ACTION_PREFIX, thread=None)


def _choice(index=1, value="Leave it", shown=None, prefix="kage", **message_kwargs):
    action = {
        "action_id": f"{prefix}.choice.{index}",
        "text": {"type": "plain_text", "text": value if shown is None else shown, "emoji": True},
        "value": value,
        "action_ts": ACTION_TS,
    }
    return {"message": _message(**message_kwargs)}, action


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)
        patcher = mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ack(self, adapter):
        async def ack():
            adapter.acks += 1

        return ack

    def _answer(self, adapter, body, action, kind=runtime.CHOICE_KIND):
        _run(runtime.answer(adapter, self._ack(adapter), body, action, kind))

    def test_choice_is_the_clickers_turn_shown_once_on_the_message(self):
        adapter = _Adapter()
        with self.assertNoLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "message"])
        update, turn = (entry[1] for entry in adapter.log)
        self.assertEqual((update["channel"], update["ts"]), (CHANNEL, MESSAGE_TS))
        actions = [b for b in update["blocks"] if b["type"] == "actions"]
        self.assertEqual(
            [[e["action_id"] for e in b["elements"]] for b in actions], [["kage.link.0"]]
        )
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], "✓ <@U1>: Leave it")
        # The note leads the text; the message's own text stays under it for a later thread read.
        self.assertEqual(update["text"], "✓ <@U1>: Leave it\n\nfallback")
        self.assertEqual(
            turn,
            {
                "type": "message", "user": USER, "text": "Leave it", "channel": CHANNEL, "ts": ACTION_TS,
                "thread_ts": THREAD, "_hermes_force_process": True, "team": TEAM,
            },
        )
        self.assertEqual(adapter.acks, 1)

    def test_a_click_on_a_cards_question_names_the_card(self):
        moments = SimpleNamespace(
            question_card=lambda channel, ts: "t_e0c1" if (channel, ts) == (CHANNEL, MESSAGE_TS) else None
        )
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_moments=moments), "gateway.slack_ux_moments": moments}):
            adapter = _Adapter()
            self._answer(adapter, *_choice())
        turn = adapter.log[-1][1]
        self.assertEqual(turn["text"], "Leave it\n\n" + runtime.CARD_NOTE.format(card="t_e0c1"))

    def test_a_clicked_session_is_titled_from_the_label_not_the_card_note(self):
        adapter = _Adapter()
        status = SimpleNamespace(note_ask=lambda chat, thread, text: adapter.log.append(("note_ask", (chat, thread, text))))
        moments = SimpleNamespace(question_card=lambda channel, ts: "t_e0c1")
        gateway = SimpleNamespace(slack_ux_moments=moments, slack_ux_status=status)
        modules = {"gateway": gateway, "gateway.slack_ux_moments": moments, "gateway.slack_ux_status": status}
        with mock.patch.dict(sys.modules, modules):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "note_ask", "message"])
        self.assertEqual(adapter.log[1][1], (CHANNEL, THREAD, "Leave it"))

    def test_a_clicked_dm_thread_is_titled_from_the_label(self):
        adapter = _Adapter()
        titles = []

        async def set_title(channel, thread, title, team_id=None):
            titles.append((channel, thread, title, team_id))

        adapter._set_assistant_thread_title = set_title
        asks = []
        status = SimpleNamespace(note_ask=lambda *args: asks.append(args))
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_status=status), "gateway.slack_ux_status": status}):
            _run(runtime._title_from_label(adapter, "D1", TEAM, THREAD, "Leave it"))
            _run(runtime._title_from_label(adapter, CHANNEL, TEAM, THREAD, "Leave it"))
        self.assertEqual(titles, [("D1", THREAD, "Leave it", TEAM)])
        # A DM's title is upstream's, set once; offering the label as its ask would rename it.
        self.assertEqual(asks, [(CHANNEL, THREAD, "Leave it")])

    def test_a_title_failure_still_runs_the_turn(self):
        def broken(*args):
            raise RuntimeError("status unavailable")

        status = SimpleNamespace(note_ask=broken)
        adapter = _Adapter()
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_status=status), "gateway.slack_ux_status": status}):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "message"])

    def test_the_card_is_looked_up_before_the_rewrite(self):
        cards = {(CHANNEL, MESSAGE_TS): "t_e0c1"}
        moments = SimpleNamespace(question_card=lambda channel, ts: cards.get((channel, ts)))
        adapter = _Adapter()
        client = _Client(adapter.log)
        update = client.chat_update

        async def settle_then_update(**kwargs):
            cards.clear()  # the card moved on and its question was settled meanwhile
            await update(**kwargs)

        client.chat_update = settle_then_update
        adapter._get_client = lambda chat_id, team_id=None: client
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_moments=moments), "gateway.slack_ux_moments": moments}):
            self._answer(adapter, *_choice())
        self.assertEqual(adapter.log[-1][1]["text"], "Leave it\n\n" + runtime.CARD_NOTE.format(card="t_e0c1"))

    def test_a_click_on_any_other_message_is_the_label_alone(self):
        moments = SimpleNamespace(question_card=lambda channel, ts: None)
        for modules in (
            {"gateway": SimpleNamespace(slack_ux_moments=moments), "gateway.slack_ux_moments": moments},
            {"gateway": None, "gateway.slack_ux_moments": None},
        ):
            with self.subTest(modules=modules), mock.patch.dict(sys.modules, modules):
                runtime._answered.clear()
                adapter = _Adapter()
                self._answer(adapter, *_choice())
                self.assertEqual(adapter.log[-1][1]["text"], "Leave it")

    def test_unlisted_user_changes_nothing(self):
        adapter = _Adapter(authorized=False)
        self._answer(adapter, *_choice())
        self.assertEqual(adapter.log, [])
        self.assertEqual(adapter.acks, 1)
        # And the message is still answerable by someone who is listed.
        listed = _Adapter()
        self._answer(listed, *_choice())
        self.assertEqual(len(listed.log), 2)

    def test_a_message_is_answered_once(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(1, "Leave it"))
        self._answer(adapter, *_choice(0, "Raise to 512Mi"))
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Leave it"])

    def test_answered_reports_a_clicked_message_only(self):
        self.assertFalse(runtime.answered(CHANNEL, MESSAGE_TS))
        self._answer(_Adapter(authorized=False), *_choice())
        self.assertFalse(runtime.answered(CHANNEL, MESSAGE_TS), "an unlisted click answered it")
        self._answer(_Adapter(), *_choice())
        self.assertTrue(runtime.answered(CHANNEL, MESSAGE_TS))
        self.assertFalse(runtime.answered("C0OTHER", MESSAGE_TS))

    def test_a_failed_rewrite_is_not_reported_answered(self):
        adapter = _Adapter(fail=("chat_update",))
        with self.assertLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice(1, "Leave it"))
        self.assertFalse(runtime.answered(CHANNEL, MESSAGE_TS), "a question the click did not rewrite reads settled")

    def test_two_clicks_at_once_run_one_turn(self):
        adapter = _Adapter()

        async def both():
            release = asyncio.Event()
            client = _Client(adapter.log)
            update = client.chat_update

            async def held(**kwargs):
                await release.wait()
                await update(**kwargs)

            client.chat_update = held
            adapter._get_client = lambda chat_id, team_id=None: client
            clicks = [
                asyncio.ensure_future(runtime.answer(adapter, self._ack(adapter), *_choice(1, "Leave it"), runtime.CHOICE_KIND)),
                asyncio.ensure_future(runtime.answer(adapter, self._ack(adapter), *_choice(0, "Raise to 512Mi"), runtime.CHOICE_KIND)),
            ]
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(adapter.log, [], "the first click's rewrite was not held")
            release.set()
            await asyncio.gather(*clicks)

        _run(both())
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Leave it"])
        self.assertEqual(adapter.acks, 2)

    def test_label_is_escaped_in_what_slack_shows_but_not_in_the_turn(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(value="<!channel> & go"))
        update, turn = (entry[1] for entry in adapter.log)
        self.assertNotIn("<!channel>", update["blocks"][-1]["elements"][0]["text"])
        self.assertEqual(turn["text"], "<!channel> & go")

    def test_slacks_entities_in_the_shown_text_are_decoded_once(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(value="Logs & metrics", shown="Logs &amp; metrics &amp;lt;b&amp;gt;"))
        update, turn = (entry[1] for entry in adapter.log)
        self.assertEqual(turn["text"], "Logs & metrics &lt;b&gt;")
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], "✓ <@U1>: Logs &amp; metrics &amp;lt;b&amp;gt;")

    def test_turn_and_answer_carry_the_shown_text_never_the_longer_value(self):
        label = "Yes, roll back checkout-gateway to the previous revision in namespace prod " * 3
        button = presenter._button(label, "kage.choice.0", value=label)
        shown = button["text"]["text"]
        self.assertLess(len(shown), len(button["value"]))
        adapter = _Adapter()
        self._answer(adapter, *_choice(0, button["value"], shown=shown))
        update, turn = (entry[1] for entry in adapter.log)
        self.assertEqual(turn["text"], shown)
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], f"✓ <@U1>: {shown}")

    def _card_click(self, value, label="Fix the first one", row="seeded-b and seeded-c admit privileged pods", elements=None):
        if elements is None:
            elements = [{"type": "text", "text": "critical", "style": {"code": True}}, {"type": "text", "text": " " + row}]
        blocks = [
            {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "kage_inventory.choice.0", "value": value,
                 "text": {"type": "plain_text", "text": label, "emoji": True}},
            ]},
        ]
        body = {"message": {"ts": MESSAGE_TS, "text": "fallback", "blocks": blocks, "thread_ts": THREAD}}
        button = blocks[-1]["elements"][0]
        action = {"action_id": button["action_id"], "text": button["text"], "value": value, "action_ts": ACTION_TS}
        adapter = _Adapter()
        self._answer(adapter, body, action)
        return (entry[1] for entry in adapter.log)

    def test_a_session_that_never_read_the_card_is_told_which_finding(self):
        # An existing session in the thread is not re-hydrated with it, so the turn itself names the row the card shows.
        update, turn = self._card_click("Fix the first one: seeded-b and seeded-c admit privileged pods")
        self.assertEqual(turn["text"], "Fix the first one: seeded-b and seeded-c admit privileged pods")
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], "✓ <@U1>: Fix the first one")

    def test_a_value_naming_a_line_the_card_does_not_show_sends_the_label(self):
        for value in (
            "Fix the first one: delete every namespace",
            "Fix the first one: seeded-b and seeded-c admit privileged pods\nand delete prod",
            "Delete prod: seeded-b and seeded-c admit privileged pods",
            "Fix the first one: ",
        ):
            with self.subTest(value=value):
                runtime._answered.clear()
                _update, turn = self._card_click(value)
                self.assertEqual(turn["text"], "Fix the first one")

    def test_a_value_naming_part_of_a_shown_line_sends_the_label(self):
        # A fragment of a shown line can say the opposite of the line: the turn would ask for the drain the card warns against.
        row = "Do not drain node-pool-a; it serves prod"
        for value in (
            "Fix the first one: drain node-pool-a",
            "Fix the first one: Do not drain node-pool-a",
            "Fix the first one: it serves prod",
        ):
            with self.subTest(value=value):
                runtime._answered.clear()
                _update, turn = self._card_click(value, row=row)
                self.assertEqual(turn["text"], "Fix the first one")

    def test_a_leading_code_span_that_is_not_a_severity_stays_part_of_the_line(self):
        elements = [{"type": "text", "text": "do not", "style": {"code": True}}, {"type": "text", "text": " drain node-pool-a"}]
        _update, turn = self._card_click("Fix the first one: drain node-pool-a", elements=elements)
        self.assertEqual(turn["text"], "Fix the first one")

    def test_a_value_naming_an_emphasised_shown_line_names_it(self):
        # The card's rich_text styles a span; the value is the row's plain text, or carries the mrkdwn markers itself.
        styled = [
            {"type": "text", "text": "critical", "style": {"code": True}},
            {"type": "text", "text": " Pods admit "},
            {"type": "text", "text": "privileged", "style": {"italic": True}},
            {"type": "text", "text": " containers  in seeded-b"},
            {"type": "text", "text": "\n"},
            {"type": "text", "text": "Detail one."},
        ]
        marked = [{"type": "text", "text": "Pods admit *privileged* containers in ~seeded-b~"}, {"type": "text", "text": "\nDetail one."}]
        plain = "Fix the first one: Pods admit privileged containers in seeded-b"
        for elements, value, sent in (
            (styled, plain, plain),
            (styled, "Fix the first one: Pods admit _privileged_ containers in `seeded-b`", plain),
            (marked, plain, "Fix the first one: Pods admit *privileged* containers in ~seeded-b~"),
        ):
            with self.subTest(value=value, elements=elements):
                runtime._answered.clear()
                _update, turn = self._card_click(value, elements=elements)
                self.assertEqual(turn["text"], sent)

    def test_the_turn_names_the_line_as_shown_not_the_values_markup(self):
        # The match ignores markup, so the value's own would reach the agent: a strikethrough cancelling "Do not".
        _update, turn = self._card_click("Fix the first one: ~Do not~  drain node-pool-a", row="Do not drain node-pool-a")
        self.assertEqual(turn["text"], "Fix the first one: Do not drain node-pool-a")

    def test_a_line_with_struck_text_sends_the_label(self):
        # The turn carries a line as plain text, so a struck "Do not" would reach the agent unstruck.
        struck = [{"type": "text", "text": "Do not", "style": {"strike": True}}, {"type": "text", "text": " drain node-pool-a"}]
        row = [{"type": "text", "text": "critical", "style": {"code": True}}] + struck
        for elements, value in (
            (struck, "Fix the first one: Do not drain node-pool-a"),
            (struck, "Fix the first one: drain node-pool-a"),
            (row, "Fix the first one: Do not drain node-pool-a"),
        ):
            with self.subTest(value=value, elements=elements):
                runtime._answered.clear()
                _update, turn = self._card_click(value, elements=elements)
                self.assertEqual(turn["text"], "Fix the first one")

    def test_a_line_holding_an_element_with_no_text_sends_the_label(self):
        # A mention, an emoji or a bare link shows something its text does not hold; joining the rest drops it.
        for element, value in (
            ({"type": "user", "user_id": "U9"}, "Fix the first one: Page  before draining"),
            ({"type": "channel", "channel_id": "C9"}, "Fix the first one: Page  before draining"),
            ({"type": "emoji", "name": "no_entry"}, "Fix the first one: Page  before draining"),
            ({"type": "link", "url": "https://x/keep"}, "Fix the first one: Page  before draining"),
        ):
            elements = [{"type": "text", "text": "Page "}, element, {"type": "text", "text": " before draining"}]
            with self.subTest(element=element):
                runtime._answered.clear()
                _update, turn = self._card_click(value, elements=elements)
                self.assertEqual(turn["text"], "Fix the first one")

    def test_struck_or_textless_elements_leave_the_other_lines_matchable(self):
        elements = [
            {"type": "text", "text": "Pods admit privileged containers"},
            {"type": "link", "url": "https://x/runbook", "text": " (runbook)"},
            {"type": "text", "text": "\n"},
            {"type": "text", "text": "old note", "style": {"strike": True}},
            {"type": "user", "user_id": "U9"},
        ]
        _update, turn = self._card_click("Fix the first one: Pods admit privileged containers (runbook)", elements=elements)
        self.assertEqual(turn["text"], "Fix the first one: Pods admit privileged containers (runbook)")

    def test_a_cards_question_with_a_value_names_both_the_finding_and_the_card(self):
        moments = SimpleNamespace(question_card=lambda channel, ts: "t_e0c1" if (channel, ts) == (CHANNEL, MESSAGE_TS) else None)
        with mock.patch.dict(sys.modules, {"gateway": SimpleNamespace(slack_ux_moments=moments), "gateway.slack_ux_moments": moments}):
            _update, turn = self._card_click("Fix the first one: seeded-b and seeded-c admit privileged pods")
        self.assertEqual(
            turn["text"],
            "Fix the first one: seeded-b and seeded-c admit privileged pods\n\n" + runtime.CARD_NOTE.format(card="t_e0c1"),
        )

    def test_a_click_with_no_shown_text_does_nothing(self):
        adapter = _Adapter()
        body, action = _choice()
        del action["text"]
        with self.assertLogs(runtime.logger, level="WARNING") as logs:
            self._answer(adapter, body, action)
        self.assertEqual(adapter.log, [])
        self.assertTrue(any("dropping a kage choice click" in line and "no button text" in line for line in logs.output))

    def test_command_shaped_label_is_an_answer_not_a_command(self):
        for label in ("/stop", "!approve"):
            with self.subTest(label=label):
                importlib.reload(runtime)
                adapter = _Adapter()
                self._answer(adapter, *_choice(value=label))
                turn = adapter.log[-1][1]
                self.assertEqual(turn["text"], runtime.COMMAND_GUARD + label)
                self.assertFalse(turn["text"].lstrip().startswith(runtime.COMMAND_PREFIXES))
                self.assertEqual(adapter.log[0][1]["blocks"][-1]["elements"][0]["text"], f"✓ <@U1>: {label}")

    def test_click_where_a_typed_message_is_ignored_changes_nothing(self):
        cases = {
            "outside allowed_channels": _Adapter(allowed_channels={"C2"}),
            "dm with dms disabled": _Adapter(disable_dms=True),
            "an ignored channel": _Adapter(ignored={CHANNEL}),
        }
        for name, adapter in cases.items():
            with self.subTest(name):
                importlib.reload(runtime)
                if name.startswith("dm"):
                    adapter._begin_interaction = self._dm_begin(adapter)
                self._answer(adapter, *_choice())
                self.assertEqual(adapter.log, [])
        # A channel on the list still answers.
        listed = _Adapter(allowed_channels={CHANNEL})
        self._answer(listed, *_choice())
        self.assertEqual(len(listed.log), 2)

    def test_allowed_channels_gates_channels_and_group_dms_but_not_a_one_to_one_dm(self):
        # Upstream's message handler skips the channel gate for an im; DMs are disable_dms's.
        adapter = _Adapter(allowed_channels={"C2"})
        adapter._begin_interaction = self._dm_begin(adapter)
        self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "message"])
        self.assertEqual(adapter.log[-1][1]["channel"], "D1")
        for name, channel in (("a channel", CHANNEL), ("a group dm", "mpdm-alice--bob--kage-1")):
            with self.subTest(name):
                importlib.reload(runtime)
                outside = _Adapter(allowed_channels={"C2"})
                body, action = _choice()
                if channel.startswith("mpdm-"):
                    body["channel"] = {"id": CHANNEL, "name": channel}
                self._answer(outside, body, action)
                self.assertEqual(outside.log, [])

    def test_a_click_in_a_group_dm_with_dms_disabled_changes_nothing(self):
        # The gateway ignores an mpim as it does an im when DMs are disabled.
        for disable_dms, calls in ((True, 0), (False, 2)):
            with self.subTest(disable_dms=disable_dms):
                importlib.reload(runtime)
                adapter = _Adapter(disable_dms=disable_dms)
                body, action = _choice()
                body["channel"] = {"id": CHANNEL, "name": "mpdm-alice--bob--kage-1"}
                self._answer(adapter, body, action)
                self.assertEqual(len(adapter.log), calls)

    def _dm_begin(self, adapter):
        async def begin(ack, body, action, kind, *, team_scoped=True):
            await ack()
            message = body.get("message", {})
            return (TEAM, action["action_id"], action["value"], message, message["ts"], "D1", "someone", USER)

        return begin

    def test_top_level_message_threads_under_itself(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(thread=None))
        self.assertEqual(adapter.log[1][1]["thread_ts"], MESSAGE_TS)

    def test_a_failed_rewrite_posts_the_echo_instead(self):
        adapter = _Adapter(fail=("chat_update",))
        with self.assertLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_postMessage", "message"])
        self.assertEqual(adapter.log[0][1], {"channel": CHANNEL, "thread_ts": THREAD, "text": "↳ <@U1>: Leave it"})

    def test_failed_rewrite_and_echo_still_run_the_turn(self):
        adapter = _Adapter(fail=("chat_update", "chat_postMessage"))
        with self.assertLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["message"])

    def test_buttons_left_by_a_failed_rewrite_do_not_run_a_second_turn(self):
        adapter = _Adapter(fail=("chat_update",))
        with self.assertLogs(runtime.logger, level="INFO") as logs:
            self._answer(adapter, *_choice(1, "Leave it"))
            self._answer(adapter, *_choice(0, "Raise to 512Mi"))
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Leave it"])
        self.assertTrue(any("dropping a second" in line for line in logs.output))

    def test_empty_label_does_nothing(self):
        adapter = _Adapter()
        with self.assertLogs(runtime.logger, level="WARNING") as logs:
            self._answer(adapter, *_choice(value="  "))
        self.assertEqual(adapter.log, [])
        self.assertTrue(any("with no button text" in line for line in logs.output))

    def test_link_click_is_acked_only(self):
        acks = []

        async def ack():
            acks.append(True)

        _run(presenter.ack_link_click(ack, {}, {"action_id": "kage.link.0"}))
        self.assertEqual(acks, [True])

    def _incident(self, adapter, value="Apply Option B", edited=None, channel_name=None):
        body, action = _alert_choice(1, value)
        if edited:
            body["message"]["edited"] = {"user": "B1", "ts": edited}
        if channel_name is not None:
            body["channel"] = {"id": CHANNEL, "name": channel_name}
        self._answer(adapter, body, action)

    def test_the_incident_prefix_is_the_one_the_alert_buttons_carry(self):
        self.assertEqual(
            runtime.INCIDENT_CHOICE_PREFIX, f"{incident.ACTION_PREFIX}.{presenter.CHOICE_ACTION}."
        )

    def test_a_typed_apply_strikes_the_buttons_and_drops_the_click(self):
        typed = {"type": "message", "user": "U2", "text": "apply Option B", "ts": "223.000"}
        adapter = _Adapter(replies=[{"type": "message", "bot_id": "B1", "text": "alert", "ts": MESSAGE_TS}, typed])
        self._incident(adapter)
        self.assertEqual([entry[0] for entry in adapter.log], ["conversations_replies", "chat_update"])
        read, update = (entry[1] for entry in adapter.log)
        self.assertEqual((read["channel"], read["ts"], read["oldest"]), (CHANNEL, MESSAGE_TS, MESSAGE_TS))
        self.assertEqual(adapter.asked, [("U2", CHANNEL, TEAM)])
        self.assertFalse([b for b in update["blocks"] if b["type"] == "actions" and len(b["elements"]) > 1])
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], runtime.ANSWERED_IN_THREAD)
        # A second click on the struck message does not read or run either.
        self._incident(adapter, "Apply Option A")
        self.assertEqual(len(adapter.log), 2)

    def test_a_typed_apply_of_another_option_drops_the_click_too(self):
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "apply A", "ts": "223.000"}])
        self._incident(adapter, "Apply Option B")
        self._drops(adapter)
        update = adapter.log[1][1]
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], runtime.ANSWERED_IN_THREAD)

    def test_typed_apply_is_matched_after_a_leading_mention_and_in_any_case(self):
        for text in ("<@U0BOT> apply Option B", "  Apply option b"):
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._incident(adapter)
                self.assertNotIn("message", [entry[0] for entry in adapter.log])

    def test_what_is_not_a_human_typing_apply_after_the_alert_does_not_drop_the_click(self):
        cases = {
            "a bot": {"type": "message", "user": "U9", "bot_id": "B1", "text": "apply Option B", "ts": "223.000"},
            "a subtype": {"type": "message", "subtype": "bot_message", "user": "U2", "text": "apply B", "ts": "223.000"},
            "a bot profile": {"type": "message", "user": "U2", "bot_profile": {"id": "B1"}, "text": "apply B", "ts": "223.000"},
            "a bot user": {"type": "message", "user": "U2", "user_profile": {"is_bot": True}, "text": "apply B", "ts": "223.000"},
            "an app's post": {"type": "message", "user": "U2", "app_id": "A1", "text": "apply B", "ts": "223.000"},
            "a deleted reply": {"type": "message", "subtype": "message_deleted", "user": "U2", "text": "apply B", "ts": "223.000"},
            "before the alert": {"type": "message", "user": "U2", "text": "apply Option A", "ts": "221.000"},
            "the alert itself": {"type": "message", "user": "U2", "text": "apply", "ts": MESSAGE_TS},
            "apply mid-sentence": {"type": "message", "user": "U2", "text": "should we apply B?", "ts": "223.000"},
            "a longer word": {"type": "message", "user": "U2", "text": "applying B now", "ts": "223.000"},
        }
        for name, reply in cases.items():
            with self.subTest(name):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[reply])
                self._incident(adapter)
                self.assertEqual(
                    [entry[0] for entry in adapter.log],
                    ["conversations_replies", "chat_update", "message"],
                )

    def _runs(self, adapter):
        self.assertEqual([entry[0] for entry in adapter.log], ["conversations_replies", "chat_update", "message"])

    def _drops(self, adapter):
        self.assertEqual([entry[0] for entry in adapter.log], ["conversations_replies", "chat_update"])

    def test_a_typed_apply_from_someone_the_adapter_ignores_does_not_drop_the_click(self):
        adapter = _Adapter(
            replies=[{"type": "message", "user": "U3", "text": "apply Option B", "ts": "223.000"}], unlisted={"U3"},
        )
        self._incident(adapter)
        self._runs(adapter)
        self.assertEqual(adapter.asked, [("U3", CHANNEL, TEAM)])
        self.assertEqual(adapter.gated, [])

    def test_a_typed_apply_the_gateway_would_not_hear_does_not_drop_the_click(self):
        # A channel that requires an @-mention: the gateway ignored U4's bare "apply B".
        adapter = _Adapter(
            replies=[{"type": "message", "user": "U4", "text": "apply B", "ts": "223.000"}], unheard={"U4"},
        )
        self._incident(adapter)
        self._runs(adapter)
        self.assertEqual(adapter.gated, [{
            "channel_id": CHANNEL, "routing_text": "apply B", "bot_uid": "U0TEAMBOT", "is_mentioned": False,
            "is_thread_reply": True, "event_thread_ts": MESSAGE_TS, "user_id": "U4", "team_id": TEAM,
            "is_dm": False, "force_process": False,
        }])

    def test_a_typed_apply_addressed_to_someone_else_does_not_drop_the_click(self):
        reply = {"type": "message", "user": "U2", "text": "<@U0ALICE> apply B", "ts": "223.000"}
        adapter = _Adapter(replies=[reply], ignore_other_user_mentions=True)
        self._incident(adapter)
        self._runs(adapter)
        self.assertEqual([(g["routing_text"], g["is_mentioned"]) for g in adapter.gated], [(reply["text"], False)])

        # With the flag off the same reply is the gateway's, so it drops the click; and so is one
        # naming someone after another token, since upstream reads only a leading mention.
        for kwargs, text in (({}, reply["text"]), ({"ignore_other_user_mentions": True}, ":eyes: <@U0ALICE> apply B")):
            with self.subTest(text=text, **kwargs):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{**reply, "text": text}], **kwargs)
                self._incident(adapter)
                self._drops(adapter)

    def test_a_mention_only_in_the_blocks_reaches_the_gate_as_the_gateway_reads_it(self):
        blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
            {"type": "user", "user_id": "U0TEAMBOT"}, {"type": "text", "text": " apply B"},
        ]}]}]
        reply = {"type": "message", "user": "U4", "text": "apply B", "blocks": blocks, "ts": "223.000"}
        adapter = _Adapter(replies=[reply])
        self._incident(adapter)
        self.assertEqual(
            [(g["routing_text"], g["is_mentioned"]) for g in adapter.gated], [("apply B\n<@U0TEAMBOT>", True)],
        )

    def test_a_group_dm_reaches_the_gate_as_a_dm_as_the_gateway_reads_it(self):
        # The gateway passes is_dm for an mpim; a click's payload names a group DM "mpdm-...".
        for name, is_dm in (("mpdm-alice--bob--kage-1", True), ("incidents", False), ("", False)):
            with self.subTest(name=name):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U4", "text": "apply B", "ts": "223.000"}])
                self._incident(adapter, channel_name=name)
                self.assertEqual([g["is_dm"] for g in adapter.gated], [is_dm])

    def test_a_mention_the_gateway_reads_is_passed_to_its_gate(self):
        for text in ("<@U0TEAMBOT> apply B", "@kage apply B"):
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._incident(adapter)
                self._drops(adapter)
                self.assertEqual([g["is_mentioned"] for g in adapter.gated], [True])
        importlib.reload(runtime)
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "<@U0BOT> apply B", "ts": "223.000"}])
        self._incident(adapter)
        # Another workspace's bot id is not a mention of this one.
        self.assertEqual([g["is_mentioned"] for g in adapter.gated], [False])

    def test_where_the_gateway_skips_its_channel_gate_the_check_skips_it_too(self):
        reply = {"type": "message", "user": "U4", "text": "apply B", "ts": "223.000"}
        adapter = _Adapter(replies=[reply], unheard={"U4"})
        adapter._begin_interaction = self._dm_begin(adapter)
        self._incident(adapter)
        self._drops(adapter)
        self.assertEqual(adapter.gated, [])

        importlib.reload(runtime)
        adapter = _Adapter(replies=[reply], unheard={"U4"})
        adapter._bot_user_id, adapter._team_bot_user_ids = None, {}
        self._incident(adapter)
        self._drops(adapter)
        self.assertEqual(adapter.gated, [])

    def test_a_reply_typed_before_the_options_appeared_does_not_drop_the_click(self):
        # The alert is posted at 222 and edited into its options at 230.
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "apply Option B", "ts": "225.000"}])
        self._incident(adapter, edited="230.000")
        self._runs(adapter)
        self.assertEqual(adapter.log[0][1]["oldest"], "230.000")

        importlib.reload(runtime)
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "apply Option B", "ts": "231.000"}])
        self._incident(adapter, edited="230.000")
        self._drops(adapter)

    def test_only_the_call_to_actions_forms_drop_the_click(self):
        drops = (
            "apply", "apply.", "Apply!", "apply B", "apply Option B",
            "yes, apply B", "please apply B", "ok apply", "@kage apply B", "<@U0BOT> <@U0BOT2> apply B",
            "*apply* B", "'apply'", "`apply Option A`", "&gt; apply B", "> apply B", ":white_check_mark: apply B",
            "\u2705 apply B",
        )
        runs = (
            "Apply the label later?", "apply nothing", "apply? which one", "Apply Option B - wait, not yet",
            "apply-now", "reapply B", "applying B now", "should we apply B?", "apply a fix",
        )
        for text, check in [*((t, self._drops) for t in drops), *((t, self._runs) for t in runs)]:
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._incident(adapter)
                check(adapter)

    def _options_incident(self, adapter, *labels):
        body, action = _alert_choice(1, labels[-1])
        triage = {"headline": "Pod OOMKilled", "links": [], "fold_title": "Options", "choices": [
            (label, False) for label in labels
        ]}
        body["message"]["blocks"] = incident.blocks_triage(triage, [])
        self._answer(adapter, body, action)

    def test_a_colon_counts_only_before_that_options_own_text(self):
        options = ("apply Option A: Raise the limit", "apply Option B: Restore the secret")
        long_title = "Roll back checkout-gateway to the last revision that served without OOMKills in prod"
        quoted = ("apply Option A: Don't restart it", f"apply Option B: {long_title}", "apply Option C: Scale & wait")
        single = ("apply: Roll back checkout-gateway",)
        cases = [
            (options, self._drops, (
                "apply Option B: Restore the secret", "apply b: restore  the *secret*.", "<@U0BOT> apply A: Raise the limit",
            )),
            (options, self._runs, (
                "apply: no wait", "apply B: actually no, hold off", "apply A: Restore the secret",
                "apply: Restore the secret", "apply Option B:", "apply Option B: Restore the secret, then wait",
            )),
            (single, self._drops, ("apply: roll back checkout-gateway",)),
            (single, self._runs, ("apply: no wait", "apply A: Roll back checkout-gateway")),
            # The text after the colon is matched as typed: an apostrophe, a title the button clips,
            # and an ampersand as Slack sends it.
            (quoted, self._drops, ("apply A: Don't restart it", f"apply B: {long_title}", "apply C: Scale &amp; wait")),
        ]
        for labels, check, texts in cases:
            for text in texts:
                with self.subTest(labels=labels, text=text):
                    importlib.reload(runtime)
                    adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                    self._options_incident(adapter, *labels)
                    check(adapter)

    def test_a_bare_apply_counts_only_for_a_letter_the_alert_offers(self):
        options = ("apply Option A: Raise the limit", "apply Option B: Restore the secret")
        single = ("apply: Roll back checkout-gateway",)
        cases = [
            (options, self._drops, ("apply B", "apply option a", "apply")),
            (options, self._runs, ("apply D", "apply Option C")),
            # A single fix has no letter to check a typed one against; the agent may apply it anyway.
            (single, self._drops, ("apply", "apply A")),
        ]
        for labels, check, texts in cases:
            for text in texts:
                with self.subTest(labels=labels, text=text):
                    importlib.reload(runtime)
                    adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                    self._options_incident(adapter, *labels)
                    check(adapter)

    def test_a_clipped_buttons_shown_text_counts_as_its_whole_text(self):
        # The button shows "apply Option B: Roll back checkout-gateway to the last revision that…".
        labels = (
            "apply Option A: Raise the limit",
            "apply Option B: Roll back checkout-gateway to the last revision that served without OOMKills in prod",
        )
        cases = (
            (self._drops, "apply Option B: Roll back checkout-gateway to the last revision that…"),
            (self._drops, "apply B: roll back checkout-gateway to the last revision that"),
            (self._runs, "apply B: Roll back checkout-gateway to the last revision"),
            (self._runs, "apply A: Roll back checkout-gateway to the last revision that"),
        )
        for check, text in cases:
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._options_incident(adapter, *labels)
                check(adapter)

    def test_a_colon_form_with_a_hostname_slack_linked_still_counts(self):
        # Slack sends a typed hostname as <http://host|host>, and a typed url as <url>.
        labels = (
            "apply Option A: Drain checkout.example.com", "apply Option B: Open https://grafana.example.com/d/x",
            "apply Option C: Ping @U0BOT", "apply Option D: Drain\ncheckout",
        )
        cases = (
            (self._drops, "apply A: Drain <http://checkout.example.com|checkout.example.com>"),
            (self._drops, "apply B: Open <https://grafana.example.com/d/x>"),
            (self._runs, "apply A: Drain <http://checkout.example.com|other.example.com>"),
            # Only a link is unwrapped: a mention names someone, not the option's text.
            (self._runs, "apply C: Ping <@U0BOT>"),
            (self._drops, "apply D: Drain checkout"),
        )
        for check, text in cases:
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._options_incident(adapter, *labels)
                check(adapter)

    def test_a_reply_of_unclosed_links_stays_linear(self):
        # Each "<http://" scanned to the end of the text for a ">" that never comes.
        options = frozenset({("A", "drain checkout.example.com")})
        for run in ("<http://", "<http://a|", "<http://a|" + "|" * 20):
            text = "apply A: " + run * (SLACK_TEXT_LIMIT // len(run))
            with self.subTest(run=run[:12]):
                start = time.monotonic()
                self.assertFalse(runtime._typed_apply(text, options))
                self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_colon_on_a_message_without_the_option_buttons_does_not_drop_the_click(self):
        adapter = _Adapter(replies=[
            {"type": "message", "user": "U2", "text": "apply option b: Restore the secret", "ts": "223.000"},
        ])
        body, action = _alert_choice(1, "Apply Option B")
        # A button in that form that is not an incident option is not one to type.
        body["message"]["blocks"][1]["elements"][2]["value"] = "apply Option B: Restore the secret"
        self._answer(adapter, body, action)
        self._runs(adapter)

    def test_a_courtesy_after_the_option_still_counts_and_nothing_else_does(self):
        drops = (
            "apply B please", "apply Option B, thanks", "Apply B thank you!", "apply, please", "apply B ty",
            "apply B. Thanks.",
        )
        runs = (
            "Apply B now", "apply B please?", "apply B :+1:", "apply B please wait", "apply B thanks but not yet",
            "apply B: please",
        )
        for text, check in [*((t, self._drops) for t in drops), *((t, self._runs) for t in runs)]:
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._incident(adapter)
                check(adapter)

    def test_a_struck_through_apply_does_not_drop_the_click(self):
        # The last four are tildes Slack shows as typed, each held by one of the strike's four edge tests.
        for text, check in (("~apply B~", self._runs), ("~apply A~ apply B", self._drops), ("~no~ apply B", self._drops),
                            ("~no~ apply B ~now~", self._drops), ("~no\n~ apply B", self._runs),
                            ("~no~apply B", self._runs), ("apply B~no~", self._runs), ("apply B ~ no~", self._runs),
                            ("apply B ~no ~", self._runs)):
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._incident(adapter)
                check(adapter)

    def test_a_colon_form_asked_as_a_question_does_not_drop_the_click(self):
        for text in ("Apply B: Restore the secret?", "apply B: will it restart the pods?"):
            with self.subTest(text=text):
                importlib.reload(runtime)
                adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": text, "ts": "223.000"}])
                self._options_incident(adapter, "apply Option A: Raise the limit", "apply Option B: Restore the secret")
                self._runs(adapter)

    def test_a_typed_apply_also_sent_to_the_channel_drops_the_click(self):
        adapter = _Adapter(replies=[
            {"type": "message", "subtype": "thread_broadcast", "user": "U2", "text": "apply B", "ts": "223.000"},
        ])
        self._incident(adapter)
        self._drops(adapter)

    def test_a_typed_apply_the_gateway_takes_as_a_turn_drops_the_click(self):
        cases = {
            "with a file": {"subtype": "file_share", "client_msg_id": "m1"},
            "as a /me": {"subtype": "me_message"},
            "from a person posting through an app": {"app_id": "A1"},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                importlib.reload(runtime)
                reply = {"type": "message", "user": "U2", "text": "apply B", "ts": "223.000", **fields}
                adapter = _Adapter(replies=[reply], api_human_users={"U2"})
                self._incident(adapter)
                self._drops(adapter)

    def test_a_tilde_inside_an_options_text_is_not_a_strike(self):
        adapter = _Adapter(replies=[
            {"type": "message", "user": "U2", "text": "apply Option C: Scale to ~3 to ~5 replicas", "ts": "223.000"},
        ])
        self._options_incident(adapter, "apply Option A: Restart the pod", "apply Option C: Scale to ~3 to ~5 replicas")
        self._drops(adapter)

    def test_a_failed_thread_read_runs_the_click_as_before(self):
        adapter = _Adapter(fail=("conversations_replies",))
        with self.assertLogs(runtime.logger, level="WARNING") as logs:
            self._incident(adapter)
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "message"])
        self.assertTrue(any("could not check the thread" in line for line in logs.output))

    def test_a_check_that_raises_after_a_typed_apply_matched_counts_the_reply(self):
        # Running the click as well would apply both the typed option and the clicked one.
        for broken in ("authorized", "patterns", "gate"):
            with self.subTest(broken=broken):
                importlib.reload(runtime)
                adapter = _Adapter(
                    replies=[{"type": "message", "user": "U2", "text": "apply B", "ts": "223.000"}], broken={broken},
                )
                with self.assertLogs(runtime.logger, level="WARNING") as logs:
                    self._incident(adapter)
                self._drops(adapter)
                self.assertTrue(any("could not check the thread" in line for line in logs.output))

    def test_a_bot_test_that_raises_after_a_typed_apply_does_not_count_the_reply(self):
        # The adapter makes that test first, so it raised there too and took no turn.
        adapter = _Adapter(
            replies=[{"type": "message", "user": "U2", "text": "apply B", "ts": "223.000"}], broken={"bot"},
        )
        with self.assertLogs(runtime.logger, level="WARNING") as logs:
            self._incident(adapter)
        self._runs(adapter)
        self.assertTrue(any("not counting it" in line for line in logs.output))

    def test_a_check_that_raises_on_a_reply_that_is_no_apply_runs_the_click(self):
        for broken in ("bot", "authorized", "patterns", "gate"):
            with self.subTest(broken=broken):
                importlib.reload(runtime)
                adapter = _Adapter(
                    replies=[{"type": "message", "user": "U2", "text": "what does B do?", "ts": "223.000"}],
                    broken={broken},
                )
                self._incident(adapter)
                self._runs(adapter)

    def test_the_answered_alert_keeps_its_report_for_the_clicks_own_turn(self):
        report = "*Pod OOMKilled*\nOption A: raise the limit\nOption B: roll back checkout-gateway"
        adapter = _Adapter()
        seen = []

        async def turn(event, payload=None):
            seen.append([entry[1]["text"] for entry in adapter.log if entry[0] == "chat_update"])

        adapter._handle_slack_message = turn
        body, action = _alert_choice(1, "Apply Option B")
        body["message"]["text"] = report
        self._answer(adapter, body, action)
        self.assertEqual(seen, [[f"✓ <@U1>: Apply Option B\n\n{report}"]])

    def test_the_answered_alert_drops_its_reply_with_line_and_keeps_the_rest(self):
        triage = {
            "headline": "Pod OOMKilled",
            "links": [("Cloud Logs", "https://console.cloud.google.com/logs")],
            "choices": [("Apply Option A", "raise"), ("Apply Option B", "roll back")],
        }
        # The report's own line that reads like the call to action stays: only the fallback's goes.
        report = f"Option A: raise the limit\n{presenter.CHOICES_LEAD}apply Option A or apply Option B"
        alert = incident.message_text(triage, report)
        self.assertEqual(alert.count(presenter.CHOICES_LEAD), 2)
        head = incident.fallback_text(triage).rsplit("\n", 1)[0]
        for note, check in (("✓ <@U1>: Apply Option B", ()), (runtime.ANSWERED_IN_THREAD, ("typed",))):
            with self.subTest(note=note):
                importlib.reload(runtime)
                replies = [{"type": "message", "user": "U2", "text": "apply B", "ts": "223.000"}] if check else []
                adapter = _Adapter(replies=replies)
                body, action = _alert_choice(1, "Apply Option B")
                body["message"]["text"] = alert
                self._answer(adapter, body, action)
                text = next(entry[1]["text"] for entry in adapter.log if entry[0] == "chat_update")
                self.assertEqual(text, f"{note}\n\n{head}\n\n{report}")

    def test_a_reply_with_line_alone_leaves_the_note_and_the_report(self):
        body, action = _alert_choice(1, "Apply Option B")
        body["message"]["text"] = f"{presenter.CHOICES_LEAD}Apply Option A\n\nthe report"
        adapter = _Adapter()
        self._answer(adapter, body, action)
        update = next(entry[1] for entry in adapter.log if entry[0] == "chat_update")
        self.assertEqual(update["text"], "✓ <@U1>: Apply Option B\n\nthe report")

    def test_a_headline_quoting_the_reply_with_words_stays(self):
        headline = f"*{presenter.CHOICES_LEAD}nobody answered*"
        body, action = _alert_choice(1, "Apply Option B")
        body["message"]["text"] = f"{headline}\n{presenter.CHOICES_LEAD}Apply Option A\n\nthe report"
        adapter = _Adapter()
        self._answer(adapter, body, action)
        update = next(entry[1] for entry in adapter.log if entry[0] == "chat_update")
        self.assertEqual(update["text"], f"✓ <@U1>: Apply Option B\n\n{headline}\n\nthe report")

    def test_a_card_questions_reply_with_line_goes_after_a_detail_with_a_blank_line(self):
        body, action = _choice(1, "seeded-b", prefix="kage_needs")
        question = "*Which cluster?*\nI found it in two.\n\nBoth are healthy. Which one?"
        body["message"]["text"] = f"{question}\n{presenter.CHOICES_LEAD}seeded-a · seeded-b"
        adapter = _Adapter()
        self._answer(adapter, body, action)
        update = next(entry[1] for entry in adapter.log if entry[0] == "chat_update")
        self.assertEqual(update["text"], f"✓ <@U1>: seeded-b\n\n{question}")

    def test_a_card_questions_rewrite_keeps_the_line_naming_its_card(self):
        body, action = _choice(1, "seeded-b", prefix="kage_needs")
        question = "*Which cluster?*\nI found it in two.\n\nWhich one?\n(Question from card t_e0c1.)"
        body["message"]["text"] = f"{question}\n{presenter.CHOICES_LEAD}seeded-a · seeded-b"
        adapter = _Adapter()
        self._answer(adapter, body, action)
        update = next(entry[1] for entry in adapter.log if entry[0] == "chat_update")
        self.assertEqual(update["text"], f"✓ <@U1>: seeded-b\n\n{question}")

    def test_typed_apply_keeps_the_report_too(self):
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "apply Option B", "ts": "223.000"}])
        body, action = _alert_choice(1, "Apply Option A")
        body["message"]["text"] = "the report"
        self._answer(adapter, body, action)
        self.assertEqual(adapter.log[-1][1]["text"], f"{runtime.ANSWERED_IN_THREAD}\n\nthe report")

    def test_answered_text_is_clipped_to_slacks_limit_and_keeps_the_note(self):
        adapter = _Adapter()
        body, action = _choice()
        body["message"]["text"] = "word " * runtime.SLACK_TEXT_MAX
        self._answer(adapter, body, action)
        text = adapter.log[0][1]["text"]
        self.assertLessEqual(len(text), runtime.SLACK_TEXT_MAX)
        self.assertTrue(text.startswith("✓ <@U1>: Leave it\n\nword word"))
        self.assertTrue(text.endswith(presenter.ELLIPSIS))

    def test_a_message_with_no_text_is_answered_with_the_note_alone(self):
        adapter = _Adapter()
        body, action = _choice()
        del body["message"]["text"]
        self._answer(adapter, body, action)
        self.assertEqual(adapter.log[0][1]["text"], "✓ <@U1>: Leave it")

    def test_two_clicks_during_the_thread_read_run_one_turn(self):
        adapter = _Adapter()

        async def both():
            await asyncio.gather(
                runtime.answer(adapter, self._ack(adapter), *_alert_choice(1, "Apply Option B"),
                               runtime.CHOICE_KIND),
                runtime.answer(adapter, self._ack(adapter), *_alert_choice(0, "Apply Option A"),
                               runtime.CHOICE_KIND),
            )

        _run(both())
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Apply Option B"])

    def test_a_click_on_a_card_question_does_not_read_the_thread(self):
        adapter = _Adapter(replies=[{"type": "message", "user": "U2", "text": "apply Option B", "ts": "223.000"}])
        self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "message"])

    def test_flag_off_is_disabled(self):
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": "0"}):
            self.assertFalse(runtime.enabled())


class AnsweredBlocksTest(unittest.TestCase):
    def test_non_action_blocks_are_kept_in_order(self):
        blocks = _message()["blocks"]
        out = runtime.answered_blocks(blocks, lambda i: bool(re.search(r"\.choice\.", i)), "note")
        self.assertEqual(out[0], blocks[0])
        self.assertEqual(out[-1], {"type": "context", "elements": [{"type": "mrkdwn", "text": "note"}]})
        self.assertEqual(blocks[1]["elements"][1]["action_id"], "kage.choice.0")  # input untouched

    def test_the_waiting_line_goes_with_the_buttons(self):
        waiting = {"type": "context", "block_id": runtime._presenter.WAITING_BLOCK_ID, "elements": []}
        out = runtime.answered_blocks([*_message()["blocks"], waiting], runtime._answered_by, "note")
        self.assertNotIn(waiting, out)
        self.assertEqual(out[-1]["elements"][0]["text"], "note")

    def test_texts_and_block_count_are_clamped_to_slacks_caps_keeping_the_note(self):
        # Slack echoes ``&`` back as ``&amp;``, so a text sent at the cap returns past it.
        long_text = "&amp; " * runtime.SECTION_TEXT_MAX
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": long_text}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": long_text}, {"type": "image", "image_url": "u"}]},
        ] + [{"type": "divider"}] * (runtime.MESSAGE_BLOCKS_MAX + 5)
        out = runtime.answered_blocks(blocks, lambda i: False, "note")
        self.assertEqual(len(out), runtime.MESSAGE_BLOCKS_MAX)
        self.assertEqual(out[-1], {"type": "context", "elements": [{"type": "mrkdwn", "text": "note"}]})
        self.assertLessEqual(len(out[0]["text"]["text"]), runtime.SECTION_TEXT_MAX)
        self.assertLessEqual(len(out[1]["elements"][0]["text"]), runtime.SECTION_TEXT_MAX)
        self.assertEqual(out[1]["elements"][1], {"type": "image", "image_url": "u"})
        self.assertEqual(len(blocks[0]["text"]["text"]), len(long_text))  # input untouched


if __name__ == "__main__":
    unittest.main()
