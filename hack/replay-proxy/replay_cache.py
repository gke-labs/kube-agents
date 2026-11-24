"""Saved answers for the proxy's record and replay modes.

Record mode writes one file per distinct answer to a fingerprint under the
cache directory, `alts/<fingerprint>/<answer>.json`, named by the hash of the
answer exactly as stored, opaque ids and signatures aside (stored_hash), so
two answers that differ in anything served never share a name: the answer
with the values its request
bound swapped for placeholders, its finish reason and usage, and the request's
hashes for diagnosis. Each file is written once; a second delivery of the same
answer finds it there and writes nothing, and a different answer to the same
request gets a file of its own. Each distinct request message, normalized and
scrubbed, is written once beside them, so a replay miss can show how a message
differed.

Replay mode reads the directory: a hit restores one of the key's answers with
the current request's values (a placeholder the request does not bind makes it
a miss) and serves it in the shape an OpenAI-compatible client expects. The
older layout, one `answers/<fingerprint>.json` per key, is still read: a key
with no alternates serves that file as its one answer. The replay lane's
download may write `weights/<fingerprint>.json` beside a key's alternates,
how often graded runs produced each (AnswerCache.weights), which replay
draws them in proportion to.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import normalizer

# The older layout: one answer per fingerprint, the first written. Still read
# (a key with no alternates), never written by record mode.
ANSWERS_DIR = "answers"
# Every distinct answer recorded for a fingerprint, one file each:
# alts/<fingerprint>/<stored answer hash>.json (stored_hash).
ALTS_DIR = "alts"
MESSAGES_DIR = "messages"
# Saved answers by answer-slot key, and the answers recorded for each slot context.
SLOTS_DIR = "slots"
VARIANTS_DIR = "variants"
JSON_SUFFIX = ".json"
# Every name the cache writes, relative to its directory: one key per file, a
# variant and an alternate one directory deeper. The one definition of the
# layout, which the collector, the publisher and the lane's download check
# names against. CACHE_LAYOUT_PATTERN holds VARIANT_JOINER where a variant's
# context and answer (or an alternate's fingerprint and answer) meet: "/" on
# disk (CACHE_REL_RE); the publisher flattens both to one object name with its
# own separator (publish_cache.OBJECT_NAME_RE).
CACHE_HEX = r"[0-9a-f]{16,64}"
VARIANT_JOINER = "<variant-joiner>"
CACHE_LAYOUT_PATTERN = (
    rf"^(?:(?:{re.escape(ANSWERS_DIR)}|{re.escape(SLOTS_DIR)}|{re.escape(MESSAGES_DIR)})/{CACHE_HEX}{re.escape(JSON_SUFFIX)}"
    rf"|(?:{re.escape(VARIANTS_DIR)}|{re.escape(ALTS_DIR)})/{CACHE_HEX}{VARIANT_JOINER}{CACHE_HEX}{re.escape(JSON_SUFFIX)})\Z"
)
DISK_VARIANT_JOINER = "/"
# How often graded runs produced each of a key's alternates,
# weights/<fingerprint>.json holding {answer identity: count}: written only by
# the replay lane's download (fetch_cache.py), never by record mode or the
# publisher, so it is in the directory's layout (CACHE_REL_RE, what the lane
# seeds) but not among the bucket's objects (CACHE_LAYOUT_PATTERN).
WEIGHTS_DIR = "weights"
WEIGHTS_PATTERN = rf"{re.escape(WEIGHTS_DIR)}/{CACHE_HEX}{re.escape(JSON_SUFFIX)}"
CACHE_REL_RE = re.compile(
    rf"(?:{CACHE_LAYOUT_PATTERN.replace(VARIANT_JOINER, re.escape(DISK_VARIANT_JOINER))}|^{WEIGHTS_PATTERN}\Z)")
# The most one answer weighs (AnswerCache.weights): a larger count is read as
# this, so a key's weighted turn order stays short enough to build per lookup.
# The busiest keys of the 2026-11-17 and -18 nightlies gave about a hundred
# producing requests a build.
MAX_ANSWER_WEIGHT = 1000
# A fingerprint or an answer identity as one path segment.
CACHE_HEX_RE = re.compile(rf"^{CACHE_HEX}\Z")
# The identity AnswerCache.answers gives the older layout's single answer,
# which is saved under its fingerprint alone.
LEGACY_ANSWER = ""
TMP_SUFFIX = ".tmp"
# Written last by whatever seeds a replay cache from outside the pod
# (hack/replay-proxy/collect_shadow.py, in the replay lane): a replay proxy
# rebuilds the index its miss report searches whenever this file changes.
SEED_MARKER = ".seeded"
TEXT_ENCODING = "utf-8"
TOOL_CALLS_KEY = "tool_calls"
# The model's reasoning text in an answer, never stored (stored_answer).
REASONING_KEY = "reasoning_content"
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


def stored_answer(message: Dict[str, Any], mapping: normalizer.Mapping) -> Dict[str, Any]:
    """The answer as record mode saves it: normalized, without its `reasoning_content`.

    The client never sends reasoning back (the lane trials' requests hold none),
    so dropping it changes no later key, and a served answer carries none of
    the recording's reasoning. Thinking blocks stay: a client that sends them
    back would key on them.
    """
    kept = {k: v for k, v in message.items() if k != REASONING_KEY}
    return normalize_answer(kept, mapping)


def _without_opaque(value: Any, key: Optional[str] = None) -> Any:
    """`value` with every opaque field (ids, signatures) left out, as the normalizer tells them."""
    if isinstance(value, list):
        return [{k: _without_opaque(v[k], k) for k in v if not normalizer.Normalizer.is_opaque(k, True)}
                if key == TOOL_CALLS_KEY and isinstance(v, dict) else _without_opaque(v) for v in value]
    if isinstance(value, dict):
        return {k: _without_opaque(v, k) for k, v in value.items() if not normalizer.Normalizer.is_opaque(k, False)}
    return value


def stored_hash(stored: Dict[str, Any], finish_reason: Optional[str]) -> str:
    """An alternate's name: the hash of the answer exactly as stored (stored_answer) and of the
    finish reason it is served with, opaque fields aside.

    Everything served counts (text, tool calls, thinking text, a refusal, and
    the finish reason, read as the DEFAULT_FINISH_REASON a served answer
    without one gets), so two answers that differ in anything a client reads
    get different names; only ids and signatures, which differ between two
    deliveries of the same answer, are left out, so identical answers from
    different runs share one.
    """
    return normalizer.stable_hash([_without_opaque(stored), finish_reason or DEFAULT_FINISH_REASON])


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



def weights_text(weights: Dict[str, int]) -> str:
    """A weights file's content as save_weights writes it (fetch_cache.py costs it the same way)."""
    return json.dumps(weights, sort_keys=True)

class AnswerCache:
    """One directory of saved answers. Writes are atomic and every file is written once."""

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
            os.link(tmp, path)  # fails if the name is already there, so a file is never rewritten
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
        """The older layout's answer to a fingerprint (answers/), or None."""
        return self._load_entry(self._path(ANSWERS_DIR, fingerprint))

    @staticmethod
    def _load_entry(path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, encoding=TEXT_ENCODING) as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            return None
        return entry if isinstance(entry, dict) else None

    def _alternate_path(self, fingerprint: str, answer: str) -> str:
        return self._path(os.path.join(ALTS_DIR, fingerprint), answer)

    def save_alternate(self, fingerprint: str, answer: str, entry: Dict[str, Any]) -> bool:
        """Save one answer to a fingerprint under its identity; True when this call wrote it.

        Written once: the same answer delivered again writes nothing, and the
        file never changes after it is written.
        """
        if not (CACHE_HEX_RE.match(fingerprint) and CACHE_HEX_RE.match(answer)):
            raise ValueError("an alternate is named by two hex digests")
        return self._write_once(self._alternate_path(fingerprint, answer),
                                json.dumps(entry, sort_keys=True, ensure_ascii=True))

    def alternate_names(self, fingerprint: str) -> List[str]:
        """The answer identities saved for a fingerprint, sorted, so every reader sees one order."""
        if not CACHE_HEX_RE.match(fingerprint):
            return []
        try:
            names = os.listdir(os.path.join(self.root, ALTS_DIR, fingerprint))
        except OSError:
            return []
        return sorted(name[: -len(JSON_SUFFIX)] for name in names
                      if name.endswith(JSON_SUFFIX) and CACHE_HEX_RE.match(name[: -len(JSON_SUFFIX)]))

    def answers(self, fingerprint: str) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
        """Every answer saved for a fingerprint, in a fixed order: (identity, entry, or None when unreadable).

        The alternates, by identity; for a key with none, the older layout's
        answer as (LEGACY_ANSWER, entry). A fingerprint that is not a hex
        digest (a slot's pointer is read from a file) names nothing.
        """
        if not CACHE_HEX_RE.match(fingerprint):
            return []
        names = self.alternate_names(fingerprint)
        if names:
            return [(name, self._load_entry(self._alternate_path(fingerprint, name))) for name in names]
        if os.path.exists(self._path(ANSWERS_DIR, fingerprint)):
            return [(LEGACY_ANSWER, self.load(fingerprint))]
        return []

    def weights(self, fingerprint: str) -> Dict[str, int]:
        """How often graded runs produced each alternate of a fingerprint (weights/), by identity.

        Empty when there is no file or it is not a map; an entry that is not
        a positive integer is left out, and one above MAX_ANSWER_WEIGHT is
        read as it. An alternate with no entry weighs 1 (the caller's rule).
        """
        if not CACHE_HEX_RE.match(fingerprint):
            return {}
        found = self._load_entry(self._path(WEIGHTS_DIR, fingerprint))
        return {name: min(count, MAX_ANSWER_WEIGHT) for name, count in (found or {}).items()
                if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0}

    def save_weights(self, fingerprint: str, weights: Dict[str, int]) -> bool:
        """Write a fingerprint's weights file once (the lane's download); True when this call wrote it."""
        if not CACHE_HEX_RE.match(fingerprint):
            raise ValueError("a weights file is named by a hex digest")
        return self._write_once(self._path(WEIGHTS_DIR, fingerprint), weights_text(weights))

    def has_answer(self, fingerprint: str) -> bool:
        """Whether a readable answer to the fingerprint is saved, in either layout."""
        return any(entry is not None for _, entry in self.answers(fingerprint))

    def save_slot(self, slot_key: str, fingerprint: str) -> bool:
        """Point a slot at an entry; True when this call wrote the pointer.

        The first pointer wins, unless the entry it names does not exist: then
        it is replaced atomically, so a pointer left dangling heals.
        """
        path, data = self._path(SLOTS_DIR, slot_key), json.dumps(fingerprint)
        if self._write_once(path, data):
            return True
        current = self.load_slot(slot_key)
        if current is not None and self.has_answer(current):
            return False
        self._replace(path, data)
        return True

    def _replace(self, path: str, data: str) -> None:
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}"
        try:
            with open(tmp, "w", encoding=TEXT_ENCODING) as handle:
                handle.write(data)
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

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
        """One readable entry per saved fingerprint, either layout: what the miss index searches.

        The alternates of one fingerprint share its request, so the first is enough.
        """
        fingerprints: set = set()
        for sub in (ALTS_DIR, ANSWERS_DIR):
            try:
                names = os.listdir(os.path.join(self.root, sub))
            except OSError:
                continue
            for name in names:
                key = name if sub == ALTS_DIR else name[: -len(JSON_SUFFIX)] if name.endswith(JSON_SUFFIX) else ""
                if CACHE_HEX_RE.match(key):
                    fingerprints.add(key)
        for fingerprint in sorted(fingerprints):
            entry = next((entry for _, entry in self.answers(fingerprint) if entry is not None), None)
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
