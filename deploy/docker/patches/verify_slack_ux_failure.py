"""Build-time behaviour gate for the KAGE_SLACK_UX failure-reply module.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes`` once
``gateway/slack_ux_failure.py`` is installed and ``apply_slack_ux_failure.py``
has run, with ``slack_presenter.py`` staged beside this script.

Two things are checked:

1. The five calls are in place, read from the parsed tree inside the function
   that must make each: ``build_wake_text`` calls ``note_wake`` after the
   moments wake-text line, ``_process_message_background`` calls ``start``
   after its processing-start hook, ``send_final_ledgered`` brackets its send
   with ``begin`` and ``end``, ``SlackAdapter._maybe_blocks`` hands upstream's
   renamed body to ``maybe_blocks``, and ``_run_agent_queued_followup`` calls
   ``drop``, each importing the module and reading only names bound where
   it runs (``patchlib.unbound``).
2. The module, loaded by path: flag off a failure wake marks nothing; flag on,
   mock 06's reply to a ``gave_up`` wake's turn is drawn with its first
   sentence in bold and one choice button reading "check it there", a second
   reply in the thread is drawn as upstream draws it, a user's turn starting
   after a wake's claim is never marked, a user message that arrived after the
   mark clears it, and the reply a wake's turn sends under a queued follow-up's
   event or the outer user turn's event keeps the look.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import patchlib

RUNTIME = "gateway/slack_ux_failure.py"
FLAG_ENV = "KAGE_SLACK_UX"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_failure"
ALIAS = "_kage_slack_failure"

NOTIFIER = "gateway/kanban_watchers_notifier.py"
NOTE_WAKE = "_kage_slack_failure.note_wake(self.sub, self.wake_kinds, self.synth)"
#: Each file's inserted statements, by the function that must make them.
CALLS = {
    NOTIFIER: {"build_wake_text": (NOTE_WAKE,)},
    "gateway/platforms/base.py": {
        "_process_message_background": ("_kage_slack_failure.start(event)",),
        "send_final_ledgered": (
            "_kage_failure_token = _kage_slack_failure.begin(event)",
            "_kage_slack_failure.end(_kage_failure_token)",
        ),
    },
    "plugins/platforms/slack/adapter.py": {
        "_maybe_blocks": ("return _kage_slack_failure.maybe_blocks(content, self._kage_upstream_maybe_blocks)",),
        "_kage_upstream_maybe_blocks": (),
    },
    "gateway/run_turn.py": {"_run_agent_queued_followup": ("_kage_slack_failure.drop(turn_ctx.source, pending_event)",)},
}
MOMENTS_LINE = "self.synth = _kage_moments_wake_text("

REPLY = (
    "I couldn't find seeded-z. The fleet has seeded-a, -b and -c. checkout-gateway runs on seeded-a. Check it there?"
)
LEAD = "**I couldn't find seeded-z.**"
LABEL = "check it there"
USER_MESSAGE_ID = "1700000001.000200"
#: How much later than the mark a user's message arrives, so the two never tie.
LATER = timedelta(seconds=1)
SUB = {"platform": "slack", "chat_id": "C0KAGE", "thread_id": "1700000000.000100", "task_id": "t_verify"}


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_failure verify: {detail}")


def check_callers(root: Path) -> None:
    for rel, functions in CALLS.items():
        path = root / rel
        tree = ast.parse(path.read_text() if path.is_file() else "")
        # Read from the tree, so a call left only in a comment or a string does not count.
        for function, calls in functions.items():
            made = _statements_in(tree, function)
            if made is None:
                raise _fail(f"{rel} defines no {function}")
            for call in calls:
                if call not in made:
                    raise _fail(f"{rel}'s {function} does not make {call!r}")
        # A call compiles without its import and raises NameError only when it runs.
        if not any(
            isinstance(stmt, ast.ImportFrom)
            and stmt.module == IMPORT_MODULE
            and any(a.name == IMPORT_NAME and a.asname == ALIAS for a in stmt.names)
            for stmt in tree.body
        ):
            raise _fail(f"{rel} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {ALIAS}")
        # An anchor pins the text it replaces, not the names the inserted call reads.
        for stmt in _calling_statements(tree):
            unbound = patchlib.unbound(tree, stmt)
            if unbound:
                raise _fail(f"{rel} calls {ALIAS} with {', '.join(unbound)}, which nothing binds there")
    lines = {
        ast.unparse(stmt): stmt.lineno
        for stmt in ast.walk(ast.parse((root / NOTIFIER).read_text()))
        if isinstance(stmt, (ast.Expr, ast.Assign))
    }
    moments = [line for code, line in lines.items() if code.startswith(MOMENTS_LINE)]
    if not moments or min(moments) > lines[NOTE_WAKE]:
        raise _fail("note_wake does not follow the moments note on the wake")


def _statements_in(tree: ast.Module, function: str) -> "set[str] | None":
    """The simple statements the functions named ``function`` make, as ``ast.unparse``
    writes them, or None when ``tree`` defines no such function."""
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
    ]
    if not found:
        return None
    return {
        ast.unparse(stmt)
        for node in found
        for stmt in ast.walk(node)
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.Return))
    }


def _calling_statements(tree: ast.Module) -> list[ast.stmt]:
    """The simple statements in ``tree`` that call into :data:`ALIAS`."""
    return [
        stmt
        for stmt in ast.walk(tree)
        if isinstance(stmt, (ast.Expr, ast.Assign, ast.Return))
        and any(isinstance(n, ast.Name) and n.id == ALIAS for n in ast.walk(stmt))
    ]


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_failure_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


def _event(internal: bool = True, later: bool = False):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="slack"), chat_id=SUB["chat_id"], thread_id=SUB["thread_id"]
    )
    message_id = None if internal else USER_MESSAGE_ID
    arrived = datetime.now() + (LATER if later else timedelta())
    return SimpleNamespace(internal=internal, source=source, message_id=message_id, text="", timestamp=arrived)


def _started(module):
    """A wake event whose turn has started."""
    event = _event()
    module.start(event)
    return event


def _render(content: str) -> list:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": content}}]


def _draw(module, event) -> list:
    token = module.begin(event)
    try:
        return module.maybe_blocks(REPLY, _render)
    finally:
        module.end(token)


def drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    module.note_wake(SUB, {"gave_up"}, "wake")
    if module._marks:
        raise _fail(f"a failure wake marked its thread with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"
    try:
        module.note_wake(SUB, {"gave_up"}, "wake")
        blocks = _draw(module, _started(module))
        buttons = [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]
        if not blocks[0]["text"]["text"].startswith(LEAD):
            raise _fail(f"the reply's lead is not bold: {blocks[0]!r}")
        pattern = re.compile(module._presenter.CHOICE_ACTION_ID_PATTERN)
        if [b["text"]["text"] for b in buttons] != [LABEL] or not pattern.search(buttons[0]["action_id"]):
            raise _fail(f"the reply's offer was {buttons!r}")
        if _draw(module, _started(module)) != _render(REPLY):
            raise _fail("a second reply in the thread was drawn as the failure's")
        module.note_wake(SUB, {"gave_up"}, "wake")
        _started(module)
        user = _event(internal=False, later=True)
        module.start(user)
        if _draw(module, user) != _render(REPLY):
            raise _fail("a reply to the user's own message was drawn as the failure's")
        module.note_wake(SUB, {"gave_up"}, "wake")
        module.drop(_event().source, _event(internal=False, later=True))
        if _draw(module, _started(module)) != _render(REPLY):
            raise _fail("a queued follow-up's reply was drawn as the failure's")
        outer = _event(internal=False)
        module.note_wake(SUB, {"gave_up"}, "wake")
        module.drop(_event().source, _event())
        carried = _draw(module, outer)
        if not carried[0]["text"]["text"].startswith(LEAD):
            raise _fail("a wake queued behind the user's turn lost the failure's look")
        lane = _event(internal=False)
        lane.message_id, lane.ledger_message_id, lane.text = None, None, ""
        module.note_wake(SUB, {"gave_up"}, "wake")
        _started(module)
        queued = _draw(module, lane)
        if not queued[0]["text"]["text"].startswith(LEAD):
            raise _fail("a wake turn with a follow-up queued behind it lost the failure's look")
    finally:
        os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_callers(root)
    drive(_load_runtime(root))
    print(
        "slack_ux_failure verify: marked from the wake, claimed when its turn starts, "
        "bracketed in the final send, drawn in _maybe_blocks, dropped by a later user "
        "message; a failure reply leads in bold and offers its question once"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
