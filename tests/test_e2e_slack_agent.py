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
_TEST_USER = "UTEST"
_CHANNEL = "C0CI"
_ASK_TS = "1800000000.000100"
_ANSWER_TS = "1800000004.000300"


class _Skipped(Exception):
    pass


class _Failed(Exception):
    pass


def _raise(exc_type):
    def raiser(reason):
        raise exc_type(reason)
    return raiser


def _load():
    stub = types.ModuleType("pytest")
    stub.fixture = lambda *a, **k: (lambda f: f)
    stub.skip = _raise(_Skipped)
    stub.fail = _raise(_Failed)
    with mock.patch.dict(sys.modules, {"pytest": stub}), mock.patch.dict("os.environ", {"TEST_TIMEOUT_SEC": "5", "SLACK_POLL_INTERVAL_SEC": "0"}):
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SLACK_AGENT_TEST)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


MOD = _load()

_HEADLINE_BLOCKS = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "It is 5.", "style": {"bold": True}}]}]}]
_ANSWER = {"ts": _ANSWER_TS, "user": _BOT, "bot_id": "B1", "text": "*It is 5.*", "blocks": _HEADLINE_BLOCKS}


class FakeSlack:
    """Answers the three methods the test calls, turn by turn: each thread read advances one step."""

    def __init__(self, steps, ask_bot_id=""):
        self.steps = steps
        self.polls = 0
        self.posted = []
        self.ask_bot_id = ask_bot_id

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
            ask = {"ts": _ASK_TS, "user": _TEST_USER, "text": "ask"}
            if self.ask_bot_id:
                ask["bot_id"] = self.ask_bot_id
            return {"ok": True, "messages": [ask, *step.get("replies", [])]}
        raise AssertionError(method)


def _run(fake, mode="next", listed=None):
    env = {"E2E_SLACK_MODE": mode}
    with mock.patch.dict("os.environ", env), mock.patch.object(MOD.time, "sleep", lambda s: None), \
            mock.patch.object(MOD, "gateway_test_users", lambda: listed):
        MOD.test_slack_agent_math_response((fake, _CHANNEL, _BOT))


