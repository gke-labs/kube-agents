"""Tests for the chat_feedback_reactions hook.

Hermes fans every human reaction out as ``reaction:added`` or
``reaction:removed`` with the reactor, the reacted-to message's author, the
channel and the message timestamp. The hook counts a thumbs-down a person put
on one of the bot's messages into the chat_metrics registry, once per person
and message, and nothing else.
"""

import asyncio
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins"))

from chat_metrics import metrics  # noqa: E402

# Loaded under the name Hermes' hook loader gives it, so it never collides with
# another hook's `handler` module when the suites run in one process.
_SPEC = importlib.util.spec_from_file_location("hermes_hook_chat_feedback_reactions", Path(__file__).resolve().parent / "handler.py")
handler = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = handler
_SPEC.loader.exec_module(handler)

_NEGATIVE_LINE = re.compile(r'^kubeagents_chat_feedback_negative_total\{platform="([a-z]+)"\} (\d+)$', re.M)
BOT = "UBOT"
PERSON = "UPERSON"


class _Platform:
    def __init__(self, value):
        self.value = value


class _Adapter:
    def __init__(self, bot):
        self._bot_user_id = bot
        self._team_bot_user_ids = {"T1": bot}


class _Gateway:
    def __init__(self, bot):
        self.adapters = {_Platform("slack"): _Adapter(bot)}


def reaction(action="added", reaction="-1", user=PERSON, author=BOT, ts="1700000000.000100", channel="C1"):
    return {
        "platform": "slack", "event_name": f"reaction:{action}", "reaction": reaction,
        "user_id": user, "item_user_id": author, "item_type": "message",
        "channel_id": channel, "message_ts": ts, "team_id": "T1", "event_ts": "1700000001.000000",
    }


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self._registry = metrics.REGISTRY
        metrics.REGISTRY = metrics.ChatMetrics()
        metrics.note_gateway(_Gateway(BOT))
        handler.reset()

    def tearDown(self):
        metrics.REGISTRY = self._registry
        metrics.note_gateway(None)

    def fire(self, ctx, action=None):
        asyncio.run(handler.handle(action or ctx["event_name"], ctx))

    def counts(self):
        return _NEGATIVE_LINE.findall(metrics.REGISTRY.render())

    def test_a_thumbs_down_on_the_bots_message_counts_under_slack(self):
        self.fire(reaction())
        self.assertEqual([("slack", "1")], self.counts())

    def test_both_slack_spellings_of_thumbs_down_count(self):
        self.fire(reaction(reaction="thumbsdown", ts="1.1"))
        self.fire(reaction(reaction=":-1:", ts="1.2"))
        self.assertEqual([("slack", "2")], self.counts())

    def test_a_skin_toned_thumbs_down_counts(self):
        # Slack names a skin-toned reaction `-1::skin-tone-3`; the modifier is not the emoji.
        self.fire(reaction(reaction="-1::skin-tone-3", ts="1.3"))
        self.fire(reaction(reaction="thumbsdown::skin-tone-5", ts="1.4"))
        self.fire(reaction(reaction="+1::skin-tone-2", ts="1.5"))
        self.assertEqual([("slack", "2")], self.counts())

    def test_a_reaction_before_the_bots_identity_is_known_logs_once(self):
        metrics.note_gateway(None)
        with self.assertLogs(handler.logger, level="INFO") as logs:
            self.fire(reaction(ts="2.1"))
            self.fire(reaction(ts="2.2"))
        self.assertEqual(1, len(logs.output), logs.output)
        self.assertEqual([], self.counts())

    def test_the_bots_own_reaction_does_not_count(self):
        self.fire(reaction(user=BOT))
        self.assertEqual([], self.counts())

    def test_a_reaction_on_a_persons_message_does_not_count(self):
        self.fire(reaction(author="UOTHER"))
        self.assertEqual([], self.counts())

    def test_a_thumbs_up_does_not_count(self):
        self.fire(reaction(reaction="+1"))
        self.fire(reaction(reaction="eyes"))
        self.assertEqual([], self.counts())

    def test_a_toggle_counts_once(self):
        self.fire(reaction())
        self.fire(reaction(action="removed"))
        self.fire(reaction())
        self.assertEqual([("slack", "1")], self.counts())

    def test_two_people_on_one_message_count_twice(self):
        self.fire(reaction(user="UONE"))
        self.fire(reaction(user="UTWO"))
        self.assertEqual([("slack", "2")], self.counts())

    def test_without_the_bots_identity_nothing_counts(self):
        metrics.note_gateway(None)
        self.fire(reaction())
        self.assertEqual([], self.counts())

    def test_without_a_registry_nothing_counts_and_nothing_raises(self):
        with mock.patch.object(handler, "_metrics", return_value=None):
            self.fire(reaction())
        self.assertEqual([], self.counts())

    def test_a_malformed_context_never_raises(self):
        for ctx in ({}, {"reaction": "-1"}, {"platform": "slack", "reaction": "-1", "user_id": None, "item_user_id": BOT}):
            self.fire(ctx, action="reaction:added")
        self.assertEqual([], self.counts())


if __name__ == "__main__":
    unittest.main()
