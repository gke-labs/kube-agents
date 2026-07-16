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
    ctx = context or {}
    record = {
        "audit_event": audit_event,
        "platform": ctx.get("platform", ""),
        "user_id": ctx.get("user_id", ""),
        "session_id": ctx.get("session_id", ""),
    }
    if "message" in ctx:
        record["message"] = _truncate(ctx.get("message"))
    if "response" in ctx:
        record["response"] = _truncate(ctx.get("response"))
    if "iteration" in ctx:
        record["iteration"] = ctx.get("iteration")
    if "tool_names" in ctx:
        record["tool_names"] = ctx.get("tool_names")
    logger.info(json.dumps(record, default=str, sort_keys=True))


async def handle(event_type: str, context: Dict[str, Any]) -> None:
    try:
        if event_type == "agent:start":
            _emit("chat_message_start", context)
        elif event_type == "agent:end":
            _emit("chat_message_end", context)
        elif event_type == "agent:step":
            _emit("chat_message_step", context)
    except Exception as exc:
        logger.error(
            "Error in chat_message_audit handler for %s: %s", event_type, exc, exc_info=True
        )
