"""Send each Vertex Gemini conversation a ``session_id`` so turn 1 can read a warm prefix cache.

Vertex keeps Gemini's implicit prefix cache on the serving replica that wrote
it. Without a ``session_id`` the ``global`` endpoint picks the replica from the
system instruction plus the first user message, so a conversation's later turns
return to its own replica, but turn 1 of every new conversation lands somewhere
new and pays full price for a system prompt and tool list that another
conversation of the same profile sent moments earlier. On a project carrying
Vertex's ``LLM_SESSION_ID`` label, a request's ``session_id`` picks the replica
instead.

This pre-call hook gives a conversation one of a few ids per prompt prefix. The
id is a hash of the model alias, the head of the system prompt, the tool list,
and a slot; the slot is a hash of the whole system prompt and the first user
message, modulo the slot count. The system prompt here is the system text
ahead of the first user message, so a system message added later in the
conversation does not move the id. (That request still misses its cache:
LiteLLM folds every system message into Vertex's system instruction.) Every turn of a conversation therefore gets the
same id on every gateway replica and across restarts, with no shared state,
and a new conversation lands on a replica that another conversation with its
prompt has used, warm if that was within the last few minutes.

The slot count trades cache reuse against queueing: one slot puts every
concurrent conversation of a profile on one replica, which measured slower;
more slots reuse each id less often. Two is the default.

A caller's own ``session_id`` is left alone. On a project without the label
Vertex ignores the field, so the request routes as it did without this hook.
The chart ships this hook only when litellm.vertex.sessionAffinity.enabled is
set on a vertex_ai install.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("kube_agents.litellm_session_affinity")

# Request fields the hook reads and writes.
MODEL_KEY = "model"
MESSAGES_KEY = "messages"
TOOLS_KEY = "tools"
SYSTEM_KEY = "system"
ROLE_KEY = "role"
CONTENT_KEY = "content"
EXTRA_BODY_KEY = "extra_body"
SESSION_ID_KEY = "session_id"
SYSTEM_ROLES = frozenset({"system", "developer"})
USER_ROLE = "user"
ASSISTANT_ROLE = "assistant"
# OpenAI-format tool entries name the tool under function.name.
TOOL_FUNCTION_KEY = "function"
TOOL_NAME_KEY = "name"
# A deployment's parameters, and the provider-qualified model string in them.
DEPLOYMENT_PARAMS_KEY = "litellm_params"
DEPLOYMENT_MODEL_KEY = "model"
# session_id is a Vertex field; the Gemini API provider would refuse it.
VERTEX_PROVIDER = "vertex_ai"
GEMINI_MODEL_PREFIX = "gemini-"
PROVIDER_SEPARATOR = "/"
# The slot count, from the chart.
SLOTS_ENV = "KUBE_AGENTS_SESSION_AFFINITY_SLOTS"
DEFAULT_SLOTS = 2
# How much of the system prompt names the prefix a slot's id is shared across.
# Profiles differ within their first thousand characters, and the agent puts
# per-session text last, so the head is what conversations of one profile share.
PREFIX_HEAD_CHARS = 8192
# Vertex reads session_id as a signed 64-bit integer, sent as a string.
SESSION_ID_BYTES = 8
FIELD_SEPARATOR = "\0"
# Hex characters of each digest in the per-conversation diagnostic line: enough
# to tell two inputs apart in a log, too few to stand for the text.
DIAGNOSTIC_DIGEST_CHARS = 12
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# Whether the hook has said it is active, or that it cannot see the router;
# each is logged once rather than on every request.
_announced = False
_blind_warned = False


def is_vertex_gemini_model(model: str) -> bool:
    """Whether a deployment's ``litellm_params.model`` names a Gemini model served by Vertex."""
    provider, _, name = str(model or "").partition(PROVIDER_SEPARATOR)
    return provider == VERTEX_PROVIDER and name.rsplit(PROVIDER_SEPARATOR, 1)[-1].startswith(GEMINI_MODEL_PREFIX)


def routes_to_vertex_gemini(model_name: str, router: Any) -> bool:
    """Whether every deployment ``router`` serves ``model_name`` with is a Vertex Gemini model."""
    if router is None:
        _warn_blind("no LiteLLM router to resolve aliases with")
        return False
    if not model_name:
        return False
    try:
        deployments = router.get_model_list(model_name=model_name) or []
    except Exception as exc:  # noqa: BLE001 - an unresolvable alias is left alone
        _warn_blind(f"the router could not resolve {model_name!r} ({exc})")
        return False
    models = [(d.get(DEPLOYMENT_PARAMS_KEY) or {}).get(DEPLOYMENT_MODEL_KEY, "") for d in deployments]
    # No deployments is what the router answers for an alias it does not
    # serve, which the proxy refuses right after this hook: not a sign the hook
    # is blind, so no warning, and the one warning stays for a real blindness.
    if not models or not any(models):
        return False
    return all(is_vertex_gemini_model(m) for m in models)


