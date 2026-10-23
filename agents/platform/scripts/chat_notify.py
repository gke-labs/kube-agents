"""Route a proactive chat post through the A2A gateway when the next stack owns the chat backend.

Under ``spec.mode: next`` the operator stops rendering the Hermes chat platform
that ``hermes send`` posts through, because the A2A gateway consumes the chat
backend instead. Every caller that posts unprompted (the alert path, the cron
relay, the Cluster Agent reconcile notice, ``send_notification``) would then post
nowhere. The operator names the platform the gateway now holds in
``A2A_NOTIFY_PLATFORM``, and for that platform these callers run
``a2a notify`` instead, which asks the gateway to post to the home channel over
its ``chat.notify`` route.

:func:`command` is the whole switch: it takes the target the callers already
build for ``hermes send --to`` (``platform``, ``platform:chat``, or
``platform:chat:thread``) and returns the argv to run. Both commands print one
JSON object with ``message_id``; ``a2a notify`` adds ``thread_id``, which
:func:`thread_from_response` prefers over deriving the thread from the message
name.

The gateway posts these to the configured home channel (it posts into a
conversation only for a request naming one, which the kanban notifier sends
for a card's report). A target's chat id is therefore not forwarded: a thread names its own space, and a post
with no thread goes to the home channel, which is where a bare platform target
went under ``hermes send`` too.

:func:`conversation_command` is the other route: a post into one gateway
conversation, home channel or not, for a caller that knows the conversation and
its context id (:func:`conversation_route` reads them from what the hermes-bridge
recorded). Onboarding's report goes this way to the conversation that started it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

# The variable the operator renders into the agent container exactly when the
# next stack holds the chat backend (platformagent_manifests.go,
# a2aNotifyPlatformEnvVar). Its value is a platform name as Hermes spells it.
NOTIFY_PLATFORM_ENV = "A2A_NOTIFY_PLATFORM"
# The variable the operator renders whenever the gateway arms its chat.notify
# route, home channel or not (platformagent_a2a_manifests.go,
# a2aNotifyConversationsEnvVar): the platform whose backend the gateway holds.
NOTIFY_CONVERSATIONS_ENV = "A2A_NOTIFY_CONVERSATIONS"
#: The Slack platform name, whose threads go out channel-qualified (command).
SLACK_PLATFORM = "slack"
# What `a2a notify` needs to reach the bus (a2a/cmd/a2a/main.go, connect): the
# bus URL and an identity, A2A_BUS_USER or the older NATS_USER.
BUS_URL_ENV = "NATS_URL"
BUS_USER_ENVS = ("A2A_BUS_USER", "NATS_USER")
# The CA the CLI trusts the bus by (a2a/lib/tls.go, EnvNATSCAFile). Not read
# here: the Go CLI reads it, so a child that runs `a2a notify` has to be
# handed it, and the MCP env contract test asserts that from this name.
BUS_CA_ENV = "NATS_CA_FILE"
# Where the hermes-bridge records which gateway conversation an `a2a-*` session
# answers (a2a/hermes-bridge/route.go; session_kv_server.py, CONVERSATION_ROUTE_KEY).
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DEFAULT_DB = "/var/lib/kube-agents/session/session_kv.db"
CONVERSATION_ROUTE_KEY = "conversation_route"
SESSION_KV_TIMEOUT_SECONDS = 2.0
# The route's fields (route.go, conversationRoute).
ROUTE_PLATFORM = "platform"
ROUTE_CONVERSATION = "conversation"
ROUTE_CONTEXT = "context_id"
# The bus CLI in the agent image (a2a/cmd/a2a), on PATH.
A2A_CLI = "a2a"
# The Hermes CLI the today path posts through.
HERMES_CLI = "hermes"
# The exit status `a2a notify` uses when the gateway did not answer in time: the
# post may or may not have landed, so a caller must not post it again.
NOTIFY_OUTCOME_UNKNOWN = 3
# The exit status `a2a notify` uses when the route is not there right now: no
# gateway subscribed (a roll, or its bind retry) or the bus unreachable. Nothing
# was posted, and waiting may help where retrying a refusal would not.
NOTIFY_ROUTE_UNAVAILABLE = 4
# The waits before each retry of a send that found the route unavailable, for
# a caller with one shot at the post. They sum past the gateway's longest wait
# between attempts to bind the route (a2a/gateway/notify.go,
# notifyStartRetryMax, 30s), so a gateway that is up again is heard from.
NOTIFY_ROUTE_RETRY_DELAYS_SECONDS = (5, 10, 20)
# How long `a2a notify` waits for the gateway by default (a2a/cmd/a2a/notify.go),
# its own bound on connecting, and a margin: a caller that kills the child
# sooner than their sum turns a post that may have landed into a failure.
NOTIFY_WAIT_SECONDS = 60
NOTIFY_CONNECT_SECONDS = 30
NOTIFY_MARGIN_SECONDS = 15
NOTIFY_SUBPROCESS_TIMEOUT_SECONDS = NOTIFY_WAIT_SECONDS + NOTIFY_CONNECT_SECONDS + NOTIFY_MARGIN_SECONDS
# The `a2a notify --update` key an event alert is posted under, one per alert
# (its event session's id): the gateway edits the message it posted with that
# key, which is how the alert's triage replaces the alert in place under next
# (slack_ux_incident's chat route, which finds the session by the alert's
# thread). Hermes's send has no such key, so the today path posts as before.
ALERT_UPDATE_FORMAT = "alert-{session}"
# A Google Chat message name and the thread it starts: spaces/S/messages/M.M is
# in thread spaces/S/threads/M when Hermes posted it.
GCHAT_PLATFORM = "google_chat"
GCHAT_MESSAGES_TOKEN = "/messages/"
GCHAT_THREADS_TOKEN = "/threads/"


def alert_update_key(session_id: str) -> str:
    """The ``--update`` key of the alert an event session posted, or "" for no session."""
    return ALERT_UPDATE_FORMAT.format(session=session_id) if session_id else ""


def routed_platform() -> str:
    """The platform whose posts go through the gateway, or "" when none does."""
    return os.environ.get(NOTIFY_PLATFORM_ENV, "").strip()


def gateway_platform() -> str:
    """The platform whose backend the gateway holds, or "" when it holds none.

    Unlike :func:`routed_platform` this is set with no home channel, so it names
    the platform an install has when nothing routes a proactive post there, and
    whose conversations :func:`conversation_command` can post into.
    """
    return os.environ.get(NOTIFY_CONVERSATIONS_ENV, "").strip()


def routes(platform: str) -> bool:
    """Whether a post to ``platform`` goes through the gateway's chat.notify route."""
    routed = routed_platform()
    return bool(routed) and platform == routed


