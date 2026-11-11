"""Slack agent E2E test: a real conversation through Slack, beside the Google Chat one.

A test user posts "@bot what is 2 + 3?" in a CI channel through Slack's Web API with a
user token, and Slack delivers it to the install's bot over Socket Mode. Slack attributes
a post made with a user token through an app to that app (bot_id), which the A2A gateway
drops as bot traffic unless the install lists the test user in A2A_SLACK_TEST_USERS (the
chart's operator.slackTestUsers); a preflight reads the posted ask back and fails at
once, naming that, when the install does not. The test polls the thread, fails at once
when the bot's first reply is a refusal or a not-started notice, or on next when the turn
fails (quoting it), and asserts an answer saying 5 arrived in time and, on next, that it
opens on a bold headline; it records Slack's timestamps for each leg.

The bot's reaction on the ask is asserted on today, where the arrival reaction stays up
for the turn. On next it comes only from the hermes-bridge's 60-second progress
heartbeat, so a faster turn shows none (#5499): there the check is expected to fail, and
reports, rather than fails, until that is fixed.

It skips while the user token, the channel or the bot's member id is unset. The Slack
client is hack/slack-live-check/harness.py's, loaded from the checkout.
"""

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LIVE_CHECK = _REPO_ROOT / "hack" / "slack-live-check" / "harness.py"


def _load_live_check():
    spec = importlib.util.spec_from_file_location("slack_live_check_harness", _LIVE_CHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = _load_live_check()

# The three values the test needs. The user token (xoxp- or xoxe.xoxp-) is a test user's,
# never the bot's.
USER_TOKEN_ENV = "E2E_SLACK_USER_TOKEN"
CHANNEL_ENV = "E2E_SLACK_CHANNEL_ID"
BOT_USER_ENV = "E2E_SLACK_BOT_USER_ID"
REQUIRED_ENV = (USER_TOKEN_ENV, CHANNEL_ENV, BOT_USER_ENV)
# "next" where the install runs spec.mode next (e2e-run.yml sets it there): the answer is
# the gateway's Block Kit, so its headline is asserted, and the reaction check is the
# expected failure #5499 records. Unset, the PlatformAgent's spec.mode is read with
# kubectl, so a manual run against any cluster asserts what its mode promises.
MODE_ENV = "E2E_SLACK_MODE"
MODE_NEXT = "next"
REACTION_GAP_ISSUE = "#5499"

TEST_TIMEOUT_SEC = int(os.environ.get("TEST_TIMEOUT_SEC", "120"))
POLL_INTERVAL_SEC = float(os.environ.get("SLACK_POLL_INTERVAL_SEC", "1.5"))
REPLIES_PAGE_LIMIT = 50
EXPECTED_ANSWER_PATTERN = re.compile(r"\b5\b")
PROMPT_TEMPLATE = "<@{bot}> [E2E Slack test {stamp}] what is 2 + 3?"
TIMING_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

DEFAULT_AGENT_NAMESPACE = "kubeagents-system"
AGENT_NAMESPACE = os.environ.get("AGENT_NAMESPACE") or os.environ.get("NAMESPACE") or DEFAULT_AGENT_NAMESPACE
GATEWAY_DEPLOYMENT = "platform-agent-a2a-gateway"
PLATFORM_AGENT_RESOURCE = "platformagents"
TEST_USERS_ENV_ON_GATEWAY = "A2A_SLACK_TEST_USERS"
KUBECTL_TIMEOUT_SEC = 30


def is_bot_message(msg: dict, bot_user: str) -> bool:
    """Whether the install's bot posted it: by its member id, never by a bot_id alone,
    which another app's post, or a test user's post through one, also carries."""
    return msg.get("user") == bot_user


def opens_on_bold_headline(msg: dict) -> bool:
    """Whether the message's first block is the gateway's headline: rich text in bold
    (slackChatHead in a2a/gateway/slack_chat.go), or a section whose mrkdwn opens on `*`."""
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


# A first bot reply of one of these kinds means the turn never started: the test fails
# on it at once, quoting it, instead of waiting out the timeout for an answer.
ACK_REFUSED_KINDS = (live.KIND_REFUSAL, live.KIND_FAILURE, live.KIND_NOT_STARTED)


def _kubectl_json(*args: str) -> dict | None:
    """kubectl get ... -o json in the agent's namespace, or None when kubectl cannot say."""
    cmd = ["kubectl", "get", *args, "-n", AGENT_NAMESPACE, "-o", "json"]
    if os.environ.get("KUBE_CONTEXT"):
        cmd[1:1] = ["--context", os.environ["KUBE_CONTEXT"]]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=KUBECTL_TIMEOUT_SEC)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return json.loads(res.stdout)