def _text(value: Any) -> str:
    """A stable string for a message content or tool list, whatever its shape."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def system_prompt(data: dict) -> str:
    """The request's system text: the /v1/messages ``system`` field, then the system-role messages
    before the first user message. One a harness adds later in the conversation is left out, so
    it cannot move a conversation's id mid-way."""
    parts = [_text(data.get(SYSTEM_KEY))] if data.get(SYSTEM_KEY) else []
    for message in data.get(MESSAGES_KEY) or []:
        if not isinstance(message, dict):
            continue
        if message.get(ROLE_KEY) == USER_ROLE:
            break
        if message.get(ROLE_KEY) in SYSTEM_ROLES:
            parts.append(_text(message.get(CONTENT_KEY)))
    return FIELD_SEPARATOR.join(parts)


def first_user_message(data: dict) -> str:
    for message in data.get(MESSAGES_KEY) or []:
        if isinstance(message, dict) and message.get(ROLE_KEY) == USER_ROLE:
            return _text(message.get(CONTENT_KEY))
    return ""


def _digest(*fields: str) -> bytes:
    return hashlib.sha256(FIELD_SEPARATOR.join(fields).encode("utf-8")).digest()


def _key(data: dict, slots: int) -> tuple[str, str, int, str]:
    """The id's inputs, computed once: the system prompt, the tool list's text, the slot, and the id."""
    system = system_prompt(data)
    tools = _text(data.get(TOOLS_KEY))
    slot = int.from_bytes(_digest(system, first_user_message(data)), "big") % slots
    prefix = _digest(str(data.get(MODEL_KEY, "")), system[:PREFIX_HEAD_CHARS], tools, str(slot))
    return system, tools, slot, str(int.from_bytes(prefix[:SESSION_ID_BYTES], "big", signed=True))


def session_id_for(data: dict, slots: int) -> str:
    """The id for this request's conversation: the same for every turn, shared by ``slots`` conversations' worth of prompt."""
    return _key(data, slots)[3]


def _short(*fields: str) -> str:
    return _digest(*fields).hex()[:DIAGNOSTIC_DIGEST_CHARS]


def _tool_name(tool: Any) -> str:
    """The tool's name: OpenAI format nests it under ``function``, Anthropic format has it at the top."""
    if not isinstance(tool, dict):
        return ""
    function = tool.get(TOOL_FUNCTION_KEY)
    return str(function.get(TOOL_NAME_KEY, "") if isinstance(function, dict) else tool.get(TOOL_NAME_KEY, ""))


def is_first_turn(data: dict) -> bool:
    """Whether the request opens a conversation: no assistant message in its history."""
    return not any(isinstance(m, dict) and m.get(ROLE_KEY) == ASSISTANT_ROLE for m in data.get(MESSAGES_KEY) or [])


def key_diagnostics(data: dict, slots: int) -> dict:
    """Short digests, never content, of each input to the id, so conversations whose ids differ show which input did."""
    system, tools_text, slot, session_id = _key(data, slots)
    tools = data.get(TOOLS_KEY) if isinstance(data.get(TOOLS_KEY), list) else []
    names = [_tool_name(t) for t in tools]
    by_name = [t for _, t in sorted(zip(names, tools), key=lambda pair: pair[0])]
    return {
        "model": str(data.get(MODEL_KEY, "")),
        "head": _short(system[:PREFIX_HEAD_CHARS]),
        "system": _short(system),
        "system_chars": len(system),
        "tools": _short(tools_text),
        "tools_sorted": _short(_text(by_name)),
        "tool_order": _short(*names),
        "tool_count": len(tools),
        "slot": slot,
        "session_id": session_id,
    }


def slot_count() -> int:
    raw = os.environ.get(SLOTS_ENV, "")
    try:
        slots = int(raw) if raw else DEFAULT_SLOTS
    except ValueError:
        slots = 0
    if slots < 1:
        logger.warning("session affinity: %s=%r is not a positive integer; using %d", SLOTS_ENV, raw, DEFAULT_SLOTS)
        return DEFAULT_SLOTS
    return slots


def _warn_blind(reason: str) -> None:
    global _blind_warned
    if not _blind_warned:
        _blind_warned = True
        logger.warning("session affinity: hook inactive, %s; requests route without a session_id", reason)


def _configure_logger() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


def _router() -> Any:
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:  # noqa: BLE001 - outside the proxy there is no router
        return None
    return llm_router


class SessionAffinity(CustomLogger):
    def __init__(self, slots: int | None = None) -> None:
        super().__init__()
        self.slots = slots or slot_count()

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        global _announced
        if not routes_to_vertex_gemini(data.get(MODEL_KEY, ""), _router()):
            return data
        extra = data.get(EXTRA_BODY_KEY)
        if not isinstance(extra, dict):
            extra = {}
        if SESSION_ID_KEY in extra:
            return data
        if is_first_turn(data):
            # One line per new conversation: conversations of one profile that
            # should share an id but do not show which input differs.
            diagnostics = key_diagnostics(data, self.slots)
            extra[SESSION_ID_KEY] = diagnostics["session_id"]
            logger.info("session affinity: new conversation %s", " ".join(f"{k}={v}" for k, v in diagnostics.items()))
        else:
            extra[SESSION_ID_KEY] = session_id_for(data, self.slots)
        data[EXTRA_BODY_KEY] = extra
        if not _announced:
            _announced = True
            logger.info("session affinity: sending session_id on Vertex Gemini requests, %d slot(s) per prompt", self.slots)
        return data


_configure_logger()
proxy_handler_instance = SessionAffinity()
