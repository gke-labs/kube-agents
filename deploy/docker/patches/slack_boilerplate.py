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
does not turn it on. Where an operator has, a turn that replies in a thread
already shows the adapter's status line, which counts the minutes itself, so
the heartbeat is dropped there. Elsewhere on Slack it uses Hermes' generic
mode (a phrase from ``gateway/assets/status_phrases.yaml``, "still on it"), the
same as ``long_running_notifications: generic`` for Slack alone. ``off`` stays
off.

**The gateway's lifecycle notices.** A restart or shutdown tells the chats it
interrupts, the home channel and each interrupted cron job's owner about it in
Hermes' own terms (``⚠️ Gateway restarting — Your current task will be
interrupted.``). On Slack the shutdown, restarting and interrupted-cron notices
are reworded in the agent's first person: a user whose question was cut off
needs to know. The two that only announce the gateway is back, ``♻ Gateway
restarted successfully.`` to the chat that asked for the restart and ``♻️
Gateway online — Hermes is back and ready.`` to the home channel, are not sent
to Slack at all: ``drop_notice`` gates both call sites. Text this module does
not recognise, a later upstream rewording included, passes through unchanged.
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

#: Upstream's interrupting notices, verbatim, and what Slack is sent instead.
NOTICE_REWORDS = {
    "⚠️ Gateway shutting down — Your current task will be interrupted.":
        "I'm going offline for a moment, so I've had to stop what I was working on.",
    "⚠️ Gateway restarting — Your current task will be interrupted. "
    "Send any message after restart and I'll try to resume where you left off.":
        "I'm restarting, so I've had to stop what I was working on. "
        "Send me a message in a minute and I'll pick up where I left off.",
}

#: ``_notify_interrupted_cron_jobs``'s notice, and its action words in plain voice.
CRON_INTERRUPTED = re.compile(
    r"⚠️ Cron job '(?P<name>.*)' was interrupted — the gateway is "
    r"(?P<action>restarting|shutting down) and killed the run before it finished\. "
    r"No result was produced for this run\.",
    re.DOTALL,
)
CRON_INTERRUPTED_REWORD = (
    "I had to stop the scheduled job '{name}' before it finished because I was {why}, "
    "so there's no result from this run."
)
CRON_INTERRUPTED_WHY = {"restarting": "restarting", "shutting down": "going offline"}

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


def long_running_mode(source: Any, mode: str, metadata: Any = None) -> str:
    """The heartbeat's display mode for ``source``, whose status metadata is ``metadata``.

    On Slack: off where the turn replies in a thread, since the status line
    shows there; generic where it was raw. Anything else returns ``mode``.
    """
    if mode == MODE_OFF or not enabled():
        return mode
    if _platform_name(getattr(source, "platform", "")) != PLATFORM:
        return mode
    if isinstance(metadata, dict) and metadata.get("thread_id"):
        return MODE_OFF
    return MODE_GENERIC if mode == MODE_RAW else mode


def drop_notice(platform: Any) -> bool:
    """Whether a back-online notice to ``platform`` is skipped: Slack, with the flag on."""
    return _platform_name(platform) == PLATFORM and enabled()


def notice_text(platform: Any, text: str) -> str:
    """A gateway interrupting notice as ``platform`` is sent it: reworded on Slack."""
    if _platform_name(platform) != PLATFORM or not isinstance(text, str) or not enabled():
        return text
    if text in NOTICE_REWORDS:
        return NOTICE_REWORDS[text]
    match = CRON_INTERRUPTED.fullmatch(text)
    if match:
        return CRON_INTERRUPTED_REWORD.format(
            name=match["name"], why=CRON_INTERRUPTED_WHY[match["action"]])
    return text
