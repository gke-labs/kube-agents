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
per-message field. A tool call's arguments held as JSON in a string (and an
Anthropic tool input sent as a string) are decoded, walked as a document and
re-encoded only if a rule changed them, so they stay valid JSON. A tool's
output that is a whole JSON object or array (a chat tool message's string
content, an Anthropic tool result's string or text parts, a Responses function
call output, a top-level string in a Gemini function response's response) is
decoded the same way, up to 256 KiB and 64 levels deep, and so is whole JSON
inside it, two levels in at most. It still gets the text rules over its string
too, and a string the structure rules mask whole where it sits is not opened,
so decoding only adds to what is masked. One with a repeated key, past the
bounds, or not whole JSON is matched as text. A changed one keeps its indent
width and compact or spaced separators; numbers, escapes and other spacing are
normalised.

Left exactly as they came, and only at the positions the provider APIs
define (content parts, including a Responses `input` item's and a Responses
function call output's, a tool result's own content list, `system`,
`thinking_blocks`, Gemini parts and the parts inside a Gemini function
response; beyond those parts, never inside a tool's input or output or a
function's arguments): in an assistant's or the model's own turn, a thinking
block's text and signature (the provider checks the text against the
signature, and the model wrote it from input this hook had already redacted),
redacted thinking's data and a thought signature; provider-encrypted
content the provider checks byte for byte (a Responses reasoning item's
`encrypted_content`, and in the assistant's turn a web search result's
`encrypted_content`, `reasoning_details` data and signature, and the
`thought_signature` LiteLLM carries on the message and its tool calls); and,
anywhere a part may
carry one, a binary payload of a listed binary media type (images, PDF,
audio, video) that decodes as base64 and starts the way that media does; a
payload of any other type is refused. Generation
settings and the gateway's own bookkeeping are not walked. A request the hook
cannot walk is refused: one that carries no conversation, input or prompt, a
`messages` or `contents` that is not a list of objects, or anything at a
payload position that is not binary base64, answered with 400 since the same
request is refused every time. Model responses are not touched.

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

import base64
import binascii
import copy
import importlib.util
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
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
# Fields left exactly as they came, and only at the positions the provider
# APIs define them -- a content part directly under `messages[].content`, a
# tool result's own content list, `system`, an assistant's `thinking_blocks`,
# Gemini `parts` -- never inside a tool's input, a tool's output or a
# function's arguments, where any shape can appear:
# - a thinking block's text and signature: the provider verifies the text
#   against the signature, and the model wrote that text from input this hook
#   had already redacted;
# - redacted thinking and Gemini's thought signature: opaque;
# - a binary payload (base64 image, document, audio or video, as a source, a
#   `data:` URL or Gemini inline data): no text rule applies, and a
#   substitution would corrupt it. It is kept only if its media type is a
#   known binary one and its data is base64 (standard or URL-safe, padding
#   optional) that starts the way that type does; anything else in that
#   position is refused, since it may carry text the walk cannot read.
TYPE_KEY = "type"
THINKING_TYPE = "thinking"
THINKING_KEPT_KEYS = ("thinking", "signature")
REDACTED_THINKING_TYPE = "redacted_thinking"
TOOL_RESULT_TYPE = "tool_result"
GEMINI_OPAQUE_PART_KEYS = ("thoughtSignature", "thought_signature")
# Provider-opaque fields a provider checks byte for byte, kept as they came at
# the positions their APIs define, and only as strings: a Responses reasoning
# item's encrypted content, an Anthropic web search result's encrypted
# content, an assistant's reasoning details, and the thought signature LiteLLM
# carries in the chat shape. A text rule that matched inside one would make
# the provider reject the item on every later turn.
ENCRYPTED_CONTENT_KEY = "encrypted_content"
REASONING_ITEM_TYPE = "reasoning"
WEB_SEARCH_RESULT_BLOCK_TYPE = "web_search_tool_result"
REASONING_DETAILS_KEY = "reasoning_details"
REASONING_DETAIL_KEPT_KEYS = ("data", "signature")
PROVIDER_FIELDS_KEY = "provider_specific_fields"
PROVIDER_THOUGHT_SIGNATURE_KEY = "thought_signature"
TOOL_CALLS_KEY = "tool_calls"
RESPONSES_INPUT_KEY = "input"
# A Responses input part whose base64 payload sits at the part's own file_data.
RESPONSES_INPUT_FILE_TYPE = "input_file"
RESPONSES_FUNCTION_CALL_OUTPUT_TYPE = "function_call_output"
RESPONSES_OUTPUT_KEY = "output"
# A media type the refusal message may name: anything else is reported in
# fixed words, since the value comes from the request.
NAMEABLE_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
GEMINI_INLINE_KEYS = ("inlineData", "inline_data")
GEMINI_MIME_KEYS = ("mimeType", "mime_type")
GEMINI_SYSTEM_KEYS = ("systemInstruction", "system_instruction")
# A function response may carry parts of its own (an image a tool returned).
GEMINI_FUNCTION_RESPONSE_KEYS = ("functionResponse", "function_response")
GEMINI_RESPONSE_KEY = "response"
# Tool output that is a whole JSON object or array is walked as the document
# it encodes, within these bounds; past either it is matched as text.
TOOL_OUTPUT_MAX_BYTES = 256 * 1024
TOOL_OUTPUT_MAX_DEPTH = 64
# A string inside a decoded tool output that is itself whole JSON (a shell
# result's stdout, say) is decoded too: this many documents deep in all, the
# tool output itself and two levels inside it.
TOOL_OUTPUT_MAX_NESTING = 3
# How many nested strings one tool output may have decoded; past that the
# rest stay strings inside it.
TOOL_OUTPUT_MAX_NESTED = 32
# Stands in for a string while asking whether the structure rules would mask
# it whole where it sits (a credential key, a Secret's data, an env value).
MASK_PROBE = "kube-agents-redaction-probe"
TOOL_ROLE = "tool"
TEXT_TYPE = "text"
TEXT_KEY = "text"
# The binary media types a payload may claim, each with how its bytes start:
# a payload is kept only if its bytes start that way, so text labelled as one
# of these is refused rather than kept unread. A type not listed here is
# refused at a payload position.
_FTYP = (lambda d: d[4:8] == b"ftyp")
_RIFF = (lambda form: (lambda d: d[:4] == b"RIFF" and d[8:12] == form))
_EBML = (lambda d: d[:4] == b"\x1aE\xdf\xa3")
_MPEG_AUDIO = (lambda d: d[:3] == b"ID3" or (len(d) > 1 and d[0] == 0xFF and d[1] & 0xE0 == 0xE0))
MEDIA_SIGNATURES = {
    "image/png": lambda d: d[:8] == b"\x89PNG\r\n\x1a\n",
    "image/jpeg": lambda d: d[:3] == b"\xff\xd8\xff",
    # A common mislabel of JPEG, checked as JPEG. Gemini takes the label; a
    # provider that doesn't still refuses the request itself.
    "image/jpg": lambda d: d[:3] == b"\xff\xd8\xff",
    "image/gif": lambda d: d[:6] in (b"GIF87a", b"GIF89a"),
    "image/webp": _RIFF(b"WEBP"),
    "image/heic": _FTYP,
    "image/heif": _FTYP,
    "application/pdf": lambda d: d[:5] == b"%PDF-",
    "audio/wav": _RIFF(b"WAVE"),
    "audio/x-wav": _RIFF(b"WAVE"),
    "audio/mpeg": _MPEG_AUDIO,
    "audio/mp3": _MPEG_AUDIO,
    "audio/aac": _MPEG_AUDIO,
    "audio/ogg": lambda d: d[:4] == b"OggS",
    "audio/flac": lambda d: d[:4] == b"fLaC",
    "audio/mp4": _FTYP,
    "audio/webm": _EBML,
    "video/mp4": _FTYP,
    "video/quicktime": _FTYP,
    "video/webm": _EBML,
    "video/x-matroska": _EBML,
}
# A data URL with no parameter but `;base64`: a parameter is text the check
# would otherwise pass along unread.
STRICT_DATA_URL = re.compile(r"^data:([^;,]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)
ASSISTANT_ROLES = frozenset({"assistant", "model"})
# Field names the carve reads, as the provider APIs spell them.
SOURCE_KEY = "source"
MEDIA_TYPE_KEY = "media_type"
BASE64_SOURCE_TYPE = "base64"
IMAGE_URL_KEY = "image_url"
URL_KEY = "url"
DATA_URL_PREFIX = "data:"
INPUT_AUDIO_KEY = "input_audio"
AUDIO_FORMAT_KEY = "format"
AUDIO_MEDIA_PREFIX = "audio/"
FILE_KEY = "file"
FILE_DATA_KEY = "file_data"
CONTENT_KEY = "content"
ROLE_KEY = "role"
THINKING_BLOCKS_KEY = "thinking_blocks"
SYSTEM_KEY = "system"
PARTS_KEY = "parts"
# Tool calls whose arguments are JSON held in a string: a chat tool call's
# `function.arguments`, a legacy `function_call.arguments`, and an Anthropic
# `tool_use.input` sent as a string.
FUNCTION_KEY = "function"
FUNCTION_CALL_KEY = "function_call"
ARGUMENTS_KEY = "arguments"
TOOL_USE_TYPE = "tool_use"
TOOL_USE_INPUT_KEY = "input"
DATA_KEY = "data"


def _is_data_url(value: Any) -> bool:
    """A `data:` URL, as providers read the scheme: case-insensitive, after
    surrounding whitespace."""
    return isinstance(value, str) and value.strip()[: len(DATA_URL_PREFIX)].lower() == DATA_URL_PREFIX


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

    @staticmethod
    def _refuse(what: str) -> UnwalkableRequestError:
        return UnwalkableRequestError(f"the gateway redaction hook refuses a request it cannot walk: {what}")

    def _keep_payload(self, media: Any, data: Any, path: tuple, kept: List[tuple]) -> None:
        """Keep a base64 payload of a known binary media type that decodes as
        that media; refuse anything else."""
        media = media.strip().lower() if isinstance(media, str) else media
        starts = MEDIA_SIGNATURES.get(media) if isinstance(media, str) else None
        if starts is None:
            if isinstance(media, str) and NAMEABLE_MEDIA_TYPE.match(media):
                raise self._refuse(f"a payload of media type {media!r}")
            raise self._refuse("a payload whose media type is not a media type name")
        if not isinstance(data, str):
            raise self._refuse(f"a {media} payload that is not a string")
        # Providers take the URL-safe alphabet and unpadded base64 too; both
        # are normalised before the strict decode. The payload is kept as it
        # came either way.
        # Whitespace only as an encoder wraps lines: a line break, with
        # every line but the last the same length. Anything else (a space,
        # a tab, uneven lines) is not a payload as encoded.
        lines = data.strip("\r\n").replace("\r\n", "\n").split("\n")
        if any(not line or any(c.isspace() for c in line) for line in lines) or (
            len(lines) > 1 and (len({len(line) for line in lines[:-1]}) != 1 or len(lines[-1]) > len(lines[0]))
        ):
            raise self._refuse(f"a {media} payload that is not base64")
        text = "".join(lines).replace("-", "+").replace("_", "/")
        text += "=" * (-len(text) % 4)
        try:
            decoded = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise self._refuse(f"a {media} payload that is not base64") from None
        if not starts(decoded):
            raise self._refuse(f"a payload that does not start as {media}")
        kept.append((path, data))

    def _keep_data_url(self, url: str, path: tuple, kept: List[tuple]) -> None:
        match = STRICT_DATA_URL.match(url.strip())
        if match is None:
            raise self._refuse("a data URL that is not base64")
        self._keep_payload(match.group(1), match.group(2), path, kept)
        # The whole URL is what goes back.
        kept[-1] = (path, url)

    def _carve_part(self, part: Dict[str, Any], path: tuple, kept: List[tuple], nested: bool, assistant: bool) -> None:
        """A chat or Anthropic content part, at a position the API defines.
        Thinking is exempt only in an assistant's own turn, never in a user's
        or a tool's, and never inside a tool result."""
        kind = part.get(TYPE_KEY)
        if kind == THINKING_TYPE and assistant and not nested:
            for key in THINKING_KEPT_KEYS:
                if isinstance(part.get(key), str):
                    kept.append((path + (key,), part[key]))
            return
        if kind == REDACTED_THINKING_TYPE and assistant and not nested:
            if isinstance(part.get(DATA_KEY), str):
                kept.append((path + (DATA_KEY,), part[DATA_KEY]))
            return
        if kind == WEB_SEARCH_RESULT_BLOCK_TYPE and assistant and not nested and isinstance(part.get(CONTENT_KEY), list):
            for j, result in enumerate(part[CONTENT_KEY]):
                if isinstance(result, dict) and isinstance(result.get(ENCRYPTED_CONTENT_KEY), str):
                    kept.append((path + (CONTENT_KEY, j, ENCRYPTED_CONTENT_KEY), result[ENCRYPTED_CONTENT_KEY]))
        source = part.get(SOURCE_KEY)
        if isinstance(source, dict) and source.get(TYPE_KEY) == BASE64_SOURCE_TYPE:
            self._keep_payload(source.get(MEDIA_TYPE_KEY), source.get(DATA_KEY), path + (SOURCE_KEY, DATA_KEY), kept)
        image_url = part.get(IMAGE_URL_KEY)
        if isinstance(image_url, dict) and _is_data_url(image_url.get(URL_KEY)):
            self._keep_data_url(image_url[URL_KEY], path + (IMAGE_URL_KEY, URL_KEY), kept)
        elif _is_data_url(image_url):
            self._keep_data_url(image_url, path + (IMAGE_URL_KEY,), kept)
        audio = part.get(INPUT_AUDIO_KEY)
        if isinstance(audio, dict) and DATA_KEY in audio:
            self._keep_payload(f"{AUDIO_MEDIA_PREFIX}{audio.get(AUDIO_FORMAT_KEY, '')}", audio.get(DATA_KEY), path + (INPUT_AUDIO_KEY, DATA_KEY), kept)
        filed = part.get(FILE_KEY)
        if isinstance(filed, dict) and isinstance(filed.get(FILE_DATA_KEY), str):
            self._keep_data_url(filed[FILE_DATA_KEY], path + (FILE_KEY, FILE_DATA_KEY), kept)
        # A Responses input_file holds its data URL on the part itself;
        # input_image's image_url string is the data-URL case above.
        if kind == RESPONSES_INPUT_FILE_TYPE and isinstance(part.get(FILE_DATA_KEY), str):
            self._keep_data_url(part[FILE_DATA_KEY], path + (FILE_DATA_KEY,), kept)
        # A tool result's own content list holds parts too (an image a tool
        # returned); one level only, since deeper is the tool's own output.
        if kind == TOOL_RESULT_TYPE and not nested and isinstance(part.get(CONTENT_KEY), list):
            self._carve_parts(part[CONTENT_KEY], path + (CONTENT_KEY,), kept, nested=True, assistant=False)

    @staticmethod
    def _carve_assistant_opaque(message: Dict[str, Any], path: tuple, kept: List[tuple]) -> None:
        """An assistant message's reasoning details and the thought signature
        LiteLLM carries on the message and on each tool call."""
        details = message.get(REASONING_DETAILS_KEY)
        for j, detail in enumerate(details if isinstance(details, list) else []):
            if isinstance(detail, dict):
                for key in REASONING_DETAIL_KEPT_KEYS:
                    if isinstance(detail.get(key), str):
                        kept.append((path + (REASONING_DETAILS_KEY, j, key), detail[key]))
        holders = [(path, message)]
        calls = message.get(TOOL_CALLS_KEY)
        holders += [(path + (TOOL_CALLS_KEY, k), call) for k, call in enumerate(calls if isinstance(calls, list) else []) if isinstance(call, dict)]
        for where, holder in holders:
            fields = holder.get(PROVIDER_FIELDS_KEY)
            if isinstance(fields, dict) and isinstance(fields.get(PROVIDER_THOUGHT_SIGNATURE_KEY), str):
                kept.append((where + (PROVIDER_FIELDS_KEY, PROVIDER_THOUGHT_SIGNATURE_KEY), fields[PROVIDER_THOUGHT_SIGNATURE_KEY]))

    def _carve_parts(self, parts: List[Any], path: tuple, kept: List[tuple], nested: bool = False, assistant: bool = False) -> None:
        for i, part in enumerate(parts):
            if isinstance(part, dict):
                self._carve_part(part, path + (i,), kept, nested, assistant)

    def _carve_gemini_parts(self, parts: Any, path: tuple, kept: List[tuple], model: bool) -> None:
        if not isinstance(parts, list):
            return
        for i, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            # A thought signature is exempt only in the model's own turn.
            for key in GEMINI_OPAQUE_PART_KEYS:
                if model and isinstance(part.get(key), str):
                    kept.append((path + (i, key), part[key]))
            self._carve_gemini_inline(part, path + (i,), kept)
            # A function response's own parts: one level, inline payloads only
            # (a thought signature is never the tool's).
            for key in GEMINI_FUNCTION_RESPONSE_KEYS:
                response = part.get(key)
                inner = response.get(PARTS_KEY) if isinstance(response, dict) else None
                for j, sub in enumerate(inner if isinstance(inner, list) else []):
                    if isinstance(sub, dict):
                        self._carve_gemini_inline(sub, path + (i, key, PARTS_KEY, j), kept)

    def _carve_gemini_inline(self, part: Dict[str, Any], path: tuple, kept: List[tuple]) -> None:
        for key in GEMINI_INLINE_KEYS:
            inline = part.get(key)
            if isinstance(inline, dict):
                mime = next((inline[k] for k in GEMINI_MIME_KEYS if k in inline), None)
                self._keep_payload(mime, inline.get(DATA_KEY), path + (key, DATA_KEY), kept)

    def _carve(self, data: Dict[str, Any]) -> List[tuple]:
        """The fields to leave as they are, as (path from the request, value)."""
        kept: List[tuple] = []
        for i, message in enumerate(data.get(MESSAGES_KEY) or []):
            assistant = message.get(ROLE_KEY) in ASSISTANT_ROLES
            if isinstance(message.get(CONTENT_KEY), list):
                self._carve_parts(message[CONTENT_KEY], (MESSAGES_KEY, i, CONTENT_KEY), kept, assistant=assistant)
            if assistant and isinstance(message.get(THINKING_BLOCKS_KEY), list):
                self._carve_parts(message[THINKING_BLOCKS_KEY], (MESSAGES_KEY, i, THINKING_BLOCKS_KEY), kept, assistant=True)
            if assistant:
                self._carve_assistant_opaque(message, (MESSAGES_KEY, i), kept)
        for i, item in enumerate(data.get(RESPONSES_INPUT_KEY) if isinstance(data.get(RESPONSES_INPUT_KEY), list) else []):
            if isinstance(item, dict) and item.get(TYPE_KEY) == REASONING_ITEM_TYPE and isinstance(item.get(ENCRYPTED_CONTENT_KEY), str):
                kept.append(((RESPONSES_INPUT_KEY, i, ENCRYPTED_CONTENT_KEY), item[ENCRYPTED_CONTENT_KEY]))
            # An input item's content parts (input_image, input_file): carved as
            # chat parts are, never as an assistant's (no thinking exemption).
            if isinstance(item, dict) and isinstance(item.get(CONTENT_KEY), list):
                self._carve_parts(item[CONTENT_KEY], (RESPONSES_INPUT_KEY, i, CONTENT_KEY), kept)
            # A function_call_output's output may be a list of the same parts.
            if isinstance(item, dict) and item.get(TYPE_KEY) == RESPONSES_FUNCTION_CALL_OUTPUT_TYPE and isinstance(item.get(RESPONSES_OUTPUT_KEY), list):
                self._carve_parts(item[RESPONSES_OUTPUT_KEY], (RESPONSES_INPUT_KEY, i, RESPONSES_OUTPUT_KEY), kept)
        if isinstance(data.get(SYSTEM_KEY), list):
            self._carve_parts(data[SYSTEM_KEY], (SYSTEM_KEY,), kept)
        for i, content in enumerate(data.get(CONTENTS_KEY) or []):
            model = content.get(ROLE_KEY) in ASSISTANT_ROLES
            self._carve_gemini_parts(content.get(PARTS_KEY), (CONTENTS_KEY, i, PARTS_KEY), kept, model)
        for key in GEMINI_SYSTEM_KEYS:
            if isinstance(data.get(key), dict):
                self._carve_gemini_parts(data[key].get(PARTS_KEY), (key, PARTS_KEY), kept, False)
        return kept

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

    @staticmethod
    def _get(root: Any, path: tuple) -> Any:
        node = root
        for step in path:
            node = node[step]
        return node

    def _redact_field(self, value: Any, kept: List[tuple], arguments: List[tuple], counts: Dict[str, int]) -> Any:
        """One top-level field: every string redacted, key names counted, the
        kept fields put back as they came. Tool-call arguments and tool output
        held as JSON in a string are walked as the document they encode and
        re-encoded only if a rule changed it. The original is left unchanged (the walk returns a
        copy)."""
        if (kept or arguments) and isinstance(value, (dict, list)):
            placeholder = copy.deepcopy(value)
            for path, _ in kept:
                self._put(placeholder, path, None)
            for path, _, document, _ in arguments:
                self._put(placeholder, path, copy.deepcopy(document))
        else:
            placeholder = value
        redacted, made = AuditRedactor.redact_counted(placeholder, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        # Innermost first: a document decoded inside another goes back to its
        # string before the outer one is compared, so an unchanged inner one
        # leaves the outer one unchanged too.
        paths = [path for path, _, _, _ in arguments]
        for path, source, document, style in reversed(arguments):
            walked = self._get(redacted, path)
            # Compared, not counted: a substitution that swaps one marker for
            # another nets zero on the counts but still changed the payload.
            keep = source is not None and walked == document
            text = source if keep else self._encode(walked, style)
            # A tool output also gets the text rules over its string, as it
            # did before it was decoded, so decoding only ever adds to what
            # is masked (a number under a credential key, say, which the
            # structure walk leaves alone).
            if style is not None and not any(other != path and path[: len(other)] == other for other in paths):
                text, made = AuditRedactor.redact_counted(text, self._rules)
                for name, count in made.items():
                    counts[name] = counts.get(name, 0) + count
            self._put(redacted, path, text)
        for path, original in kept:
            self._put(redacted, path, original)
        return redacted

    @staticmethod
    def _tool_arguments(data: Dict[str, Any]) -> List[tuple]:
        """(path from the request, string, decoded document, None) for each
        tool call's arguments held as JSON in a string: a chat tool call's and a
        legacy function call's `arguments`, and an Anthropic tool input sent
        as a string. Arguments that are not a JSON object, array or string
        stay a string, walked as text."""
        found: List[tuple] = []

        def take(path: tuple, value: Any) -> None:
            if not isinstance(value, str):
                return
            repeated = False

            def pairs(items: List[tuple]) -> Dict[str, Any]:
                nonlocal repeated
                keys = [key for key, _ in items]
                repeated = repeated or len(keys) != len(set(keys))
                return dict(items)

            try:
                document = json.loads(value, object_pairs_hook=pairs)
            except ValueError:
                return
            # A JSON string is walked decoded too, so a rule that needs a word
            # boundary sees a value after an escaped line break or tab.
            if isinstance(document, (dict, list, str)):
                # A repeated key: the decoder kept one value and the walk never
                # sees the other, so the string is re-encoded from the document
                # rather than sent as it came.
                found.append((path, None if repeated else value, document, None))

        for i, message in enumerate(data.get(MESSAGES_KEY) or []):
            for j, call in enumerate(message.get(TOOL_CALLS_KEY) or []):
                function = call.get(FUNCTION_KEY) if isinstance(call, dict) else None
                if isinstance(function, dict):
                    take((MESSAGES_KEY, i, TOOL_CALLS_KEY, j, FUNCTION_KEY, ARGUMENTS_KEY), function.get(ARGUMENTS_KEY))
            legacy = message.get(FUNCTION_CALL_KEY)
            if isinstance(legacy, dict):
                take((MESSAGES_KEY, i, FUNCTION_CALL_KEY, ARGUMENTS_KEY), legacy.get(ARGUMENTS_KEY))
            content = message.get(CONTENT_KEY)
            if isinstance(content, list):
                for j, part in enumerate(content):
                    if isinstance(part, dict) and part.get(TYPE_KEY) == TOOL_USE_TYPE:
                        take((MESSAGES_KEY, i, CONTENT_KEY, j, TOOL_USE_INPUT_KEY), part.get(TOOL_USE_INPUT_KEY))
        return found

    @staticmethod
    def _encode(document: Any, style: Optional[tuple]) -> str:
        """A changed document back to a string: in the style it came in when
        that was noted (indent, separators, a trailing line break), else as
        tool-call arguments always have been."""
        if style is None:
            return json.dumps(document, ensure_ascii=False)
        indent, separators, newline = style
        return json.dumps(document, ensure_ascii=False, indent=indent, separators=separators) + newline

    @staticmethod
    def _style(text: str) -> tuple:
        """How a JSON text is laid out: its indent (None for one line, else
        the first indented line's leading whitespace), the separators that go
        with it, and whether it ends with a line break."""
        body = text.strip()
        indent: Any = None
        if "\n" in body:
            for line in body.split("\n")[1:]:
                lead = line[: len(line) - len(line.lstrip(" \t"))]
                if lead:
                    indent = lead if "\t" in lead else len(lead)
                    break
        if indent is not None:
            separators = (",", ": ")
        else:
            # Space or tab only: the whitespace JSON allows, so the separators
            # chosen always give valid JSON.
            colon = re.search(r'"[ \t]*:([ \t]?)', body)
            comma = re.search(r'[\]}"0-9el],([ \t]?)', body)
            separators = ("," + (comma.group(1) if comma else " "), ":" + (colon.group(1) if colon else " "))
        return indent, separators, "\n" if text.endswith("\n") else ""

    @staticmethod
    def _depth(document: Any, limit: int) -> bool:
        """Whether document nests no deeper than limit, without recursion."""
        stack = [(document, 1)]
        while stack:
            node, level = stack.pop()
            if isinstance(node, (dict, list)):
                if level > limit:
                    return False
                stack.extend((child, level + 1) for child in (node.values() if isinstance(node, dict) else node))
        return True

    @staticmethod
    def _context_keys() -> tuple:
        """The sibling keys the structure rules read beside a value: a
        mapping's kind (a Secret) and an env entry's name."""
        module = sys.modules.get(AuditRedactor.__module__)
        return (getattr(module, "KIND_KEY", "kind"), getattr(module, "ENV_NAME_KEY", "name"))

    def _masked_whole(self, root: Any, path: tuple) -> bool:
        """Whether the structure rules would mask the string at path in root
        whole (it sits under a credential key, in a Secret's data, as a
        credential env value). Such a string is never decoded: it stays a
        string and is masked as one."""
        # The structure rules look at most two levels up from a value: its
        # key, its mapping (an env entry's name), and that mapping's own
        # mapping (a Secret's kind beside data). So the probe is that much
        # context only, a fixed cost whatever the size of root.
        try:
            parent = self._get(root, path[:-1])
            grand = self._get(root, path[:-2]) if len(path) >= 2 else None
        except (KeyError, IndexError, TypeError):
            return True
        key = path[-1]
        mini: Any = {key: MASK_PROBE}
        context = self._context_keys()
        if isinstance(parent, dict):
            mini.update({k: parent[k] for k in context if k != key and isinstance(parent.get(k), str)})
        elif isinstance(key, int):
            mini = [MASK_PROBE]
            key = 0
        where: tuple = (key,)
        if isinstance(grand, dict) and len(path) >= 2:
            outer = {k: grand[k] for k in context if k != path[-2] and isinstance(grand.get(k), str)}
            outer[path[-2] if not isinstance(path[-2], int) else "_"] = mini
            if isinstance(path[-2], int):
                outer["_"] = [mini]
                where = ("_", 0, key)
            else:
                where = (path[-2], key)
            mini = outer
        walked, _ = AuditRedactor.redact_counted(mini, self._rules)
        try:
            return self._get(walked, where) != MASK_PROBE
        except (KeyError, IndexError, TypeError):
            return True

    def _decode_output(self, path: tuple, value: Any, found: List[tuple], level: int = 0, budget: Optional[List[int]] = None) -> None:
        """Note value as a document to walk when it is a whole JSON object or
        array within the bounds, then the same for whole-JSON strings inside
        it. Anything else is left a string and matched as text."""
        if not isinstance(value, str) or level >= TOOL_OUTPUT_MAX_NESTING:
            return
        body = value.strip()
        if not body or body[0] not in "{[" or len(value.encode("utf-8", "replace")) > TOOL_OUTPUT_MAX_BYTES:
            return
        repeated = False

        def pairs(items: List[tuple]) -> Dict[str, Any]:
            nonlocal repeated
            keys = [key for key, _ in items]
            repeated = repeated or len(keys) != len(set(keys))
            return dict(items)

        try:
            document = json.loads(value, object_pairs_hook=pairs)
        except (ValueError, RecursionError):
            return
        if not isinstance(document, (dict, list)) or not self._depth(document, TOOL_OUTPUT_MAX_DEPTH):
            return
        # A repeated key: the decoder keeps one value, so what the structure
        # rules would see differs from the text. Such output stays a string
        # and gets the text rules only, as before.
        if repeated:
            return
        found.append((path, value, document, self._style(value)))
        if level + 1 >= TOOL_OUTPUT_MAX_NESTING:
            return
        # One budget per tool output, shared down the nesting.
        budget = budget if budget is not None else [TOOL_OUTPUT_MAX_NESTED]
        stack: List[tuple] = [((), document)]
        while stack:
            rel, node = stack.pop()
            items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
            for key, child in items:
                if isinstance(child, str):
                    body = child.strip()
                    if not body or body[0] not in "{[" or budget[0] <= 0:
                        continue
                    budget[0] -= 1
                    if not self._masked_whole(document, rel + (key,)):
                        self._decode_output(path + rel + (key,), child, found, level + 1, budget)
                elif isinstance(child, (dict, list)):
                    stack.append((rel + (key,), child))

    def _tool_outputs(self, data: Dict[str, Any]) -> List[tuple]:
        """(path, string, document, style) for each tool output that is a
        whole JSON object or array: a chat tool message's content, an
        Anthropic tool result's content (a string or its text parts), a
        Responses function call output, and each string in a Gemini function
        response's response object. The structure rules (key names, Secret
        data, env pairs) then apply to it as to any mapping."""
        found: List[tuple] = []
        for i, message in enumerate(data.get(MESSAGES_KEY) or []):
            if not isinstance(message, dict):
                continue
            content = message.get(CONTENT_KEY)
            if message.get(ROLE_KEY) == TOOL_ROLE:
                self._decode_output((MESSAGES_KEY, i, CONTENT_KEY), content, found)
            for j, part in enumerate(content if isinstance(content, list) else []):
                if not isinstance(part, dict) or part.get(TYPE_KEY) != TOOL_RESULT_TYPE:
                    continue
                inner = part.get(CONTENT_KEY)
                where = (MESSAGES_KEY, i, CONTENT_KEY, j, CONTENT_KEY)
                self._decode_output(where, inner, found)
                for k, sub in enumerate(inner if isinstance(inner, list) else []):
                    if isinstance(sub, dict) and sub.get(TYPE_KEY) == TEXT_TYPE:
                        self._decode_output(where + (k, TEXT_KEY), sub.get(TEXT_KEY), found)
        items = data.get(RESPONSES_INPUT_KEY)
        for i, item in enumerate(items if isinstance(items, list) else []):
            if isinstance(item, dict) and item.get(TYPE_KEY) == RESPONSES_FUNCTION_CALL_OUTPUT_TYPE:
                self._decode_output((RESPONSES_INPUT_KEY, i, RESPONSES_OUTPUT_KEY), item.get(RESPONSES_OUTPUT_KEY), found)
        for i, content in enumerate(data.get(CONTENTS_KEY) or []):
            parts = content.get(PARTS_KEY) if isinstance(content, dict) else None
            for j, part in enumerate(parts if isinstance(parts, list) else []):
                for key in GEMINI_FUNCTION_RESPONSE_KEYS:
                    response = part.get(key) if isinstance(part, dict) else None
                    body = response.get(GEMINI_RESPONSE_KEY) if isinstance(response, dict) else None
                    # Each top-level string is a tool output of its own, with its
                    # own nesting budget; the probe below is a fixed cost.
                    for name, value in (body.items() if isinstance(body, dict) else ()):
                        if not isinstance(value, str) or value.strip()[:1] not in ("{", "["):
                            continue
                        # A string the structure rules mask whole where it
                        # sits stays a string.
                        if self._masked_whole(body, (name,)):
                            continue
                        self._decode_output((CONTENTS_KEY, i, PARTS_KEY, j, key, GEMINI_RESPONSE_KEY, name), value, found)
        return found

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
        kept = self._carve(data)
        arguments = self._tool_arguments(data) + self._tool_outputs(data)
        counts: Dict[str, int] = {}
        for key in list(data):
            if key in CONTROL_KEYS or str(key).startswith(CONTROL_KEY_PREFIXES):
                continue
            mine = [(path[1:], value) for path, value in kept if path[0] == key]
            args = [(path[1:], source, document, style) for path, source, document, style in arguments if path[0] == key]
            data[key] = self._redact_field(data[key], mine, args, counts)
        return counts


    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        try:
            counts = self.redact_request(data)
        except UnwalkableRequestError as exc:
            # A refused shape is the request's, and the same request is
            # refused every time: 400, so a caller doesn't retry it as it
            # would a 5xx. Any other exception stays a 500, a fault in the
            # hook rather than in the request.
            raise HTTPException(status_code=400, detail=str(exc)) from None
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
