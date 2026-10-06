"""Name a failed API-server turn's ``failure_reason`` in a response header.

One anchored insertion in ``gateway/platforms/api_server_openai_routes.py``'s
non-streaming chat-completions handler. Upstream classifies every failed turn
(``rate_limit``, ``billing``, a tool error, ...) into ``result["failure_reason"]``
but answers the client with only ``failed``/``partial``/``completed`` and the
error text, so a client cannot tell a turn that gave up on the provider's rate
limit from one that failed on its own. The Hermes bridge's API executor
(``a2a/hermes-bridge``) needs exactly that line: the subprocess executor reads it
from exit 75 (``apply_quiet_rate_limit_exit.py``) and names the task
``hermes-rate-limited``, which the eval harness classes as infrastructure.

The insertion follows the line that builds ``response_headers``, which both the
502 and the 200 answer send, and adds ``X-Hermes-Failure-Reason`` when the
result carries a reason, reduced to ASCII letters, digits and ``_:.-`` and
bounded, since the reason can carry a cause suffix. Nothing else in the response
changes; the marker is the trailing comment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

ROUTES_RELATIVE = "gateway/platforms/api_server_openai_routes.py"

HEADERS_ANCHOR = (
    '        response_headers = {"X-Hermes-Session-Id": (provided_session_id or result.get("session_id", session_id))}\n'
)

MARKER = "kube-agents patch: api_failure_reason_header"

FAILURE_REASON_HEADER = "X-Hermes-Failure-Reason"

HEADERS_REPLACEMENT = HEADERS_ANCHOR + (
    f'        if isinstance(result, dict) and result.get("failure_reason"):  # {MARKER}\n'
    f'            response_headers["{FAILURE_REASON_HEADER}"] = "".join(\n'
    '                ch for ch in str(result["failure_reason"])\n'
    '                if ch.isascii() and (ch.isalnum() or ch in "_:.-"))[:64]\n'
)


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, ROUTES_RELATIVE, prefix="api-failure-reason-header")
    patch.refuse_if_patched(MARKER)
    patch.substitute(HEADERS_ANCHOR, HEADERS_REPLACEMENT, label="chat-completions response headers")
    patch.commit("a failed API-server turn names its failure_reason in X-Hermes-Failure-Reason")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
