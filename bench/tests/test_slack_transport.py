# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the Slack transport and its harness branch, against a scripted fake Slack.

The fake answers the three methods the transport calls the way Slack does for a
gateway turn: the bot's reaction on the ask while work is in flight, a first
reply (the status line) that is edited, the answer posted after it, and the
reaction removed once the work succeeded.
"""

import json

import pytest
from kube_agents_bench import harness, stop_hook
from kube_agents_bench import slack_transport as slack

BOT = "UBOT"
CHANNEL = "C0CI"
ASK_TS = "1800000000.000100"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeSlack:
    """Each conversations.replies call advances one scripted step."""

    def __init__(self, steps):
        self.steps = steps
        self.polls = 0
        self.posted = []

    def call(self, method, **params):
        if method == "chat.postMessage":
            self.posted.append(params)
            return {"ok": True, "ts": ASK_TS}
        step = self.steps[min(self.polls, len(self.steps) - 1)]
        if method == "reactions.get":
            names = (["eyes"] if step.get("reacting") else []) + ([step["reaction"]] if step.get("reaction") else [])
            reactions = [{"name": n, "users": [BOT]} for n in names]
            return {"ok": True, "message": {"reactions": reactions}}
        if method == "conversations.replies":
            self.polls += 1
            return {"ok": True, "messages": [{"ts": ASK_TS, "user": "UTEST", "text": "ask"}, *step.get("replies", [])]}
        raise AssertionError(method)


def _turn(fake, clock):
    return slack.SlackTurn(fake, CHANNEL, BOT, clock=clock.clock, wall=lambda: 1800000001.5, sleep=clock.sleep,
                           poll_seconds=1.0, quiet_seconds=3.0)


STATUS = {"ts": "1800000001.000200", "user": BOT, "bot_id": "B1", "text": "⏳ working"}
ANSWER = {"ts": "1800000009.000300", "user": BOT, "bot_id": "B1", "text": "*Three clusters.*",
          "blocks": [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "More"}}]}]}


def test_a_turn_settles_once_the_reaction_is_gone_and_the_thread_is_quiet():
    fake = FakeSlack([
        {"reacting": True},
        {"reacting": True, "replies": [STATUS]},
        {"reacting": True, "replies": [STATUS, ANSWER]},
        {"reacting": False, "replies": [STATUS, ANSWER]},
    ])
    ex = _turn(fake, FakeClock()).run("how many clusters?", 60)
    assert ex.outcome == "settled"
    assert ex.deliverable == "*Three clusters.*"
    assert ex.reaction == "eyes" and ex.buttons
    assert fake.posted[0]["text"] == f"<@{BOT}> how many clusters?"
    timing = ex.timing()
    assert timing["slack_post_to_ack_s"] == 1.0
    assert timing["slack_post_to_answer_s"] == 9.0
    assert timing["slack_post_to_first_reaction_s"] == 1.5


def test_a_reaction_still_on_keeps_the_turn_open_until_the_deadline():
    fake = FakeSlack([{"reacting": True, "replies": [STATUS]}])
    ex = _turn(fake, FakeClock()).run("q", 10)
    assert ex.outcome == "timeout"
    assert ex.timing()["slack_post_to_answer_s"] is None


@pytest.mark.parametrize("mark", ["double_vertical_bar", "x"])
def test_a_blocked_or_failed_mark_ends_the_turn(mark):
    # The ask's own mark stays on beside the blocked one, as the gateway leaves it.
    fake = FakeSlack([{"reacting": True, "replies": [STATUS]},
                      {"reacting": True, "reaction": mark, "replies": [STATUS, ANSWER]}])
    ex = _turn(fake, FakeClock()).run("q", 60)
    assert ex.outcome == "settled" and ex.deliverable == "*Three clusters.*"


def test_another_apps_post_in_the_thread_is_not_the_bots():
    other = {"ts": "1800000010.000100", "user": "UOTHER", "bot_id": "BOTHER", "text": "noise"}
    fake = FakeSlack([{"replies": [STATUS, ANSWER, other]}])
    ex = _turn(fake, FakeClock()).run("q", 30)
    assert ex.outcome == "settled" and ex.deliverable == "*Three clusters.*"


def test_a_long_thread_is_read_to_its_end():
    class Paged(FakeSlack):
        def call(self, method, **params):
            if method == "conversations.replies" and not params.get("cursor"):
                self.polls += 1
                return {"ok": True, "messages": [{"ts": ASK_TS, "user": "UTEST", "text": "ask"}, STATUS],
                        "response_metadata": {"next_cursor": "page2"}}
            if method == "conversations.replies":
                return {"ok": True, "messages": [ANSWER]}
            return super().call(method, **params)

    ex = _turn(Paged([{}]), FakeClock()).run("q", 30)
    assert ex.deliverable == "*Three clusters.*"


def test_no_reply_at_all():
    ex = _turn(FakeSlack([{}]), FakeClock()).run("q", 5)
    assert ex.outcome == "no_reply" and ex.deliverable == ""


def test_a_transient_read_error_is_polled_through():
    class Flaky(FakeSlack):
        def call(self, method, **params):
            if method == "reactions.get" and self.polls == 0 and not getattr(self, "failed", False):
                self.failed = True
                raise slack.SlackAPIError(method, "http_503")
            return super().call(method, **params)

    fake = Flaky([{"replies": [ANSWER]}])
    ex = _turn(fake, FakeClock()).run("q", 30)
    assert ex.outcome == "settled" and ex.errors == ["reactions.get: http_503"]


def test_a_dropped_connection_is_polled_through():
    class Dropping(FakeSlack):
        def call(self, method, **params):
            if method == "conversations.replies" and not getattr(self, "dropped", False):
                self.dropped = True
                raise slack.SlackAPIError(method, f"{slack.CONNECTION_ERROR} (ConnectionResetError)")
            return super().call(method, **params)

    ex = _turn(Dropping([{"replies": [ANSWER]}]), FakeClock()).run("q", 30)
    assert ex.outcome == "settled" and len(ex.errors) == 1


def test_the_client_reports_no_answer_as_a_transient_error(monkeypatch):
    def refuse(*a, **k):
        raise slack.urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(slack.urllib.request, "urlopen", refuse)
    with pytest.raises(slack.SlackAPIError) as caught:
        slack.SlackClient("xoxp-1").call("reactions.get", channel="C", timestamp="1")
    assert caught.value.transient


def test_retry_after_is_bounded():
    assert slack.retry_after(None) == slack.RATE_LIMIT_DEFAULT_WAIT_SECONDS
    assert slack.retry_after("nan") == slack.RATE_LIMIT_DEFAULT_WAIT_SECONDS
    assert slack.retry_after("-1") == slack.RATE_LIMIT_DEFAULT_WAIT_SECONDS
    assert slack.retry_after("999") == slack.RATE_LIMIT_MAX_WAIT_SECONDS


def test_the_harness_reads_the_slack_transport(monkeypatch, tmp_path):
    timing_file = tmp_path / "timing.json"
    monkeypatch.setenv("AGENT_TRANSPORT", harness.TRANSPORT_SLACK)
    monkeypatch.setenv(slack.USER_TOKEN_ENV, "xoxp-test")
    monkeypatch.setenv(slack.CHANNEL_ENV, CHANNEL)
    monkeypatch.setenv(slack.BOT_USER_ENV, BOT)
    monkeypatch.setenv("KUBE_AGENTS_TIMING_FILE", str(timing_file))
    fake = FakeSlack([{"replies": [ANSWER]}])
    monkeypatch.setattr(slack, "SlackClient", lambda token: fake)
    clock = FakeClock()
    monkeypatch.setattr(slack.time, "monotonic", clock.clock)
    monkeypatch.setattr(slack.time, "sleep", clock.sleep)
    result = harness._execute_slack("how many clusters?")
    assert result.output == "*Three clusters.*"
    assert result.metadata["transport"] == harness.TRANSPORT_SLACK
    assert result.metadata["slack_buttons"] is True
    assert result.errors == []
    assert "turn1_s" in json.loads(timing_file.read_text())


@pytest.mark.parametrize("missing", slack.REQUIRED_ENV)
def test_missing_configuration_is_infrastructure(monkeypatch, missing):
    for name in slack.REQUIRED_ENV:
        monkeypatch.setenv(name, "xoxp-x" if name == slack.USER_TOKEN_ENV else "v")
    monkeypatch.delenv(missing)
    result = harness._execute_slack("q")
    assert result.output == ""
    assert result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_a_bot_token_is_refused_as_infrastructure(monkeypatch):
    for name in slack.REQUIRED_ENV:
        monkeypatch.setenv(name, "v")
    monkeypatch.setenv(slack.USER_TOKEN_ENV, "xoxb-bot")
    assert harness._execute_slack("q").errors[0].startswith(harness.INFRA_FAILURE_MARKER)


def test_an_inject_only_directive_is_refused_before_anything_is_posted(monkeypatch):
    monkeypatch.setenv("AGENT_TRANSPORT", harness.TRANSPORT_SLACK)

    def posted(prompt):
        raise AssertionError("the Slack transport ran a stopped ask")

    monkeypatch.setattr(harness, "_execute_slack", posted)
    result = harness.KubeAgentsHarness()._execute(f"{stop_hook.STOP_DIRECTIVE} observe=60\nscale it", opening_turn=True)
    assert result.has_errors() and "needs AGENT_TRANSPORT=inject" in result.errors[0]
