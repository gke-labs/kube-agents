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
effect without a pod restart, and returns a `[RUNTIME MODEL IDENTITY]` context
block under `context`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_HEADER = "[RUNTIME MODEL IDENTITY]"
_PRE_LLM_CALL_HOOK = "pre_llm_call"
_CONTEXT_RESULT_KEY = "context"
_BACKING_MODEL_LABEL = "Backing model: "
_PROVIDER_LABEL = "Provider: "
_CONTEXT_WITH_PROVIDER_INSTRUCTIONS = (
    "When asked what model or provider you are running on, state this backing model "
    "and provider directly; never report internal router alias names or the `custom` "
    "provider placeholder."
)
_CONTEXT_WITHOUT_PROVIDER_INSTRUCTIONS = (
    "When asked what model you are running on, state this backing model directly, "
    "and if asked about the provider, say that provider details are not exposed in "
    "your active environment; never report internal router alias names or the `custom` "
    "provider placeholder."
)

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

_PROVIDER_SEPARATOR = "/"
_RESOURCE_PATH_PREFIX = "projects/"
_RESOURCE_PATH_MIN_SEGMENTS = 6
_RESOURCE_PATH_LOCATIONS_INDEX = 2
_RESOURCE_PATH_LOCATIONS_SEGMENT = "locations"
_RESOURCE_PATH_OWNER_SEGMENT = "projects"
_PUBLISHER_PATH_PREFIX = "publishers/"
_PUBLISHER_PATH_MIN_SEGMENTS = 4
_PUBLISHER_PATH_MODELS_INDEX = 2
_PUBLISHER_PATH_MODELS_SEGMENT = "models"
_PUBLISHER_PATH_OWNER_SEGMENT = "publishers"
_ARN_PREFIX = "arn:"
_ARN_FIELD_SEPARATOR = ":"
_ARN_FIELD_COUNT = 6

_GEMINI_PREFIX = "gemini-"
_GEMINI_WORD = "gemini"
_GEMINI_TITLE = "Gemini"
_GEMINI_TITLE_TOKENS = frozenset({"pro", "flash", "lite", "preview", "exp", "ultra", "nano"})
_CLAUDE_PREFIX = "claude-"
_CLAUDE_WORD = "claude"
_CLAUDE_TITLE = "Claude"
_CLAUDE_TITLE_TOKENS = frozenset({"sonnet", "opus", "haiku"})
_DISPLAY_FAMILIES: Tuple[Tuple[str, str, str, FrozenSet[str]], ...] = (
    (_GEMINI_PREFIX, _GEMINI_WORD, _GEMINI_TITLE, _GEMINI_TITLE_TOKENS),
    (_CLAUDE_PREFIX, _CLAUDE_WORD, _CLAUDE_TITLE, _CLAUDE_TITLE_TOKENS),
)
_SHORT_VERSION_MAX_DIGITS = 2
_VERSION_DOT_JOINER = "."
_DISPLAY_SPACE_JOINER = " "
_DISPLAY_HYPHEN = "-"
_GPT_PREFIX = "gpt-"
_GPT_TITLE_PREFIX = "GPT-"

_CONFIG_DIR_ENV_VARS = ("HERMES_MANAGED_DIR", "HERMES_HOME")
_CONFIG_FILENAME = "config.yaml"
_CONFIG_MODEL_SECTION = "model"
_CONFIG_BASE_URL_FIELD = "base_url"
# Named without `API_KEY` per CodeQL's SensitiveDataHeuristics rule: these hold
# config field / environment variable names, never credential values.
_CONFIG_KEY_FIELD = "api_key"
_BASE_URL_ENV_VARS = ("OPENAI_BASE_URL", "HERMES_BASE_URL")
_GATEWAY_KEY_ENV_VARS = ("LITELLM_API_KEY", "OPENAI_API_KEY")
_ENV_VAR_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PLACEHOLDER_KEY_VALUES = frozenset({"none", "no-key-required"})
_API_VERSION_SUFFIX = "/v1"
_MODEL_INFO_SUFFIX = "/model/info"
_MODEL_INFO_V1_SUFFIX = "/v1/model/info"
_ALLOWED_SCHEMES = frozenset({"http", "https"})

