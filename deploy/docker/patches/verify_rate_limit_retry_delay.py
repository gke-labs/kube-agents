#!/usr/bin/env python3
"""Build gate for the rate-limit retryDelay patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_rate_limit_retry_delay.py``. The applier proves its anchor matched
once; that says nothing about whether the branch sits where ``_retry_after``
is decided, whether the error the worker actually sees reaches it as a rate
limit, or whether the parser reads that error.

Three things are checked:

1. **Placement.** Parsed out of the patched ``agent/conversation_loop.py``: the
   branch is inside the block that assigns ``_retry_after = None``, runs after
   the header read and before the ``wait_time`` assignment that consumes it,
   and is gated on ``not _retry_after`` so a header still wins.
2. **Composition.** The error shape the worker receives during a storm, an
   ``openai.RateLimitError`` whose message is LiteLLM's pass-through of a Google
   ``RetryInfo`` body with no ``Retry-After`` header, is classified
   ``rate_limit`` by the real ``agent.error_classifier`` (otherwise
   ``is_rate_limited`` is False and the branch never runs) and yields the body's
   delay from the real parser.
3. **Bounds.** The cap matches the header path's 600 s, and an error without a
   delay yields ``None`` so the stock backoff still applies.

Usage::

    cd /opt/hermes && python3 verify_rate_limit_retry_delay.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

FAILURES: list[str] = []

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))

#: The delay Google quoted throughout the 2026-09-03 storm.
STORM_DELAY_SECONDS = 54.0

#: Cap on the ``Retry-After`` header path in ``conversation_loop.py``.
HEADER_CAP_SECONDS = 600.0

GOOGLE_429_BODY = {
    "error": {
        "message": (
            "litellm.RateLimitError: VertexAIException - "
            '{"error": {"code": 429, "message": "Resource exhausted. '
            'Please try again later.", "status": "RESOURCE_EXHAUSTED", '
            '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
            '"retryDelay": "54s"}]}}'
        ),
        "type": None,
        "param": None,
        "code": "429",
    }
}

PLAIN_429_BODY = {
    "error": {"message": "Rate limit reached", "type": None, "code": "429"}
}

HUGE_DELAY_BODY = {
    "error": {"message": '{"details": [{"retryDelay": "99999s"}]}', "code": "429"}
}

# ``ast.unparse`` parenthesises the ``not``; derive the spelling rather than
# guess it so the comparison survives a Python bump.
BRANCH_TEST = ast.unparse(
    ast.parse("is_rate_limited and not _retry_after", mode="eval").body
)
HEADER_TEST = "is_rate_limited"
MARKER = "_kube_retry_delay_from_error"


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))


# --- 1. Placement -----------------------------------------------------------
print("retryDelay branch (agent/conversation_loop.py):")

loop_tree = ast.parse((HERMES / "agent" / "conversation_loop.py").read_text())

branches = [
    node
    for node in ast.walk(loop_tree)
    if isinstance(node, ast.If) and ast.unparse(node.test) == BRANCH_TEST
]
check(
    "the branch is present exactly once",
    len(branches) == 1,
    f"found {len(branches)}",
)

enclosing = None
if len(branches) == 1:
    branch = branches[0]
    check(
        "the branch calls the parser",
        MARKER in ast.unparse(branch),
    )
    for node in ast.walk(loop_tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and branch in block:
                enclosing = block
    check("the branch sits in a statement block", enclosing is not None)

if enclosing is not None:
    index = enclosing.index(branch)
    before = enclosing[:index]
    after = enclosing[index + 1 :]

    def _assigns(stmts, name):
        return [
            s
            for s in stmts
            if isinstance(s, ast.Assign)
            and any(ast.unparse(t) == name for t in s.targets)
        ]

    check(
        "_retry_after is initialised before the branch",
        any(ast.unparse(s.value) == "None" for s in _assigns(before, "_retry_after")),
        "the branch would read an unbound name on a non-rate-limit error",
    )
    check(
        "the Retry-After header is read before the branch",
        any(
            isinstance(s, ast.If) and ast.unparse(s.test) == HEADER_TEST
            for s in before
        ),
        "the header has to win; the branch is gated on its answer",
    )
    waits = _assigns(after, "wait_time")
    check(
        "the wait_time that consumes _retry_after follows the branch",
        waits
        and "_retry_after" in ast.unparse(waits[0].value)
        and "jittered_backoff" in ast.unparse(waits[0].value),
        f"following wait_time assignments: {[ast.unparse(w) for w in waits]}",
    )
    check(
        "the branch itself does not compute wait_time",
        not _assigns(branch.body, "wait_time"),
        "upstream's assignment is the one that has to read _retry_after",
    )
    check(
        "the branch never widens the retry count",
        "max_retries" not in ast.unparse(branch)
        and "retry_count" not in ast.unparse(branch),
    )


# --- 2. Composition ---------------------------------------------------------
print("composition with the worker's error shape:")

import httpx  # noqa: E402
import openai  # noqa: E402

from agent.error_classifier import FailoverReason, classify_api_error  # noqa: E402
from hermes_cli.rate_limit_retry_delay import (  # noqa: E402
    RETRY_DELAY_CAP_SECONDS,
    retry_delay_from_error,
    retry_delay_from_text,
)


def sdk_error(body):
    """An ``openai.RateLimitError`` the way the SDK raises it off a 429."""
    request = httpx.Request("POST", "http://litellm/v1/chat/completions")
    response = httpx.Response(429, request=request, json=body)
    return openai.RateLimitError(
        f"Error code: 429 - {body}", response=response, body=body.get("error")
    )


storm = sdk_error(GOOGLE_429_BODY)
headers = getattr(getattr(storm, "response", None), "headers", None)
check(
    "the storm error carries no Retry-After header",
    headers is not None
    and not headers.get("retry-after")
    and not headers.get("Retry-After"),
    "then the header path already covered it and this patch is moot",
)
classified = classify_api_error(storm, provider="openai", model="gemini")
check(
    "the real classifier calls it a rate limit",
    classified.reason == FailoverReason.rate_limit,
    f"classified as {classified.reason!r}; is_rate_limited would be False",
)
check(
    "the parser reads the delay out of the SDK error",
    retry_delay_from_error(storm) == STORM_DELAY_SECONDS,
    f"got {retry_delay_from_error(storm)!r}",
)
check(
    "the delay is also legible in the plain string the loop logs",
    retry_delay_from_text(str(storm)) == STORM_DELAY_SECONDS,
)


class _Wrapper(Exception):
    pass


try:
    raise _Wrapper("provider call failed") from storm
except _Wrapper as wrapped:
    check(
        "a wrapped error is read through its cause chain",
        retry_delay_from_error(wrapped) == STORM_DELAY_SECONDS,
    )


# --- 3. Bounds --------------------------------------------------------------
print("bounds:")

check(
    "the cap matches the Retry-After header path",
    RETRY_DELAY_CAP_SECONDS == HEADER_CAP_SECONDS,
    f"{RETRY_DELAY_CAP_SECONDS} != {HEADER_CAP_SECONDS}",
)
check(
    "a pathological delay is capped",
    retry_delay_from_error(sdk_error(HUGE_DELAY_BODY)) == HEADER_CAP_SECONDS,
)
check(
    "a 429 without a delay leaves the stock backoff in charge",
    retry_delay_from_error(sdk_error(PLAIN_429_BODY)) is None,
)
check(
    "a non-error value never raises",
    retry_delay_from_error(None) is None and retry_delay_from_error(object()) is None,
)


print()
if FAILURES:
    print(f"verify_rate_limit_retry_delay: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_rate_limit_retry_delay: all checks passed")
