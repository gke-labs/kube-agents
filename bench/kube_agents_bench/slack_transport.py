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

"""The Slack transport behind ``AGENT_TRANSPORT=slack``.

The other two transports skip Slack on purpose: ``api`` is the agent's own
endpoint and ``inject`` enters the A2A gateway through its side door. This one
goes the way a person does. A test user mentions the install's bot in a CI
channel with ``chat.postMessage`` and a user token, Slack delivers it over
Socket Mode, and the transport reads the thread back until the turn settles.
What it grades is the report the thread delivered (split_thread; the harness folds it), and it times each Slack leg:
post to the bot's first reaction, to its first reply (the ack), and to the
answer.

Slack attributes a post made with a user token through an app to the app
(``bot_id``), which the gateway drops as bot traffic unless the install lists
the test user in ``A2A_SLACK_TEST_USERS`` (the chart's
``operator.slackTestUsers``). The install must also allow the user, and the
bot and the user must both be in the channel.

A turn has settled once the thread holds a bot reply, the bot's reaction on the
ask is gone or is one of the settle marks (the gateway removes the in-flight
mark once work succeeds, and leaves ⏸️ on a blocked turn and ❌ on a failed one,
a2a/gateway/slack_reactions.go), and no bot message has arrived or changed for
:data:`SETTLE_QUIET_SECONDS`.

Advisory: the lane this transport serves gates nothing. Only the standard
library is used, like the inject transport and ``hack/slack-live-check``.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

SLACK_API_BASE = "https://slack.com/api/"
# The gateway's progress posts, which carry no content of the turn's own: the
# status line's placeholder and its "<icon> *<state>*[ — <progress>]" edits
# (relay.go statusLine, terminalLine), and a non-final status note (relay.go,
# "ℹ️ " + note). hack/slack-live-check/harness.py reads the same grammar.
STATUS_PLACEHOLDER = "⏳ submitted…"
STATUS_LINE_PATTERN = re.compile(r"\S+ \*[a-z-]+\*(?: — .*)?", re.DOTALL)
STATUS_NOTE_PREFIX = "ℹ️ "
# The id a thread post stands under in a delivered section: Slack shows no
# task id, so the post's own timestamp names it.
POST_ID_PREFIX = "slack-"
# The three values the transport needs, from the environment; the lane script
# reads the token from Secret Manager into the process and never onto disk.
USER_TOKEN_ENV = "BENCH_SLACK_USER_TOKEN"
CHANNEL_ENV = "BENCH_SLACK_CHANNEL_ID"
BOT_USER_ENV = "BENCH_SLACK_BOT_USER_ID"
REQUIRED_ENV = (USER_TOKEN_ENV, CHANNEL_ENV, BOT_USER_ENV)
# A user token, rotating (xoxe.xoxp-) or not; never the bot's.
USER_TOKEN_PREFIXES = ("xoxp-", "xoxe.xoxp-")
# Where the turn's three Slack legs are written as JSON, when set; the lane
# script sets it beside the run's KUBE_AGENTS_TIMING_FILE, which carries only
# the post-to-answer leg the scorer reads.
TIMING_FILE_ENV = "BENCH_SLACK_TIMING_FILE"

POLL_SECONDS = 2.0
# The bot's reactions that end a turn rather than mark it in flight: blocked and
# failed (slackMarkEmoji in a2a/gateway/slack_reactions.go).
SETTLE_REACTIONS = frozenset({"double_vertical_bar", "x"})
# How long the thread must stay unchanged, after the reaction is gone, for the
# turn to count as settled: long enough to cover the gap between a status line's
# last edit and the answer's post.
SETTLE_QUIET_SECONDS = 15.0
HTTP_TIMEOUT_SECONDS = 20
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
# The error a request that never got an HTTP answer (DNS, connect, reset, read
# timeout, a short read) is reported under; transient, so the poll goes on.
CONNECTION_ERROR = "connection_error"
RATE_LIMIT_DEFAULT_WAIT_SECONDS = 2.0
RATE_LIMIT_MAX_WAIT_SECONDS = 15.0
RATE_LIMIT_MAX_RETRIES = 3
REPLIES_PAGE_LIMIT = 100


class SlackAPIError(Exception):
    def __init__(self, method: str, error: str) -> None:
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error

    @property
    def transient(self) -> bool:
        return (self.error == f"http_{HTTP_TOO_MANY_REQUESTS}" or self.error.startswith("http_5")
                or self.error.startswith(CONNECTION_ERROR))


class SlackClient:
    """The Slack Web API over plain HTTPS, as the test user."""

    def __init__(self, user_token: str, base: str = SLACK_API_BASE,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._user_token = user_token
        self._base = base
        self._sleep = sleep

    def call(self, method: str, **params: str) -> dict:
        body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        headers = {"Authorization": f"Bearer {self._user_token}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        status, payload = 0, b""
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            req = urllib.request.Request(self._base + method, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    status, payload = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, payload = exc.code, b""
                if status == HTTP_TOO_MANY_REQUESTS and attempt < RATE_LIMIT_MAX_RETRIES:
                    self._sleep(retry_after(exc.headers.get("Retry-After")))
                    continue
            except (OSError, http.client.HTTPException) as exc:
                # URLError, a timeout, a reset or a short read: no answer at all.
                raise SlackAPIError(method, f"{CONNECTION_ERROR} ({type(exc).__name__})") from exc
            break
        if status != HTTP_OK:
            raise SlackAPIError(method, f"http_{status}")
        data = json.loads(payload or b"{}")
        if not data.get("ok"):
            raise SlackAPIError(method, str(data.get("error", "unknown_error")))
        return data


def retry_after(value: str | None) -> float:
    try:
        wait = float(value) if value is not None else RATE_LIMIT_DEFAULT_WAIT_SECONDS
    except ValueError:
        wait = RATE_LIMIT_DEFAULT_WAIT_SECONDS
    if not math.isfinite(wait) or wait < 0:
        wait = RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return min(wait, RATE_LIMIT_MAX_WAIT_SECONDS)


def ts_value(ts: str | None) -> Decimal | None:
    try:
        return Decimal(ts) if ts else None
    except InvalidOperation:
        return None


def is_bot_message(msg: dict, bot_user: str) -> bool:
    """Whether the install's bot posted it: by its member id, never by a bot_id alone,
    which another app's post, or a test user's post through one, also carries."""
    return msg.get("user") == bot_user


def has_buttons(msg: dict) -> bool:
    """Whether the message carries an actions block with a button: a question's choices."""
    for block in msg.get("blocks") or []:
        if block.get("type") == "actions" and any(e.get("type") == "button" for e in block.get("elements") or []):
            return True
    return False


