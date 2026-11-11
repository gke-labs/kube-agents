"""Slack agent E2E test: a real conversation through Slack, beside the Google Chat one.

A test user posts "@bot what is 2 + 3?" in a CI channel through Slack's Web API with a
user token. Slack delivers it to the install's bot like any person's message, so this
reaches the agent the way a user does: Socket Mode, the gateway's Slack adapter and the
Block Kit it posts. The test polls the thread and asserts the bot reacted to the ask,
acknowledged it, and answered in time; on `next` it also asserts the answer opens on a
bold headline. It records Slack's timestamps for each leg.

It skips, rather than fails, while the user token or the channel is not configured, so
an environment that has not provisioned the Slack test account reports a skip.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pytest

SLACK_API_BASE = "https://slack.com/api/"
# The three values the test needs. The user token (xoxp-) is a test user's, never the
# bot's: a message the bot posts itself is one the gateway drops as its own.
USER_TOKEN_ENV = "E2E_SLACK_USER_TOKEN"
CHANNEL_ENV = "E2E_SLACK_CHANNEL_ID"
BOT_USER_ENV = "E2E_SLACK_BOT_USER_ID"
REQUIRED_ENV = (USER_TOKEN_ENV, CHANNEL_ENV, BOT_USER_ENV)
USER_TOKEN_PREFIX = "xoxp-"
# Set to "true" where the answer is the gateway's Block Kit (spec.mode next); today's
# Hermes presenter posts text, so the headline is asserted only when asked.
EXPECT_HEADLINE_ENV = "E2E_SLACK_EXPECT_HEADLINE"

TEST_TIMEOUT_SEC = int(os.environ.get("TEST_TIMEOUT_SEC", "120"))
# Short, because a reaction marks work in flight and the gateway removes it once the
# answer lands: a slow poll can miss a fast turn's reaction entirely.
POLL_INTERVAL_SEC = float(os.environ.get("SLACK_POLL_INTERVAL_SEC", "1.5"))
HTTP_TIMEOUT_SEC = 20
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
RATE_LIMIT_DEFAULT_WAIT_SEC = 2.0
RATE_LIMIT_MAX_WAIT_SEC = 10.0
REPLIES_PAGE_LIMIT = 50
EXPECTED_ANSWER_PATTERN = re.compile(r"\b5\b")
PROMPT_TEMPLATE = "<@{bot}> [E2E Slack test {stamp}] what is 2 + 3?"
TIMING_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"


class SlackAPIError(Exception):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


class SlackClient:
    """The Slack Web API over plain HTTPS, as the test user. No dependency beyond the stdlib."""

    def __init__(self, user_token: str, base: str = SLACK_API_BASE) -> None:
        self._user_token = user_token
        self._base = base

    def call(self, method: str, **params) -> dict:
        body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        headers = {"Authorization": f"Bearer {self._user_token}", "Content-Type": "application/x-www-form-urlencoded"}
        status, payload = 0, b""
        for _ in range(2):
            req = urllib.request.Request(self._base + method, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                    status, payload = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, b""
                if status == HTTP_TOO_MANY_REQUESTS:
                    time.sleep(_retry_after(exc.headers.get("Retry-After")))
                    continue
            break
        if status != HTTP_OK:
            raise SlackAPIError(method, f"http_{status}")
        data = json.loads(payload or b"{}")
        if not data.get("ok"):
            raise SlackAPIError(method, str(data.get("error", "unknown_error")))
        return data


def _retry_after(value: str | None) -> float:
    try:
        wait = float(value) if value is not None else RATE_LIMIT_DEFAULT_WAIT_SEC
    except ValueError:
        wait = RATE_LIMIT_DEFAULT_WAIT_SEC
    return min(max(wait, 0.0), RATE_LIMIT_MAX_WAIT_SEC)


def ts_value(ts: str | None) -> Decimal | None:
    try:
        return Decimal(ts) if ts else None
    except InvalidOperation:
        return None


def is_bot_message(msg: dict, bot_user: str) -> bool:
    return msg.get("user") == bot_user or bool(msg.get("bot_id"))


def opens_on_bold_headline(msg: dict) -> bool:
    """Whether the message's first block is the gateway's headline: rich text in bold.

    A section whose mrkdwn opens on `*` counts too, the form an older presenter drew.
    """
    blocks = msg.get("blocks") or []
    if not blocks:
        return False
    first = blocks[0]
    if first.get("type") == "rich_text":
        for section in first.get("elements") or []:
            for element in section.get("elements") or []:
                return bool((element.get("style") or {}).get("bold"))
        return False
    if first.get("type") == "section":
        return (first.get("text") or {}).get("text", "").lstrip().startswith("*")
    return False


@dataclass
class Timeline:
    """What the test saw of the turn, in Slack's own timestamps where Slack gives one.

    A reaction carries no timestamp, so its time is when the poll first saw it.
    """
    posted: Decimal | None = None
    first_reaction_seen: Decimal | None = None
    reaction: str = ""
    ack: Decimal | None = None
    answer: Decimal | None = None
    answer_text: str = ""
    answer_headline: bool = False
    notes: list[str] = field(default_factory=list)

    def legs(self) -> dict:
        def since_post(t: Decimal | None) -> float | None:
            return None if t is None or self.posted is None else round(float(t - self.posted), 3)
        return {
            "post_ts": str(self.posted) if self.posted is not None else None,
            "post_to_first_reaction_s": since_post(self.first_reaction_seen),
            "post_to_ack_s": since_post(self.ack),
            "post_to_answer_s": since_post(self.answer),
            "reaction": self.reaction or None,
        }


def record_timing(timeline: Timeline) -> None:
    line = json.dumps(timeline.legs(), sort_keys=True)
    print(f"[E2E Slack timing] {line}")
    summary = os.environ.get(TIMING_SUMMARY_ENV)
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"\n**Slack E2E timing**: `{line}`\n")


@pytest.fixture(scope="module")
def slack_settings() -> tuple[SlackClient, str, str]:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"Slack E2E not provisioned: {', '.join(missing)} unset (tests/e2e/README.md, 'Slack E2E test')")
    token = os.environ[USER_TOKEN_ENV]
    if not token.startswith(USER_TOKEN_PREFIX):
        pytest.fail(f"{USER_TOKEN_ENV} must be a user token ({USER_TOKEN_PREFIX}...): a bot's own message is dropped as the bot's")
    return SlackClient(token), os.environ[CHANNEL_ENV], os.environ[BOT_USER_ENV]


def _bot_reaction(client: SlackClient, channel: str, ts: str, bot_user: str) -> str:
    message = client.call("reactions.get", channel=channel, timestamp=ts, full="true").get("message") or {}
    for reaction in message.get("reactions") or []:
        if bot_user in (reaction.get("users") or []):
            return str(reaction.get("name", ""))
    return ""


def _bot_replies(client: SlackClient, channel: str, ts: str, bot_user: str) -> list[dict]:
    messages = client.call("conversations.replies", channel=channel, ts=ts, limit=REPLIES_PAGE_LIMIT).get("messages") or []
    return [m for m in messages if m.get("ts") != ts and is_bot_message(m, bot_user)]


def _answer(replies: list[dict]) -> dict | None:
    for msg in reversed(replies):
        if EXPECTED_ANSWER_PATTERN.search(msg.get("text", "")):
            return msg
    return None


def test_slack_agent_math_response(slack_settings: tuple[SlackClient, str, str]) -> None:
    client, channel, bot_user = slack_settings
    expect_headline = os.environ.get(EXPECT_HEADLINE_ENV, "").lower() == "true"
    prompt = PROMPT_TEMPLATE.format(bot=bot_user, stamp=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    print(f"\n[E2E Slack] channel={channel} bot={bot_user} expect_headline={expect_headline}")
    print(f"[E2E Slack] prompt: {prompt!r}")

    try:
        sent = client.call("chat.postMessage", channel=channel, text=prompt)
    except SlackAPIError as err:
        pytest.fail(f"Could not post the ask as the test user: {err}")
    ask_ts = sent["ts"]
    timeline = Timeline(posted=ts_value(ask_ts))
    print(f"[E2E Slack] posted ask ts={ask_ts}")

    deadline = time.monotonic() + TEST_TIMEOUT_SEC
    answer: dict | None = None
    try:
        while time.monotonic() < deadline and answer is None:
            time.sleep(POLL_INTERVAL_SEC)
            try:
                if not timeline.reaction:
                    name = _bot_reaction(client, channel, ask_ts, bot_user)
                    if name:
                        timeline.reaction = name
                        timeline.first_reaction_seen = Decimal(str(time.time()))
                        print(f"[E2E Slack] bot reacted :{name}:")
                replies = _bot_replies(client, channel, ask_ts, bot_user)
            except SlackAPIError as err:
                if err.error == f"http_{HTTP_TOO_MANY_REQUESTS}" or err.error.startswith("http_5"):
                    print(f"[E2E Slack] transient read error, polling again: {err}")
                    continue
                pytest.fail(f"Cannot read the thread back as the test user: {err}")
            if replies and timeline.ack is None:
                timeline.ack = ts_value(replies[0].get("ts"))
                print(f"[E2E Slack] first bot reply (ack) ts={replies[0].get('ts')}: {replies[0].get('text', '')[:80]!r}")
            answer = _answer(replies)
        if answer is not None:
            edited = (answer.get("edited") or {}).get("ts")
            timeline.answer = ts_value(edited) or ts_value(answer.get("ts"))
            timeline.answer_text = answer.get("text", "")
            timeline.answer_headline = opens_on_bold_headline(answer)
    finally:
        record_timing(timeline)

    assert answer is not None, f"No answer containing '5' in the thread within {TEST_TIMEOUT_SEC}s"
    assert timeline.ack is not None, "The bot never replied in the ask's thread"
    assert timeline.reaction, (
        "The bot's reaction on the ask was never seen; it marks work in flight and is removed "
        f"once the answer lands, so a turn faster than the {POLL_INTERVAL_SEC}s poll can hide it"
    )
    if expect_headline:
        assert timeline.answer_headline, f"The answer does not open on a bold headline: blocks={json.dumps(answer.get('blocks'))[:400]}"
    print(f"[E2E Slack SUCCESS] answer: {timeline.answer_text[:120]!r}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
