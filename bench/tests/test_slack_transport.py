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


STATUS = {"ts": "1800000001.000200", "user": BOT, "bot_id": "B1", "text": "⚙️ *working*"}
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


ACK = {"ts": "1800000002.000100", "user": BOT, "text": "I've asked the seeded-b cluster agent; it will post here."}
CARD = {"ts": "1800000005.000100", "user": BOT, "text": "checkout-gateway runs 3 replicas on seeded-b."}
QUESTION = {"ts": "1800000006.000100", "user": BOT, "text": "Which cluster should I scale?",
            "blocks": [{"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "seeded-a"}}]}]}
NOTE = {"ts": "1800000003.000100", "user": BOT, "text": "ℹ️ looking at seeded-b"}


def test_the_slack_report_is_the_report_the_other_transports_build(monkeypatch):
    """The same thread content folds to the same report _append_delivered builds on api and inject."""
    posts = [STATUS, ACK, NOTE, CARD, QUESTION]
    got = harness._slack_report(posts)

    card_id, question_id = slack.post_id(CARD), slack.post_id(QUESTION)
    monkeypatch.setattr(harness, "delivered_results", lambda observed, ids: {card_id: CARD["text"]})
    monkeypatch.setattr(harness, "paused_questions", lambda observed, ids: {question_id: QUESTION["text"]})
    reference = harness.AgentResult(output=ACK["text"], trajectory=[], tokens={}, errors=[],
                                    metadata={"final_message": ACK["text"]})
    harness._append_delivered(reference, [], [card_id, question_id])

    assert got == reference.output == reference.metadata["final_message"]
    assert got.startswith(ACK["text"])
    assert f"Result of delegated task {card_id}:\n{CARD['text']}" in got
    assert f"Question from delegated task {question_id}:\n{QUESTION['text']}" in got
    assert "looking at" not in got and "working" not in got


def test_a_direct_answer_is_the_whole_report():
    assert harness._slack_report([STATUS, ANSWER]) == ANSWER["text"]


class ThreadSlack(FakeSlack):
    """FakeSlack whose second post (the lane's reply) gets its own ts, so the follow-up reads after it."""

    REPLY_TS = "1800000010.000100"

    def call(self, method, **params):
        if method == "chat.postMessage" and self.posted:
            self.posted.append(params)
            return {"ok": True, "ts": self.REPLY_TS}
        return super().call(method, **params)


def test_a_follow_up_replies_in_the_thread_and_reads_only_what_came_after_it():
    later = {"ts": "1800000012.000100", "user": BOT, "text": "Only the platform agent can change a deployment."}
    fake = ThreadSlack([{"replies": [ANSWER]}, {"replies": [ANSWER]}, {"replies": [ANSWER, later]}])
    clock = FakeClock()
    turn = _turn(fake, clock)
    first = turn.run("what can you do?", 60)
    assert first.outcome == "settled"
    then = turn.follow_up(first, "which can change a deployment?", 60)
    assert fake.posted[1]["thread_ts"] == ASK_TS
    assert fake.posted[1]["text"] == f"<@{BOT}> which can change a deployment?"
    assert then.outcome == "settled" and then.deliverable == later["text"]
    assert [p["ts"] for p in then.posts] == [later["ts"]]
    assert then.timing()["slack_post_to_answer_s"] == 2.0


def test_the_then_turn_is_recorded_not_graded():
    later = {"ts": "1800000012.000100", "user": BOT, "text": "Only the platform agent can."}
    fake = ThreadSlack([{"replies": [ANSWER]}, {"replies": [ANSWER]}, {"replies": [ANSWER, later]}])
    turn = _turn(fake, FakeClock())
    first = turn.run("q", 60)
    out = harness._slack_then(turn, first, "and?", 60)
    assert out["slack_then_outcome"] == "settled"
    assert out["slack_then_posts"] == 1
    assert out["slack_then_post_to_answer_s"] == 2.0
    assert set(out) >= {"slack_then_post_to_first_reaction_s", "slack_then_post_to_ack_s", "slack_then_reaction"}


def test_a_then_turn_that_fails_is_recorded_as_an_error():
    class Refusing(ThreadSlack):
        def call(self, method, **params):
            if method == "chat.postMessage" and self.posted:
                raise slack.SlackAPIError(method, "not_in_channel")
            return super().call(method, **params)

    fake = Refusing([{"replies": [ANSWER]}])
    turn = _turn(fake, FakeClock())
    first = turn.run("q", 60)
    assert harness._slack_then(turn, first, "and?", 60) == {"slack_then_outcome": "error: chat.postMessage: not_in_channel"}


def test_a_typed_answer_waits_while_the_first_ask_is_marked_in_flight():
    """The gateway marks the continued first ask, not the reply, while the resumed work runs."""
    interim = {"ts": "1800000011.000100", "user": BOT, "text": "picking it back up"}
    done = {"ts": "1800000030.000100", "user": BOT, "text": "Left as it is."}

    class ContinuedSlack(ThreadSlack):
        def call(self, method, **params):
            if method == "reactions.get":
                step = self.steps[min(self.polls, len(self.steps) - 1)]
                names = step.get("root", []) if params.get("timestamp") == ASK_TS else []
                return {"ok": True, "message": {"reactions": [{"name": n, "users": [BOT]} for n in names]}}
            return super().call(method, **params)

    # Turn 1 settles blocked ([eyes, ⏸️]); the answer's interim post lands while the
    # first ask still carries that mark, then the continuing task marks it in flight,
    # then its terminal clears it.
    paused = ["eyes", "double_vertical_bar"]
    steps = [{"replies": [ANSWER], "root": paused}] * 5 + \
        [{"replies": [ANSWER, interim], "root": paused}] * 20 + \
        [{"replies": [ANSWER, interim], "root": ["eyes"]}] * 10 + \
        [{"replies": [ANSWER, interim, done]}]
    fake = ContinuedSlack(steps)
    turn = _turn(fake, FakeClock())
    first = turn.run("q", 60)
    then = turn.follow_up(first, "none of those", 120)
    assert then.outcome == "settled"
    assert then.deliverable == "Left as it is.", "settled on the interim post while the first ask was still marked"
    assert then.reaction == "" and then.reaction_seen is None, "the first ask's mark was read as the reply's own"


def test_a_follow_up_after_a_failed_first_turn_settles():
    """A failed first ask keeps its ❌ for good; it must not hold the follow-up to the deadline."""
    later = {"ts": "1800000012.000100", "user": BOT, "text": "Here is what I can do."}

    class FailedFirst(ThreadSlack):
        def call(self, method, **params):
            if method == "reactions.get":
                on_root = params.get("timestamp") == ASK_TS
                return {"ok": True, "message": {"reactions": [{"name": "x", "users": [BOT]}] if on_root else []}}
            return super().call(method, **params)

    fake = FailedFirst([{"replies": [ANSWER]}, {"replies": [ANSWER]}, {"replies": [ANSWER, later]}])
    turn = _turn(fake, FakeClock())
    first = turn.run("q", 60)
    then = turn.follow_up(first, "and?", 300)
    assert then.outcome == "settled" and then.deliverable == later["text"]


# A reply under a post the agent made on its own (HOME_POST_DIRECTIVE).
HOME_TS = "1800000000.000050"
MARKER = "compliance-audit 2026-11-20"
HOME_POST = {"ts": HOME_TS, "user": BOT, "bot_id": "B1", "text": f"*Report* {MARKER}: 2 findings."}


class HomeSlack(FakeSlack):
    """FakeSlack with a channel history: each conversations.history call takes the next scripted page."""

    def __init__(self, histories, steps):
        super().__init__(steps)
        self.histories = histories
        self.history_calls = []

    def call(self, method, **params):
        if method == "conversations.history":
            self.history_calls.append(params)
            return {"ok": True, "messages": self.histories[min(len(self.history_calls) - 1, len(self.histories) - 1)]}
        return super().call(method, **params)


def test_a_home_post_prompt_is_parsed_and_any_other_is_not():
    assert slack.parse_home_post("how many clusters?") is None
    assert slack.parse_home_post(f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\nhow many findings?\n") == (MARKER, "how many findings?")
    for bad in (f"{slack.HOME_POST_DIRECTIVE} {MARKER}\nq", f"{slack.HOME_POST_DIRECTIVE} ]\nq", f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\n  "):
        with pytest.raises(ValueError):
            slack.parse_home_post(bad)


def test_the_home_post_is_the_bots_newest_top_level_post_holding_the_marker():
    older = {**HOME_POST, "ts": "1799999000.000100"}
    reply_in_thread = {"ts": "1800000001.000100", "thread_ts": "1799999000.000100", "user": BOT, "text": MARKER}
    test_user = {"ts": "1800000002.000100", "user": "UTEST", "text": MARKER}
    other_app = {"ts": "1800000003.000100", "user": "UOTHER", "bot_id": "BOTHER", "text": MARKER}
    unmarked = {"ts": "1800000004.000100", "user": BOT, "text": "another report"}
    fake = HomeSlack([[older, HOME_POST, reply_in_thread, test_user, other_app, unmarked]], [{}])
    post = _turn(fake, FakeClock()).find_home_post(MARKER, 1799996401.5, 60)
    assert post["ts"] == HOME_TS
    assert fake.history_calls[0]["oldest"] == "1799996401.500000"


def test_a_home_post_is_watched_for_until_it_lands_and_given_up_on_after():
    late = HomeSlack([[], [], [HOME_POST]], [{}])
    assert _turn(late, FakeClock()).find_home_post(MARKER, 0, 60)["ts"] == HOME_TS
    never = HomeSlack([[]], [{}])
    clock = FakeClock()
    assert _turn(never, clock).find_home_post(MARKER, 0, 10) is None
    assert clock.now >= 10


def test_the_home_post_search_reads_every_page_and_polls_through_a_transient_error():
    class Flaky(HomeSlack):
        def call(self, method, **params):
            if method == "conversations.history":
                self.history_calls.append(params)
                if len(self.history_calls) == 1:
                    raise slack.SlackAPIError("conversations.history", "http_503")
                if not params.get("cursor"):
                    return {"ok": True, "messages": [STATUS], "response_metadata": {"next_cursor": "p2"}}
                return {"ok": True, "messages": [HOME_POST]}
            return super().call(method, **params)

    errors = []
    post = _turn(Flaky([], [{}]), FakeClock()).find_home_post(MARKER, 0, 60, errors)
    assert post["ts"] == HOME_TS
    assert len(errors) == 1


def test_a_reply_under_the_home_post_reads_only_what_came_after_it():
    earlier = {"ts": "1800000000.000060", "user": BOT, "text": "an earlier report in the day's thread"}
    answer = {"ts": "1800000012.000100", "user": BOT, "text": "Two findings: CIS-1.2 and CIS-4.1."}
    reply_ts = "1800000010.000100"

    class Replying(HomeSlack):
        def call(self, method, **params):
            if method == "chat.postMessage":
                self.posted.append(params)
                return {"ok": True, "ts": reply_ts}
            return super().call(method, **params)

    fake = Replying([[HOME_POST]], [{"replies": [earlier]}, {"replies": [earlier, answer]}])
    ex = _turn(fake, FakeClock()).reply_under(HOME_TS, "how many findings?", 60)
    assert fake.posted[0]["thread_ts"] == HOME_TS
    assert fake.posted[0]["text"] == f"<@{BOT}> how many findings?"
    assert ex.outcome == "settled" and [p["ts"] for p in ex.posts] == [answer["ts"]]


def _slack_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TRANSPORT", harness.TRANSPORT_SLACK)
    monkeypatch.setenv(slack.USER_TOKEN_ENV, "xoxp-test")
    monkeypatch.setenv(slack.CHANNEL_ENV, CHANNEL)
    monkeypatch.setenv(slack.BOT_USER_ENV, BOT)
    monkeypatch.setenv("KUBE_AGENTS_TIMING_FILE", str(tmp_path / "timing.json"))
    clock = FakeClock()
    monkeypatch.setattr(slack.time, "monotonic", clock.clock)
    monkeypatch.setattr(slack.time, "sleep", clock.sleep)
    monkeypatch.setattr(slack.time, "time", lambda: 1800000001.5)


def test_the_harness_replies_under_the_home_post(monkeypatch, tmp_path):
    _slack_env(monkeypatch, tmp_path)
    monkeypatch.setenv(slack.THEN_ENV, "and then?")
    answer = {"ts": "1800000012.000100", "user": BOT, "text": "Two findings."}
    fake = HomeSlack([[HOME_POST]], [{"replies": [answer]}])
    monkeypatch.setattr(slack, "SlackClient", lambda token: fake)
    result = harness._execute_slack(f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\nhow many findings?")
    assert result.output == "Two findings." and result.errors == []
    assert result.metadata["slack_home_post_ts"] == HOME_TS
    assert result.metadata["slack_then_outcome"].startswith("skipped")
    assert [p["thread_ts"] for p in fake.posted] == [HOME_TS]


def test_the_case_start_bounds_the_search_when_the_lane_sets_it(monkeypatch, tmp_path):
    _slack_env(monkeypatch, tmp_path)
    monkeypatch.setenv(slack.CASE_START_ENV, "1800000050")
    answer = {"ts": "1800000012.000100", "user": BOT, "text": "Two findings."}
    fake = HomeSlack([[HOME_POST]], [{"replies": [answer]}])
    monkeypatch.setattr(slack, "SlackClient", lambda token: fake)
    harness._execute_slack(f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\nhow many findings?")
    # Less the clock-skew margin.
    assert fake.history_calls[0]["oldest"] == "1799999990.000000"


@pytest.mark.parametrize("name,value", [(slack.HOME_POST_WAIT_ENV, "inf"), (slack.HOME_POST_LOOKBACK_ENV, "nan"),
                                        (slack.CASE_START_ENV, "-1")])
def test_a_home_post_knob_that_is_not_finite_seconds_is_refused(monkeypatch, tmp_path, name, value):
    _slack_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    fake = HomeSlack([[HOME_POST]], [{}])
    monkeypatch.setattr(slack, "SlackClient", lambda token: fake)
    result = harness._execute_slack(f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\nq")
    assert result.has_errors() and name in result.errors[0] and fake.posted == []


def test_no_home_post_is_a_graded_failure_not_infrastructure(monkeypatch, tmp_path):
    _slack_env(monkeypatch, tmp_path)
    monkeypatch.setenv(slack.HOME_POST_WAIT_ENV, "5")
    fake = HomeSlack([[]], [{}])
    monkeypatch.setattr(slack, "SlackClient", lambda token: fake)
    result = harness._execute_slack(f"{slack.HOME_POST_DIRECTIVE} {MARKER}]\nhow many findings?")
    assert result.output == "" and fake.posted == []
    assert result.errors and not result.errors[0].startswith(harness.INFRA_FAILURE_MARKER)
    assert result.metadata["outcome"] == "no_home_post"


def test_a_home_post_directive_is_refused_off_the_slack_transport(monkeypatch):
    h = harness.KubeAgentsHarness()

    def sent(*args, **kwargs):
        raise AssertionError("a home-post prompt was sent off the Slack transport")

    monkeypatch.setattr(h, "_execute_inject", sent)
    monkeypatch.setattr(h, "_execute_api", sent)
    for transport in (harness.TRANSPORT_INJECT, harness.TRANSPORT_API):
        monkeypatch.setenv("AGENT_TRANSPORT", transport)
        result = h._execute(f"  \n{slack.HOME_POST_DIRECTIVE} {MARKER}]\nq", opening_turn=True)
        assert result.has_errors() and "needs AGENT_TRANSPORT=slack" in result.errors[0]