class SlackAgentTestLogic(unittest.TestCase):
    def test_skips_while_unprovisioned(self):
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(_Skipped) as caught:
            MOD.slack_settings()
        self.assertIn("E2E_SLACK_USER_TOKEN", str(caught.exception))

    def test_refuses_a_bot_token_and_takes_a_rotating_user_token(self):
        base = {"E2E_SLACK_CHANNEL_ID": _CHANNEL, "E2E_SLACK_BOT_USER_ID": _BOT}
        with mock.patch.dict("os.environ", {**base, "E2E_SLACK_USER_TOKEN": "xoxb-1"}, clear=True), self.assertRaises(_Failed):
            MOD.slack_settings()
        with mock.patch.dict("os.environ", {**base, "E2E_SLACK_USER_TOKEN": "xoxe.xoxp-1"}, clear=True):
            _client, channel, bot = MOD.slack_settings()
        self.assertEqual((_CHANNEL, _BOT), (channel, bot))

    def test_a_full_turn_passes_and_mentions_the_bot(self):
        fake = FakeSlack([{"reaction": "eyes"}, {"reaction": "eyes", "replies": [_ANSWER]}])
        _run(fake)
        self.assertTrue(fake.posted[0]["text"].startswith(f"<@{_BOT}> "))

    def test_no_reaction_is_the_expected_failure_on_next_and_a_failure_on_today(self):
        step = [{"replies": [_ANSWER]}]
        _run(FakeSlack(step), mode="next")
        with self.assertRaises(AssertionError) as caught:
            _run(FakeSlack(step), mode="today")
        self.assertIn("reacted", str(caught.exception))

    def test_a_plain_text_answer_fails_only_on_next(self):
        answer = {"ts": _ANSWER_TS, "user": _BOT, "text": "It is 5."}
        step = [{"reaction": "eyes", "replies": [answer]}]
        with self.assertRaises(AssertionError) as caught:
            _run(FakeSlack(step), mode="next")
        self.assertIn("headline", str(caught.exception))
        _run(FakeSlack(step), mode="today")

    def test_no_answer_times_out(self):
        with mock.patch.object(MOD, "TEST_TIMEOUT_SEC", 0), self.assertRaises(AssertionError):
            _run(FakeSlack([{"reaction": "eyes"}]))

    def test_preflight_fails_fast_when_the_gateway_does_not_list_the_test_user(self):
        fake = FakeSlack([{"replies": [_ANSWER]}], ask_bot_id="BDRIVER")
        with self.assertRaises(_Failed) as caught:
            _run(fake, listed=[])
        self.assertIn("operator.slackTestUsers", str(caught.exception))
        _run(FakeSlack([{"reaction": "eyes", "replies": [_ANSWER]}], ask_bot_id="BDRIVER"), listed=[_TEST_USER])
        # kubectl unavailable: the run goes on and says what it relies on.
        _run(FakeSlack([{"reaction": "eyes", "replies": [_ANSWER]}], ask_bot_id="BDRIVER"), listed=None)

    def test_a_refusal_as_the_first_reply_fails_at_once_quoting_it(self):
        refusal = {"ts": _ANSWER_TS, "user": _BOT, "bot_id": "B1", "text": MOD.live.REFUSAL_NOTICE_PREFIX + " you are not on the allowlist"}
        fake = FakeSlack([{"replies": [refusal]}, {"replies": [refusal]}, {"replies": [refusal, _ANSWER]}])
        with self.assertRaises(_Failed) as caught:
            _run(fake)
        self.assertIn("refusal", str(caught.exception))
        self.assertIn("allowlist", str(caught.exception))
        self.assertEqual(2, fake.polls)  # the preflight's read-back, then the poll that saw it

    def test_mode_comes_from_the_env_then_the_platform_agent(self):
        with mock.patch.dict("os.environ", {"E2E_SLACK_MODE": "today"}), \
                mock.patch.object(MOD, "_kubectl_json", lambda *a: {"items": [{"spec": {"mode": "next"}}]}):
            self.assertEqual("today", MOD.install_mode())
        with mock.patch.dict("os.environ", {"E2E_SLACK_MODE": ""}):
            with mock.patch.object(MOD, "_kubectl_json", lambda *a: {"items": [{"spec": {"mode": "next"}}]}):
                self.assertEqual("next", MOD.install_mode())
            with mock.patch.object(MOD, "_kubectl_json", lambda *a: {"items": [{"spec": {}}]}):
                self.assertEqual("", MOD.install_mode())
            with mock.patch.object(MOD, "_kubectl_json", lambda *a: None):
                self.assertIsNone(MOD.install_mode())

    def test_on_next_a_status_line_quoting_the_result_is_not_the_answer(self):
        working = {"ts": "1800000001.000100", "user": _BOT, "bot_id": "B1", "text": "⚙️ *working* — 2 + 3 = 5"}
        done = dict(working, text="✅ *completed*")
        fake = FakeSlack([{}, {"replies": [working]}, {"reaction": "eyes", "replies": [done, _ANSWER]}])
        _run(fake, mode="next")
        self.assertGreaterEqual(fake.polls, 3)  # the preflight, the working line, then the answer

    def test_on_next_a_failed_turn_fails_at_once(self):
        failed = {"ts": "1800000001.000100", "user": _BOT, "bot_id": "B1", "text": "❌ *failed*"}
        notice = {"ts": "1800000002.000100", "user": _BOT, "bot_id": "B1", "text": "❌ failed: the model returned 5xx"}
        fake = FakeSlack([{"replies": [failed, notice]}])
        with self.assertRaises(_Failed) as caught:
            _run(fake, mode="next")
        self.assertIn("failure", str(caught.exception))

    def test_an_unknown_mode_skips_the_modes_own_checks(self):
        answer = {"ts": _ANSWER_TS, "user": _BOT, "text": "It is 5."}
        with mock.patch.object(MOD, "install_mode", lambda: None):
            _run(FakeSlack([{"replies": [answer]}]), mode="")

    def test_timing_legs_are_measured_from_the_post(self):
        t = MOD.Timeline(posted=MOD.live.ts_value(_ASK_TS), first_reply=MOD.live.ts_value("1800000001.000100"), answer=MOD.live.ts_value(_ANSWER_TS))
        legs = t.legs()
        self.assertEqual(1.0, legs["post_to_first_reply_s"])
        self.assertEqual(4.0, legs["post_to_answer_s"])
        json.dumps(legs)

    def test_headline_reader(self):
        self.assertTrue(MOD.opens_on_bold_headline({"blocks": _HEADLINE_BLOCKS}))
        self.assertTrue(MOD.opens_on_bold_headline({"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Bold*"}}]}))
        self.assertFalse(MOD.opens_on_bold_headline({"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "plain"}}]}))
        self.assertFalse(MOD.opens_on_bold_headline({"text": "*no blocks*"}))


if __name__ == "__main__":
    unittest.main()
