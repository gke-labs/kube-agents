"""Per-message hashes for diagnosing misses, stable across one conversation.

The fingerprint and `message_hashes` normalize every message with the whole
request's context: its time (timestamps become how-far-from-now tokens) and
whether any message names a CIDR (IPs then stay literal). So the same earlier
message can hash differently in a later request of the same conversation, and
the closest-earlier-request search would report a difference that is only
drift. These hashes are for that search only; they never decide a match.

The proxy remembers when it first saw each raw message (one float per distinct
message, oldest dropped past FIRST_SEEN_CAPACITY). A request's messages are
normalized in order with a fresh mapping; each uses its own first-seen time as
"now", and IPs stay literal only if a CIDR appeared in that message or an
earlier one. This runs after the response has gone back, off the agent's path.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, List

import normalizer

FIRST_SEEN_CAPACITY = 1_000_000
TEXT_ENCODING = "utf-8"
# A lone surrogate is valid in a JSON string but not in UTF-8.
ENCODING_ERRORS = "surrogatepass"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode(TEXT_ENCODING, ENCODING_ERRORS)).hexdigest()


class DiagnosisHasher:
    def __init__(self, capacity: int = FIRST_SEEN_CAPACITY):
        self.capacity = capacity
        self.first_seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def _first_seen(self, raw: str, now: float) -> float:
        key = _digest(raw)
        with self._lock:
            seen = self.first_seen.get(key)
            if seen is None:
                self.first_seen[key] = seen = now
                while len(self.first_seen) > self.capacity:
                    self.first_seen.popitem(last=False)
            return seen

    def hashes(self, messages: List[Any], now: float) -> List[str]:
        norm = normalizer.Normalizer(now, keep_ips=False)
        out = []
        for message in messages:
            raw = json.dumps(message, sort_keys=True, ensure_ascii=False)
            norm.now = self._first_seen(raw, now)
            norm.keep_ips = norm.keep_ips or normalizer.CIDR_RE.search(raw) is not None
            out.append(normalizer.stable_hash(norm.walk(message))[: normalizer.MESSAGE_HASH_HEX_CHARS])
        return out