def is_progress(msg: dict) -> bool:
    """Whether a bot post is the gateway's progress rather than content: the
    status line, its placeholder, or a status note."""
    text = msg.get("text", "").strip()
    return (text == STATUS_PLACEHOLDER or text.startswith(STATUS_NOTE_PREFIX)
            or STATUS_LINE_PATTERN.fullmatch(text) is not None)


def post_id(msg: dict) -> str:
    return POST_ID_PREFIX + str(msg.get("ts", "")).replace(".", "")


def split_thread(posts: list[dict]) -> tuple[dict | None, list[dict]]:
    """The thread's content posts as the ack and the card posts after it.

    The router's first content post is its reply to the ask (the ack, or the
    whole answer when nothing was delegated); every later content post is a
    delegated card's delivery, a question when it carries buttons.
    """
    content = [p for p in posts if not is_progress(p)]
    return (content[0], content[1:]) if content else (None, [])


@dataclass
class SlackExchange:
    """One turn as the thread showed it.

    ``outcome`` is ``settled`` (the turn ended), ``timeout`` (the deadline came
    first) or ``no_reply`` (the deadline came with no bot reply at all). The
    timestamps are Slack's, except the reaction's, which is when the poll first
    saw it: a reaction carries none.
    """

    ask_ts: str
    outcome: str = ""
    posts: list[dict] = field(default_factory=list)
    reaction: str = ""
    reaction_seen: Decimal | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def deliverable(self) -> str:
        return self.posts[-1].get("text", "") if self.posts else ""

    @property
    def buttons(self) -> bool:
        return any(has_buttons(p) for p in self.posts)

    def timing(self) -> dict:
        posted = ts_value(self.ask_ts)

        def since(t: Decimal | None) -> float | None:
            return None if t is None or posted is None else round(float(t - posted), 3)

        first = ts_value(self.posts[0].get("ts")) if self.posts else None
        last = self.posts[-1] if self.posts else {}
        answer = ts_value((last.get("edited") or {}).get("ts")) or ts_value(last.get("ts"))
        return {
            "slack_post_to_first_reaction_s": since(self.reaction_seen),
            "slack_post_to_ack_s": since(first),
            "slack_post_to_answer_s": since(answer) if self.outcome == "settled" else None,
        }


