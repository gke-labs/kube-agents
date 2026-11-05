"""Saved answers for the proxy's record and replay modes.

Record mode writes one file per fingerprint under the cache directory: the
answer with the values its request bound swapped for placeholders, its finish
reason and usage, and the request's hashes for diagnosis. The first write for
a key wins. Each distinct request message, normalized and scrubbed, is written
once beside them, so a replay miss can show how a message differed.

Replay mode reads the directory: a hit restores the answer with the current
request's values (a placeholder the request does not bind makes it a miss)
and serves it in the shape an OpenAI-compatible client expects.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import normalizer

ANSWERS_DIR = "answers"
MESSAGES_DIR = "messages"
# Saved answers by answer-slot key, and the answers recorded for each slot context.
SLOTS_DIR = "slots"
VARIANTS_DIR = "variants"
JSON_SUFFIX = ".json"
TMP_SUFFIX = ".tmp"
TEXT_ENCODING = "utf-8"
TOOL_CALLS_KEY = "tool_calls"
# The parts of a message whose words a miss is ranked by.
CONTENT_KEY = "content"
TEXT_KEY = "text"
FUNCTION_KEY = "function"
NAME_KEY = "name"
ARGUMENTS_KEY = "arguments"

# Fields of a saved entry.
ENTRY_FINGERPRINT = "fingerprint"
ENTRY_ANSWER = "answer"
ENTRY_FINISH_REASON = "finish_reason"
ENTRY_USAGE = "usage"
ENTRY_MESSAGE_HASHES = "message_hashes"
ENTRY_DIAG_MESSAGE_HASHES = "diag_message_hashes"
ENTRY_DIAG_SETTINGS_HASH = "diag_settings_hash"
ENTRY_RECORDED_BY = "recorded_by"
ENTRY_RECORDED_AT = "recorded_at"
ENTRY_NORMALIZER_VERSION = "normalizer_version"

# The served response.
SERVED_ID_PREFIX = "replay-"
SERVED_ID_HEX_CHARS = 24
COMPLETION_OBJECT = "chat.completion"
CHUNK_OBJECT = "chat.completion.chunk"
ASSISTANT_ROLE = "assistant"
DEFAULT_FINISH_REASON = "stop"
SSE_DONE_EVENT = b"data: [DONE]\n\n"
# Distinct next messages kept per prefix for a miss to be compared against.
MAX_PREFIX_CANDIDATES = 64
# A prefix is keyed by a rolling hash: the previous prefix's key and the next message's hash.
PREFIX_SEPARATOR = "|"


def _walk(value: Any, fn, key: Optional[str] = None, in_tool_call: bool = False) -> Any:
    """Apply `fn` to every string, skipping opaque fields (ids, signatures) as the normalizer does."""
    if key is not None and normalizer.Normalizer.is_opaque(key, in_tool_call):
        return value
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [
            {k: _walk(v[k], fn, k, True) for k in v} if key == TOOL_CALLS_KEY and isinstance(v, dict) else _walk(v, fn)
            for v in value
        ]
    if isinstance(value, dict):
        return {k: _walk(v, fn, k, False) for k, v in value.items()}
    return value


def normalize_answer(message: Dict[str, Any], mapping: normalizer.Mapping) -> Dict[str, Any]:
    """The answer with values the request bound swapped for placeholders, by whole token."""
    return _walk(message, mapping.apply_to_answer)


def answer_texts(message: Dict[str, Any]) -> List[str]:
    """Every string of an answer the normalizer reads: opaque fields (ids, signatures) left out."""
    found: List[str] = []
    _walk(message, lambda text: found.append(text) or text)
    return found


def restore_answer(saved: Dict[str, Any], mapping: normalizer.Mapping) -> Tuple[Dict[str, Any], List[str]]:
    """The saved answer with the current request's values; and the placeholders it does not bind."""
    missing: List[str] = []

    def restore(text: str) -> str:
        restored, unbound = mapping.restore(text)
        missing.extend(unbound)
        return restored

    return _walk(saved, restore), missing


