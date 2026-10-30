"""Log, per model call, what decides whether the prompt cache is reused.

A diagnostic, off unless `litellm.cacheProbeEnabled` is set. The eval installs
set it (hack/ci-deploy.sh) to find out why an agent session writes its whole
prompt to the cache about three times instead of once. Anthropic's cache
prefix runs tools, then system, then messages: a change in any earlier part
re-writes everything after it. So each successful call logs one line holding
hashes and counts of those parts beside the usage the provider returned, and
nothing of the prompt's text:

    cache probe: {"conv": ..., "msgs": ..., "tools": ..., ...}

`conv` is a hash of the first non-system message, which a session keeps for
its whole life, and `msgs` the request's message count, which grows with every
call; together they order one session's calls across both gateway replicas,
which no per-process counter could. `tools_hash` covers the tool list,
`system_hash` the whole system text, and `system_chunks` one short hash per
fixed-size slice of it, so the first slice that changes says where in the
prompt the change sits (Hermes puts its stable part first and its volatile
part last). The usage fields are the provider's own token counts.

The chart renders this file into the litellm-config ConfigMap only while the
value is on; tests/test_litellm_cache_probe.py covers both. Delete it, its
value and its eval setting once the cause is fixed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from typing import Any

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    class CustomLogger:  # type: ignore[no-redef]
        pass

logger = logging.getLogger("kube_agents.litellm_cache_probe")

LOG_PREFIX = "cache probe: "
# Hex digits kept of each sha256: enough to tell one session's versions of a
# part apart, too short to be worth reversing.
HASH_CHARS = 8
CHUNK_HASH_CHARS = 4
# Characters per system-prompt slice. Fixed rather than a fraction of the
# length, so a change near the end shifts no earlier slice's boundary.
SYSTEM_CHUNK_CHARS = 16384
CHUNK_SEPARATOR = "."
SYSTEM_ROLE = "system"
ROLE_KEY = "role"
CONTENT_KEY = "content"
TEXT_KEY = "text"
TOOLS_KEY = "tools"
USAGE_KEY = "usage"
# Usage fields copied as they come, when the provider returns them.
USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# LiteLLM's split of the cache write by TTL tier, when it carries one.
CACHE_WRITE_DETAILS_KEY = "cache_creation_token_details"
CACHE_WRITE_TIER_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these the per-call line never reaches the pod log.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
_ENCODING = "utf-8"

# Whether a failed record has been reported, so a LiteLLM change that breaks
# the probe is logged once rather than on every call.
_failure_warned = False


def _configure_logger() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


_configure_logger()


def _short_hash(text: str, length: int = HASH_CHARS) -> str:
    return hashlib.sha256(text.encode(_ENCODING)).hexdigest()[:length]


def _as_dict(obj: Any) -> dict:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            dumped = obj.model_dump()
        except (TypeError, ValueError):
            return {}
        if isinstance(dumped, dict):
            return dumped
    return {}


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def content_text(content: Any) -> str:
    """A message's text alone: the parts' text joined, without the
    cache_control markers the gateway adds to them."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(_get(part, TEXT_KEY) or "")
            for part in content
        )
    return "" if content is None else str(content)


def system_chunk_hashes(text: str, chunk_chars: int = SYSTEM_CHUNK_CHARS) -> str:
    """One short hash per fixed-size slice of the system text, dot-joined."""
    return CHUNK_SEPARATOR.join(
        _short_hash(text[start : start + chunk_chars], CHUNK_HASH_CHARS)
        for start in range(0, len(text), chunk_chars)
    )


def tools_hash(tools: Any) -> str:
    """A hash of the tool list as sent, independent of key order."""
    return _short_hash(json.dumps(tools or [], sort_keys=True, default=str))


def usage_fields(response_obj: Any) -> dict:
    """The token counts LiteLLM reports for the call, as it reports them."""
    usage = _as_dict(_get(response_obj, USAGE_KEY))
    fields = {name: usage[name] for name in USAGE_FIELDS if isinstance(usage.get(name), int)}
    details = _as_dict(usage.get(CACHE_WRITE_DETAILS_KEY))
    for name in CACHE_WRITE_TIER_FIELDS:
        if isinstance(details.get(name), int):
            fields[name] = details[name]
    return fields


def probe_record(kwargs: dict, response_obj: Any) -> dict:
    """The per-call record: hashes and counts only, never prompt text."""
    messages = kwargs.get("messages") or []
    optional_params = kwargs.get("optional_params") or {}
    litellm_params = kwargs.get("litellm_params") or {}
    system_text = "".join(
        content_text(_get(m, CONTENT_KEY)) for m in messages if _get(m, ROLE_KEY) == SYSTEM_ROLE
    )
    first_turn: Any | None = next(
        (m for m in messages if _get(m, ROLE_KEY) != SYSTEM_ROLE), None
    )
    tools = optional_params.get(TOOLS_KEY)
    record = {
        "conv": _short_hash(content_text(_get(first_turn, CONTENT_KEY))) if first_turn is not None else None,
        "msgs": len(messages),
        "tools": len(tools or []),
        "tools_hash": tools_hash(tools),
        "system_chars": len(system_text),
        "system_hash": _short_hash(system_text),
        "system_chunks": system_chunk_hashes(system_text),
        "model": litellm_params.get("model") or kwargs.get("model"),
        "reasoning_effort": optional_params.get("reasoning_effort"),
    }
    record.update(usage_fields(response_obj))
    return record


class CacheProbe(CustomLogger):
    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        global _failure_warned
        try:
            line = json.dumps(probe_record(kwargs, response_obj), sort_keys=True, default=str)
        # Any failure, so a payload shape this file did not foresee costs one
        # warning and never the call it describes.
        except Exception as exc:  # noqa: BLE001
            if not _failure_warned:
                _failure_warned = True
                logger.warning("cache probe: record not built: %s", exc)
            return
        logger.info("%s%s", LOG_PREFIX, line)


proxy_handler_instance = CacheProbe()
