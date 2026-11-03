"""Per-message hashes for diagnosing misses, frozen at each message's first sighting.

The fingerprint and `message_hashes` normalize every message with the whole
request's context: its time (timestamps become how-far-from-now tokens) and
whether any message names a CIDR (IPs then stay literal). So the same earlier
message can hash differently in a later request of the same conversation, and
the closest-earlier-request search would report a difference that is only
drift. These hashes are for that search only; they never decide a match.

Each message is normalized once, when this proxy first sees it after a given
history, with that request's time and with IPs kept literal only if a CIDR
appeared in that message or an earlier one. The result is reused for the same
message after the same history (keyed by a hash of the raw history plus the
message), from a bounded in-memory LRU.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, List

import normalizer

DEFAULT_CAPACITY = 65536
CHAIN_ROOT = "diag"
TEXT_ENCODING = "utf-8"
# A lone surrogate is valid in a JSON string but not in UTF-8.
ENCODING_ERRORS = "surrogatepass"


@dataclass(frozen=True)
class _Entry:
    hash: str
    mapping: normalizer.Mapping
    cidr_seen: bool


def _copy_mapping(mapping: normalizer.Mapping) -> normalizer.Mapping:
    return normalizer.Mapping(
        to_placeholder=dict(mapping.to_placeholder),
        to_value=dict(mapping.to_value),
        counts=dict(mapping.counts),
        bare_numbers=set(mapping.bare_numbers),
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode(TEXT_ENCODING, ENCODING_ERRORS)).hexdigest()


class DiagnosisHasher:
    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        self.capacity = capacity
        self._cache: "OrderedDict[str, _Entry]" = OrderedDict()
        self._lock = threading.Lock()

    def hashes(self, messages: List[Any], now: float) -> List[str]:
        out = []
        chain = CHAIN_ROOT
        previous = _Entry("", normalizer.Mapping(), False)
        for message in messages:
            raw = json.dumps(message, sort_keys=True, ensure_ascii=False)
            chain = _digest(chain + "|" + _digest(raw))
            with self._lock:
                entry = self._cache.get(chain)
                if entry is not None:
                    self._cache.move_to_end(chain)
            if entry is None:
                cidr_seen = previous.cidr_seen or normalizer.CIDR_RE.search(raw) is not None
                norm = normalizer.Normalizer(now, keep_ips=cidr_seen)
                norm.mapping = _copy_mapping(previous.mapping)
                digest = normalizer.stable_hash(norm.walk(message))[: normalizer.MESSAGE_HASH_HEX_CHARS]
                entry = _Entry(digest, norm.mapping, cidr_seen)
                with self._lock:
                    self._cache[chain] = entry
                    while len(self._cache) > self.capacity:
                        self._cache.popitem(last=False)
            out.append(entry.hash)
            previous = entry
        return out
