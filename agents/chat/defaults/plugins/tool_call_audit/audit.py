import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# See the matching note in plugins/session_store/store.py.
_PLUGINS_DIR = str(Path(__file__).resolve().parents[1])
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from common.redactor import AuditRedactor  # noqa: E402

logger = logging.getLogger("hermes.plugin.tool_call_audit")

_PAYLOAD_LOG_LIMIT = 2000


def _redacted_repr(value: Any) -> str:
    """`json.dumps` fallback for values it cannot encode.

    Redaction has already walked the structure by the time this runs, but it
    left non-JSON objects alone — it has no way to know what ``str()`` will make
    of them. An object whose ``__repr__`` embeds a token or an address would
    otherwise reach the log verbatim, so redact the rendered form too.
    """
    return AuditRedactor.redact_text(str(value))


def _serialize(value: Any) -> str:
    """Redact, then serialise, then truncate.

    Redaction runs on the structure rather than the rendered string so that a
    sensitive mapping key (``clientSecret``) is caught by name even when its
    value looks like ordinary text. Truncation runs last so a redacted marker is
    never cut in half.
    """
    value = AuditRedactor.redact(value)
    if isinstance(value, str):
        if len(value) > _PAYLOAD_LOG_LIMIT:
            return value[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
        return value
    try:
        serialized = json.dumps(value, default=_redacted_repr, sort_keys=True)
    except Exception:
        serialized = AuditRedactor.redact_text(str(value))
    if len(serialized) > _PAYLOAD_LOG_LIMIT:
        return serialized[:_PAYLOAD_LOG_LIMIT] + "...(truncated)"
    return serialized


def _emit(event: str, fields: Dict[str, Any]) -> None:
    record = {"audit_event": event, **fields}
    logger.info(json.dumps(record, default=str, sort_keys=True))


def log_pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "tool_call_start",
            {"tool_name": tool_name, "task_id": task_id, "args": _serialize(args or {})},
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_tool_call hook: %s", exc, exc_info=True)


def log_post_tool_call(
    tool_name: str = "",
    result: Any = None,
    duration_ms: Optional[float] = None,
    task_id: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "tool_call_end",
            {
                "tool_name": tool_name,
                "task_id": task_id,
                "duration_ms": duration_ms,
                "result": _serialize(result),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit post_tool_call hook: %s", exc, exc_info=True)


def log_pre_approval_request(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_request",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_approval_request hook: %s", exc, exc_info=True)


def log_post_approval_response(
    command: str = "",
    description: str = "",
    pattern_key: str = "",
    surface: str = "",
    choice: str = "",
    **kwargs: Any,
) -> None:
    try:
        _emit(
            "approval_response",
            {
                "surface": surface,
                "pattern_key": pattern_key,
                "choice": choice,
                "description": description,
                "command": _serialize(command),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit post_approval_response hook: %s", exc, exc_info=True)


def log_pre_gateway_dispatch(
    event: Any,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> None:
    try:
        source = getattr(event, "source", None)
        session_id = ""
        if source is not None and session_store is not None:
            try:
                session_entry = session_store.get_or_create_session(source)
                session_id = getattr(session_entry, "session_id", "") or ""
            except Exception:
                pass

        text = getattr(event, "text", "") or ""
        platform = ""
        user_id = ""
        if source is not None:
            platform_obj = getattr(source, "platform", "") or ""
            platform = getattr(platform_obj, "value", None) or str(platform_obj)
            user_id = getattr(source, "user_id", "") or ""

        _emit(
            "gateway_dispatch",
            {
                "session_id": session_id,
                "platform": platform,
                "user_id": AuditRedactor.pseudonymise_identity(user_id),
                "text": _serialize(text),
            },
        )
    except Exception as exc:
        logger.error("Error in tool_call_audit pre_gateway_dispatch hook: %s", exc, exc_info=True)

