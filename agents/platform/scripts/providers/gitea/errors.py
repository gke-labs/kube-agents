#!/usr/bin/env python3
"""The two statuses whose shared reading is wrong for Gitea.

401 on Gitea is a stored access token that expired or was revoked. Nothing in
this broker refreshes one: an administrator replaces it in the Secret. The
shared 401 guidance says nothing here will fix it, which is true; this says
what will, and where.

409 is where Gitea answers a second change proposal between the same two
branches (`pull request already exists for these targets`). The shared guidance
for 409 says the state moved underneath the call and to re-read and retry,
which sends a caller around the same loop: the proposal is not going away.
GitHub answers the same fault with 422, and the guidance for that -- fix the
field the detail names -- is the one that is right here too, so the two forges
hand a caller one code for one fault. Any other 409 keeps the shared reading.
"""

from __future__ import annotations

from ..errors import GUIDANCE, Guidance

TOKEN_REFUSED = Guidance(
    401,
    "FORGE_UNAUTHENTICATED",
    "Gitea refused this install's access token: it has expired or been revoked. "
    "Nothing you can do from here will fix it. An administrator replaces the "
    "token in the Secret named by this forge's credentialsRef; the next call "
    "reads the new one, with no restart.",
)

# What Gitea says when the thing a create would make is already there.
_ALREADY_EXISTS_MARKERS = ("already exists", "has already been taken")


def _conflict(message: str) -> Guidance | None:
    """422's guidance when a 409 is a duplicate create, otherwise the shared one."""
    lowered = message.lower()
    if any(marker in lowered for marker in _ALREADY_EXISTS_MARKERS):
        return GUIDANCE[422]
    return None


ERROR_OVERRIDES = {401: TOKEN_REFUSED, 409: _conflict}
