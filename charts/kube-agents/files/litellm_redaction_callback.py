"""LiteLLM pre-call hook that redacts outbound request bodies at the gateway.

The chart mounts this file, `redactor.py` beside it and the rendered
`redaction.yaml` into the stock LiteLLM image next to `/app/config.yaml`, and
names `litellm_redaction_callback.proxy_handler_instance` in
`litellm_settings.callbacks`. LiteLLM resolves that string to a file in the
config's directory, so the three files have to share one; `proxy_handler_instance`
is the attribute it reads.

What runs: `AuditRedactor`'s credential patterns, then the operator's rules
from the file `KUBE_AGENTS_REDACTION_CONFIG` names, over every top-level
field of the request that is not a control: the conversation under any
shape the gateway serves (chat and Anthropic `messages`, Gemini `contents`),
the system text under any spelling, tool definitions, embeddings `input`, a
completion `prompt`, and any field newer than this file. Each field is
walked whole with the key-aware walk the audit hooks use, so a value is
redacted wherever it sits -- a tool's input, what a tool returned, a
per-message field -- and JSON held in a string stays valid JSON.

Left exactly as they came: a thinking block's text and signature (the
provider checks the text against the signature, and the model wrote it from
input this hook had already redacted); opaque blocks (redacted thinking, a
thought signature, encrypted content); and binary payloads (base64 image or
document data, Gemini inline data, input audio, a `data:` URL). Generation
settings and the gateway's own bookkeeping are not walked. A request the hook
cannot walk is refused: one that carries no conversation, input or prompt, a
`messages` or `contents` that is not a list of objects, or a payload whose
media type is text. Responses are not touched; the limitation is documented
on the site's inference-gateway page.

Two failure modes are deliberate. A rule that does not load raises at import,
which stops the gateway pod at startup rather than letting it forward
unredacted; and an exception inside the hook propagates, which fails the one
request rather than forwarding it unredacted. That is the opposite of the
audit-hook copy's fail-open stance, because here the alternative to an error
is the payload leaving the estate.

The log line per request carries the count of substitutions by rule name and
never the payload.
"""

from __future__ import annotations

import copy
import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("kube_agents.litellm_redaction")

# The rendered redaction.yaml; the chart sets this on the gateway container.
CONFIG_PATH_ENV_VAR = "KUBE_AGENTS_REDACTION_CONFIG"
# `redactor.py` is mounted beside this file. It is loaded by path under a name
# of its own rather than by putting the config directory on sys.path: that
# directory is LiteLLM's working directory in the image, and a sys.path entry
# there would change name resolution for the whole proxy process.
REDACTOR_FILE_NAME = "redactor.py"
REDACTOR_MODULE_NAME = "kube_agents_litellm_redactor"
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these the per-request count line never reaches the
# pod log. The line is the one operator-visible signal that a rule fired.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
# A request has to carry at least one of these for the hook to know it is a
# model call it can read. One that carries none is refused rather than
# forwarded. `messages` is the chat and Anthropic shape, `contents` Gemini's,
# `input` embeddings and `prompt` the legacy text completion.
MESSAGES_KEY = "messages"
CONTENTS_KEY = "contents"
WALKED_REQUEST_KEYS = (MESSAGES_KEY, CONTENTS_KEY, "input", "prompt")
# Top-level request keys that are controls, never text sent as content:
# generation settings, routing, and the gateway's own bookkeeping (its
# metadata carries the caller's key hash, read after this hook, and must not
# be touched). Every other top-level key is walked whole -- the conversation,
# the system text under any spelling, tool definitions, `instructions`,
# a field newer than this file.
CONTROL_KEYS = frozenset(
    {
        "model", "stream", "stream_options", "temperature", "top_p", "top_k", "n", "seed",
        "max_tokens", "max_completion_tokens", "max_output_tokens", "stop", "stop_sequences",
        "presence_penalty", "frequency_penalty", "logit_bias", "logprobs", "top_logprobs",
        "response_format", "parallel_tool_calls", "tool_choice", "user", "thinking",
        "reasoning_effort", "reasoning", "modalities", "service_tier", "store",
        "generationConfig", "generation_config", "safetySettings", "safety_settings",
        "toolConfig", "tool_config", "cachedContent", "cached_content", "anthropic_version",
        "metadata", "litellm_metadata", "proxy_server_request", "secret_fields",
        "mock_response", "timeout", "num_retries", "api_base", "api_version", "drop_params",
        "api_key", "organization", "headers", "extra_headers", "custom_llm_provider",
        "fallbacks", "tags", "guardrails", "mcp_servers",
    }
)
CONTROL_KEY_PREFIXES = ("litellm_", "langfuse_", "_")
# Fields left exactly as they came, because the provider checks them or
# because they are binary:
# - a thinking block's text and signature: the provider verifies the text
#   against the signature, and the model wrote that text from input this hook
#   had already redacted;
# - redacted thinking, Gemini's thought signature and encrypted blobs: opaque;
# - base64 payloads (an image or document source, Gemini inline data, input
#   audio) and `data:` URLs: no text rule applies, and a substitution would
#   corrupt them. A payload whose media type is text is refused instead,
#   since it carries text the walk cannot reach without decoding it.
TYPE_KEY = "type"
THINKING_TYPE = "thinking"
THINKING_KEPT_KEYS = frozenset({"thinking", "signature"})
REDACTED_THINKING_TYPE = "redacted_thinking"
OPAQUE_KEYS = frozenset({"thoughtSignature", "thought_signature", "encrypted_content", "encrypted_index"})
DATA_KEY = "data"
BASE64_SOURCE_TYPE = "base64"
# An Anthropic document whose source is plain text: its data is text, walked.
TEXT_SOURCE_TYPE = "text"
MEDIA_TYPE_KEYS = ("media_type", "mimeType", "mime_type")
INPUT_AUDIO_KEY = "input_audio"
DATA_URL_PATTERN = re.compile(r"^data:([^;,]*)(?:;[^,]*)?;base64,", re.IGNORECASE)
TEXT_MEDIA_PREFIXES = ("text/", "application/json", "application/xml", "application/yaml", "application/x-yaml", "application/javascript")


