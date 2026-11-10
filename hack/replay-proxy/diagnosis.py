"""Per-message hashes for diagnosing misses, stable across one conversation.

The fingerprint, `settings_hash` and `message_hashes` normalize the request
with its whole context: its time (timestamps become how-far-from-now tokens),
whether a CIDR appears anywhere in the request (IPs then stay literal), and
one mapping, so a placeholder number in the tools depends on the messages. So the same earlier
message can hash differently in a later request of the same conversation, and
the closest-earlier-request search would report a difference that is only
drift. analyze_shadow.py also uses them to decide which assistant messages
the model generated in the conversation (the evidence its replayed estimates
rest on), never as part of a cache key or the replayed key itself.

The proxy remembers when it first saw each raw message (one float per distinct
message, oldest dropped past FIRST_SEEN_CAPACITY). A request's messages are
normalized in order with a fresh mapping; each uses its own first-seen time as
"now", and IPs stay literal only if a CIDR appeared in that message or an
earlier one. The settings (tools, model, parameters) get their own hash with a
fresh mapping, IPs kept literal only for a CIDR in the settings themselves,
and the conversation's first-seen time (its first message's) as "now". This
runs on the proxy's background analysis thread, off the agent's path.

Replay mode also reads the first-seen times to decide a match: before a
lookup, on the request's path, it notes each message's (first_seen_times),
and checks a slot's recorded answer against the slot's prefix normalized as
of when the prefix's last message arrived, as record mode normalized that
request (normalizer.answer_variant).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Tuple

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
        return self._first_seen_digest(_digest(raw), now)

    def _first_seen_digest(self, key: str, now: float) -> float:
        with self._lock:
            seen = self.first_seen.get(key)
            if seen is None:
                self.first_seen[key] = seen = now
                while len(self.first_seen) > self.capacity:
                    self.first_seen.popitem(last=False)
            return seen

    def first_seen_times(self, messages: List[Any], now: float) -> List[float]:
        """When this proxy first saw each message, noting `now` for the ones it has not seen."""
        return self.first_seen_with_digests(messages, now)[1]

    def first_seen_with_digests(self, messages: List[Any], now: float) -> Tuple[List[str], List[float]]:
        """Each message's digest, and when this proxy first saw it (noting `now` for a new one)."""
        digests = [_digest(json.dumps(m, sort_keys=True, ensure_ascii=False)) for m in messages]
        return digests, [self._first_seen_digest(d, now) for d in digests]

    def settings_hash(self, body: Dict[str, Any], context: Dict[str, str], now: float) -> str:
        """The request's settings alone, normalized without any message's context."""
        kept = {k: v for k, v in body.items() if k not in normalizer.KEY_DROP_FIELDS and k != normalizer.MESSAGES_FIELD}
        messages = body.get(normalizer.MESSAGES_FIELD)
        reference = now
        if isinstance(messages, list) and messages:
            reference = self._first_seen(json.dumps(messages[0], sort_keys=True, ensure_ascii=False), now)
        keep_ips = normalizer.CIDR_RE.search(json.dumps(kept, ensure_ascii=False)) is not None
        norm = normalizer.Normalizer(reference, keep_ips)
        settings = {k: norm.walk(kept[k], k) for k in sorted(kept)}
        return normalizer.stable_hash({"settings": settings, "context": context})

    def hashes(self, messages: List[Any], now: float) -> List[str]:
        return self.normalized(messages, now)[0]

    def normalized(self, messages: List[Any], now: float) -> Tuple[List[str], List[Any]]:
        """The diagnosis hashes and the normalized messages they are hashes of."""
        norm = normalizer.Normalizer(now, keep_ips=False)
        hashes, walked = [], []
        for message in messages:
            raw = json.dumps(message, sort_keys=True, ensure_ascii=False)
            norm.now = self._first_seen(raw, now)
            norm.keep_ips = norm.keep_ips or normalizer.CIDR_RE.search(raw) is not None
            value = norm.walk(message)
            walked.append(value)
            hashes.append(normalizer.stable_hash(value)[: normalizer.MESSAGE_HASH_HEX_CHARS])
        return hashes, walked
