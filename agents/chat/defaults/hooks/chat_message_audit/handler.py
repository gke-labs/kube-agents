import logging
import sys
from pathlib import Path
from typing import Any, Dict

# This hook lives outside plugins/, so it reaches one level further up than the
# plugins do. See the note in plugins/session_store/store.py for why the path is
# computed rather than expressed as a relative import — `hooks/` has no
# __init__.py, so no relative form can resolve from here at all.
_PLUGINS_DIR = str(Path(__file__).resolve().parents[2] / "plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from common import audit_sink  # noqa: E402
from common.audit_schema import envelope  # noqa: E402
from common.redactor import AuditRedactor  # noqa: E402

logger = logging.getLogger("hermes.hook.chat_message_audit")

_TEXT_LOG_LIMIT = 4000


def _truncate(value: Any) -> str:
    text = AuditRedactor.redact_text(str(value or ""))
    if len(text) > _TEXT_LOG_LIMIT:
        return text[:_TEXT_LOG_LIMIT] + "...(truncated)"
    return text


def _record(audit_event: str, context: Dict[str, Any]) -> Dict[str, Any]:
    ctx = context or {}
    # On Google Chat the user field is the user's address, so it needs the same
    # pseudonymisation the tool-call audit applies; `principal` is the same
    # pseudonym under the structured schema's name.
    principal = AuditRedactor.pseudonymise_identity(ctx.get("user_id", ""))
    record = envelope(
        audit_event,
        {
            "platform": ctx.get("platform", ""),
            "user_id": principal,
            "principal": principal,
            "session_id": ctx.get("session_id", ""),
        },
    )
    if "message" in ctx:
        record["message"] = _truncate(ctx.get("message"))
    if "response" in ctx:
        record["response"] = _truncate(ctx.get("response"))
    if "iteration" in ctx:
        record["iteration"] = ctx.get("iteration")
    if "tool_names" in ctx:
        record["tool_names"] = ctx.get("tool_names")
    return record


def _emit(audit_event: str, context: Dict[str, Any]) -> None:
    # One JSON object per line of the profile's audit file (common/audit_sink.py),
    # which the fluent-bit sidecar tails as JSON. This hook runs on the gateway's
    # event loop; the sink sees the running loop and hands the write to its
    # writer thread, having resolved the profile on this thread first.
    audit_sink.emit(_record(audit_event, context), logger)


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