_PAYLOAD_DATA_KEY = "data"
_PAYLOAD_MODEL_NAME_KEY = "model_name"
_PAYLOAD_LITELLM_PARAMS_KEY = "litellm_params"
_PAYLOAD_MODEL_KEY = "model"
_PAYLOAD_MODEL_INFO_KEY = "model_info"
_PAYLOAD_LITELLM_PROVIDER_KEY = "litellm_provider"

_HTTP_GET = "GET"
_HTTP_ACCEPT_HEADER = "Accept"
_HTTP_ACCEPT_JSON = "application/json"
_HTTP_AUTH_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "
_MAX_RESPONSE_BYTES = 1024 * 1024
_UTF8_ENCODING = "utf-8"
_DECODE_ERRORS = "replace"

_PROBE_TIMEOUT_SECS = 0.5
_PROBE_SUCCESS_CACHE_TTL_SECS = 300.0
_PROBE_RETRY_COOLDOWN_SECS = 30.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so Authorization headers are never forwarded off-gateway."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# ProxyHandler({}) rather than urlopen's default opener, which reads http_proxy
# from the environment: a proxy set on the container (an AgentPlugin's env, a
# base image) would carry the ClusterIP probe off the pod even when NO_PROXY
# exempts .svc and .cluster.local suffixes. Paired with _NoRedirect so a 3xx
# response cannot forward the gateway bearer token to another host.
_MODEL_INFO_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

_CACHE_LOCK = threading.Lock()
_cached_gateway_model: str = ""
_cached_gateway_model_expires_at: float = 0.0
_next_gateway_probe_at: float = 0.0
_cache_generation: int = 0


def _reset_cache() -> None:
    """Reset the in-memory model identity cache (used by unit tests)."""
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at, _cache_generation
    with _CACHE_LOCK:
        _cached_gateway_model = ""
        _cached_gateway_model_expires_at = 0.0
        _next_gateway_probe_at = 0.0
        _cache_generation += 1


def _clear_cache_with_cooldown(gen: int) -> str:
    """Clear the cached backing model and enter retry cooldown if generation still matches."""
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at, _cache_generation
    with _CACHE_LOCK:
        if _cache_generation == gen:
            _cached_gateway_model = ""
            _cached_gateway_model_expires_at = 0.0
            _next_gateway_probe_at = time.monotonic() + _PROBE_RETRY_COOLDOWN_SECS
            _cache_generation += 1
        return _cached_gateway_model


def _expand_env_ref(value: str) -> str:
    """Expand a whole-string `${VAR}` reference against `os.environ`."""
    match = _ENV_VAR_REF_RE.fullmatch(value)
    if match:
        return os.environ.get(match.group(1), "").strip()
    return value


def _normalize_endpoint(base_url: str, api_key: str) -> Tuple[str, str]:
    """Normalize `(base_url, api_key)` into `(model_info_url, sanitized_key)`."""
    sanitized_key = (api_key or "").strip()
    if sanitized_key.lower() in _PLACEHOLDER_KEY_VALUES:
        sanitized_key = ""

    trimmed = (base_url or "").strip().rstrip(_PROVIDER_SEPARATOR)
    if not trimmed:
        return "", sanitized_key

    scheme = urllib.parse.urlparse(trimmed).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        logger.warning("ignoring non-HTTP(S) model.base_url scheme %r", scheme)
        return "", sanitized_key

    if trimmed.endswith(_API_VERSION_SUFFIX):
        info_url = f"{trimmed}{_MODEL_INFO_SUFFIX}"
    else:
        info_url = f"{trimmed}{_MODEL_INFO_V1_SUFFIX}"
    return info_url, sanitized_key


