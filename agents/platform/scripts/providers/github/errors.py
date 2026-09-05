#!/usr/bin/env python3
"""The one status whose shared reading is wrong for GitHub.

GitHub spends 403 on two different things: a credential missing a scope, and a
throttle. The shared guidance for 403 says retrying will not change the answer,
which is right for the first and exactly wrong for the second -- an agent told
that gives up on a call that would have succeeded in ten seconds. Telling them
apart means matching throttle wording in prose, which is a heuristic about
GitHub's phrasing and belongs nowhere else. Most forges answer a throttle with
429 and need none of this.
"""

from __future__ import annotations

from ..errors import GUIDANCE, Guidance

_THROTTLE_MARKERS = (
    "rate limit",
    "ratelimit",
    "abuse detection",
    "secondary rate",
    "retry-after",
    "too many requests",
)


def _forbidden(message: str) -> Guidance | None:
    """429's guidance when a 403 is really a throttle, otherwise the shared one."""
    lowered = message.lower()
    if any(marker in lowered for marker in _THROTTLE_MARKERS):
        return GUIDANCE[429]
    return None


ERROR_OVERRIDES = {403: _forbidden}
