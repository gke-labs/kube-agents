#!/usr/bin/env python3
"""Build gate for the unedited ``message_changed`` patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_slack_unedited_change.py``. The applier proves its anchor matched once;
this proves the patched ``_normalize_changed_message`` behaves, executed from
the real ``adapter.py`` text against a stand-in adapter whose processed-ts map
is empty, as it is after a restart:

- a thread parent's change with reply metadata and no ``edited`` returns None;
- so does one for a parent edited earlier, whose ``edited.ts`` is unchanged
  from ``previous_message``'s;
- a user's edit, a second edit, and an edit with no ``previous_message`` still
  return the normalized message;
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
#: What run() returns when the method raised; the failure is already recorded.
RAISED = object()

ROOT_TS = "1792165462.838609"
EDIT_TS = "1792180249.000100"
EARLIER_EDIT = {"user": "U1", "ts": "1792165470.000001"}

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

#: The same change for a parent the user edited once, long before: both sides keep that `edited`.
EDITED_PARENT_METADATA_CHANGE = {
    **PARENT_METADATA_CHANGE,
    "message": {**PARENT_METADATA_CHANGE["message"], "edited": EARLIER_EDIT},
    "previous_message": {**PARENT_METADATA_CHANGE["message"], "reply_count": 3, "edited": EARLIER_EDIT},
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
    "previous_message": {"text": "is checkout-gateway healthy in seeded-z?", "user": "U1", "ts": ROOT_TS},
}

#: A second edit of a message edited before: `edited` moves on.
SECOND_EDIT = {**USER_EDIT, "previous_message": {**USER_EDIT["previous_message"], "edited": EARLIER_EDIT}}

#: A user's edit delivered without previous_message: `edited` alone decides.
BARE_EDIT = {k: v for k, v in USER_EDIT.items() if k != "previous_message"}

#: A user's edit whose previous_message is malformed: treated as absent, not raised on.
MALFORMED_PREVIOUS_EDIT = {**USER_EDIT, "previous_message": "garbled"}


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
        return RAISED


def main() -> int:
    text = method_source((HERMES / RELATIVE).read_text())
    if text:
        dropped = [
            (PARENT_METADATA_CHANGE, (), "a thread parent's metadata change with no `edited` is routed as a message"),
            (EDITED_PARENT_METADATA_CHANGE, (), "a metadata change on a parent edited earlier is routed as a message"),
            (USER_EDIT, (ROOT_TS,), "an edit to a message already routed to the agent is routed again"),
        ]
        for event, processed, why in dropped:
            got = run(text, event, processed)
            if got is not None and got is not RAISED:
                fail(why)
        for event, what in ((USER_EDIT, "a user's edit"), (SECOND_EDIT, "a second edit"),
                            (BARE_EDIT, "an edit with no previous_message"),
                            (MALFORMED_PREVIOUS_EDIT, "an edit with a malformed previous_message")):
            got = run(text, event)
            if got is not RAISED and (not isinstance(got, dict) or got.get("ts") != ROOT_TS):
                fail(f"{what} is not routed as the edited message: {got!r}")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_slack_unedited_change: {f}", file=sys.stderr)
        return 1
    print("verify_slack_unedited_change: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