def _trim_model_resource_name(name: str) -> str:
    """Strip GCP project / AWS account identifiers from cloud resource model paths.

    Matches `trimInferenceModelName` in `k8s-operator/internal/controller/inference_status.go`
    and adds Vertex Model Garden `publishers/<pub>/models/<id>` trimming so project
    IDs, regions, and AWS account IDs never reach chat replies.
    """
    if name.startswith(_RESOURCE_PATH_PREFIX):
        segments = name.split(_PROVIDER_SEPARATOR)
        if (
            len(segments) < _RESOURCE_PATH_MIN_SEGMENTS
            or segments[_RESOURCE_PATH_LOCATIONS_INDEX] != _RESOURCE_PATH_LOCATIONS_SEGMENT
            or not segments[-1]
        ):
            return ""
        return segments[-1]

    if name.startswith(_PUBLISHER_PATH_PREFIX):
        segments = name.split(_PROVIDER_SEPARATOR)
        if (
            len(segments) < _PUBLISHER_PATH_MIN_SEGMENTS
            or segments[_PUBLISHER_PATH_MODELS_INDEX] != _PUBLISHER_PATH_MODELS_SEGMENT
            or not segments[-1]
        ):
            return ""
        return segments[-1]

    segments = name.split(_PROVIDER_SEPARATOR)
    for idx, segment in enumerate(segments):
        if not segment.startswith(_ARN_PREFIX):
            continue
        arn = _PROVIDER_SEPARATOR.join(segments[idx:])
        fields = arn.split(_ARN_FIELD_SEPARATOR, _ARN_FIELD_COUNT - 1)
        if len(fields) < _ARN_FIELD_COUNT or not fields[_ARN_FIELD_COUNT - 1]:
            return ""
        resource = fields[_ARN_FIELD_COUNT - 1]
        if _PROVIDER_SEPARATOR in resource:
            return resource.rsplit(_PROVIDER_SEPARATOR, 1)[-1]
        return resource.rsplit(_ARN_FIELD_SEPARATOR, 1)[-1]

    return name


def _split_backing(raw: str) -> Tuple[str, str]:
    """Split `<provider>/<model>`, trim cloud resource references, and drop router aliases.

    The provider key is casefolded, and the model half is compared casefolded,
    so `Model-Default` or `Custom` is caught the same way as the lowercase
    spelling. Bare Vertex AI resource paths or Bedrock ARNs without a provider
    prefix are rejected so project or account identifiers cannot leak. The model
    half keeps its own spelling, because a served model id (a Hugging Face id on
    vLLM) is case-sensitive.
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    if _PROVIDER_SEPARATOR in s:
        provider_key, model_name = s.split(_PROVIDER_SEPARATOR, 1)
    else:
        provider_key, model_name = "", s
    provider_key, model_name = provider_key.strip().casefold(), model_name.strip()

    if provider_key == _PUBLISHER_PATH_OWNER_SEGMENT:
        provider_key = ""
        model_name = _trim_model_resource_name(s)
    elif (
        provider_key == _RESOURCE_PATH_OWNER_SEGMENT
        or provider_key.startswith(_ARN_PREFIX)
        or _ARN_FIELD_SEPARATOR in provider_key
    ):
        return "", ""
    else:
        model_name = _trim_model_resource_name(model_name)

    folded = model_name.casefold()
    if not model_name or folded in _PLACEHOLDER_ALIASES or folded == _CUSTOM_PROVIDER:
        return "", ""
    return provider_key, model_name


def _is_short_version_token(token: str) -> bool:
    return token.isdigit() and len(token) <= _SHORT_VERSION_MAX_DIGITS


def _format_display_model(model_id: str) -> str:
    """Render a readable display title for common model IDs."""
    lower = model_id.lower()
    for prefix, family_word, family_title, title_tokens in _DISPLAY_FAMILIES:
        if not lower.startswith(prefix):
            continue
        tokens = model_id.split(_DISPLAY_HYPHEN)
        parts = []
        for idx, tok in enumerate(tokens):
            t_low = tok.lower()
            if t_low == family_word:
                rendered = family_title
            elif t_low in title_tokens:
                rendered = t_low.capitalize()
            else:
                rendered = tok
            if idx > 0 and _is_short_version_token(tokens[idx - 1]) and _is_short_version_token(tok):
                parts[-1] = f"{parts[-1]}{_VERSION_DOT_JOINER}{rendered}"
            else:
                parts.append(rendered)
        return _DISPLAY_SPACE_JOINER.join(parts)
    if lower.startswith(_GPT_PREFIX):
        return _GPT_TITLE_PREFIX + model_id[len(_GPT_PREFIX) :]
    return model_id


def _load_profile_endpoint() -> Tuple[str, str]:
    """Return `(model_info_url, api_key)` from the active Hermes profile configuration.

    Derives the probe URL from the profile's own `model.base_url` (written by
    `renderConfigYAML` in `k8s-operator/internal/controller/platformagent_manifests.go`),
    rather than `INFERENCE_GATEWAY_SERVICE_HOST`, so every Hermes profile reaches
    the gateway using the exact URL and API key configured for its LLM calls.
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
        for env_var in _CONFIG_DIR_ENV_VARS:
            root = os.environ.get(env_var, "").strip()
            if not root:
                continue
            cfg_path = Path(root) / _CONFIG_FILENAME
            if cfg_path.is_file():
                try:
                    loaded = yaml.safe_load(cfg_path.read_text(encoding=_UTF8_ENCODING))
                    if isinstance(loaded, dict):
                        cfg = loaded
                        break
                except Exception:
                    continue

    model_cfg = cfg.get(_CONFIG_MODEL_SECTION) if isinstance(cfg.get(_CONFIG_MODEL_SECTION), dict) else {}
    base_url = str(model_cfg.get(_CONFIG_BASE_URL_FIELD) or "").strip()
    if not base_url:
        for env_var in _BASE_URL_ENV_VARS:
            base_url = os.environ.get(env_var, "").strip()
            if base_url:
                break

    raw_key = str(model_cfg.get(_CONFIG_KEY_FIELD) or "").strip()
    if raw_key:
        api_key = _expand_env_ref(raw_key)
    else:
        api_key = ""
        for env_var in _GATEWAY_KEY_ENV_VARS:
            api_key = os.environ.get(env_var, "").strip()
            if api_key:
                break

    return _normalize_endpoint(base_url, api_key)