class SlackTurn:
    """Post one ask as the test user and read the thread until it settles."""

    def __init__(self, client: SlackClient, channel: str, bot_user: str, *,
                 clock: Callable[[], float] | None = None,
                 wall: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 poll_seconds: float = POLL_SECONDS,
                 quiet_seconds: float = SETTLE_QUIET_SECONDS) -> None:
        self.client = client
        self.channel = channel
        self.bot_user = bot_user
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._sleep = sleep or time.sleep
        self._poll, self._quiet = poll_seconds, quiet_seconds

    def run(self, prompt: str, deadline_seconds: float) -> SlackExchange:
        sent = self.client.call("chat.postMessage", channel=self.channel, text=f"<@{self.bot_user}> {prompt}")
        ex = SlackExchange(ask_ts=str(sent["ts"]))
        deadline = self._clock() + deadline_seconds
        last_shape: tuple = ()
        quiet_since: float | None = None
        reacting = False
        while self._clock() < deadline:
            self._sleep(self._poll)
            try:
                names = self._bot_reactions(ex.ask_ts)
                ex.posts = self._bot_replies(ex.ask_ts)
            except SlackAPIError as err:
                if err.transient:
                    ex.errors.append(str(err))
                    continue
                raise
            if names and not ex.reaction:
                ex.reaction, ex.reaction_seen = names[0], Decimal(str(self._wall()))
            # The ask's own mark stays on beside a blocked mark (the gateway adds
            # it after the ask's), so any settle mark ends the turn.
            reacting = bool(names) and not SETTLE_REACTIONS.intersection(names)
            shape = tuple((p.get("ts"), (p.get("edited") or {}).get("ts"), p.get("text")) for p in ex.posts)
            if shape != last_shape:
                last_shape, quiet_since = shape, self._clock()
            if ex.posts and not reacting and quiet_since is not None and self._clock() - quiet_since >= self._quiet:
                ex.outcome = "settled"
                return ex
        ex.outcome = "timeout" if ex.posts else "no_reply"
        return ex

    def _bot_reactions(self, ts: str) -> list[str]:
        """Every reaction the bot has on the ask, in Slack's order."""
        message = self.client.call("reactions.get", channel=self.channel, timestamp=ts, full="true").get("message") or {}
        return [str(r.get("name", "")) for r in message.get("reactions") or []
                if self.bot_user in (r.get("users") or [])]

    def _bot_replies(self, ts: str) -> list[dict]:
        messages: list[dict] = []
        cursor = ""
        while True:
            params = {"channel": self.channel, "ts": ts, "limit": str(REPLIES_PAGE_LIMIT)}
            if cursor:
                params["cursor"] = cursor
            page = self.client.call("conversations.replies", **params)
            messages.extend(page.get("messages") or [])
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return [m for m in messages if m.get("ts") != ts and is_bot_message(m, self.bot_user)]


def write_timing(timing: dict) -> None:
    """Write the turn's Slack legs to TIMING_FILE_ENV's path, when set. Best effort."""
    path = os.environ.get(TIMING_FILE_ENV)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(timing, fh, sort_keys=True)
    except OSError:
        pass
