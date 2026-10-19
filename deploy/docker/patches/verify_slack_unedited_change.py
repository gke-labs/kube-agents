#!/usr/bin/env python3
"""Build gate for the unedited ``message_changed`` patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_slack_unedited_change.py``. The applier proves its anchor matched once;
this proves the patched ``_normalize_changed_message`` behaves, executed from
the real ``adapter.py`` text against a stand-in adapter whose processed-ts map
is empty, as it is after a restart:

- a thread parent's change with reply metadata and no ``edited`` returns None;
- a user's edit (``edited`` present) still returns the normalized message;
- a change to a message already routed to the agent still returns None.

The method is located with ``ast``, so an upstream move is reported, never a
traceback out of a build.

Usage::

    cd /opt/hermes && python3 verify_slack_unedited_change.py
"""

from __future__ import annotations

import ast
import os
import sys
import textwrap
from pathlib import Path
from typing import Optional

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
RELATIVE = "plugins/platforms/slack/adapter.py"
METHOD = "_normalize_changed_message"
MARKER = "kube-agents patch: slack_unedited_change"
FAILURES: list[str] = []

ROOT_TS = "1792165462.838609"
EDIT_TS = "1792180249.000100"

#: Slack's change to a thread parent when a reply in the thread is updated.
PARENT_METADATA_CHANGE = {
    "subtype": "message_changed",
    "channel": "C1",
    "ts": "1792180249.000200",
    "message": {
        "text": "<@UBOT> is checkout-gateway healthy in seeded-z?",
        "user": "U1",
        "ts": ROOT_TS,
        "thread_ts": ROOT_TS,
        "reply_count": 4,
        "latest_reply": "1792180249.000150",
    },
}

#: A user editing an @mention into their message.
USER_EDIT = {
    "subtype": "message_changed",
    "channel": "C1",
    "ts": EDIT_TS,
    "message": {
        "text": "<@UBOT> is checkout-gateway healthy in seeded-z?",
        "user": "U1",
        "ts": ROOT_TS,
        "edited": {"user": "U1", "ts": EDIT_TS},
    },
}


def fail(msg: str) -> None:
    FAILURES.append(msg)


def method_source(source: str) -> str:
    """The method's text, dedented to module level; empty after recording why."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(f"{RELATIVE} does not parse: {exc}")
        return ""
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == METHOD]
    if len(found) != 1:
        fail(f"expected one {METHOD}() in {RELATIVE}, found {len(found)}; the method moved")
        return ""
    node = found[0]
    lines = source.splitlines(keepends=True)
    text = textwrap.dedent("".join(lines[node.lineno - 1 : node.end_lineno]))
    if MARKER not in text:
        fail(f"the patch marker is not inside {METHOD}(); the guard is missing or moved")
    return text


class _Adapter:
    def __init__(self, processed=()):
        self._processed_message_ts = {ts: 0.0 for ts in processed}


def run(text: str, event: dict, processed=()):
    ns: dict = {"Optional": Optional}
    try:
        exec(compile(text, f"<{METHOD}>", "exec"), ns)  # noqa: S102 -- the method under test, from the image
        return ns[METHOD](_Adapter(processed), event)
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"{METHOD}() raised when run: {exc!r}")
        return "raised"


def main() -> int:
    text = method_source((HERMES / RELATIVE).read_text())
    if text:
        got = run(text, PARENT_METADATA_CHANGE)
        if got is not None and got != "raised":
            fail("a thread parent's metadata change with no `edited` is routed as a message")
        got = run(text, USER_EDIT)
        if not isinstance(got, dict) or got.get("ts") != ROOT_TS:
            fail(f"a user's edit is not routed as the edited message: {got!r}")
        got = run(text, USER_EDIT, processed=(ROOT_TS,))
        if got is not None and got != "raised":
            fail("an edit to a message already routed to the agent is routed again")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_slack_unedited_change: {f}", file=sys.stderr)
        return 1
    print("verify_slack_unedited_change: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