def install_mode() -> str | None:
    """E2E_SLACK_MODE when set, else the PlatformAgent's spec.mode ("" reads as today), or
    None when neither says, in which case the mode's own checks are skipped."""
    if os.environ.get(MODE_ENV):
        return os.environ[MODE_ENV]
    agents = _kubectl_json(PLATFORM_AGENT_RESOURCE)
    for item in (agents or {}).get("items") or []:
        return (item.get("spec") or {}).get("mode", "")
    return None


def gateway_test_users() -> list[str] | None:
    """The A2A gateway's A2A_SLACK_TEST_USERS, or None when kubectl cannot say."""
    deployment = _kubectl_json("deployment", GATEWAY_DEPLOYMENT)
    if deployment is None:
        return None
    for container in deployment["spec"]["template"]["spec"].get("containers", []):
        for env in container.get("env") or []:
            if env.get("name") == TEST_USERS_ENV_ON_GATEWAY:
                if "value" not in env:
                    return None  # from a ConfigMap or Secret: unknown here, so not a refusal
                return [u.strip() for u in env.get("value", "").split(",") if u.strip()]
    return []


@dataclass
class Timeline:
    """What the test saw of the turn, in Slack's own timestamps where Slack gives one.

    A reaction carries no timestamp, so its time is when the poll first saw it.
    """
    posted: Decimal | None = None
    first_reaction_seen: Decimal | None = None
    reaction: str = ""
    first_reply: Decimal | None = None
    ack_kind: str = ""
    answer: Decimal | None = None
    answer_text: str = ""
    answer_headline: bool = False

    def legs(self) -> dict:
        def since_post(t: Decimal | None) -> float | None:
            return None if t is None or self.posted is None else round(float(t - self.posted), 3)
        return {
            "post_ts": str(self.posted) if self.posted is not None else None,
            "post_to_first_reaction_s": since_post(self.first_reaction_seen),
            "post_to_first_reply_s": since_post(self.first_reply),
            "first_reply_kind": self.ack_kind or None,
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
def slack_settings():
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(f"Slack E2E not provisioned: {', '.join(missing)} unset (tests/e2e/README.md, 'Slack E2E test')")
    token = os.environ[USER_TOKEN_ENV]
    if not token.startswith(live.USER_TOKEN_PREFIXES):
        pytest.fail(f"{USER_TOKEN_ENV} must be a user token ({' or '.join(live.USER_TOKEN_PREFIXES)}...)")
    return live.SlackClient(token, live.UrllibTransport()), os.environ[CHANNEL_ENV], os.environ[BOT_USER_ENV]


def _bot_reaction(client, channel: str, ts: str, bot_user: str) -> str:
    message = client.call("reactions.get", channel=channel, timestamp=ts, full="true").get("message") or {}
    for reaction in message.get("reactions") or []:
        if bot_user in (reaction.get("users") or []):
            return str(reaction.get("name", ""))
    return ""


def _thread(client, channel: str, ts: str) -> list[dict]:
    return client.call("conversations.replies", channel=channel, ts=ts, limit=REPLIES_PAGE_LIMIT).get("messages") or []


def _turn(replies: list[dict], next_mode: bool) -> tuple[dict, str] | None:
    """The bot's reply to the ask and its kind, once there is one, from its messages oldest
    first. On next, the gateway's grammar decides (harness.read_turn): a refusal, a
    not-started notice or a failed status line ends the turn, and the answer is the post
    after the status line once it reads completed, so a status line quoting the result is
    never taken for it. Elsewhere, the first-reply check stands and any bot message
    containing the expected answer is the answer."""
    if next_mode:
        has_line = any(live.status_state(m.get("text", "")) for m in replies)
        return live.read_turn(replies, live.WAIT_ANSWER, lineless=not has_line)
    msg = _answer(replies)
    return (msg, live.KIND_ANSWER) if msg is not None else None


def _answer(replies: list[dict]) -> dict | None:
    for msg in reversed(replies):
        if EXPECTED_ANSWER_PATTERN.search(msg.get("text", "")):
            return msg
    return None


def preflight_ask(client, channel: str, ask_ts: str) -> None:
    """Fail at once when the ask is app-attributed and the gateway does not list its author.

    Without A2A_SLACK_TEST_USERS naming the test user, the gateway drops the ask as bot
    traffic and the wait would run to the timeout saying nothing.
    """
    ask = next((m for m in _thread(client, channel, ask_ts) if m.get("ts") == ask_ts), None)
    if not ask or not ask.get("bot_id"):
        return
    listed = gateway_test_users()
    if listed is None:
        print(f"[E2E Slack] the ask carries bot_id={ask['bot_id']}; kubectl cannot read the gateway's "
              f"{TEST_USERS_ENV_ON_GATEWAY}, so this run relies on it naming {ask.get('user')}")
        return
    if ask.get("user") not in listed:
        pytest.fail(
            f"the ask carries bot_id={ask['bot_id']} (a user-token post through an app), and the gateway's "
            f"{TEST_USERS_ENV_ON_GATEWAY} does not list its author {ask.get('user')}, so the gateway drops it as "
            "bot traffic. Either the install does not list the test user in operator.slackTestUsers, or the "
            "A2A gateway is not running Slack: under next, Google Chat holds the gateway when both are enabled, "
            "and Slack then stays on the Hermes path, which has no such list"
        )


def test_slack_agent_math_response(slack_settings) -> None:
    client, channel, bot_user = slack_settings
    mode = install_mode()
    next_mode = mode == MODE_NEXT
    prompt = PROMPT_TEMPLATE.format(bot=bot_user, stamp=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    print(f"\n[E2E Slack] channel={channel} bot={bot_user} mode={mode!r}")
    if mode is None:
        print(f"[E2E Slack] {MODE_ENV} is unset and kubectl cannot read the PlatformAgent, so the mode's "
              "own checks (headline, reaction) are skipped")
    print(f"[E2E Slack] prompt: {prompt!r}")

    try:
        sent = client.call("chat.postMessage", channel=channel, text=prompt)
    except live.SlackAPIError as err:
        pytest.fail(f"Could not post the ask as the test user: {err}")
    ask_ts = sent["ts"]
    timeline = Timeline(posted=live.ts_value(ask_ts))
    print(f"[E2E Slack] posted ask ts={ask_ts}")
    preflight_ask(client, channel, ask_ts)

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
                replies = [m for m in _thread(client, channel, ask_ts) if m.get("ts") != ask_ts and is_bot_message(m, bot_user)]
            except (live.SlackAPIError, live.HarnessError, OSError) as err:
                if live.transient(err):
                    print(f"[E2E Slack] transient read error, polling again: {err}")
                    continue
                pytest.fail(f"Cannot read the thread back as the test user: {err}")
            if replies and timeline.first_reply is None:
                timeline.first_reply = live.ts_value(replies[0].get("ts"))
                timeline.ack_kind = live.classify(replies[0].get("text", ""))
                print(f"[E2E Slack] first bot reply is a {timeline.ack_kind}: {replies[0].get('text', '')[:120]!r}")
                # On next a failed status line is left to _turn, which quotes the
                # failure notice that follows it rather than the bare line.
                if timeline.ack_kind in ACK_REFUSED_KINDS and not (next_mode and timeline.ack_kind == live.KIND_FAILURE):
                    pytest.fail(f"The bot's first reply is a {timeline.ack_kind}, so the turn never ran: "
                                f"{replies[0].get('text', '')[:300]!r}")
            turn = _turn(replies, next_mode)
            if turn is not None:
                msg, kind = turn
                if kind != live.KIND_ANSWER:
                    pytest.fail(f"The turn ended as a {kind}: {msg.get('text', '')[:300]!r}")
                answer = msg
        if answer is not None:
            edited = (answer.get("edited") or {}).get("ts")
            timeline.answer = live.ts_value(edited) or live.ts_value(answer.get("ts"))
            timeline.answer_text = answer.get("text", "")
            timeline.answer_headline = opens_on_bold_headline(answer)
    finally:
        record_timing(timeline)

    assert answer is not None, f"No answer in the thread within {TEST_TIMEOUT_SEC}s"
    assert EXPECTED_ANSWER_PATTERN.search(timeline.answer_text), f"The answer does not say 5: {timeline.answer_text[:300]!r}"
    if mode is None:
        pass
    elif next_mode:
        assert timeline.answer_headline, f"The answer does not open on a bold headline: blocks={json.dumps(answer.get('blocks'))[:400]}"
        if timeline.reaction:
            print(f"[E2E Slack] the bot reacted on next; {REACTION_GAP_ISSUE} may be fixed, so make this check hard")
        else:
            print(f"[E2E Slack] expected failure: no reaction on the ask on next ({REACTION_GAP_ISSUE})")
    else:
        assert timeline.reaction, "The bot never reacted to the ask"
    print(f"[E2E Slack SUCCESS] answer: {timeline.answer_text[:120]!r}")
