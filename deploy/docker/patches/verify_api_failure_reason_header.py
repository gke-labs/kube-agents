#!/usr/bin/env python3
"""Build gate for the API-server failure-reason header patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_api_failure_reason_header.py``. The applier proves its anchor matched
once; this proves the inserted statement behaves: located with ``ast`` as the
``if`` that follows the ``response_headers = {...}`` assignment in the
chat-completions handler, and executed with a stand-in ``result``, a
``rate_limit`` failure sets the header to ``rate_limit``, a reason with a
newline in its cause is reduced to a safe header value, and a result with no
reason leaves the header unset.

Usage::

    cd /opt/hermes && python3 verify_api_failure_reason_header.py
"""

from __future__ import annotations

import ast
import os
import sys
import textwrap
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
ROUTES_RELATIVE = "gateway/platforms/api_server_openai_routes.py"
MARKER = "kube-agents patch: api_failure_reason_header"
HEADER = "X-Hermes-Failure-Reason"
FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _is_headers_assign(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "response_headers"
            and isinstance(node.value, ast.Dict) and node.value.keys):
        return False
    key = node.value.keys[0]
    names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
    return isinstance(key, ast.Constant) and key.value == "X-Hermes-Session-Id" and "provided_session_id" in names


def inserted_block(source: str) -> str:
    """The statement after the patched ``response_headers`` assignment, dedented."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(f"{ROUTES_RELATIVE} does not parse: {exc}")
        return ""
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for i, node in enumerate(body):
            if _is_headers_assign(node):
                if i + 1 >= len(body) or not isinstance(body[i + 1], ast.If):
                    fail("no `if` follows the response_headers assignment; the patch is missing or moved")
                    return ""
                nxt = body[i + 1]
                lines = source.splitlines(keepends=True)
                block = textwrap.dedent("".join(lines[nxt.lineno - 1 : nxt.end_lineno]))
                if MARKER not in block:
                    fail("the patch marker is not on the statement after the response_headers assignment")
                    return ""
                return block
    fail("the chat-completions response_headers assignment was not found; the handler moved")
    return ""


def run_block(block: str, result) -> dict | None:
    headers: dict = {}
    try:
        exec(compile(block, "<failure-reason-header>", "exec"), {"result": result, "response_headers": headers})
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"the inserted statement raised when run: {exc!r}")
        return None
    return headers


def main() -> int:
    path = HERMES / ROUTES_RELATIVE
    source = path.read_text()
    block = inserted_block(source)
    if block:
        cases = [
            ({"failed": True, "failure_reason": "rate_limit"}, "rate_limit", "a rate-limit failure"),
            ({"failed": True, "failure_reason": "billing"}, "billing", "a billing failure"),
            ({"failed": True, "failure_reason": "session_persistence_failed:disk\nfull"},
             "session_persistence_failed:diskfull", "a reason whose cause holds a newline"),
            ({"failed": True}, None, "a failure with no reason"),
            ({"final_response": "ok"}, None, "a turn that did not fail"),
        ]
        for result, want, what in cases:
            got = run_block(block, result)
            if got is not None and got.get(HEADER) != want:
                fail(f"{what} sets {HEADER}={got.get(HEADER)!r}, want {want!r}")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_api_failure_reason_header: {f}", file=sys.stderr)
        return 1
    print("verify_api_failure_reason_header: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
