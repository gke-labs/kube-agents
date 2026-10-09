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
Gemini's own signatures still ride the tool calls, and ``reasoning_content``,
which LiteLLM does send as ``thought: true``, is left alone. Any other model,
Claude above all, needs its thinking replayed and is not touched.

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
# LiteLLM providers that serve Gemini, and the model-name prefix that marks a
# Gemini model under them (vertex_ai also serves Claude, as claude-*).
GEMINI_PROVIDERS = frozenset({"gemini", "vertex_ai"})
GEMINI_MODEL_PREFIX = "gemini-"
PROVIDER_SEPARATOR = "/"
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these the per-request count never reaches the pod
# log, and that line is the one sign the hook fired.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def is_gemini_model(model: str) -> bool:
    """Whether a deployment's ``litellm_params.model`` names a Gemini model."""
    provider, _, name = str(model or "").partition(PROVIDER_SEPARATOR)
    return provider in GEMINI_PROVIDERS and name.startswith(GEMINI_MODEL_PREFIX)


def routes_to_gemini(model_name: str, router: Any) -> bool:
    """Whether every deployment ``router`` serves ``model_name`` with is a Gemini model.

    False when the alias cannot be resolved: the hook then leaves the request
    as it is, which is right for any model that is not Gemini.
    """
    if router is None or not model_name:
        return False
    try:
        deployments = router.get_model_list(model_name=model_name) or []
    except Exception:  # noqa: BLE001 - an unresolvable alias is left alone
        return False
    models = [(d.get("litellm_params") or {}).get("model", "") for d in deployments]
    return bool(models) and all(is_gemini_model(m) for m in models)


def strip_replayed_thinking(messages: Any) -> int:
    """Drop replayed thinking from assistant messages in place; return how many blocks went."""
    stripped = 0
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict) or message.get("role") != ASSISTANT_ROLE:
            continue
        content = message.get("content")
        if isinstance(content, list):
            kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in THINKING_BLOCK_TYPES)]
            stripped += len(content) - len(kept)
            message["content"] = kept
        blocks = message.pop(THINKING_BLOCKS_KEY, None)
        if isinstance(blocks, list):
            stripped += len(blocks)
    return stripped


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
        if routes_to_gemini(data.get("model", ""), _router()):
            stripped = strip_replayed_thinking(data.get("messages"))
            if stripped:
                logger.info("thinking replay: dropped %d replayed thinking block(s) bound for a Gemini model", stripped)
        return data


_configure_logger()
proxy_handler_instance = ThinkingReplayStripper()
