#!/usr/bin/env python3
"""The two statuses whose shared reading is wrong for GitHub.

GitHub spends 403 on two different things: a credential missing a scope, and a
throttle. The shared guidance for 403 says retrying will not change the answer,
which is right for the first and exactly wrong for the second -- an agent told
that gives up on a call that would have succeeded in ten seconds. Telling them
apart means matching throttle wording in prose, which is a heuristic about
GitHub's phrasing and belongs nowhere else. Most forges answer a throttle with
429 and need none of this.

It spends 422 on two as well. `search/issues` answers 422, not 404, for a
repository that does not exist or that the credential cannot see -- so the same
fault reads as `FORGE_NOT_FOUND` through `/repos/{repo}/issues` and as
`FORGE_REJECTED`, "fix the field named in the detail", through the search
route. `issue_list` takes the search route when the caller passes `query` or
`excludeLabels` -- a plain `labels` filter still goes to `/repos/{repo}/issues`
-- and the issue resolver's poll passes `excludeLabels` on every tick, so the
code an operator is shown for a deleted or unreachable repository depended on
which half of the same sweep hit it first.
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


#: What `search/issues` says when the `repo:` qualifier names something it
#: cannot reach. Matched on the phrase and not on the endpoint, because
#: `forge_error` is given a status and a body and not a route -- and because a
#: 422 from any other call really is a bad argument.
_UNSEARCHABLE_MARKERS = (
    "cannot be searched",
    "do not have permission to view",
)


def _forbidden(message: str) -> Guidance | None:
    """429's guidance when a 403 is really a throttle, otherwise the shared one."""
    lowered = message.lower()
    if any(marker in lowered for marker in _THROTTLE_MARKERS):
        return GUIDANCE[429]
    return None


def _rejected(message: str) -> Guidance | None:
    """404's guidance when a 422 is really an unreachable repository.

    Not a cosmetic renaming. `FORGE_REJECTED` tells the caller to fix a field
    it sent, and there is no field to fix: the repository is gone, renamed,
    private to this credential, or outside the App install. `FORGE_NOT_FOUND`
    is the code the SKILL and the scan gate both document for that, and it is
    what the unfiltered route already returns for the same repository.
    """
    lowered = message.lower()
    if any(marker in lowered for marker in _UNSEARCHABLE_MARKERS):
        return GUIDANCE[404]
    return None


ERROR_OVERRIDES = {403: _forbidden, 422: _rejected}