def _extract_entry_model(entry: Dict[str, Any]) -> str:
    """Return `<provider>/<model>` or `<model>` from one LiteLLM `/model/info` entry."""
    params = entry.get(_PAYLOAD_LITELLM_PARAMS_KEY)
    model = str((params if isinstance(params, dict) else {}).get(_PAYLOAD_MODEL_KEY) or "").strip()
    if not model:
        return ""
    model_info = entry.get(_PAYLOAD_MODEL_INFO_KEY)
    provider = str((model_info if isinstance(model_info, dict) else {}).get(_PAYLOAD_LITELLM_PROVIDER_KEY) or "").strip()
    if not provider:
        return model
    if _PROVIDER_SEPARATOR not in model:
        return f"{provider}{_PROVIDER_SEPARATOR}{model}"
    lead = model.split(_PROVIDER_SEPARATOR, 1)[0].strip().casefold()
    folded_provider = provider.casefold()
    already_prefixed = (
        lead == folded_provider
        or folded_provider.startswith(f"{lead}{_DISPLAY_HYPHEN}")
        or lead.startswith(f"{folded_provider}{_DISPLAY_HYPHEN}")
    )
    if not already_prefixed and (
        folded_provider in _PROVIDER_LABELS or folded_provider in _SELF_HOSTED_PROVIDER_KEYS
    ):
        return f"{provider}{_PROVIDER_SEPARATOR}{model}"
    return model


