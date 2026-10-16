"""Build-time behaviour gate for the KAGE_SLACK_UX failure-reply module.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes`` once
``gateway/slack_ux_failure.py`` is installed and ``apply_slack_ux_failure.py``
has run, with ``slack_presenter.py`` staged beside this script.

Two things are checked:

1. The five calls are in place, read from the parsed tree as the statement
   right after its upstream anchor, in the same block of the function that
   must make it: ``build_wake_text`` calls ``note_wake`` after the moments
   wake-text line, ``_process_message_background`` calls ``start`` after its
   processing-start hook, ``send_final_ledgered`` calls ``begin`` and then
   sends inside a ``try`` whose ``finally`` calls ``end``,
   ``SlackAdapter._maybe_blocks`` opens by handing upstream's renamed body to
   ``maybe_blocks``, and ``_run_agent_queued_followup`` calls ``drop`` after
   its processing-start hook, each importing the module and reading only
   names bound where it runs (``patchlib.unbound``).
2. The module, loaded by path: flag off a failure wake marks nothing; flag on,
   mock 06's reply to a ``gave_up`` wake's turn is drawn with its first
   sentence in bold and one choice button reading "Try again", a second
   reply in the thread is drawn as upstream draws it, a user's turn starting
   after a wake's claim is never marked, a user message that arrived after the
   mark clears it, and the reply a wake's turn sends under a queued follow-up's
   event or the outer user turn's event keeps the look.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import patchlib

RUNTIME = "gateway/slack_ux_failure.py"
FLAG_ENV = "KAGE_SLACK_UX"
#: Unset is on, so the flag-off path needs a value.
FLAG_OFF = "false"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_failure"
ALIAS = "_kage_slack_failure"

ADAPTER_CLASS = "SlackAdapter"
#: The send ``begin`` must sit right above, with ``end`` in its ``finally``.
BRACKETED_SEND = (
    "try:\n"
    "    result = await delivery_adapter._send_with_retry(chat_id=event.source.chat_id, "
    "content=text_content, reply_to=reply_to, metadata=metadata)\n"
    "finally:\n"
    "    _kage_slack_failure.end(_kage_failure_token)"
)
#: Each file's inserted statements, by the function that must make them (``Class.name`` for a
#: method looked up on that class only), as ``(anchor, statement)`` source pairs: the statement
#: comes right after the anchor in the same block, or first in the body when the anchor is None.
PLACED = {
    "gateway/kanban_watchers_notifier.py": {
        "build_wake_text": (
            (
                "self.synth = _kage_moments_wake_text(self.sub, self.d['events'], self.wake_kinds, self.synth)",
                "_kage_slack_failure.note_wake(self.sub, self.wake_kinds, self.synth)",
            ),
        ),
    },
    "gateway/platforms/base.py": {
        "_process_message_background": (
            ("await self._run_processing_hook('on_processing_start', event)", "_kage_slack_failure.start(event)"),
        ),
        "send_final_ledgered": (("_kage_failure_token = _kage_slack_failure.begin(event)", BRACKETED_SEND),),
    },
    "plugins/platforms/slack/adapter.py": {
        f"{ADAPTER_CLASS}._maybe_blocks": (
            (None, "return _kage_slack_failure.maybe_blocks(content, self._kage_upstream_maybe_blocks)"),
        ),
        f"{ADAPTER_CLASS}._kage_upstream_maybe_blocks": (),
    },
    "gateway/run_turn.py": {
        "_run_agent_queued_followup": (
            (
                "await _run_followup_processing_hook(_hook_adapter, pending_event, 'on_processing_start')",
                "_kage_slack_failure.drop(turn_ctx.source, pending_event)",
            ),
        ),
    },
}
FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
BLOCK_FIELDS = ("body", "orelse", "finalbody")

REPLY = (
    "I couldn't find seeded-z. The fleet has seeded-a, -b and -c. checkout-gateway runs on seeded-a. Check it there?"
)
LEAD = "**I couldn't find seeded-z.**"
LABEL = "Try again"
USER_MESSAGE_ID = "1700000001.000200"
#: How much later than the mark a user's message arrives, so the two never tie.
LATER = timedelta(seconds=1)
SUB = {"platform": "slack", "chat_id": "C0KAGE", "thread_id": "1700000000.000100", "task_id": "t_verify"}


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_failure verify: {detail}")


def check_callers(root: Path) -> None:
    for rel, functions in PLACED.items():
        path = root / rel
        tree = ast.parse(path.read_text() if path.is_file() else "")
        # Read from the tree, so a call left only in a comment or a string does not count.
        for function, pairs in functions.items():
            defs = _defs(tree, function)
            if not defs:
                raise _fail(f"{rel} defines no {function}")
            for anchor, call in pairs:
                anchor, call = anchor and _code(anchor), _code(call)
                if not any(_placed(node, anchor, call) for node in defs):
                    where = f"right after {anchor!r}" if anchor else "first"
                    raise _fail(f"{rel}'s {function} does not make {call!r} {where}")
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


def _code(source: str) -> str:
    """``source``'s one statement as ``ast.unparse`` writes it."""
    return ast.unparse(ast.parse(source).body[0])


def _defs(tree: ast.Module, function: str) -> list:
    """The defs named ``function`` anywhere in ``tree``, or, for ``Class.name``, on that class only."""
    owner, _, name = function.rpartition(".")
    if owner:
        nodes = [n for c in tree.body if isinstance(c, ast.ClassDef) and c.name == owner for n in c.body]
    else:
        nodes = list(ast.walk(tree))
    return [n for n in nodes if isinstance(n, FUNCTION_DEFS) and n.name == name]


def _blocks(node: ast.AST):
    """Each statement list ``node`` runs, nested blocks included and nested defs not."""
    for field in BLOCK_FIELDS:
        block = getattr(node, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            yield block
            for stmt in block:
                if not isinstance(stmt, (*FUNCTION_DEFS, ast.ClassDef)):
                    yield from _blocks(stmt)
    for inner in (*getattr(node, "handlers", ()), *getattr(node, "cases", ())):
        yield from _blocks(inner)


def _placed(node: ast.AST, anchor: str | None, call: str) -> bool:
    """Whether ``call`` is ``node``'s first statement (no anchor) or right after ``anchor`` in one block."""
    if anchor is None:
        return ast.unparse(node.body[0]) == call
    for block in _blocks(node):
        code = [ast.unparse(stmt) for stmt in block]
        if (anchor, call) in itertools.pairwise(code):
            return True
    return False


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
    os.environ[FLAG_ENV] = FLAG_OFF
    module.note_wake(SUB, {"gave_up"}, "wake")
    if module._marks:
        raise _fail(f"a failure wake marked its thread with {FLAG_ENV}={FLAG_OFF}")
    os.environ.pop(FLAG_ENV, None)
    if not module.enabled():
        raise _fail(f"enabled() is false with {FLAG_ENV} unset, which is on")
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
