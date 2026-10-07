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

The gateway posts to the configured home channel and nowhere else. A target's
chat id is therefore not forwarded: a thread names its own space, and a post
with no thread goes to the home channel, which is where a bare platform target
went under ``hermes send`` too.
"""

from __future__ import annotations

import os

# The variable the operator renders into the agent container exactly when the
# next stack holds the chat backend (platformagent_manifests.go,
# a2aNotifyPlatformEnvVar). Its value is a platform name as Hermes spells it.
NOTIFY_PLATFORM_ENV = "A2A_NOTIFY_PLATFORM"
# The bus CLI in the agent image (a2a/cmd/a2a), on PATH.
A2A_CLI = "a2a"
# The Hermes CLI the today path posts through.
HERMES_CLI = "hermes"
# The exit status `a2a notify` uses when the gateway did not answer in time: the
# post may or may not have landed, so a caller must not post it again.
NOTIFY_OUTCOME_UNKNOWN = 3
# A Google Chat message name and the thread it starts: spaces/S/messages/M.M is
# in thread spaces/S/threads/M when Hermes posted it.
GCHAT_PLATFORM = "google_chat"
GCHAT_MESSAGES_TOKEN = "/messages/"
GCHAT_THREADS_TOKEN = "/threads/"


def routed_platform() -> str:
    """The platform whose posts go through the gateway, or "" when none does."""
    return os.environ.get(NOTIFY_PLATFORM_ENV, "").strip()


def routes(platform: str) -> bool:
    """Whether a post to ``platform`` goes through the gateway's chat.notify route."""
    routed = routed_platform()
    return bool(routed) and platform == routed


def command(target: str, message: str, json_output: bool = True, hermes_bin: str = HERMES_CLI) -> list[str]:
    """The argv that posts ``message`` to ``target``.

    ``target`` is a ``hermes send --to`` target. ``json_output`` asks the
    Hermes path for ``--json``; the gateway path always answers in JSON.
    """
    platform, _, rest = target.partition(":")
    if not routes(platform):
        argv = [hermes_bin, "send"]
        if json_output:
            argv.append("--json")
        return argv + ["--to", target, message]
    _chat, _, thread = rest.partition(":")
    argv = [A2A_CLI, "notify", "--platform", platform]
    if thread:
        argv += ["--thread", thread]
    # "--" ends the flags: a report that opens with a bullet, a rule or a
    # negative number is text, not an option, and a message that is exactly
    # "--thread=..." must not redirect the post.
    return argv + ["--", message]


def blocks_command(platform: str, thread: str, text: str, blocks_path: str) -> list[str]:
    """The argv that posts a Slack Block Kit message through the gateway.

    ``blocks_path`` names a file holding the JSON array of blocks; ``text`` is
    the notification and fallback. Only for a platform :func:`routes` sends
    through the gateway; the today path posts blocks through the broker's
    Slack relay (slack_blocks_post.py).
    """
    argv = [A2A_CLI, "notify", "--platform", platform, "--blocks-file", blocks_path]
    if thread:
        argv += ["--thread", thread]
    return argv + ["--", text]


def outcome_unknown(returncode: int) -> bool:
    """Whether a failed send may still have posted (the gateway did not answer in time)."""
    return returncode == NOTIFY_OUTCOME_UNKNOWN


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