def _probe_gateway_model(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = _PROBE_TIMEOUT_SECS,
) -> str:
    """Resolve the backing `<provider>/<model>` from the profile's LiteLLM gateway.

    Results are cached for `_PROBE_SUCCESS_CACHE_TTL_SECS` (5 minutes) so
    provider switches in LiteLLM take effect without pod restarts. A gateway
    response that names no qualifying backing model (or an absent/invalid
    `model.base_url`) clears any cached value and waits 30s before re-probing,
    while transport or parse errors keep the last-known good model across the
    30s failure cooldown.
    """
    global _cached_gateway_model, _cached_gateway_model_expires_at, _next_gateway_probe_at, _cache_generation
    now = time.monotonic()
    gen = 0
    if base_url is None:
        with _CACHE_LOCK:
            if _cached_gateway_model and now < _cached_gateway_model_expires_at:
                return _cached_gateway_model
            if now < _next_gateway_probe_at:
                return _cached_gateway_model
            gen = _cache_generation
        info_url, resolved_key = _load_profile_endpoint()
        if api_key is None:
            api_key = resolved_key
    else:
        info_url, api_key = _normalize_endpoint(base_url, api_key or "")

    if not info_url or urllib.parse.urlparse(info_url).scheme.lower() not in _ALLOWED_SCHEMES:
        if base_url is None:
            return _clear_cache_with_cooldown(gen)
        return ""

    headers: Dict[str, str] = {_HTTP_ACCEPT_HEADER: _HTTP_ACCEPT_JSON}
    if api_key:
        headers[_HTTP_AUTH_HEADER] = f"{_BEARER_PREFIX}{api_key}"

    req = urllib.request.Request(info_url, headers=headers, method=_HTTP_GET)
    try:
        with _MODEL_INFO_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError(f"model info response from {info_url} exceeded {_MAX_RESPONSE_BYTES} bytes")
        payload = json.loads(raw.decode(_UTF8_ENCODING, errors=_DECODE_ERRORS))
        if not isinstance(payload, dict):
            logger.warning("model info from %s is not a JSON object; backing model unknown", info_url)
            if base_url is None:
                return _clear_cache_with_cooldown(gen)
            return ""
        raw_entries = payload.get(_PAYLOAD_DATA_KEY)
        entries = raw_entries if isinstance(raw_entries, (list, tuple)) else ()
        for alias in _PLACEHOLDER_ALIASES:
            for entry in entries:
                if (
                    isinstance(entry, dict)
                    and str(entry.get(_PAYLOAD_MODEL_NAME_KEY) or "").strip().casefold() == alias
                ):
                    model = _extract_entry_model(entry)
                    if _split_backing(model)[1]:
                        if base_url is None:
                            with _CACHE_LOCK:
                                if _cache_generation == gen:
                                    _cached_gateway_model = model
                                    _cached_gateway_model_expires_at = (
                                        time.monotonic() + _PROBE_SUCCESS_CACHE_TTL_SECS
                                    )
                                    _cache_generation += 1
                                return _cached_gateway_model
                        return model
        logger.warning(
            "model info from %s has no %s entry with a backing model",
            info_url,
            list(_PLACEHOLDER_ALIASES),
        )
        if base_url is None:
            return _clear_cache_with_cooldown(gen)
        return ""
    except Exception as exc:
        logger.debug("cannot read model info from %s: %s", info_url, exc)

    if base_url is None:
        with _CACHE_LOCK:
            if _cache_generation == gen:
                _next_gateway_probe_at = time.monotonic() + _PROBE_RETRY_COOLDOWN_SECS
                _cache_generation += 1
            return _cached_gateway_model
    return ""


def render_model_identity(raw_model: Optional[str] = None) -> Optional[str]:
    """Render the `[RUNTIME MODEL IDENTITY]` context block when known, or `None`.

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
        prov = f"{_PROVIDER_LABEL}{label} ({provider_key})" if label else f"{_PROVIDER_LABEL}{provider_key}"
        return (
            f"{_HEADER}\n"
            f"{_BACKING_MODEL_LABEL}{model_str}\n"
            f"{prov}\n"
            f"{_CONTEXT_WITH_PROVIDER_INSTRUCTIONS}"
        )
    return (
        f"{_HEADER}\n"
        f"{_BACKING_MODEL_LABEL}{model_str}\n"
        f"{_CONTEXT_WITHOUT_PROVIDER_INSTRUCTIONS}"
    )


def handle_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, str]]:
    """Append `[RUNTIME MODEL IDENTITY]` to turn context on every profile."""
    try:
        block = render_model_identity()
    except Exception as exc:
        logger.warning("Could not render runtime model identity: %s", exc)
        return None
    if not block:
        return None
    return {_CONTEXT_RESULT_KEY: f"\n\n{block}\n"}


def register(ctx: Any) -> None:
    ctx.register_hook(_PRE_LLM_CALL_HOOK, handle_pre_llm_call)