class AnswerCache:
    """One directory of saved answers. Writes are atomic and the first one for a key wins."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, kind: str, key: str) -> str:
        return os.path.join(self.root, kind, key + JSON_SUFFIX)

    def _write_once(self, path: str, data: str) -> bool:
        """Write `path` unless it exists; True when this call wrote it."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"
        try:
            with open(tmp, "w", encoding=TEXT_ENCODING) as handle:
                handle.write(data)
            os.link(tmp, path)  # fails if the key is already there: the first write wins
            return True
        except FileExistsError:
            return False
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def save(self, fingerprint: str, entry: Dict[str, Any]) -> bool:
        return self._write_once(self._path(ANSWERS_DIR, fingerprint), json.dumps(entry, sort_keys=True, ensure_ascii=True))

    def load(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._path(ANSWERS_DIR, fingerprint), encoding=TEXT_ENCODING) as handle:
                entry = json.load(handle)
        except (FileNotFoundError, ValueError):
            return None
        return entry if isinstance(entry, dict) else None

    def save_slot(self, slot_key: str, fingerprint: str) -> bool:
        return self._write_once(self._path(SLOTS_DIR, slot_key), json.dumps(fingerprint))

    def load_slot(self, slot_key: str) -> Optional[str]:
        try:
            with open(self._path(SLOTS_DIR, slot_key), encoding=TEXT_ENCODING) as handle:
                fingerprint = json.load(handle)
        except (FileNotFoundError, ValueError):
            return None
        return fingerprint if isinstance(fingerprint, str) else None

    def add_variant(self, context: str, answer_hash: str) -> bool:
        """Note that the model answered `context` with `answer_hash` in a recording."""
        return self._write_once(self._path(os.path.join(VARIANTS_DIR, context), answer_hash), "")

    def has_variant(self, context: str, answer_hash: str) -> bool:
        return os.path.exists(self._path(os.path.join(VARIANTS_DIR, context), answer_hash))

    def save_message(self, message_hash: str, text: str) -> bool:
        return self._write_once(self._path(MESSAGES_DIR, message_hash), json.dumps(text, ensure_ascii=True))

    def has_message(self, message_hash: str) -> bool:
        return os.path.exists(self._path(MESSAGES_DIR, message_hash))

    def load_message(self, message_hash: str) -> Optional[str]:
        try:
            with open(self._path(MESSAGES_DIR, message_hash), encoding=TEXT_ENCODING) as handle:
                text = json.load(handle)
        except (FileNotFoundError, ValueError):
            return None
        return text if isinstance(text, str) else None

    def entries(self) -> Iterator[Dict[str, Any]]:
        directory = os.path.join(self.root, ANSWERS_DIR)
        if not os.path.isdir(directory):
            return
        for name in sorted(os.listdir(directory)):
            if name.endswith(JSON_SUFFIX):
                entry = self.load(name[: -len(JSON_SUFFIX)])
                if entry is not None:
                    yield entry


def prefix_keys(diag_settings_hash: str, diag_hashes: List[str]) -> Iterator[str]:
    """The keys of a request's prefixes, from the settings alone to every message.

    Each is a hash of the one before and the next message's hash, so a prefix
    costs one fixed-size key however long it is.
    """
    key = hashlib.sha256(diag_settings_hash.encode(TEXT_ENCODING)).hexdigest()
    yield key
    for message_hash in diag_hashes:
        key = hashlib.sha256(f"{key}{PREFIX_SEPARATOR}{message_hash}".encode(TEXT_ENCODING)).hexdigest()
        yield key


