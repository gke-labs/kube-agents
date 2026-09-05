#!/usr/bin/env python3
"""What the forge said, and what to do about it.

The reader of these messages is a model deciding its next tool call, not an
operator reading a log, so each one names the cause and then the action that
follows from it. The distinction matters most where the right action differs
while the symptom does not: a rate limit and a missing scope are both HTTP 403,
and an agent told only "the forge refused it" retries the one that will never
succeed and gives up on the one that would have succeeded in ten seconds.
Collapsing every failure into one status is the same bug wearing a number.

The forge's own first line is kept as `detail` underneath. It says which field
was rejected or that the branch has no commits, which no fixed message can, and
the two are answering different questions.

Two things are deliberately not in this module, and both are places a
single-forge design would have put them.

*Recovering the status.* An HTTP transport has it as an integer; a CLI prints
`(HTTP 404)` into its stderr and something has to dig it out with a regex. That
regex is a property of how the call was made rather than of what the forge
answered, so it belongs to the transport.

*Splitting one status by message text.* At least one forge spends 403 on both
a missing scope and a throttle, and telling those apart means matching markers
in prose the forge wrote. A forge that needs it supplies it through
`error_overrides`; a forge that returns a distinct status for throttling --
most of them do -- inherits nothing it has to opt out of.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Union

from workspace_paths import WorkspaceError


@dataclass(frozen=True)
class Guidance:
    """A refusal's status, its stable symbol, and what to do next."""

    status: int
    code: str
    text: str


# An override is either a fixed reading of a status or a function of the forge's
# own message, for the case where one status carries two meanings. Returning
# `None` from the function falls through to the shared table below, so a forge
# only has to describe the case it disagrees about.
Override = Union[Guidance, Callable[[str], "Guidance | None"]]

__all__ = [
    "GUIDANCE",
    "Guidance",
    "Override",
    "UNAVAILABLE",
    "UNRECOGNISED",
    "forge_error",
]


GUIDANCE: dict[int, Guidance] = {
    401: Guidance(
        401,
        "FORGE_UNAUTHENTICATED",
        "The forge rejected this install's credential. It has expired or been "
        "revoked. Nothing you can do from here will fix it -- report it and "
        "stop rather than retrying.",
    ),
    403: Guidance(
        403,
        "FORGE_FORBIDDEN",
        "The forge accepted the credential and refused the operation. The "
        "credential is missing the permission this call needs, or the "
        "repository denies it. Retrying will not change the answer; try a "
        "read-only route, or report what you were denied.",
    ),
    404: Guidance(
        404,
        "FORGE_NOT_FOUND",
        "No such repository, revision, or path. A private repository this "
        "install's credential cannot see also answers 404, so this does not "
        "prove the thing does not exist. Check the spelling and the branch "
        "with `files` or `log` before concluding it is missing.",
    ),
    409: Guidance(
        409,
        "FORGE_CONFLICT",
        "The forge says the state changed underneath this call -- something "
        "else moved the branch or the proposal. Re-read it and try again "
        "against what is there now.",
    ),
    422: Guidance(
        422,
        "FORGE_REJECTED",
        "The forge understood the request and rejected its contents. This is "
        "a bad argument, not a transient failure: fix the field named in the "
        "detail below rather than retrying the same call.",
    ),
    429: Guidance(
        429,
        "FORGE_RATE_LIMITED",
        "The forge is rate-limiting this install. Wait before the next call "
        "and prefer one wide request over many narrow ones -- `files` over a "
        "`show` per path. This will succeed later.",
    ),
}

UNAVAILABLE = Guidance(
    503,
    "FORGE_UNAVAILABLE",
    "The forge is having its own problems -- this call failed on its side, not "
    "on anything you sent. Wait a few minutes and retry the same call "
    "unchanged.",
)

UNRECOGNISED = Guidance(
    502,
    "FORGE_CALL_FAILED",
    "The forge did not answer this call and did not say why in a form this "
    "broker recognises. One retry is reasonable; two is not.",
)


def forge_error(
    status: int,
    detail: str = "",
    overrides: Mapping[int, Override] | None = None,
    message: str | None = None,
) -> WorkspaceError:
    """Turn a forge's refusal into an answer the caller can act on.

    `detail` is the one line the caller is shown underneath the guidance.
    `message` is everything the forge said, which is what an override reads
    when it has to split a status on wording -- the marker that distinguishes
    the two meanings is often not on the first line. It defaults to `detail`.
    """
    full = detail if message is None else message
    detail = detail.strip()[:400]
    chosen = None
    override = (overrides or {}).get(status)
    if isinstance(override, Guidance):
        chosen = override
    elif callable(override):
        chosen = override(full)
    if chosen is None:
        if status >= 500:
            chosen = UNAVAILABLE
        else:
            chosen = GUIDANCE.get(status, UNRECOGNISED)
    return WorkspaceError(
        chosen.text, status=chosen.status, code=chosen.code, detail=detail
    )
