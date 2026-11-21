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

Binary parts (images, audio, inline file data) and opaque ones (redacted
thinking) are left as they are; any other part is walked whole. A request
that carries none of these shapes is refused: the hook cannot say what in it
is text, so it does not forward it. Responses are not touched; the
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
# Anthropic messages: the top-level system text, and the content blocks that
# carry text under a key other than `text`.
SYSTEM_KEY = "system"
TOOL_USE_TYPE = "tool_use"
TOOL_USE_INPUT_KEY = "input"
TOOL_RESULT_TYPE = "tool_result"
TOOL_RESULT_CONTENT_KEY = "content"
THINKING_TYPE = "thinking"
THINKING_KEY = "thinking"
# Gemini: the conversation, its parts, and the system instruction under
# either spelling the API takes.
CONTENTS_KEY = "contents"
PARTS_KEY = "parts"
GEMINI_SYSTEM_KEYS = ("systemInstruction", "system_instruction")
FUNCTION_CALL_KEY = "functionCall"
FUNCTION_CALL_ARGS_KEY = "args"
FUNCTION_RESPONSE_KEY = "functionResponse"
FUNCTION_RESPONSE_KEY_RESPONSE = "response"
# Parts left as they are: binary payloads, which no text rule applies to and
# which a substitution would corrupt, and opaque blocks the provider checks.
UNWALKED_PART_TYPES = frozenset(
    {"image", "image_url", "input_image", "input_audio", "audio", "document", "file", "redacted_thinking"}
)
UNWALKED_GEMINI_PART_KEYS = frozenset({"inlineData", "inline_data", "fileData", "file_data", "thoughtSignature"})
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

        A text part by its text; a tool call's input and a tool result's
        content by walking them; thinking by its text. Binary and opaque parts
        are left alone. Any other part is walked whole, key names included:
        a part type this file has never seen still has its text redacted.
        """
        kind = part.get(PART_TYPE_KEY)
        if kind in UNWALKED_PART_TYPES:
            return part
        if kind == TOOL_USE_TYPE:
            if TOOL_USE_INPUT_KEY in part:
                part[TOOL_USE_INPUT_KEY] = self._redact_arguments(part[TOOL_USE_INPUT_KEY], counts)
            return part
        if kind == TOOL_RESULT_TYPE:
            if TOOL_RESULT_CONTENT_KEY in part:
                part[TOOL_RESULT_CONTENT_KEY] = self._redact_value(part[TOOL_RESULT_CONTENT_KEY], counts)
            return part
        if kind == THINKING_TYPE:
            if isinstance(part.get(THINKING_KEY), str):
                part[THINKING_KEY] = self._redact_text(part[THINKING_KEY], counts)
            return part
        if isinstance(part.get(TEXT_KEY), str) and set(part) <= {PART_TYPE_KEY, TEXT_KEY, "cache_control", "citations"}:
            part[TEXT_KEY] = self._redact_text(part[TEXT_KEY], counts)
            return part
        return self._redact_structure(part, counts)

    def _redact_gemini_parts(self, parts: Any, counts: Dict[str, int]) -> Any:
        """Gemini parts, in place: text, a function call's args, a function
        response's response; binary parts left alone; any other part walked
        whole."""
        if not isinstance(parts, list):
            return self._redact_structure(parts, counts)
        for i, part in enumerate(parts):
            if not isinstance(part, dict):
                parts[i] = self._redact_value(part, counts)
                continue
            for key in list(part):
                value = part[key]
                if key in UNWALKED_GEMINI_PART_KEYS:
                    continue
                if key == TEXT_KEY and isinstance(value, str):
                    part[key] = self._redact_text(value, counts)
                elif key == FUNCTION_CALL_KEY and isinstance(value, dict):
                    if FUNCTION_CALL_ARGS_KEY in value:
                        value[FUNCTION_CALL_ARGS_KEY] = self._redact_arguments(value[FUNCTION_CALL_ARGS_KEY], counts)
                elif key == FUNCTION_RESPONSE_KEY and isinstance(value, dict):
                    if FUNCTION_RESPONSE_KEY_RESPONSE in value:
                        value[FUNCTION_RESPONSE_KEY_RESPONSE] = self._redact_structure(
                            value[FUNCTION_RESPONSE_KEY_RESPONSE], counts
                        )
                elif isinstance(value, (str, list, dict)):
                    part[key] = self._redact_structure(value, counts)
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
