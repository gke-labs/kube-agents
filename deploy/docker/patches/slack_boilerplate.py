"""Keep Hermes' system boilerplate out of Slack when KAGE_SLACK_UX is on.

Installed into the image at ``/opt/hermes/gateway/slack_boilerplate.py``.
``apply_slack_boilerplate.py`` wires the call sites to it; with the flag off
each returns what it was handed, so every platform, and Slack itself, get
upstream's text.

What it removes or rewords, and why
-----------------------------------
**The cron wrapper.** ``cron/scheduler_delivery.py::_deliver_result`` wraps
every cron delivery in::

    Cronjob Response: <job name>
    (job_id: <id>)
    -------------

    <report>

    To stop or manage this job, send me a new message (e.g. "stop reminder <name>").

The first message a new Slack user reads is the first-install inventory report,
and it opened with that header. On Slack the report goes out as
``cron.wrap_response: false`` would send it. The switch itself stays on: the
Chat Agent relay (``deploy/docker/plugins/chat/adapter.py``) reads the job id
back out of the header, and it and every non-Slack target keep the wrapper. The
choice is per target, so one delivery to both Slack and the relay wraps the
relay's copy only.

**The heartbeat.** Hermes can post an edited-in-place heartbeat into a turn
that runs past three minutes, ``⏳ Working — 3 min — terminal``: the name of the
tool running, or ``starting API call #52``. It is opt-in on Slack: upstream's
Slack display tier sets ``long_running_notifications`` off, and a stock install
does not turn it on. Where an operator has, Slack gets Hermes' generic mode (a
phrase from ``gateway/assets/status_phrases.yaml``, "still on it"), the same as
``long_running_notifications: generic`` for Slack alone. It is not dropped in a
thread: the thread's status line needs a Slack scope the app may not have and
fails silently without it, so the heartbeat is the one sign of life that always
arrives. ``off`` stays off.

**The gateway's lifecycle notices.** A restart or shutdown tells the chats it
interrupts, the home channel and each interrupted cron job's owner about it in
Hermes' own terms (``⚠️ Gateway restarting — Your current task will be
interrupted.``). On Slack the shutdown, restarting and interrupted-cron notices
are reworded in the agent's first person: a user whose question was cut off
needs to know. They reach Slack through ``SlackAdapter.send`` like the replies
below, so ``system_text`` rewords them. The two that only announce the gateway
is back, ``♻ Gateway restarted successfully.`` to the chat that asked for the
restart and ``♻️ Gateway online — Hermes is back and ready.`` to the home
channel, are not sent to Slack at all: ``drop_notice`` gates both call sites.

**The rest of the gateway's system replies.** Busy acks, drain and restart
refusals, the ``/restart``, ``/stop`` and ``/steer`` replies, the force-stop
reply, the background-task update and the provider error replies reach Slack
through ``SlackAdapter.send``, or ``SlackAdapter.edit_message`` when they
arrive as a streamed or edited message. Both pass them through
``system_text``: each known one is reworded in plain voice, with no emoji, no
"Gateway" or "agent", no console command, tool name or iteration count, and no
exception text (that is logged instead). The background-task update is the
exception: it keeps the command and its recent output, which is what the user
asked to see. The ``/restart`` and ``/stop``
replies come from Hermes' English catalog (``locales/en.yaml``); an install
set to another language gets upstream's translation. Two go entirely: the
one-time busy-input hint about Hermes' queue/interrupt setting is not appended
on Slack (so it is not marked seen either, and another platform still gets
it), and the session-database warnings with their ``hermes doctor`` commands
are not broadcast to a Slack home channel; the gateway already logs the
underlying error.

Text this module does not recognise, a later upstream rewording included,
passes through unchanged; ``verify_slack_boilerplate.py`` renders every reply
and notice from the built tree so that such a rewording fails the image build.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: The platform name cron targets and message sources carry for Slack.
PLATFORM = "slack"

#: ``_display_surface_mode``'s values: the heartbeat as upstream writes it, the
#: phrase-catalog form, and none.
MODE_RAW = "raw"
MODE_GENERIC = "generic"
MODE_OFF = "off"

#: ``_notify_interrupted_cron_jobs``'s notice in plain voice, and its action words.
CRON_INTERRUPTED_REWORD = (
    "I had to stop the scheduled job '{name}' before it finished because I was {why}, "
    "so there's no result from this run."
)
CRON_INTERRUPTED_WHY = {"restarting": "restarting", "shutting down": "going offline"}

#: What Slack is told after ``/stop``, whether or not the turn had started.
STOPPED = "Stopped. Send me a message whenever you want to carry on."

#: The longest exception text a reworded error reply may carry: one line, this
#: long. An agent reply that happens to open with the same prefix and runs on
#: past a line or this length is not the gateway's, and passes through.
ERROR_TEXT_MAX = 500

#: ``/steer``'s preview: the first 60 characters of the steer, plus "...".
STEER_PREVIEW_MAX = 63

#: ``_status_action_gerund()``'s values, as the drain replies say them on Slack.
DRAIN_WHO = {"restarting": "I'm restarting", "shutting down": "I'm going offline for a moment"}

#: The gateway's other system replies, each matched whole, and what Slack is
#: sent instead. A callable gets the match; a string is sent as it stands.
SYSTEM_REWORDS: tuple[tuple[re.Pattern, Any], ...] = (
    # The interrupting notices a restart or shutdown sends (gateway/run_shutdown.py).
    (re.compile(r"⚠️ Gateway shutting down — Your current task will be interrupted\."),
     "I'm going offline for a moment, so I've had to stop what I was working on."),
    (re.compile(r"⚠️ Gateway restarting — Your current task will be interrupted\. "
                r"Send any message after restart and I'll try to resume where you left off\."),
     "I'm restarting, so I've had to stop what I was working on. "
     "Send me a message in a minute and I'll pick up where I left off."),
    (re.compile(r"⚠️ Cron job '(?P<name>.*)' was interrupted — the gateway is "
                r"(?P<action>restarting|shutting down) and killed the run before it finished\. "
                r"No result was produced for this run\.", re.DOTALL),
     lambda m: CRON_INTERRUPTED_REWORD.format(name=m["name"], why=CRON_INTERRUPTED_WHY[m["action"]])),
    # /restart and /stop, from locales/en.yaml (gateway.draining,
    # gateway.restart.*, gateway.stop.*). No console command, and no promise of
    # a back-online notice, which Slack is not sent.
    (re.compile(r"⏳ Draining \d+ active agent\(s\) before restart\.\.\."),
     "I'll restart as soon as the work in progress finishes."),
    (re.compile(r"⏳ Gateway restart already in progress\.\.\."),
     "I'm already restarting — give me a minute."),
    (re.compile(r"♻ Restarting gateway\. If you aren't notified within 60 seconds, restart from the console "
                r"with `hermes gateway restart`\."),
     "Restarting now. Send me a message in a minute or two."),
    (re.compile(r"⚡ Stopped\. (?:The agent hadn't started yet — y|Y)ou can continue this session\."),
     STOPPED),
    # A slash command sent mid-turn, and /steer (gateway/run_busy.py).
    (re.compile(r"⏳ Agent is running — `(?P<command>/[^`\n]+)` can't run mid-turn\. "
                r"Wait for the current response or `/stop` first\."),
     lambda m: f"I'm in the middle of something — try `{m['command']}` again once I've answered."),
    (re.compile(rf"⏩ Steer queued — arrives after the next tool call: '.{{0,{STEER_PREVIEW_MAX}}}'", re.DOTALL),
     "Got it — I'll fold this into what I'm working on now."),
    (re.compile(r"(?:Agent still starting|No active agent) — /steer queued for the next turn\."),
     "Got it — I'll pick this up next."),
    (re.compile(rf"⚠️ Steer failed: (?P<error>[^\n]{{1,{ERROR_TEXT_MAX}}})"),
     # run_busy already logs the exception before replying.
     lambda m: STEER_FAILED),
    (re.compile(r"⏳ Queued for the next turn(?: \([^\n]*\))?\. I'll respond once the current task finishes\."),
     "Got it — I'll pick this up when I finish the current one."),
    (re.compile(r"⚡ Interrupting current task(?: \([^\n]*\))?\. I'll respond to your message shortly\."),
     "Stopping what I was doing to look at this."),
    (re.compile(r"⏩ Steered into current run(?: \([^\n]*\))?\. Your message arrives after the next tool call\."),
     "Got it — I'll fold this into what I'm working on now."),
    (re.compile(r"↪ Redirected current run(?: \([^\n]*\))?\. I'll adjust using your correction\."),
     "Got it — changing course with your correction."),
    # Both queue the message behind work that cannot be interrupted; the
    # subagent and compression detail is internals, so Slack hears neither.
    (re.compile(r"⏳ (?:Subagent working|Compressing context)(?: \([^\n]*\))? — your message is queued for "
                r"when it finishes \(use /stop to cancel everything\)\."),
     "Got it — I'll pick this up when the current work finishes."),
    (re.compile(r"⏳ Gateway (?P<action>restarting|shutting down) — queued for the next turn after it comes back\."),
     lambda m: f"{DRAIN_WHO[m['action']]} — I'll pick this up when I'm back."),
    (re.compile(r"⏳ Gateway is (?P<action>restarting|shutting down) and is not accepting "
                r"(?:another turn|new work) right now\."),
     lambda m: f"{DRAIN_WHO[m['action']]} — send that again in a minute."),
    (re.compile(r"⏳ This agent is draining for a maintenance action and isn't accepting new turns right now\. "
                r"It'll be back in a moment — please resend shortly\."),
     "I'm finishing some maintenance — try again in a minute."),
    (re.compile(r"⏳ Another turn is still running on this session\. To protect the transcript, this message "
                r"was not processed\. Wait for the active turn to finish, then resend it\."),
     "Still working on your last message — send this again once I've answered."),
    (re.compile(r"⚡ Force-stopped\. The agent was still starting — session unlocked\."),
     "Stopped."),
    (re.compile(r"⏳ Background task still running(?: — (?P<cmd>`[^\n]*`))?(?P<rest>\n\nRecent output:\n.*)?",
                re.DOTALL),
     lambda m: (f"Still running {m['cmd']}." if m["cmd"] else "Still running.") + (m["rest"] or "")),
    (re.compile(rf"⚠️ Provider authentication failed: (?P<error>[^\n]{{1,{ERROR_TEXT_MAX}}})"),
     lambda m: _logged("provider authentication failed", m["error"], AUTH_FAILED)),
    (re.compile(r"⚠️ Provider authentication failed\. Check the configured credentials; "
                r"raw provider details are in the gateway logs\."),
     lambda m: AUTH_FAILED),
    (re.compile(r"⚠️ The model provider rejected the request\. I kept the raw provider error out of chat; "
                r"check gateway logs for details or try rephrasing\."),
     "The model provider turned that request down. Try rephrasing it, or ask whoever runs me to check the logs."),
    (re.compile(r"⏱️ The model provider is rate-limiting requests\. Please wait a moment and try again\."),
     "I'm being rate-limited. Give me a minute and try again."),
    (re.compile(r"⚠️ The model server is not responding — it looks like the configured model endpoint is not "
                r"running or is unreachable\."),
     "I can't reach the model right now. Try again in a minute."),
    (re.compile(r"⚠️ The model provider failed after retries\. I kept raw provider details out of chat; "
                r"check gateway logs for diagnostics\."),
     "Something went wrong on my side. Try again?"),
)

#: What Slack is told when the model provider refuses the turn's credentials.
AUTH_FAILED = "I can't reach the model right now (authentication failed)."

#: What Slack is told when ``/steer`` raises; the exception is logged.
STEER_FAILED = "I couldn't add that to what I'm working on — send it again once I've answered."

_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    global _warned_missing
    if _presenter is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_boilerplate: %s is set but slack_presenter is not importable; "
            "treating the flag as off", FLAG_ENV,
        )
    return False


def _platform_name(platform: Any) -> str:
    """``Platform.SLACK`` or ``"slack"`` alike, lower-cased."""
    return str(getattr(platform, "value", platform) or "").strip().lower()


def cron_delivery_text(
    target: Any, content: str, delivery_text: str,
    extract_media: Callable[[str], tuple[list, str]],
) -> str:
    """The text one cron target is sent: ``delivery_text`` unless it is Slack.

    ``content`` is the unwrapped job output, ``delivery_text`` the wrapped one
    after ``extract_media``. For Slack with the flag on the result is
    ``extract_media(content)``'s text, which is exactly what upstream sends when
    ``cron.wrap_response`` is false; the attachments are the same either way,
    since the wrapper carries none.
    """
    if _platform_name(getattr(target, "platform_name", "")) != PLATFORM or not enabled():
        return delivery_text
    _media, text = extract_media(content)
    if text != delivery_text:
        job = getattr(target, "job", None) or {}
        logger.info(
            "slack_boilerplate: job '%s' posts to slack:%s without the cron wrapper",
            job.get("id", "?"), getattr(target, "chat_id", "?"),
        )
    return text


def long_running_mode(source: Any, mode: str) -> str:
    """The heartbeat's display mode for ``source``: generic on Slack where it was raw."""
    if mode != MODE_RAW or not enabled():
        return mode
    if _platform_name(getattr(source, "platform", "")) != PLATFORM:
        return mode
    return MODE_GENERIC


def drop_notice(platform: Any) -> bool:
    """Whether a back-online notice to ``platform`` is skipped: Slack, with the flag on."""
    return _platform_name(platform) == PLATFORM and enabled()


def _logged(what: str, error: str, reply: str) -> str:
    logger.warning("slack_boilerplate: %s (not shown on Slack): %s", what, error)
    return reply


def system_text(text: Any) -> Any:
    """A gateway system reply as the Slack adapter sends it: reworded when known.

    Called from ``SlackAdapter.send`` and ``SlackAdapter.edit_message`` only,
    so the platform is always Slack.
    Flag off, or for any text not matched whole, the input object comes back.
    """
    if not isinstance(text, str) or not enabled():
        return text
    for pattern, reword in SYSTEM_REWORDS:
        match = pattern.fullmatch(text)
        if match:
            return reword(match) if callable(reword) else reword
    return text

