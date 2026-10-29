"""Shared Hermes plugin that injects the backing LLM model and provider into context.

Every profile in the Kubernetes Agentic Harness (`chat`, `platform`, `cluster`,
`tse`) talks to the LiteLLM inference gateway under a profile-config alias
(`model-default` or `hermes-agent`) and `provider: custom`. Without runtime
model discovery, an agent asked what model or provider it runs on either leaks
the internal router alias (`model-default`, `custom`) or guesses from its
weights.

This plugin registers a `pre_llm_call` hook across all profiles. It reads the
profile's own `model.base_url` (and optional API key) from the active Hermes
config, queries `<base_url>/model/info` on the LiteLLM gateway, caches the
resolved `<provider>/<model>` with a 5-minute TTL so live gateway switches take
effect without a pod restart, and returns a `[RUNTIME MODEL IDENTITY]` system
prompt block.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_HEADER = "[RUNTIME MODEL IDENTITY]"

_PLACEHOLDER_ALIASES = ("model-default", "hermes-agent")
_CUSTOM_PROVIDER = "custom"
# LiteLLM transport handlers that carry no vendor provider behind the key:
# `openai_compatible` renders as `hosted_vllm/<model>` in `_helpers.tpl`, and
# `custom` is LiteLLM's generic custom-endpoint prefix.
_SELF_HOSTED_PROVIDER_KEYS = frozenset({"hosted_vllm", "openai_compatible", _CUSTOM_PROVIDER})

_PROVIDER_LABELS: Dict[str, str] = {
    "vertex_ai": "Google Cloud Vertex AI",
    "gemini": "Google Gemini",
    "google": "Google Gemini",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "azure": "Azure OpenAI",
    "bedrock": "Amazon Bedrock",
    "ollama": "Ollama",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "mistral": "Mistral AI",
    "cohere": "Cohere",
    "groq": "Groq",
    "together_ai": "Together AI",
    "fireworks_ai": "Fireworks AI",
}

_PROBE_TIMEOUT_SECS = 0.5
_PROBE_SUCCESS_CACHE_TTL_SECS = 300.0
_PROBE_RETRY_COOLDOWN_SECS = 30.0
# ProxyHandler({}) rather than urlopen's default opener, which reads http_proxy
# from the environment: a proxy set on the container (an AgentPlugin's env, a
# base image) would carry the ClusterIP probe off the pod even when NO_PROXY
# exempts .svc and .cluster.local suffixes.
_MODEL_INFO_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_cached_gateway_model: str = ""
_cached_gateway_model_expires_at: float = 0.0
_next_gateway_probe_at: float = 0.0


def _reset_cache() -> None:
    """Reset the in-memory model identity cache (used by unit tests)."""
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at
    _cached_gateway_model = ""
    _cached_gateway_model_expires_at = 0.0
    _next_gateway_probe_at = 0.0


def _split_backing(raw: str) -> Tuple[str, str]:
    """Split `<provider>/<model>` and drop placeholder router names.

    The provider key is casefolded, and the model half is compared casefolded,
    so `Model-Default` or `Custom` is caught the same way as the lowercase
    spelling. A model half that is a router alias or the bare `custom`
    placeholder means no backing model is known. The model half keeps its own
    spelling, because a served model id (a Hugging Face id on vLLM) is
    case-sensitive.
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    provider_key, model_name = s.split("/", 1) if "/" in s else ("", s)
    provider_key, model_name = provider_key.strip().casefold(), model_name.strip()
    folded = model_name.casefold()
    if not model_name or folded in _PLACEHOLDER_ALIASES or folded == _CUSTOM_PROVIDER:
        return "", ""
    return provider_key, model_name


def _format_display_model(model_id: str) -> str:
    """Render a readable display title for common model IDs."""
    lower = model_id.lower()
    if lower.startswith("gemini-"):
        tokens = model_id.split("-")
        formatted = []
        for tok in tokens:
            t_low = tok.lower()
            if t_low == "gemini":
                formatted.append("Gemini")
            elif t_low in ("pro", "flash", "lite", "preview", "exp", "ultra", "nano"):
                formatted.append(t_low.capitalize())
            else:
                formatted.append(tok)
        return " ".join(formatted)
    if lower.startswith("claude-"):
        tokens = model_id.split("-")
        formatted = []
        for tok in tokens:
            t_low = tok.lower()
            if t_low == "claude":
                formatted.append("Claude")
            elif t_low in ("sonnet", "opus", "haiku"):
                formatted.append(t_low.capitalize())
            else:
                formatted.append(tok)
        return " ".join(formatted)
    if lower.startswith("gpt-"):
        return "GPT-" + model_id[4:]
    return model_id