def bus_unreachable_reason() -> str:
    """Why `a2a notify` cannot reach the bus from this process's env, or "" when it can."""
    if not os.environ.get(BUS_URL_ENV, "").strip():
        return f"{BUS_URL_ENV} is not set"
    if not any(os.environ.get(name, "").strip() for name in BUS_USER_ENVS):
        return f"no bus identity ({' or '.join(BUS_USER_ENVS)})"
    return ""


def conversation_route(session_id: str) -> dict:
    """The gateway conversation ``session_id`` answers, as the hermes-bridge recorded it, or {}.

    Only a route naming a platform, a conversation and its context id is
    returned. The read is read-only and bounded; any failure is {}.
    """
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DEFAULT_DB
    if not session_id or not os.path.exists(path):
        return {}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SESSION_KV_TIMEOUT_SECONDS)) as conn:
            row = conn.execute("SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,)).fetchone()
        route = (json.loads(row[0]) if row and row[0] else {}).get(CONVERSATION_ROUTE_KEY)
    except Exception:  # noqa: BLE001 - an unreadable route is no route
        return {}
    if not isinstance(route, dict):
        return {}
    fields = {key: str(route.get(key) or "").strip() for key in (ROUTE_PLATFORM, ROUTE_CONVERSATION, ROUTE_CONTEXT)}
    return fields if all(fields.values()) else {}


def conversation_command(platform: str, conversation: str, context_id: str, text: str, chat_path: str = "",
                         update: str = "", timeout_s: float = 0) -> list[str]:
    """The argv that posts into a gateway conversation, home channel or not.

    The gateway posts it only while the conversation's session record carries
    ``context_id``. ``chat_path`` names a ``kube-agents.chat/v1`` message file,
    with ``text`` as its fallback; ``update`` edits the message an earlier post
    with the same key left in the conversation.
    """
    argv = [A2A_CLI, "notify", "--platform", platform, "--conversation", conversation, "--context", context_id]
    if chat_path:
        argv += ["--chat-file", chat_path]
    if update:
        argv += ["--update", update]
    if timeout_s > 0:
        argv += ["--timeout", f"{max(1, int(timeout_s))}s"]
    return argv + ["--", text]


def subprocess_timeout(target: str, default: float | None) -> float | None:
    """The kill timeout a caller should give the send for ``target``.

    For a routed platform it is at least what `a2a notify` itself may take, so
    the CLI answers (exit 3 on no answer) before the caller kills it. For any
    other platform it is the caller's ``default``, None included.
    """
    platform = target.partition(":")[0]
    if not routes(platform):
        return default
    return max(default or 0, NOTIFY_SUBPROCESS_TIMEOUT_SECONDS)


def command(target: str, message: str, json_output: bool = True, hermes_bin: str = HERMES_CLI,
            wait_seconds: float | None = None, update: str = "") -> list[str]:
    """The argv that posts ``message`` to ``target``.

    ``target`` is a ``hermes send --to`` target. ``json_output`` asks the
    Hermes path for ``--json``; the gateway path always answers in JSON.
    ``wait_seconds`` bounds how long `a2a notify` waits for the gateway, for a
    caller with a deadline shorter than the CLI's default. ``update`` names the
    message on the gateway path (``a2a notify --update``), so a later notify
    with the same key in the same conversation edits it; the Hermes path has
    no such key and ignores it.
    """
    platform, _, rest = target.partition(":")
    if not routes(platform):
        argv = [hermes_bin, "send"]
        if json_output:
            argv.append("--json")
        return argv + ["--to", target, message]
    chat, _, thread = rest.partition(":")
    argv = [A2A_CLI, "notify", "--platform", platform]
    if wait_seconds is not None:
        argv += ["--timeout", f"{max(1, int(wait_seconds))}s"]
    if update:
        argv += ["--update", update]
    if thread:
        # A Slack thread is a bare ts, which names no channel: it goes out
        # qualified by the target's own channel, and the gateway posts it only
        # when that channel is home. A today-era DM or other channel's thread
        # is then refused rather than followed up in home.
        if platform == SLACK_PLATFORM and chat:
            thread = f"{chat}/{thread}"
        argv += ["--thread", thread]
    # "--" ends the flags: a report that opens with a bullet, a rule or a
    # negative number is text, not an option, and a message that is exactly
    # "--thread=..." must not redirect the post.
    return argv + ["--", message]


def chat_command(platform: str, thread: str, text: str, chat_path: str, timeout_s: float = 0) -> list[str]:
    """The argv that posts a ``kube-agents.chat/v1`` message through the gateway.

    ``chat_path`` names a file holding the chat message's JSON object; ``text``
    is its fallback, which the gateway posts when it does not lay the message
    out. Only for a platform :func:`routes` sends through the gateway; the
    today path posts Block Kit through the broker's Slack relay
    (slack_blocks_post.py).
    """
    argv = [A2A_CLI, "notify", "--platform", platform, "--chat-file", chat_path]
    if thread:
        argv += ["--thread", thread]
    if timeout_s > 0:
        # The CLI's own wait, shorter than the caller's subprocess bound, so
        # an answer that does not come in time ends as the CLI's
        # outcome-unknown exit rather than a killed child.
        argv += ["--timeout", f"{int(timeout_s)}s"]
    return argv + ["--", text]


def outcome_unknown(returncode: int) -> bool:
    """Whether a failed send may still have posted (the gateway did not answer in time)."""
    return returncode == NOTIFY_OUTCOME_UNKNOWN


def route_unavailable(returncode: int) -> bool:
    """Whether a failed send found no route (nothing posted; waiting may help)."""
    return returncode == NOTIFY_ROUTE_UNAVAILABLE


def thread_from_response(platform: str, response: dict) -> str:
    """The thread a post landed in, from the command's JSON answer.

    The gateway names it outright. Hermes names only the message, and on Google
    Chat the thread is derived from it the way the callers always have.
    """
    thread = (response or {}).get("thread_id") or ""
    if thread:
        return thread
    message_id = (response or {}).get("message_id") or ""
    if not message_id:
        return ""
    if platform == GCHAT_PLATFORM and GCHAT_MESSAGES_TOKEN in message_id:
        space, message = message_id.split(GCHAT_MESSAGES_TOKEN, 1)
        return f"{space}{GCHAT_THREADS_TOKEN}{message.split('.')[0]}"
    return message_id
