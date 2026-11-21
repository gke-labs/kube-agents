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

Left exactly as they came, and only at the positions the provider APIs
define (content parts, a tool result's own content list, `system`,
`thinking_blocks`, Gemini parts; never inside a tool's input or output or a
function's arguments): in an assistant's or the model's own turn, a thinking
block's text and signature (the provider checks the text against the
signature, and the model wrote it from input this hook had already redacted),
redacted thinking's data and a thought signature; and, anywhere a part may
carry one, a binary payload of a listed binary media type (images, PDF,
audio, video) that decodes as base64 and starts the way that media does; a
payload of any other type is refused. Generation
settings and the gateway's own bookkeeping are not walked. A request the hook
cannot walk is refused: one that carries no conversation, input or prompt, a
`messages` or `contents` that is not a list of objects, or anything at a
payload position that is not binary base64. Responses are not touched; the limitation is documented
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
#   known binary one and its data is strict base64; anything else in that
#   position is refused, since it may carry text the walk cannot read.
TYPE_KEY = "type"
THINKING_TYPE = "thinking"
THINKING_KEPT_KEYS = ("thinking", "signature")
REDACTED_THINKING_TYPE = "redacted_thinking"
TOOL_RESULT_TYPE = "tool_result"
GEMINI_OPAQUE_PART_KEYS = ("thoughtSignature", "thought_signature")
GEMINI_INLINE_KEYS = ("inlineData", "inline_data")
GEMINI_MIME_KEYS = ("mimeType", "mime_type")
GEMINI_SYSTEM_KEYS = ("systemInstruction", "system_instruction")
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
# A string holding a JSON object or array (a tool call's arguments, a tool's
# JSON output) is decoded and walked as a document, so key names count and a
# value after an escaped newline is matched, then re-encoded only if a rule
# changed it.
JSON_OPENERS = ("{", "[")
DATA_KEY = "data"


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
            raise self._refuse(f"a payload of media type {media!r}")
        if not isinstance(data, str):
            raise self._refuse(f"a {media} payload that is not a string")
        try:
            decoded = base64.b64decode("".join(data.split()), validate=True)
        except (binascii.Error, ValueError):
            raise self._refuse(f"a {media} payload that is not base64") from None
        if not starts(decoded):
            raise self._refuse(f"a payload that does not start as {media}")
        kept.append((path, data))

    def _keep_data_url(self, url: str, path: tuple, kept: List[tuple]) -> None:
        match = STRICT_DATA_URL.match(url)
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
        source = part.get(SOURCE_KEY)
        if isinstance(source, dict) and source.get(TYPE_KEY) == BASE64_SOURCE_TYPE:
            self._keep_payload(source.get(MEDIA_TYPE_KEY), source.get(DATA_KEY), path + (SOURCE_KEY, DATA_KEY), kept)
        image_url = part.get(IMAGE_URL_KEY)
        if isinstance(image_url, dict) and isinstance(image_url.get(URL_KEY), str) and image_url[URL_KEY].startswith(DATA_URL_PREFIX):
            self._keep_data_url(image_url[URL_KEY], path + (IMAGE_URL_KEY, URL_KEY), kept)
        elif isinstance(image_url, str) and image_url.startswith(DATA_URL_PREFIX):
            self._keep_data_url(image_url, path + (IMAGE_URL_KEY,), kept)
        audio = part.get(INPUT_AUDIO_KEY)
        if isinstance(audio, dict) and DATA_KEY in audio:
            self._keep_payload(f"{AUDIO_MEDIA_PREFIX}{audio.get(AUDIO_FORMAT_KEY, '')}", audio.get(DATA_KEY), path + (INPUT_AUDIO_KEY, DATA_KEY), kept)
        filed = part.get(FILE_KEY)
        if isinstance(filed, dict) and isinstance(filed.get(FILE_DATA_KEY), str):
            self._keep_data_url(filed[FILE_DATA_KEY], path + (FILE_KEY, FILE_DATA_KEY), kept)
        # A tool result's own content list holds parts too (an image a tool
        # returned); one level only, since deeper is the tool's own output.
        if kind == TOOL_RESULT_TYPE and not nested and isinstance(part.get(CONTENT_KEY), list):
            self._carve_parts(part[CONTENT_KEY], path + (CONTENT_KEY,), kept, nested=True, assistant=False)

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
            for key in GEMINI_INLINE_KEYS:
                inline = part.get(key)
                if isinstance(inline, dict):
                    mime = next((inline[k] for k in GEMINI_MIME_KEYS if k in inline), None)
                    self._keep_payload(mime, inline.get(DATA_KEY), path + (i, key, DATA_KEY), kept)

    def _carve(self, data: Dict[str, Any]) -> List[tuple]:
        """The fields to leave as they are, as (path from the request, value)."""
        kept: List[tuple] = []
        for i, message in enumerate(data.get(MESSAGES_KEY) or []):
            assistant = message.get(ROLE_KEY) in ASSISTANT_ROLES
            if isinstance(message.get(CONTENT_KEY), list):
                self._carve_parts(message[CONTENT_KEY], (MESSAGES_KEY, i, CONTENT_KEY), kept, assistant=assistant)
            if assistant and isinstance(message.get(THINKING_BLOCKS_KEY), list):
                self._carve_parts(message[THINKING_BLOCKS_KEY], (MESSAGES_KEY, i, THINKING_BLOCKS_KEY), kept, assistant=True)
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

    @classmethod
    def _expand_json(cls, node: Any, path: tuple, found: List[tuple]) -> Any:
        """node with each string that holds a JSON object or array replaced by
        its decoded document, recording (path, original string) in found so
        _collapse_json can re-encode it. Nothing is added to the tree, so no
        caller data can be mistaken for a marker."""
        if isinstance(node, str):
            text = node.strip()
            if text[:1] in JSON_OPENERS:
                try:
                    decoded = json.loads(text)
                except ValueError:
                    return node
                if isinstance(decoded, (dict, list)):
                    found.append((path, node))
                    return cls._expand_json(decoded, path, found)
            return node
        if isinstance(node, list):
            return [cls._expand_json(item, path + (i,), found) for i, item in enumerate(node)]
        if isinstance(node, dict):
            return {key: cls._expand_json(value, path + (key,), found) for key, value in node.items()}
        return node

    @staticmethod
    def _get(root: Any, path: tuple) -> Any:
        node = root
        for step in path:
            node = node[step]
        return node

    def _redact_keys(self, node: Any, counts: Dict[str, int]) -> Any:
        """node with the text rules run over every key name: the walk reads
        values, so a value written as a key is redacted here."""
        if isinstance(node, list):
            return [self._redact_keys(item, counts) for item in node]
        if isinstance(node, dict):
            rebuilt = {}
            for key, value in node.items():
                if isinstance(key, str):
                    key, made = AuditRedactor.redact_text_counted(key, self._rules)
                    for name, count in made.items():
                        counts[name] = counts.get(name, 0) + count
                rebuilt[key] = self._redact_keys(value, counts)
            return rebuilt
        return node

    @staticmethod
    def _has_duplicate_keys(source: str) -> bool:
        """Whether an object in source repeats a key: the decoder keeps one
        value and drops the others, which the walk then never sees."""
        found = False

        def pairs(items: List[tuple]) -> Dict[str, Any]:
            nonlocal found
            keys = [key for key, _ in items]
            found = found or len(keys) != len(set(keys))
            return dict(items)

        json.loads(source.strip(), object_pairs_hook=pairs)
        return found

    def _encode_json(self, document: Any, source: str, counts: Dict[str, int]) -> str:
        """A walked, key-redacted document back to a string: the original one
        only if the walk changed nothing, the string has no repeated key, and
        the text rules over the raw string and over the canonical encoding
        change nothing either; otherwise the canonical encoding, redacted."""
        # The canonical encoding as well as the raw string: escapes in the
        # raw string (a key written as \u-sequences) can hide a value the
        # canonical one shows.
        canonical = json.dumps(document, ensure_ascii=False)
        encoded, made = AuditRedactor.redact_text_counted(canonical, self._rules)
        raw, _ = AuditRedactor.redact_text_counted(source, self._rules)
        unchanged = encoded == canonical and raw == source and document == json.loads(source.strip())
        if unchanged and not self._has_duplicate_keys(source):
            return source
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        return encoded

    def _collapse_json(self, walked: Any, found: List[tuple], counts: Dict[str, int]) -> Any:
        """Undo _expand_json on a walked tree, deepest document first, so an
        outer document is re-encoded with its inner ones already strings.
        Paths name the tree as expanded, and key names are redacted only
        after every path is used."""
        for path, source in sorted(found, key=lambda item: len(item[0]), reverse=True):
            document = self._redact_keys(self._get(walked, path), counts)
            encoded = self._encode_json(document, source, counts)
            if path:
                self._put(walked, path, encoded)
            else:
                walked = encoded
        return self._redact_keys(walked, counts)

    def _redact_field(self, value: Any, kept: List[tuple], counts: Dict[str, int]) -> Any:
        """One top-level field: every string redacted, key names counted, the
        kept fields put back as they came. The original is left unchanged
        (the walk returns a copy)."""
        if kept and isinstance(value, (dict, list)):
            placeholder = copy.deepcopy(value)
            for path, _ in kept:
                self._put(placeholder, path, None)
        else:
            placeholder = value
        found: List[tuple] = []
        expanded = self._expand_json(placeholder, (), found)
        walked, made = AuditRedactor.redact_counted(expanded, self._rules)
        for name, count in made.items():
            counts[name] = counts.get(name, 0) + count
        redacted = self._collapse_json(walked, found, counts)
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
        kept = self._carve(data)
        counts: Dict[str, int] = {}
        for key in list(data):
            if key in CONTROL_KEYS or str(key).startswith(CONTROL_KEY_PREFIXES):
                continue
            mine = [(path[1:], value) for path, value in kept if path[0] == key]
            data[key] = self._redact_field(data[key], mine, counts)
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