def _load_profile_endpoint() -> Tuple[str, str]:
    """Return `(model_info_url, api_key)` from the active Hermes profile configuration.

    Derives the probe URL from the profile's own `model.base_url` (written by
    `platformagent_manifests.go:1999`), rather than `INFERENCE_GATEWAY_SERVICE_HOST`,
    so profile pods running in `<agent-ns>-profiles` reach the gateway across
    namespaces using the exact URL and API key configured for their LLM calls.
    """
    cfg: Dict[str, Any] = {}
    try:
        from hermes_cli.config import load_config  # type: ignore

        loaded = load_config()
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        cfg = {}

    if not cfg:
        for env_var in ("HERMES_MANAGED_DIR", "HERMES_HOME"):
            root = os.environ.get(env_var, "").strip()
            if not root:
                continue
            cfg_path = Path(root) / "config.yaml"
            if cfg_path.is_file():
                try:
                    loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        cfg = loaded
                        break
                except Exception:
                    continue

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    base_url = str(
        model_cfg.get("base_url")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("HERMES_BASE_URL")
        or ""
    ).strip()

    api_key = str(
        model_cfg.get("api_key")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("LITELLM_API_KEY")
        or ""
    ).strip()
    if api_key.lower() == "none":
        api_key = ""

    if not base_url:
        return "", api_key

    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        info_url = f"{trimmed}/model/info"
    else:
        info_url = f"{trimmed}/v1/model/info"
    return info_url, api_key


def _probe_gateway_model(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = _PROBE_TIMEOUT_SECS,
) -> str:
    """Resolve the backing `<provider>/<model>` from the profile's LiteLLM gateway.

    Results are cached for `_PROBE_SUCCESS_CACHE_TTL_SECS` (5 minutes) so
    provider switches in LiteLLM take effect without pod restarts. A gateway
    response that names no qualifying backing model clears any cached value and
    waits 30s before re-probing, while transport or parse errors keep the
    last-known good model across the 30s failure cooldown.
    """
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at
    now = time.monotonic()
    if base_url is None:
        if _cached_gateway_model and now < _cached_gateway_model_expires_at:
            return _cached_gateway_model
        if now < _next_gateway_probe_at:
            return _cached_gateway_model
        info_url, resolved_key = _load_profile_endpoint()
        if api_key is None:
            api_key = resolved_key
    else:
        trimmed = base_url.strip().rstrip("/")
        if not trimmed:
            return ""
        info_url = f"{trimmed}/model/info" if trimmed.endswith("/v1") else f"{trimmed}/v1/model/info"
        api_key = (api_key or "").strip()
        if api_key.lower() == "none":
            api_key = ""

    if not info_url:
        return _cached_gateway_model if base_url is None else ""

    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(info_url, headers=headers, method="GET")
    try:
        with _MODEL_INFO_OPENER.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            logger.warning("model info from %s is not a JSON object; backing model unknown", info_url)
            if base_url is None:
                _cached_gateway_model = ""
                _cached_gateway_model_expires_at = 0.0
                _next_gateway_probe_at = now + _PROBE_RETRY_COOLDOWN_SECS
            return ""
        raw_entries = payload.get("data")
        entries = raw_entries if isinstance(raw_entries, (list, tuple)) else ()
        for alias in _PLACEHOLDER_ALIASES:
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("model_name") or "").strip().casefold() == alias:
                    params = entry.get("litellm_params")
                    model = str((params if isinstance(params, dict) else {}).get("model") or "").strip()
                    if _split_backing(model)[1]:
                        if base_url is None:
                            _cached_gateway_model = model
                            _cached_gateway_model_expires_at = now + _PROBE_SUCCESS_CACHE_TTL_SECS
                        return model
        logger.warning(
            "model info from %s has no %s entry with a backing model",
            info_url,
            list(_PLACEHOLDER_ALIASES),
        )
        if base_url is None:
            _cached_gateway_model = ""
            _cached_gateway_model_expires_at = 0.0
    except Exception as exc:
        logger.debug("cannot read model info from %s: %s", info_url, exc)

    if base_url is None:
        _next_gateway_probe_at = now + _PROBE_RETRY_COOLDOWN_SECS
        return _cached_gateway_model
    return ""


def render_model_identity(raw_model: Optional[str] = None) -> Optional[str]:
    """Render the `[RUNTIME MODEL IDENTITY]` prompt block when known, or `None`.

    The LiteLLM gateway configured by the profile's `model.base_url` is the
    single source of truth; container environment variables are not consulted as
    a fallback because they go stale across live gateway model switches.
    """
    target = raw_model if raw_model is not None else _probe_gateway_model()
    provider_key, model_name = _split_backing(target)
    if not model_name:
        return None
    display_model = _format_display_model(model_name)
    model_str = f"{model_name} ({display_model})" if display_model != model_name else model_name
    if provider_key and provider_key not in _SELF_HOSTED_PROVIDER_KEYS:
        label = _PROVIDER_LABELS.get(provider_key)
        prov = f"Provider: {label} ({provider_key})" if label else f"Provider: {provider_key}"
        return (
            f"{_HEADER}\n"
            f"Backing model: {model_str}\n"
            f"{prov}\n"
            "When asked what model or provider you are running on, state this backing model "
            "and provider directly; never report internal router alias names or the `custom` "
            "provider placeholder."
        )
    return (
        f"{_HEADER}\n"
        f"Backing model: {model_str}\n"
        "When asked what model you are running on, state this backing model directly, "
        "and if asked about the provider, say that provider details are not exposed in "
        "your active environment; never report internal router alias names or the `custom` "
        "provider placeholder."
    )


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Inject `[RUNTIME MODEL IDENTITY]` into the system prompt on every profile."""
    try:
        block = render_model_identity()
    except Exception as exc:
        logger.warning("Could not render runtime model identity: %s", exc)
        return None
    if not block:
        return None
    return {"system_prompt": block}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", handle_pre_llm_call)
