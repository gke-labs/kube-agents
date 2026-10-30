"""Send each Gemini session to one Vertex AI serving machine, so its prompt cache is found.

Gemini's implicit prompt cache lives in the memory of the machine that served
the request. For a session's first call Vertex picks that machine by hashing
the system prompt and the first user message, so sessions whose prompts differ
only in the first message (kanban workers, one card each) land on machines
that hold none of the shared system prompt, and later calls of one session can
land on a different machine from the one that cached its prefix. A top-level
``sessionId`` on the generateContent request pins every call carrying the same
id to one machine. Vertex honours it only for projects it has enabled; for any
other project the request is served as if the field were absent.

This pre-call hook sets that id for a request bound for a Gemini model on
vertex_ai: a short hash of the deployment's model and the request's first
non-system message, which a session keeps for its whole life. LiteLLM
v1.106.0 merges ``extra_body`` into the top level of the Vertex request body
(``_pop_and_merge_extra_body`` in its vertex_ai Gemini transformation), so the
id goes there. Nothing the model reads changes: the id only picks the machine.
A session whose context compression rewrites its first message gets a new id
and pays one cold call. Sessions that open with the same message (each run of
one cron prompt) share an id and so a machine; Vertex already sends those to
one machine today, since their system prompt and first message match too.

Only ``/v1/chat/completions`` traffic, which is what the agents send, and only
an alias whose every deployment is a vertex_ai Gemini model; Claude on
vertex_ai, the gemini (AI Studio) provider and every other provider go out as
sent. A caller's own ``sessionId`` is kept. Any failure, or a proxy with no
router to resolve aliases with, leaves the request as it was, with one
warning in the gateway log.

The chart renders this file into the litellm-config ConfigMap only while
``litellm.vertexSessionAffinity`` is on; tests/test_litellm_vertex_session.py
covers both.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("kube_agents.litellm_vertex_session")

# The Vertex request field, and LiteLLM's carrier for extra top-level fields.
SESSION_ID_KEY = "sessionId"
EXTRA_BODY_KEY = "extra_body"
# Hex digits kept of the sha256: far more than the sessions one project runs
# at once could collide on, and short enough to read in a log.
SESSION_ID_CHARS = 32
# Between the model and the message in the hashed text, so no pair of the two
# runs together into another pair's text.
HASH_SEPARATOR = "\n"
_ENCODING = "utf-8"
# The proxy's call types for /v1/chat/completions.
COMPLETION_CALL_TYPES = frozenset({"completion", "acompletion"})
# Request and message fields the hook reads.
MODEL_KEY = "model"
MESSAGES_KEY = "messages"
ROLE_KEY = "role"
CONTENT_KEY = "content"
TEXT_KEY = "text"
SYSTEM_ROLE = "system"
# A deployment's parameters, and the provider-qualified model string in them.
DEPLOYMENT_PARAMS_KEY = "litellm_params"
DEPLOYMENT_MODEL_KEY = "model"
# Only Vertex AI takes sessionId, and vertex_ai also serves Claude (claude-*).
# The prefix is matched on the last path segment, so a Model Garden path such
# as vertex_ai/publishers/google/models/gemini-3.1-pro-preview counts too.
VERTEX_PROVIDER = "vertex_ai"
GEMINI_MODEL_PREFIX = "gemini-"
PROVIDER_SEPARATOR = "/"
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these the once-only lines never reach the pod log.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# Whether the hook has said it is active, and whether it has reported a
# failure, so each is one line per process rather than one per request.
_active_logged = False
_failure_warned = False


def is_vertex_gemini_model(model: str) -> bool:
    """Whether a deployment's ``litellm_params.model`` names a Gemini model on vertex_ai."""
    provider, _, name = str(model or "").partition(PROVIDER_SEPARATOR)
    return provider == VERTEX_PROVIDER and name.rsplit(PROVIDER_SEPARATOR, 1)[-1].startswith(GEMINI_MODEL_PREFIX)


def vertex_gemini_model(model_name: str, router: Any) -> str | None:
    """The deployment model ``model_name`` resolves to, when every deployment is a vertex_ai Gemini model.

    None otherwise, and when there is no router: the request then goes out as sent.
    """
    if router is None:
        raise LookupError("no LiteLLM router to resolve aliases with")
    if not model_name:
        return None
    deployments = router.get_model_list(model_name=model_name) or []
    models = [(d.get(DEPLOYMENT_PARAMS_KEY) or {}).get(DEPLOYMENT_MODEL_KEY, "") for d in deployments]
    if not models or not all(is_vertex_gemini_model(m) for m in models):
        return None
    # The chart renders one upstream model per install; with several, the
    # lowest keeps the id stable across the alias's deployments.
    return min(models)


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def content_text(content: Any) -> str:
    """A message's text alone: the parts' text joined, without the markers a gateway adds to them."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part if isinstance(part, str) else str(_get(part, TEXT_KEY) or "") for part in content)
    return "" if content is None else str(content)


def session_id(model: str, messages: Any) -> str | None:
    """A short hash of the model and the first non-system message, or None when there is none."""
    first_turn = next((m for m in messages or [] if _get(m, ROLE_KEY) != SYSTEM_ROLE), None)
    if first_turn is None:
        return None
    text = model + HASH_SEPARATOR + content_text(_get(first_turn, CONTENT_KEY))
    return hashlib.sha256(text.encode(_ENCODING)).hexdigest()[:SESSION_ID_CHARS]


def add_session_id(data: dict, router: Any) -> bool:
    """Set ``extra_body.sessionId`` on a vertex_ai Gemini request; return whether it was set."""
    model = vertex_gemini_model(data.get(MODEL_KEY, ""), router)
    if model is None:
        return False
    sid = session_id(model, data.get(MESSAGES_KEY))
    if sid is None:
        return False
    extra_body = data.get(EXTRA_BODY_KEY)
    if not isinstance(extra_body, dict):
        extra_body = {}
    extra_body.setdefault(SESSION_ID_KEY, sid)
    data[EXTRA_BODY_KEY] = extra_body
    return True


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


class VertexSessionAffinity(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        global _active_logged, _failure_warned
        if call_type not in COMPLETION_CALL_TYPES:
            return data
        try:
            added = add_session_id(data, _router())
        # Any failure, so a request shape this file did not foresee costs one
        # warning and never the request it was routing.
        except Exception as exc:  # noqa: BLE001
            if not _failure_warned:
                _failure_warned = True
                logger.warning("vertex session affinity: request left without a sessionId (%s)", exc)
            return data
        if added and not _active_logged:
            _active_logged = True
            logger.info("vertex session affinity: sending a sessionId with vertex_ai Gemini requests")
        return data


_configure_logger()
proxy_handler_instance = VertexSessionAffinity()