class PrefixIndex:
    """Saved entries by diag-settings-hash-rooted prefix of their diag message hashes.

    Many saved requests can share a prefix (every worker conversation opens the
    same way), so each prefix keeps one entry per distinct next message, up to
    MAX_PREFIX_CANDIDATES: the caller picks the one whose next message is most
    like the current request's. Prefixes are keyed by prefix_keys, so the index
    grows with the number of saved messages, not with the square of their
    conversations' lengths.
    """

    def __init__(self, entries: Iterator[Dict[str, Any]]):
        self.by_prefix: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for entry in entries:
            hashes = [str(h) for h in entry.get(ENTRY_DIAG_MESSAGE_HASHES) or []]
            keys = prefix_keys(str(entry.get(ENTRY_DIAG_SETTINGS_HASH) or ""), hashes)
            for length, key in enumerate(keys):
                group = self.by_prefix.setdefault(key, {})
                following = hashes[length] if length < len(hashes) else ""
                if following not in group and len(group) < MAX_PREFIX_CANDIDATES:
                    group[following] = entry

    def candidates(self, diag_settings_hash: str, diag_hashes: List[str]) -> Tuple[List[Dict[str, Any]], int]:
        """The saved entries sharing the longest prefix, one per distinct next message, and the prefix length."""
        best: Optional[Dict[str, Dict[str, Any]]] = None
        matched = 0
        for length, key in enumerate(prefix_keys(diag_settings_hash, diag_hashes)):
            found = self.by_prefix.get(key)
            if found is None:
                break
            best, matched = found, length
        if best is None:
            return [], 0
        return list(best.values()), matched

    def closest(self, diag_settings_hash: str, diag_hashes: List[str]) -> Tuple[Optional[Dict[str, Any]], int]:
        """The first saved entry sharing the longest prefix, and how many messages match (settings first)."""
        found, matched = self.candidates(diag_settings_hash, diag_hashes)
        return (found[0] if found else None), matched


def message_text(text: str) -> str:
    """A saved message's words: its content and tool calls, without the JSON around them.

    Saved and current messages are JSON; split raw, a line's last word glues onto
    the next line's first, which under-scores line-per-row tool output.
    """
    try:
        message = json.loads(text)
    except ValueError:
        return text
    if not isinstance(message, dict):
        return text
    parts: List[str] = []
    content = message.get(CONTENT_KEY)
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(str(part.get(TEXT_KEY, "")) if isinstance(part, dict) else str(part) for part in content)
    for call in message.get(TOOL_CALLS_KEY) or []:
        function = call.get(FUNCTION_KEY) if isinstance(call, dict) else None
        if isinstance(function, dict):
            parts.extend((str(function.get(NAME_KEY, "")), str(function.get(ARGUMENTS_KEY, ""))))
    return "\n".join(parts)


def similarity(saved: str, current: str) -> float:
    """How alike two messages are by the words they share, relative to both lengths (order ignored)."""
    a, b = message_text(saved).split(), message_text(current).split()
    return difflib.SequenceMatcher(None, a, b, autojunk=False).quick_ratio()


def served_id(fingerprint: str) -> str:
    return SERVED_ID_PREFIX + fingerprint[:SERVED_ID_HEX_CHARS]


def completion_body(message: Dict[str, Any], entry: Dict[str, Any], model: str, fingerprint: str) -> bytes:
    """A non-streamed chat completion serving `message`."""
    body = {
        "id": served_id(fingerprint),
        "object": COMPLETION_OBJECT,
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": entry.get(ENTRY_FINISH_REASON) or DEFAULT_FINISH_REASON}],
    }
    if entry.get(ENTRY_USAGE):
        body["usage"] = entry[ENTRY_USAGE]
    return json.dumps(body).encode()


def stream_events(message: Dict[str, Any], entry: Dict[str, Any], model: str, fingerprint: str) -> List[bytes]:
    """SSE events serving `message`: one delta with the whole answer, a finish event with usage, [DONE]."""
    meta = {"id": served_id(fingerprint), "object": CHUNK_OBJECT, "created": int(time.time()), "model": model}
    delta = {k: v for k, v in message.items() if v is not None}
    delta.setdefault("role", ASSISTANT_ROLE)
    calls = delta.get(TOOL_CALLS_KEY)
    if isinstance(calls, list):
        delta[TOOL_CALLS_KEY] = [dict(call, index=i) if isinstance(call, dict) else call for i, call in enumerate(calls)]
    first = dict(meta, choices=[{"index": 0, "delta": delta, "finish_reason": None}])
    last = dict(meta, choices=[{"index": 0, "delta": {},
                                "finish_reason": entry.get(ENTRY_FINISH_REASON) or DEFAULT_FINISH_REASON}])
    if entry.get(ENTRY_USAGE):
        last["usage"] = entry[ENTRY_USAGE]
    return [f"data: {json.dumps(first)}\n\n".encode(), f"data: {json.dumps(last)}\n\n".encode(), SSE_DONE_EVENT]
