import json
import logging
from typing import Any, Dict

logger = logging.getLogger("hermes.hook.chat_message_audit")

_TEXT_LOG_LIMIT = 4000


def _truncate(value: Any) -> str:
    text = str(value or "")
    if len(text) > _TEXT_LOG_LIMIT:
        return text[:_TEXT_LOG_LIMIT] + "...(truncated)"
    return text


def _emit(audit_event: str, context: Dict[str, Any]) -> None:
    record = {
        "audit_event": audit_event,
        "platform": context.get("platform", ""),
        "user_id": context.get("user_id", ""),
        "session_id": context.get("session_id", ""),
    }
    if "message" in context:
        record["message"] = _truncate(context.get("message"))
    if "response" in context:
        record["response"] = _truncate(context.get("response"))
    logger.info(json.dumps(record, default=str, sort_keys=True))


async def handle(event_type: str, context: Dict[str, Any]) -> None:
    try:
        if event_type == "agent:start":
            _emit("chat_message_start", context)
        elif event_type == "agent:end":
            _emit("chat_message_end", context)
    except Exception as exc:
        logger.error(
            "Error in chat_message_audit handler for %s: %s", event_type, exc, exc_info=True
        )
