"""Keep a Gemini model's earlier thinking out of the answer it gives next.

A client that enables thinking replays each earlier assistant turn's thinking
in the next request: Claude Code sends Anthropic ``thinking`` blocks, and an
OpenAI-format client may send ``thinking_blocks`` on the assistant message.
LiteLLM's Gemini transformation turns each one into a part carrying the
thinking text and its ``thoughtSignature`` but not ``thought: true``, so Gemini
reads its own earlier thought summaries ("**Formulating the Report** / I will
now draft...") as text it said aloud, and keeps saying them at the top of its
answers. No prompt reaches that text.

This pre-call hook drops those blocks from assistant messages before LiteLLM
converts the request, for an alias whose every deployment is a Gemini model.
Gemini's own signatures still ride the tool calls. On ``/v1/chat/completions``
an OpenAI-format ``reasoning_content``, which LiteLLM does send as
``thought: true``, is left alone. On ``/v1/messages`` LiteLLM builds that same
thought part from the blocks this hook drops, so a Claude Code session's later
turns carry no earlier thinking at all: more than the upstream fix removes, and
measured to answer cleanly. Any other model, Claude above all, needs its
thinking replayed and is not touched. The gate reads the alias's own
deployments only; a router fallback to a Claude group from a Gemini alias
would receive the stripped history, and neither shipped config has one.

Fixed upstream in BerriAI/litellm#44661 (the Gemini request no longer builds a
part from ``thinking_blocks``). Delete this hook, its mounts and its callback
entry once images.json pins a LiteLLM release that carries that fix.

The chart renders this file into the litellm-config ConfigMap; the kustomize
base carries a copy (k8s-operator/config/integrations/litellm/base), and
tests/test_litellm_thinking_replay.py holds the two identical.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("kube_agents.litellm_thinking_replay")

# The Anthropic content block types that carry replayed thinking.
THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
# The OpenAI-format assistant message key that carries the same blocks.
THINKING_BLOCKS_KEY = "thinking_blocks"
ASSISTANT_ROLE = "assistant"
# Request and message fields the hook reads, in both formats.
MODEL_KEY = "model"
MESSAGES_KEY = "messages"
ROLE_KEY = "role"
CONTENT_KEY = "content"
PART_TYPE_KEY = "type"
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
        # The proxy only asks about names it serves, so an alias with no
        # deployments, or deployments with no model string, is a shape this
        # hook no longer reads.
        _warn_blind(f"the router returned no deployment model for {model_name!r}")
        return False
    return all(is_gemini_model(m) for m in models)


def strip_replayed_thinking(messages: Any) -> int:
    """Drop replayed thinking from assistant messages in place; return how many blocks went."""
    stripped = 0
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get(ROLE_KEY) != ASSISTANT_ROLE:
            continue
        content = message.get(CONTENT_KEY)
        if isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get(PART_TYPE_KEY) in THINKING_BLOCK_TYPES)]
            stripped += len(content) - len(kept)
            message[CONTENT_KEY] = kept
        blocks = message.pop(THINKING_BLOCKS_KEY, None)
        if isinstance(blocks, list):
            stripped += len(blocks)
    return stripped


def _warn_blind(reason: str) -> None:
    global _blind_warned
    if not _blind_warned:
        _blind_warned = True
        logger.warning("thinking replay: hook inactive, %s; Gemini requests keep their replayed thinking", reason)


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


class ThinkingReplayStripper(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        if routes_to_gemini(data.get(MODEL_KEY, ""), _router()):
            stripped = strip_replayed_thinking(data.get(MESSAGES_KEY))
            if stripped:
                logger.info("thinking replay: dropped %d replayed thinking block(s) bound for a Gemini model", stripped)
        return data


_configure_logger()
proxy_handler_instance = ThinkingReplayStripper()
