#!/usr/bin/env python3
"""Build gate for the rate-limit backoff-floor patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_rate_limit_backoff_floor.py``. The applier proves its anchor matched
once; that says nothing about whether the branch runs after every other source
of ``_retry_after``, whether the 429 the worker actually sees reaches it as a
rate limit with no delay, or what the real backoff then returns.

Four things are checked:

1. **Placement.** Parsed out of the patched ``agent/turn_recovery.py``: the
   branch is inside ``compute_error_backoff``, sits after the retryDelay
   patch's branch in the same block and before the ``wait_time`` assignment
   that consumes ``_retry_after``, is gated on ``_retry_after is None`` so
   any named wait still wins, and touches neither the retry count nor
   ``wait_time`` itself.
2. **Composition.** The error shape the worker receives from gemini-4-argon
   on dynamic shared quota, an ``openai.RateLimitError`` whose message is
   LiteLLM's pass-through of the "TPM quota exhausted" body with no
   ``Retry-After`` header and no ``RetryInfo``, is classified ``rate_limit``
   by the real ``agent.error_classifier`` (otherwise ``is_rate_limited`` is
   False and the branch never runs) and yields no delay from the real
   retryDelay parser (otherwise the floor never runs).
3. **The real backoff.** ``compute_error_backoff`` itself, driven with that
   error and a display-only agent, returns a wait inside the floor's bounds;
   a body that names a delay still gets that delay; a ``Retry-After`` header
   still wins; a non-rate-limit error keeps the stock backoff.
4. **Bounds.** Three waits at the floor's minimum outlast a quota minute, and
   the maximum stays under the header path's 600 s cap.

Usage::

    cd /opt/hermes && python3 verify_rate_limit_backoff_floor.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

FAILURES: list[str] = []

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))

#: Cap on the ``Retry-After`` header path in ``turn_recovery.py``.
HEADER_CAP_SECONDS = 600.0

#: A ``Retry-After`` header, to prove it still wins over the floor.
HEADER_RETRY_AFTER_SECONDS = 7

#: The delay a Google ``RetryInfo`` body names, to prove it still wins too.
BODY_DELAY_SECONDS = 54.0

#: How many draws to take from the real backoff; the floor is jittered.
DRAWS = 20

#: Where the requests go, and what the agent calls the model. Only
#: ``compute_error_backoff``'s signature needs them; the values are inert.
BASE_URL = "http://inference-gateway/v1"
MODEL = "gemini"

#: gemini-4-argon's dynamic-shared-quota 429 as LiteLLM passes it through:
#: no RetryInfo, no retry_after field, and (below) no Retry-After header.
ARGON_429_BODY = {
    "error": {
        "message": (
            "litellm.RateLimitError: VertexAIException - "
            '{"error": {"code": 429, "message": "TPM quota exhausted. No spare '
            "capacity is currently available to serve over-limit requests. "
            'Please try again later or use provisioned throughput.", '
            '"status": "RESOURCE_EXHAUSTED"}}'
        ),
        "type": None,
        "param": None,
        "code": "429",
    }
}

RETRY_INFO_429_BODY = {
    "error": {
        "message": (
            "litellm.RateLimitError: VertexAIException - "
            '{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", '
            '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", '
            '"retryDelay": "54s"}]}}'
        ),
        "type": None,
        "param": None,
        "code": "429",
    }
}

# ``ast.unparse`` normalises the spelling; derive it rather than guess it.
FLOOR_BRANCH_TEST = ast.unparse(ast.parse("is_rate_limited and _retry_after is None", mode="eval").body)
RETRY_DELAY_BRANCH_TEST = ast.unparse(ast.parse("is_rate_limited and not _retry_after", mode="eval").body)
BACKOFF_DEF = "compute_error_backoff"
MARKER = "_kube_rate_limit_floor_seconds"
RETRY_DELAY_MARKER = "_kube_retry_delay_from_error"


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

# --- 1. Placement -----------------------------------------------------------
print("backoff-floor branch (agent/turn_recovery.py):")

loop_tree = ast.parse((HERMES / "agent" / "turn_recovery.py").read_text())
branches = [
    node for node in ast.walk(loop_tree) if isinstance(node, ast.If) and ast.unparse(node.test) == FLOOR_BRANCH_TEST
]
check("the branch is present exactly once", len(branches) == 1, f"found {len(branches)}")

enclosing = None
if len(branches) == 1:
    branch = branches[0]
    check("the branch calls the floor", MARKER in ast.unparse(branch))
    for node in ast.walk(loop_tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and branch in block:
                enclosing = block
    check("the branch sits in a statement block", enclosing is not None)
    backoff = next(
        (n for n in ast.walk(loop_tree) if isinstance(n, ast.FunctionDef) and n.name == BACKOFF_DEF),
        None,
    )
    check(
        f"the branch is inside {BACKOFF_DEF}, with is_rate_limited as a parameter",
        backoff is not None
        and branch in list(ast.walk(backoff))
        and "is_rate_limited" in {a.arg for a in backoff.args.args + backoff.args.kwonlyargs},
    )

if enclosing is not None:
    index = enclosing.index(branch)
    before = enclosing[:index]
    after = enclosing[index + 1 :]

    def _assigns(stmts, name):
        return [s for s in stmts if isinstance(s, ast.Assign) and any(ast.unparse(t) == name for t in s.targets)]

    check(
        "the retryDelay branch precedes the floor",
        any(
            isinstance(s, ast.If) and ast.unparse(s.test) == RETRY_DELAY_BRANCH_TEST and RETRY_DELAY_MARKER in ast.unparse(s)
            for s in before
        ),
        "a delay the body names has to be read before the floor decides nothing named one",
    )
    check(
        "the cap and the zero-clear precede the floor",
        any(isinstance(s, ast.If) and "600" in ast.unparse(s) and "_retry_after = None" in ast.unparse(s) for s in before),
        "the floor must see the header path's final answer, not its raw one",
    )
    waits = _assigns(after, "wait_time")
    check(
        "the wait_time that consumes _retry_after follows the floor",
        waits and "_retry_after" in ast.unparse(waits[0].value) and "jittered_backoff" in ast.unparse(waits[0].value),
        f"following wait_time assignments: {[ast.unparse(w) for w in waits]}",
    )
    check(
        "the branch itself does not compute wait_time",
        not _assigns(branch.body, "wait_time"),
        "upstream's assignment is the one that has to read _retry_after",
    )
    check(
        "the branch never widens the retry count",
        "max_retries" not in ast.unparse(branch) and "retry_count" not in ast.unparse(branch),
    )

# --- 2. Composition ---------------------------------------------------------
print("composition with the worker's error shape:")

import httpx  # noqa: E402
import openai  # noqa: E402

from agent.error_classifier import FailoverReason, classify_api_error  # noqa: E402
from hermes_cli.rate_limit_backoff_floor import (  # noqa: E402
    FLOOR_MAX_SECONDS,
    FLOOR_MIN_SECONDS,
    rate_limit_floor_seconds,
)
from hermes_cli.rate_limit_retry_delay import retry_delay_from_error  # noqa: E402


def sdk_error(body, headers=None):
    """An ``openai.RateLimitError`` the way the SDK raises it off a 429."""
    request = httpx.Request("POST", f"{BASE_URL}/chat/completions")
    response = httpx.Response(429, request=request, json=body, headers=headers)
    return openai.RateLimitError(f"Error code: 429 - {body}", response=response, body=body.get("error"))


argon = sdk_error(ARGON_429_BODY)
argon_headers = getattr(getattr(argon, "response", None), "headers", None)
check(
    "the Argon error carries no Retry-After header",
    argon_headers is not None and not argon_headers.get("retry-after") and not argon_headers.get("Retry-After"),
    "then the header path already covered it and this patch is moot",
)
classified = classify_api_error(argon, provider="openai", model=MODEL)
check(
    "the real classifier calls it a rate limit",
    classified.reason == FailoverReason.rate_limit,
    f"classified as {classified.reason!r}; is_rate_limited would be False",
)
check(
    "the retryDelay parser finds nothing in it",
    retry_delay_from_error(argon) is None,
    f"got {retry_delay_from_error(argon)!r}; then the retryDelay patch, not the floor, decides this wait",
)
draws = [rate_limit_floor_seconds() for _ in range(DRAWS)]
check(
    "the floor draws inside its bounds",
    all(FLOOR_MIN_SECONDS <= d <= FLOOR_MAX_SECONDS for d in draws),
    f"draws: {draws}",
)

# --- 3. The real backoff, end to end ----------------------------------------
print("the real compute_error_backoff with the Argon error:")

from agent.turn_recovery import compute_error_backoff  # noqa: E402

#: The retry the error is on, and the ceiling; any values reach the wait.
RETRY_COUNT = 1
MAX_RETRIES = 3


class _QuietAgent:
    """The four things compute_error_backoff asks of the agent; all display."""

    def _buffer_status(self, text):
        pass

    def _emit_status(self, text):
        pass

    def _emit_wait_notice(self, text):
        pass

    def _client_log_context(self):
        return ""


def _wait_for(error, *, is_rate_limited):
    return compute_error_backoff(
        _QuietAgent(), error, retry_count=RETRY_COUNT, max_retries=MAX_RETRIES,
        is_rate_limited=is_rate_limited, is_zai_coding_overload=False,
        base_url=BASE_URL, model=MODEL,
    )


waits = [_wait_for(argon, is_rate_limited=True) for _ in range(DRAWS)]
check(
    "the patched backoff waits the floor on the Argon 429",
    all(FLOOR_MIN_SECONDS <= w <= FLOOR_MAX_SECONDS for w in waits),
    f"waits: {waits}",
)
check(
    "a body that names a delay still gets that delay",
    _wait_for(sdk_error(RETRY_INFO_429_BODY), is_rate_limited=True) == BODY_DELAY_SECONDS,
)
check(
    "a Retry-After header still wins",
    _wait_for(sdk_error(ARGON_429_BODY, headers={"retry-after": str(HEADER_RETRY_AFTER_SECONDS)}), is_rate_limited=True)
    == HEADER_RETRY_AFTER_SECONDS,
)
check(
    "a non-rate-limit error keeps the stock jittered backoff",
    _wait_for(argon, is_rate_limited=False) < FLOOR_MIN_SECONDS,
    f"got {_wait_for(argon, is_rate_limited=False)!r}",
)

# --- 4. Bounds --------------------------------------------------------------
print("bounds:")

check(
    "three waits at the minimum outlast a quota minute",
    3 * FLOOR_MIN_SECONDS >= 60.0,
    f"{3 * FLOOR_MIN_SECONDS} < 60",
)
check(
    "the maximum stays under the Retry-After header cap",
    FLOOR_MAX_SECONDS <= HEADER_CAP_SECONDS,
    f"{FLOOR_MAX_SECONDS} > {HEADER_CAP_SECONDS}",
)


print()
if FAILURES:
    print(f"verify_rate_limit_backoff_floor: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_rate_limit_backoff_floor: all checks passed")
