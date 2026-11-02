"""Count a thumbs-down a person put on one of the bot's messages.

Hermes fans every human reaction out to the profile's hooks as
``reaction:added`` with the reactor, the reacted-to message's author, the
channel and the message timestamp (the Slack adapter's
``_handle_slack_reaction``). This hook counts a thumbs-down, under either Slack
spelling and with or without a skin-tone modifier, when the reactor is not the
bot and the message is the bot's, once per reactor and message within one
gateway process, into the ``chat_metrics`` registry, where the operator's
usage poller reads it as ``status.usage.negativeFeedbackTotal``. The hook does
not subscribe to removals: a thumbs-down taken off and put back in the same
process is one thumbs-down. The bot's own Slack identities come from the
gateway the plugin's inbound hook noted; before the first inbound message
after a gateway start, or without a Slack adapter, nothing counts, and the
first such reaction is logged once. Nothing here reads message text or writes
anywhere.
"""

import logging
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# This hook lives outside plugins/, so it reaches one level further up than the
# plugins do; `hooks/` has no __init__.py, so no relative import can resolve here.
_PLUGINS_DIR = str(Path(__file__).resolve().parents[2] / "plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

logger = logging.getLogger("hermes.hook.chat_feedback_reactions")

ADDED_EVENT = "reaction:added"
# Slack's two names for the thumbs-down emoji, with or without the colons.
NEGATIVE_REACTIONS = frozenset({"-1", "thumbsdown"})
EMOJI_COLON = ":"
# Slack appends a skin-tone modifier to the name (`-1::skin-tone-3`); the
# emoji is the part before it.
SKIN_TONE_SEPARATOR = "::"

_lock = threading.Lock()
_seen: set = set()
_registry_missing_logged = False
_identity_missing_logged = False


def reset() -> None:
    """Forget the reactions seen; the registry is per process, as this is."""
    global _identity_missing_logged
    with _lock:
        _seen.clear()
        _identity_missing_logged = False


def _metrics() -> Optional[Any]:
    """The chat_metrics module, or None when the plugin is not there (logged once)."""
    global _registry_missing_logged
    try:
        from chat_metrics import metrics  # noqa: PLC0415 - lazy, the plugin may be absent
    except Exception as exc:  # noqa: BLE001 - a profile without the plugin counts nothing
        if not _registry_missing_logged:
            _registry_missing_logged = True
            logger.info("chat_metrics is not importable (%s); thumbs-downs are not counted", type(exc).__name__)
        return None
    return metrics


async def handle(event_type: str, context: Dict[str, Any]) -> None:
    try:
        if event_type != ADDED_EVENT:
            return
        ctx = context or {}
        reaction = str(ctx.get("reaction") or "").strip(EMOJI_COLON).split(SKIN_TONE_SEPARATOR, 1)[0]
        if reaction not in NEGATIVE_REACTIONS:
            return
        user = ctx.get("user_id")
        author = ctx.get("item_user_id")
        if not user or not author:
            return
        metrics = _metrics()
        if metrics is None:
            return
        bots = metrics.slack_bot_user_ids()
        if not bots:
            global _identity_missing_logged
            if not _identity_missing_logged:
                _identity_missing_logged = True
                logger.info("a thumbs-down arrived before the bot's Slack identity was known; not counted until a message has been dispatched")
            return
        if str(author) not in bots or str(user) in bots:
            return
        key = (str(ctx.get("team_id") or ""), str(ctx.get("channel_id") or ""), str(ctx.get("message_ts") or ""), str(user))
        with _lock:
            if key in _seen:
                return
            _seen.add(key)
        metrics.REGISTRY.record_negative_feedback(str(ctx.get("platform") or ""))
    except Exception as exc:  # noqa: BLE001 - a hook never fails the gateway
        logger.debug("chat_feedback_reactions skipped: %s", type(exc).__name__)
