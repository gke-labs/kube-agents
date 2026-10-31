"""Drop a caller's own prompt-cache markers from a request bound for a Gemini model.

Claude Code marks its prompt-cache breakpoints itself: ``cache_control`` on
the last system block, on the last tool and on the tail of the conversation,
in every ``/v1/messages`` request it sends. Behind this gateway that caller is
the A2A worker adapter's Claude Code session, pointed here by
``ANTHROPIC_BASE_URL`` (a2a/cmd/worker-adapter/main.go) and asking for
``model-default`` with no knowledge of the model behind it. The markers are
Anthropic's, and on an Anthropic-family model they are what the chart itself
injects (``cache_control_injection_points`` in _helpers.tpl). On a Gemini
model LiteLLM translates them into Vertex explicit context caching: a
``cachedContents`` create for the marked prefix before the call.
gemini-4-argon has no explicit caching and answers every such request 400,
"Model `gemini-4-argon` does not support explicit context caching"
(gke-labs/kube-agents#4318: all three worker-adapter calls in the first Argon
nightly); on a Gemini model that does have it, the create is the stall that
the chart's prompt-caching comment describes. Gemini's implicit caching needs
no markers and serves a repeated prefix anyway.

This pre-call hook removes ``cache_control`` from the request's ``system``
blocks, its message objects and content blocks (a tool result's nested
content included) and its tools, for an alias whose every deployment is a
Gemini model, before LiteLLM converts the request. Nothing else is touched: a
``cache_control`` key inside a tool's input or a tool result's text is data
and stays. Any other model, Claude above all, keeps its markers, which are
what make its prompt cache work. The gate reads the alias's own deployments,
the way the thinking-replay hook beside this one does; ``is_gemini_model``
is that hook's, kept identical by tests/test_litellm_cache_control_strip.py.

The chart renders this file into the litellm-config ConfigMap and mounts it
beside config.yaml, where LiteLLM resolves a callback module. The kustomize
dev base does not carry it.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("kube_agents.litellm_cache_control_strip")

# The Anthropic prompt-cache marker, on a block, a message or a tool.
CACHE_CONTROL_KEY = "cache_control"
# Request fields the hook reads, in both the Anthropic and the OpenAI format.
MODEL_KEY = "model"
SYSTEM_KEY = "system"
MESSAGES_KEY = "messages"
TOOLS_KEY = "tools"
CONTENT_KEY = "content"
PART_TYPE_KEY = "type"
# An Anthropic tool result carries its own content blocks, which Claude Code
# can mark too; an OpenAI-format tool carries its marker on the function.
TOOL_RESULT_TYPE = "tool_result"
TOOL_FUNCTION_KEY = "function"
# A deployment's parameters, and the provider-qualified model string in them.
DEPLOYMENT_PARAMS_KEY = "litellm_params"
DEPLOYMENT_MODEL_KEY = "model"
# LiteLLM providers that serve Gemini, and the model-name prefix that marks a
# Gemini model under them (vertex_ai also serves Claude, as claude-*). The
# prefix is matched on the last path segment, so a Model Garden path such as
# vertex_ai/publishers/google/models/gemini-3.5-flash counts too.
GEMINI_PROVIDERS = frozenset({"gemini", "vertex_ai"})
GEMINI_MODEL_PREFIX = "gemini-"
PROVIDER_SEPARATOR = "/"
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these the per-request count never reaches the pod
# log, and that line is the one sign the hook fired.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# Whether the hook has already said it cannot see the router, so a LiteLLM
# change that blinds it is logged once rather than on every request.
_blind_warned = False


def is_gemini_model(model: str) -> bool:
    """Whether a deployment's ``litellm_params.model`` names a Gemini model."""
    provider, _, name = str(model or "").partition(PROVIDER_SEPARATOR)
    return provider in GEMINI_PROVIDERS and name.rsplit(PROVIDER_SEPARATOR, 1)[-1].startswith(GEMINI_MODEL_PREFIX)


def routes_to_gemini(model_name: str, router: Any) -> bool:
    """Whether every deployment ``router`` serves ``model_name`` with is a Gemini model.

    False when the alias cannot be resolved: the hook then leaves the request
    as it is, which is right for any model that is not Gemini, and says once
    that it is blind.
    """
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
    if not models or not any(models):
        # No deployments is what the router answers for an alias it does not
        # serve, which the proxy refuses right after this hook: not this hook's
        # concern and not a sign it is blind, so no warning (it would spend
        # the one-shot warning on a client's typo).
        logger.debug("no deployment model for %r; request left as sent", model_name)
        return False
    return all(is_gemini_model(m) for m in models)


def _pop_marker(obj: Any) -> int:
    if isinstance(obj, dict) and CACHE_CONTROL_KEY in obj:
        del obj[CACHE_CONTROL_KEY]
        return 1
    return 0


def _strip_blocks(blocks: Any) -> int:
    """Markers on a list of content blocks, and inside a tool result's own blocks."""
    stripped = 0
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        stripped += _pop_marker(block)
        if block.get(PART_TYPE_KEY) == TOOL_RESULT_TYPE:
            stripped += _strip_blocks(block.get(CONTENT_KEY))
    return stripped


def strip_cache_control(data: Any) -> int:
    """Drop every ``cache_control`` marker from the request in place; return how many went.

    Reads ``system`` (a list of blocks; a plain string carries none),
    ``messages`` (the message itself and its content blocks) and ``tools``
    (the tool and, in the OpenAI format, its ``function``). A tool's input
    and a tool result's text are not read: a key named ``cache_control`` in
    there is the caller's data.
    """
    if not isinstance(data, dict):
        return 0
    stripped = _strip_blocks(data.get(SYSTEM_KEY))
    for message in data.get(MESSAGES_KEY) if isinstance(data.get(MESSAGES_KEY), list) else []:
        if not isinstance(message, dict):
            continue
        stripped += _pop_marker(message)
        stripped += _strip_blocks(message.get(CONTENT_KEY))
    for tool in data.get(TOOLS_KEY) if isinstance(data.get(TOOLS_KEY), list) else []:
        if not isinstance(tool, dict):
            continue
        stripped += _pop_marker(tool)
        stripped += _pop_marker(tool.get(TOOL_FUNCTION_KEY))
    return stripped


def _warn_blind(reason: str) -> None:
    global _blind_warned
    if not _blind_warned:
        _blind_warned = True
        logger.warning("cache markers: hook inactive, %s; Gemini requests keep their callers' cache_control", reason)


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


class CacheControlStripper(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        if routes_to_gemini(data.get(MODEL_KEY, ""), _router()):
            stripped = strip_cache_control(data)
            if stripped:
                logger.info("cache markers: dropped %d cache_control marker(s) bound for a Gemini model", stripped)
        return data


_configure_logger()
proxy_handler_instance = CacheControlStripper()
