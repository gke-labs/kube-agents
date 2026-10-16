# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Whether the Hermes gateway would post a reply, decided the way the gateway does.

Vendored from hermes-agent v2026.9.14, ``gateway/response_filters.py`` lines
15-56 (``is_intentional_silence_response`` and its helpers; MIT License,
Copyright (c) 2025 Nous Research), so a case grades what the user would see.
Bump it with the image's hermes pin. A blank reply is not silence: the gateway
posts an empty-response warning for it instead.
"""

from __future__ import annotations

import unicodedata
from typing import Any

LIVE_GATEWAY_SILENT_MARKERS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})

# Longer than any marker could plausibly be, even with stray punctuation.
_MARKER_LENGTH_CAP = 64

# Edge characters kept even though Unicode files them as punctuation, so a
# malformed ``[SILENT`` cannot become ``SILENT``.
_STRUCTURAL = "[]"
_PUNCTUATION_CATEGORY = "P"


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _is_edge_punctuation(ch: str) -> bool:
    return ch not in _STRUCTURAL and unicodedata.category(ch).startswith(_PUNCTUATION_CATEGORY)


def _strip_edge_silence_punctuation(text: str) -> str:
    start, end = 0, len(text)
    while start < end and _is_edge_punctuation(text[start]):
        start += 1
    while end > start and _is_edge_punctuation(text[end - 1]):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: Any) -> tuple[str, ...]:
    stripped = text.strip() if isinstance(text, str) else ""
    if not 0 < len(stripped) <= _MARKER_LENGTH_CAP:
        return ()
    depunctuated = _strip_edge_silence_punctuation(stripped)
    forms = (stripped,) if depunctuated == stripped else (stripped, depunctuated)
    return tuple(_canonical_silence_candidate(f) for f in forms)


def is_intentional_silence_response(response: Any) -> bool:
    """True only when ``response`` is exactly a silence marker the gateway suppresses."""
    return any(c in LIVE_GATEWAY_SILENT_MARKERS for c in _canonical_silence_candidates(response))
