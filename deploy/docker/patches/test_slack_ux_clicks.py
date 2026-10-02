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
import unittest
from pathlib import Path
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


class _Adapter:
    def __init__(
        self, authorized=True, fail=(), allowed_channels=(), disable_dms=False, ignored=(), replies=(), unlisted=(),
    ):
        self.authorized = authorized
        self.unlisted = set(unlisted)
        self.asked = []
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
        return user_id not in self.unlisted

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

    def test_choice_is_the_clickers_turn_with_an_echo(self):
        adapter = _Adapter()
        with self.assertNoLogs(runtime.logger, level="WARNING"):
            self._answer(adapter, *_choice())
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "chat_postMessage", "message"])
        update, echo, turn = (entry[1] for entry in adapter.log)
        self.assertEqual((update["channel"], update["ts"]), (CHANNEL, MESSAGE_TS))
        actions = [b for b in update["blocks"] if b["type"] == "actions"]
        self.assertEqual(
            [[e["action_id"] for e in b["elements"]] for b in actions], [["kage.link.0"]]
        )
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], "✓ <@U1>: Leave it")
        # The note leads the text; the message's own text stays under it for a later thread read.
        self.assertEqual(update["text"], "✓ <@U1>: Leave it\n\nfallback")
        self.assertEqual(echo, {"channel": CHANNEL, "thread_ts": THREAD, "text": "↳ <@U1>: Leave it"})
        self.assertEqual(
            turn,
            {
                "type": "message", "user": USER, "text": "Leave it", "channel": CHANNEL, "ts": ACTION_TS,
                "thread_ts": THREAD, "_hermes_force_process": True, "team": TEAM,
            },
        )
        self.assertEqual(adapter.acks, 1)

    def test_unlisted_user_changes_nothing(self):
        adapter = _Adapter(authorized=False)
        self._answer(adapter, *_choice())
        self.assertEqual(adapter.log, [])
        self.assertEqual(adapter.acks, 1)
        # And the message is still answerable by someone who is listed.
        listed = _Adapter()
        self._answer(listed, *_choice())
        self.assertEqual(len(listed.log), 3)

    def test_a_message_is_answered_once(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(1, "Leave it"))
        self._answer(adapter, *_choice(0, "Raise to 512Mi"))
        turns = [entry[1]["text"] for entry in adapter.log if entry[0] == "message"]
        self.assertEqual(turns, ["Leave it"])

    def test_label_is_escaped_in_what_slack_shows_but_not_in_the_turn(self):
        adapter = _Adapter()
        self._answer(adapter, *_choice(value="<!channel> & go"))
        update, echo, turn = (entry[1] for entry in adapter.log)
        self.assertEqual(echo["text"], "↳ <@U1>: &lt;!channel&gt; &amp; go")
        self.assertNotIn("<!channel>", update["blocks"][-1]["elements"][0]["text"])
        self.assertEqual(turn["text"], "<!channel> & go")

    def test_turn_and_echo_carry_the_shown_text_never_the_longer_value(self):
        label = "Yes, roll back checkout-gateway to the previous revision in namespace prod " * 3
        button = presenter._button(label, "kage.choice.0", value=label)
        shown = button["text"]["text"]
        self.assertLess(len(shown), len(button["value"]))
        adapter = _Adapter()
        self._answer(adapter, *_choice(0, button["value"], shown=shown))
        update, echo, turn = (entry[1] for entry in adapter.log)
        self.assertEqual(turn["text"], shown)
        self.assertEqual(echo["text"], f"↳ <@U1>: {shown}")
        self.assertEqual(update["blocks"][-1]["elements"][0]["text"], f"✓ <@U1>: {shown}")

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
                self.assertEqual(adapter.log[1][1]["text"], f"↳ <@U1>: {label}")

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
        self.assertEqual(len(listed.log), 3)

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
        self.assertEqual(adapter.log[2][1]["thread_ts"], MESSAGE_TS)

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

    def _incident(self, adapter, value="Apply Option B", edited=None):
        body, action = _alert_choice(1, value)
        if edited:
            body["message"]["edited"] = {"user": "B1", "ts": edited}
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
                    ["conversations_replies", "chat_update", "chat_postMessage", "message"],
                )

    def _runs(self, adapter):
        self.assertEqual([entry[0] for entry in adapter.log], ["conversations_replies", "chat_update", "chat_postMessage", "message"])

    def _drops(self, adapter):
        self.assertEqual([entry[0] for entry in adapter.log], ["conversations_replies", "chat_update"])

    def test_a_typed_apply_from_someone_the_adapter_ignores_does_not_drop_the_click(self):
        adapter = _Adapter(
            replies=[{"type": "message", "user": "U3", "text": "apply Option B", "ts": "223.000"}], unlisted={"U3"},
        )
        self._incident(adapter)
        self._runs(adapter)
        self.assertEqual(adapter.asked, [("U3", CHANNEL, TEAM)])

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
            "apply", "apply.", "Apply!", "apply B", "apply Option B", "apply option b: Restore the secret",
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

    def test_a_typed_apply_also_sent_to_the_channel_drops_the_click(self):
        adapter = _Adapter(replies=[
            {"type": "message", "subtype": "thread_broadcast", "user": "U2", "text": "apply B", "ts": "223.000"},
        ])
        self._incident(adapter)
        self._drops(adapter)

    def test_a_failed_thread_read_runs_the_click_as_before(self):
        adapter = _Adapter(fail=("conversations_replies",))
        with self.assertLogs(runtime.logger, level="WARNING") as logs:
            self._incident(adapter)
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "chat_postMessage", "message"])
        self.assertTrue(any("could not read the thread" in line for line in logs.output))

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
        self.assertEqual([entry[0] for entry in adapter.log], ["chat_update", "chat_postMessage", "message"])

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


if __name__ == "__main__":
    unittest.main()
