"""Unit tests for tests/e2e/slack_agent_test.py, run on every pull request.

The release-gate test itself needs a Slack test user and a live install; these drive its
logic against a fake Slack, so a change to how it reads the thread is caught before a
release candidate is. The module imports pytest at module scope, which
requirements-test.txt does not install, so it is loaded with a stub in its place, as
tests/test_gchat_agent_preflight.py does.
"""

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SLACK_AGENT_TEST = _REPO_ROOT / "tests" / "e2e" / "slack_agent_test.py"
_MODULE_NAME = "e2e_slack_agent_test"
_BOT = "UBOT"
_CHANNEL = "C0CI"
_ASK_TS = "1800000000.000100"
_ACK_TS = "1800000001.000200"
_ANSWER_TS = "1800000004.000300"


class _Skipped(Exception):
    pass


class _Failed(Exception):
    pass


def _load():
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda f: f)
    stub.skip = lambda reason: (_ for _ in ()).throw(_Skipped(reason))
    stub.fail = lambda reason: (_ for _ in ()).throw(_Failed(reason))
    stub.main = lambda *a, **k: None
    with mock.patch.dict(sys.modules, {"pytest": stub}), mock.patch.dict("os.environ", {"TEST_TIMEOUT_SEC": "5", "SLACK_POLL_INTERVAL_SEC": "0"}):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SLACK_AGENT_TEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


MOD = _load()

_HEADLINE_BLOCKS = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "It is 5.", "style": {"bold": True}}]}]}]


class FakeSlack:
    """Answers the four methods the test calls, turn by turn: each poll advances one step."""

    def __init__(self, steps):
        self.steps = steps
        self.polls = 0
        self.posted = []

    def call(self, method, **params):
        if method == "chat.postMessage":
            self.posted.append(params)
            return {"ok": True, "ts": _ASK_TS}
        step = self.steps[min(self.polls, len(self.steps) - 1)]
        if method == "reactions.get":
            reactions = [{"name": step["reaction"], "users": [_BOT]}] if step.get("reaction") else []
            return {"ok": True, "message": {"reactions": reactions}}
        if method == "conversations.replies":
            self.polls += 1
            return {"ok": True, "messages": [{"ts": _ASK_TS, "user": "UTEST", "text": "ask"}, *step.get("replies", [])]}
        raise AssertionError(method)


def _run(fake, expect_headline="true"):
    env = {"E2E_SLACK_EXPECT_HEADLINE": expect_headline}
    with mock.patch.dict("os.environ", env), mock.patch.object(MOD.time, "sleep", lambda s: None):
        MOD.test_slack_agent_math_response((fake, _CHANNEL, _BOT))


class SlackAgentTestLogic(unittest.TestCase):
    def test_skips_while_unprovisioned(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(_Skipped) as caught:
            MOD.slack_settings()
        self.assertIn("E2E_SLACK_USER_TOKEN", str(caught.exception))

    def test_refuses_a_bot_token(self):
        env = {"E2E_SLACK_USER_TOKEN": "xoxb-1", "E2E_SLACK_CHANNEL_ID": _CHANNEL, "E2E_SLACK_BOT_USER_ID": _BOT}
        with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(_Failed):
            MOD.slack_settings()

    def test_a_full_turn_passes_and_mentions_the_bot(self):
        ack = {"ts": _ACK_TS, "user": _BOT, "bot_id": "B1", "text": "⏳ working"}
        answer = {"ts": _ANSWER_TS, "user": _BOT, "bot_id": "B1", "text": "*It is 5.*", "blocks": _HEADLINE_BLOCKS}
        fake = FakeSlack([{"reaction": "eyes", "replies": [ack]}, {"reaction": "eyes", "replies": [ack, answer]}])
        _run(fake)
        self.assertTrue(fake.posted[0]["text"].startswith(f"<@{_BOT}> "))

    def test_a_missed_reaction_fails(self):
        answer = {"ts": _ANSWER_TS, "user": _BOT, "text": "*It is 5.*", "blocks": _HEADLINE_BLOCKS}
        with self.assertRaises(AssertionError) as caught:
            _run(FakeSlack([{"replies": [answer]}]))
        self.assertIn("reaction", str(caught.exception))

    def test_a_plain_text_answer_fails_only_where_a_headline_is_expected(self):
        answer = {"ts": _ANSWER_TS, "user": _BOT, "text": "It is 5."}
        step = [{"reaction": "eyes", "replies": [answer]}]
        with self.assertRaises(AssertionError) as caught:
            _run(FakeSlack(step), expect_headline="true")
        self.assertIn("headline", str(caught.exception))
        _run(FakeSlack(step), expect_headline="false")

    def test_no_answer_times_out(self):
        with mock.patch.object(MOD, "TEST_TIMEOUT_SEC", 0), self.assertRaises(AssertionError):
            _run(FakeSlack([{"reaction": "eyes"}]))

    def test_timing_legs_are_measured_from_the_post(self):
        t = MOD.Timeline(posted=MOD.ts_value(_ASK_TS), ack=MOD.ts_value(_ACK_TS), answer=MOD.ts_value(_ANSWER_TS))
        legs = t.legs()
        self.assertEqual(1.0, legs["post_to_ack_s"])
        self.assertEqual(4.0, legs["post_to_answer_s"])
        json.dumps(legs)

    def test_headline_reader(self):
        self.assertTrue(MOD.opens_on_bold_headline({"blocks": _HEADLINE_BLOCKS}))
        self.assertTrue(MOD.opens_on_bold_headline({"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Bold*"}}]}))
        self.assertFalse(MOD.opens_on_bold_headline({"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "plain"}}]}))
        self.assertFalse(MOD.opens_on_bold_headline({"text": "*no blocks*"}))


if __name__ == "__main__":
    unittest.main()