class UnwalkableRequestError(ValueError):
    """A request in a shape the redaction hook does not know how to walk."""


def _is_text_media(media_type: Any) -> bool:
    return isinstance(media_type, str) and media_type.strip().lower().startswith(TEXT_MEDIA_PREFIXES)


def _load_redactor():
    """Import the sibling redactor module by path, as LiteLLM imports this one."""
    path = Path(__file__).resolve().with_name(REDACTOR_FILE_NAME)
    spec = importlib.util.spec_from_file_location(REDACTOR_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the gateway redactor from {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, as the import system does: the module
    # declares a dataclass, which resolves its defining module via sys.modules.
    sys.modules[REDACTOR_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _configure_logger() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


_redactor = _load_redactor()
AuditRedactor = _redactor.AuditRedactor
RedactionRule = _redactor.RedactionRule
_configure_logger()


def load_rules(config_path: str) -> List[RedactionRule]:
    """Read the rendered config and build the rule list; raise on any defect.

    The loader is ``AuditRedactor.rules_from_file``, shared with the agent
    image's on-disk redaction, so both read the same file the same way.
    """
    return AuditRedactor.rules_from_file(config_path)


def _config_path_from_env() -> str:
    path = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
    if not path:
        raise RuntimeError(
            f"{CONFIG_PATH_ENV_VAR} is not set; the gateway redaction callback "
            f"refuses to start without its rule file"
        )
    return path


class KubeAgentsRedactionCallback(CustomLogger):
    """Redact request bodies before LiteLLM forwards them to the provider."""

    def __init__(self, rules: List[RedactionRule]) -> None:
        super().__init__()
        self._rules = list(rules)

    @property
    def rules(self) -> List[RedactionRule]:
        return list(self._rules)

    def _keep(self, value: Any, path: tuple, kept: List[tuple], where: str) -> None:
        """Record a field to leave as it is; refuse a text payload."""
        if isinstance(value, str):
            match = DATA_URL_PATTERN.match(value)
            if match and _is_text_media(match.group(1)):
                raise UnwalkableRequestError(
                    f"the gateway redaction hook refuses a request it cannot walk: a {match.group(1)} data URL at {where}"
                )
        kept.append((path, value))

    def _carve(self, node: Any, path: tuple, kept: List[tuple]) -> None:
        """Find the fields to leave as they are, anywhere in node."""
        if isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str) and DATA_URL_PATTERN.match(item):
                    self._keep(item, path + (i,), kept, "a list item")
                else:
                    self._carve(item, path + (i,), kept)
            return
        if not isinstance(node, dict):
            return
        kind = node.get(TYPE_KEY)
        media = next((node[k] for k in MEDIA_TYPE_KEYS if k in node), None)
        for key, value in node.items():
            here = path + (key,)
            if (kind == THINKING_TYPE and key in THINKING_KEPT_KEYS) or kind == REDACTED_THINKING_TYPE or key in OPAQUE_KEYS:
                kept.append((here, value))
            elif key == DATA_KEY and kind != TEXT_SOURCE_TYPE and (
                kind == BASE64_SOURCE_TYPE or media is not None or (path and path[-1] == INPUT_AUDIO_KEY)
            ):
                if _is_text_media(media):
                    raise UnwalkableRequestError(
                        f"the gateway redaction hook refuses a request it cannot walk: an inline {media} payload"
                    )
                kept.append((here, value))
            elif isinstance(value, str) and DATA_URL_PATTERN.match(value):
                self._keep(value, here, kept, key)
            else:
                self._carve(value, here, kept)

    @staticmethod
    def _put(root: Any, path: tuple, value: Any) -> None:
        """Set value at path in root; a path the walk reshaped is left as the walk left it."""
        node = root
        for step in path[:-1]:
            try:
                node = node[step]
            except (KeyError, IndexError, TypeError):
                return
        try:
            node[path[-1]] = value
        except (KeyError, IndexError, TypeError):
            return

    def _redact_field(self, value: Any, counts: Dict[str, int]) -> Any:
        """One top-level field: every string redacted, key names counted, the
        kept fields put back as they came. The original is left unchanged
        (the walk returns a copy)."""
        kept: List[tuple] = []
        self._carve(value, (), kept)
        if isinstance(value, (dict, list)):
            placeholder = copy.deepcopy(value)
            for path, _ in kept:
                self._put(placeholder, path, None)
        else:
            placeholder = value
        redacted, made = AuditRedactor.redact_counted(placeholder, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        for path, original in kept:
            self._put(redacted, path, original)
        return redacted

    @staticmethod
    def _check_shape(data: Dict[str, Any]) -> None:
        refuse = "the gateway redaction hook refuses a request it cannot walk: "
        if not any(key in data for key in WALKED_REQUEST_KEYS):
            raise UnwalkableRequestError(
                refuse + f"it carries none of {', '.join(WALKED_REQUEST_KEYS)} "
                f"(keys: {', '.join(sorted(str(k) for k in data))})"
            )
        for key in (MESSAGES_KEY, CONTENTS_KEY):
            if key not in data:
                continue
            if not isinstance(data[key], list):
                raise UnwalkableRequestError(refuse + f"{key} as {type(data[key]).__name__}")
            for item in data[key]:
                if not isinstance(item, dict):
                    raise UnwalkableRequestError(refuse + f"an item of {key} as {type(item).__name__}")

    def redact_request(self, data: Dict[str, Any]) -> Dict[str, int]:
        """Redact `data` in place and return the substitution counts by rule name.

        Every top-level field that is not a control is walked whole, so a
        field this file has never heard of is still redacted. Raises
        UnwalkableRequestError for a request it cannot walk, naming its keys
        and never their values.
        """
        self._check_shape(data)
        counts: Dict[str, int] = {}
        for key in list(data):
            if key in CONTROL_KEYS or str(key).startswith(CONTROL_KEY_PREFIXES):
                continue
            data[key] = self._redact_field(data[key], counts)
        return counts


    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        counts = self.redact_request(data)
        if counts:
            logger.info(
                "redacted %s request: %s",
                call_type,
                ", ".join(f"{name}={count}" for name, count in sorted(counts.items())),
            )
        return data


# Built at import so a bad rule file stops the pod at startup. The name is the
# one the chart's callback string ends in.
proxy_handler_instance = KubeAgentsRedactionCallback(load_rules(_config_path_from_env()))
