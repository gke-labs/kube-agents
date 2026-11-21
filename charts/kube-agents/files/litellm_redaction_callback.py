"""LiteLLM pre-call hook that redacts outbound request bodies at the gateway.

The chart mounts this file, `redactor.py` beside it and the rendered
`redaction.yaml` into the stock LiteLLM image next to `/app/config.yaml`, and
names `litellm_redaction_callback.proxy_handler_instance` in
`litellm_settings.callbacks`. LiteLLM resolves that string to a file in the
config's directory, so the three files have to share one; `proxy_handler_instance`
is the attribute it reads.

What runs: `AuditRedactor`'s credential patterns, then the operator's rules
from the file `KUBE_AGENTS_REDACTION_CONFIG` names, over every request shape
the gateway serves:

- chat completions: `messages[].content`, as a string or as content parts,
  and each assistant message's `tool_calls[].function.arguments` (parsed and
  walked with its key names);
- Anthropic messages: the same `messages`, where content blocks include a
  tool's input (`tool_use.input`), what a tool returned (`tool_result`) and
  the model's thinking, plus the top-level `system` text;
- Gemini (`/v1beta/models/...`): `contents[].parts[]` (text, a function
  call's `args`, a function response's `response`) and `systemInstruction`;
- the `input` of an embeddings request and the `prompt` of a text completion.

Only a part's binary payload (base64 image or document data, an inline
`data:` URL, Gemini inline data) and opaque blocks (redacted thinking, a
thought signature) are left as they are; the rest of every part is walked
whole. Every top-level key that is not a known control (tool definitions,
`instructions`, anything newer) is walked for text too. A request that
carries none of these shapes, a `messages` or `contents` that is not a list,
or Gemini inline data of a text type is refused: the hook cannot say what in
it is text, so it does not forward it. Responses are not touched; the
limitation is documented on the site's inference-gateway page.

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

import importlib.util
import json
import logging
import os
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
# Request fields the redactor walks. `messages` is the chat shape the agents
# send; `input` is embeddings; `prompt` is the legacy text-completion shape.
MESSAGES_KEY = "messages"
CONTENT_KEY = "content"
TEXT_PART_TYPE = "text"
TEXT_KEY = "text"
PART_TYPE_KEY = "type"
SCALAR_INPUT_KEYS = ("input", "prompt")
# An assistant message's tool calls. `arguments` is a JSON document held in a
# string, and the model builds it from what it read, so a value it copied out
# of a tool result goes back up here on every later turn.
TOOL_CALLS_KEY = "tool_calls"
FUNCTION_KEY = "function"
ARGUMENTS_KEY = "arguments"
# Anthropic messages: the top-level system text, and the thinking block,
# whose signature the provider checks and the walk must not touch.
SYSTEM_KEY = "system"
THINKING_TYPE = "thinking"
THINKING_KEY = "thinking"
# Gemini: the conversation, its parts, and the system instruction under
# either spelling the API takes. Within a part, binary data and the
# thought signature are left alone; everything else is walked.
CONTENTS_KEY = "contents"
PARTS_KEY = "parts"
GEMINI_SYSTEM_KEYS = ("systemInstruction", "system_instruction")
# Opaque blocks the provider checks, left as they are.
OPAQUE_PART_TYPES = frozenset({"redacted_thinking"})
# Base64 payloads inside a part: no text rule applies to them, and a
# substitution would corrupt them. Only the payload is skipped; the rest of
# the part (a title, a context, a URL, a text source) is walked.
SOURCE_KEY = "source"
SOURCE_TYPE_KEY = "type"
BASE64_SOURCE_TYPE = "base64"
DATA_KEY = "data"
IMAGE_URL_KEY = "image_url"
URL_KEY = "url"
DATA_URL_PREFIX = "data:"
# Gemini binary parts, and the thought signature the provider checks.
GEMINI_INLINE_KEYS = ("inlineData", "inline_data")
GEMINI_MIME_KEYS = ("mimeType", "mime_type")
GEMINI_OPAQUE_PART_KEYS = frozenset({"thoughtSignature", "thought_signature"})
# Inline data whose MIME type is text is refused: it would carry text the
# walk cannot reach without decoding it, and refusing is the safe answer.
TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/yaml", "application/x-yaml")
# Top-level request keys that are controls, never text: generation settings,
# routing, and LiteLLM's own bookkeeping (its metadata carries the caller's
# key hash, which the gateway reads after this hook, so it must not be
# touched). Every other top-level key -- tool definitions, `instructions`,
# a legacy `function_call`, a field newer than this file -- is walked for
# text, so text the hook was not told about is still redacted.
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
        "fallbacks", "tags", "guardrails",
    }
)
CONTROL_KEY_PREFIXES = ("litellm_", "_")
# A request has to carry at least one of these for the hook to know where its
# text is. One that carries none is refused rather than forwarded.
WALKED_REQUEST_KEYS = (MESSAGES_KEY, CONTENTS_KEY) + SCALAR_INPUT_KEYS


class UnwalkableRequestError(ValueError):
    """A request in a shape the redaction hook does not know how to walk."""


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

    def _redact_text(self, text: str, counts: Dict[str, int]) -> str:
        redacted, made = AuditRedactor.redact_text_counted(text, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        return redacted

    def _redact_value(self, value: Any, counts: Dict[str, int]) -> Any:
        """Strings in place; lists element-wise; content parts by what they carry."""
        if isinstance(value, str):
            return self._redact_text(value, counts)
        if isinstance(value, list):
            return [self._redact_value(item, counts) for item in value]
        if isinstance(value, dict):
            return self._redact_part(value, counts)
        return value

    def _redact_part(self, part: Dict[str, Any], counts: Dict[str, int]) -> Dict[str, Any]:
        """One content part or block, in place.

        Binary and opaque parts are left alone. A thinking block has its text
        redacted and its signature left as the provider issued it. Every other
        part -- text, a tool's input, what a tool returned, a part type this
        file has never seen -- is walked whole, with the key-aware walk the
        audit hooks use, so a value is redacted wherever in the part it sits
        and JSON held in a string stays valid JSON.
        """
        kind = part.get(PART_TYPE_KEY)
        if kind in OPAQUE_PART_TYPES:
            return part
        source = part.get(SOURCE_KEY)
        if isinstance(source, dict) and source.get(SOURCE_TYPE_KEY) == BASE64_SOURCE_TYPE and DATA_KEY in source:
            # An image or document sent as base64: walk all but the payload.
            payload = source.pop(DATA_KEY)
            try:
                redacted = self._redact_structure(part, counts)
            finally:
                source[DATA_KEY] = payload
            # The walk returns a copy: the payload goes back on it as well.
            redacted[SOURCE_KEY][DATA_KEY] = payload
            return redacted
        image_url = part.get(IMAGE_URL_KEY)
        url = image_url.get(URL_KEY) if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url.startswith(DATA_URL_PREFIX):
            # An image inlined as a data: URL is a payload, not text.
            return part
        if kind == THINKING_TYPE:
            if isinstance(part.get(THINKING_KEY), str):
                part[THINKING_KEY] = self._redact_text(part[THINKING_KEY], counts)
            return part
        return self._redact_structure(part, counts)

    def _redact_gemini_parts(self, parts: Any, counts: Dict[str, int]) -> Any:
        """Gemini parts, in place: binary and opaque fields left alone, every
        other field (text, a function call, a function response, anything
        newer) walked whole."""
        if not isinstance(parts, list):
            raise UnwalkableRequestError(
                f"the gateway redaction hook refuses a request it cannot walk: Gemini parts as {type(parts).__name__}"
            )
        for i, part in enumerate(parts):
            if not isinstance(part, dict):
                parts[i] = self._redact_value(part, counts)
                continue
            for key in list(part):
                if key in GEMINI_OPAQUE_PART_KEYS:
                    continue
                if key in GEMINI_INLINE_KEYS and isinstance(part[key], dict):
                    inline = part[key]
                    mime = next((inline[k] for k in GEMINI_MIME_KEYS if isinstance(inline.get(k), str)), "")
                    if mime.lower().startswith(TEXT_MIME_PREFIXES):
                        raise UnwalkableRequestError(
                            f"the gateway redaction hook refuses a request it cannot walk: inline data of type {mime}"
                        )
                    # A binary payload: walk all but the data.
                    payload = inline.pop(DATA_KEY, None)
                    try:
                        part[key] = self._redact_structure(inline, counts)
                    finally:
                        if payload is not None:
                            part[key][DATA_KEY] = payload
                    continue
                part[key] = self._redact_structure(part[key], counts)
        return parts

    def _redact_gemini_content(self, content: Any, counts: Dict[str, int]) -> Any:
        """A Gemini content (`{role, parts}`), or a bare string or parts list."""
        if isinstance(content, str):
            return self._redact_text(content, counts)
        if isinstance(content, list):
            return self._redact_gemini_parts(content, counts)
        if isinstance(content, dict) and PARTS_KEY in content:
            content[PARTS_KEY] = self._redact_gemini_parts(content[PARTS_KEY], counts)
            return content
        return self._redact_structure(content, counts)

    def _redact_strings(self, value: Any, counts: Dict[str, int]) -> Any:
        """Every string in value, by the text rules only: no key-name
        replacement, so a schema's `password` property stays a schema."""
        if isinstance(value, str):
            return self._redact_text(value, counts)
        if isinstance(value, list):
            return [self._redact_strings(item, counts) for item in value]
        if isinstance(value, dict):
            for key in list(value):
                value[key] = self._redact_strings(value[key], counts)
            return value
        return value

    def _redact_structure(self, value: Any, counts: Dict[str, int]) -> Any:
        """A parsed document, with the key-aware walk the audit hooks use."""
        redacted, made = AuditRedactor.redact_counted(value, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        return redacted

    def _redact_arguments(self, arguments: Any, counts: Dict[str, int]) -> Any:
        """Redact a tool call's arguments, re-serialising only when something changed.

        The parsed document is walked rather than the raw string: key names
        count (`{"password": …}`, an env `name`/`value` pair, a Secret's
        `data`), the result is still valid JSON -- a provider rejects
        tool-call arguments that do not parse -- and the line-based patterns
        see real newlines rather than `\\n` escapes. Arguments that are not
        JSON are redacted as text; arguments already sent as an object are
        walked as they are.
        """
        if not isinstance(arguments, str):
            return self._redact_structure(arguments, counts)
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return self._redact_text(arguments, counts)
        redacted = self._redact_structure(parsed, counts)
        # Compared, not counted: a substitution that swaps one marker for
        # another nets zero on the counts but still changed the payload.
        if redacted == parsed:
            return arguments
        return json.dumps(redacted, ensure_ascii=False)

    def _redact_tool_calls(self, tool_calls: Any, counts: Dict[str, int]) -> None:
        if not isinstance(tool_calls, list):
            return
        for call in tool_calls:
            function = call.get(FUNCTION_KEY) if isinstance(call, dict) else None
            if isinstance(function, dict) and function.get(ARGUMENTS_KEY) is not None:
                function[ARGUMENTS_KEY] = self._redact_arguments(function[ARGUMENTS_KEY], counts)

    def redact_request(self, data: Dict[str, Any]) -> Dict[str, int]:
        """Redact `data` in place and return the substitution counts by rule name.

        Raises UnwalkableRequestError for a request that carries none of the
        shapes above, naming its top-level keys and never its values.
        """
        if not any(key in data for key in WALKED_REQUEST_KEYS):
            raise UnwalkableRequestError(
                "the gateway redaction hook refuses a request it cannot walk: it carries none of "
                f"{', '.join(WALKED_REQUEST_KEYS)} (keys: {', '.join(sorted(str(k) for k in data))})"
            )
        for key in (MESSAGES_KEY, CONTENTS_KEY):
            if key in data and not isinstance(data[key], list):
                raise UnwalkableRequestError(
                    f"the gateway redaction hook refuses a request it cannot walk: {key} as {type(data[key]).__name__}"
                )
        counts: Dict[str, int] = {}
        if SYSTEM_KEY in data:
            data[SYSTEM_KEY] = self._redact_value(data[SYSTEM_KEY], counts)
        for key in GEMINI_SYSTEM_KEYS:
            if key in data:
                data[key] = self._redact_gemini_content(data[key], counts)
        contents = data.get(CONTENTS_KEY)
        if isinstance(contents, list):
            for i, content in enumerate(contents):
                contents[i] = self._redact_gemini_content(content, counts)
        elif contents is not None:
            data[CONTENTS_KEY] = self._redact_gemini_content(contents, counts)
        messages = data.get(MESSAGES_KEY)
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if CONTENT_KEY in message:
                    message[CONTENT_KEY] = self._redact_value(message[CONTENT_KEY], counts)
                self._redact_tool_calls(message.get(TOOL_CALLS_KEY), counts)
        for key in SCALAR_INPUT_KEYS:
            if key in data:
                data[key] = self._redact_value(data[key], counts)
        handled = {MESSAGES_KEY, CONTENTS_KEY, SYSTEM_KEY, *GEMINI_SYSTEM_KEYS, *SCALAR_INPUT_KEYS}
        for key in list(data):
            if key in handled or key in CONTROL_KEYS or str(key).startswith(CONTROL_KEY_PREFIXES):
                continue
            data[key] = self._redact_strings(data[key], counts)
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
