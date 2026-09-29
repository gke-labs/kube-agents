#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX reactions patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_ux_reactions.py``, with ``slack_presenter.py``
staged beside this script (``/opt/defaults/scripts`` is not populated yet at
that point in the build).

Two things are checked:

1. The adapter. Each hook's first statement after its docstring is the flag
   guard handing over to ``_kage_slack_ux``, and upstream's body still follows
   it, reacting through ``self._react`` — so with the flag off the hook is
   upstream's. The import the guard names is bound at module level.
2. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: flag off it is inert; flag on, an ask gets the arrival reaction for
   its kind, a direct answer settles at once, a delegated one waits for the
   notifier, and no call anywhere is a removal.

Every failure here is silent in production — a reaction that does not land
raises nothing and logs at debug — so the build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_ux_reactions.py"
FLAG_ENV = "KAGE_SLACK_UX"

HOOKS = {
    "on_processing_start": "on_processing_start",
    "on_processing_complete": "on_processing_complete",
}
GUARD_ALIAS = "_kage_slack_ux"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_reactions"
UPSTREAM_HELPER = "_react"

ASK_TS = "1700000000.000100"
CHANNEL = "C0KAGE"
THREAD = "1700000000.000100"
TEAM = "T0KAGE"
MARKER = "marker"
CARD = "t_verify"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_reactions verify: {detail}")


def _is_guard(stmt: ast.stmt, target: str) -> bool:
    """``if _kage_slack_ux.enabled(): return await _kage_slack_ux.<target>(self, ...)``."""
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if not (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "enabled"
        and isinstance(test.func.value, ast.Name)
        and test.func.value.id == GUARD_ALIAS
    ):
        return False
    if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
        return False
    value = stmt.body[0].value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return False
    call = value.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == target
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == GUARD_ALIAS
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "self"
    )


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    hooks = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in HOOKS
    }
    for name, target in HOOKS.items():
        node = hooks.get(name)
        if node is None:
            raise _fail(f"{ADAPTER} has no async def {name}()")
        body = node.body
        if len(body) < 3 or not _is_guard(body[1], target):
            raise _fail(f"{name}() does not open with the {FLAG_ENV} guard after its docstring")
        upstream = ast.Module(body=body[2:], type_ignores=[])
        if not any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == UPSTREAM_HELPER
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
            for call in ast.walk(upstream)
        ):
            raise _fail(f"{name}() no longer runs upstream's reaction body after the guard")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == IMPORT_MODULE
        and any(a.name == IMPORT_NAME and a.asname == GUARD_ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{ADAPTER} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {GUARD_ALIAS}")


class _StubAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._reacting_message_ids = {MARKER}

    def _reacting_target(self, event):
        return (ASK_TS, TEAM, MARKER) if MARKER in self._reacting_message_ids else None

    async def _react(self, channel, ts, emoji, team_id, *, remove):
        self.calls.append((channel, ts, emoji, team_id, remove))
        return True


def _event(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, source=SimpleNamespace(chat_id=CHANNEL, thread_id=THREAD))


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_reactions_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


async def _drive(module) -> None:
    success = SimpleNamespace(value="success")

    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")

    os.environ[FLAG_ENV] = "1"
    boards: list[frozenset] = []

    async def open_cards(chat_id, thread_id, board=None):
        return boards.pop(0)

    module.open_cards = open_cards

    # A direct answer: arrival by kind, then the settle at once.
    adapter = _StubAdapter()
    boards[:] = [frozenset(), frozenset()]
    await module.on_processing_start(adapter, _event("fix it"))
    await module.on_processing_complete(adapter, _event("fix it"), success)
    expected = [
        (CHANNEL, ASK_TS, "hammer_and_wrench", TEAM, False),
        (CHANNEL, ASK_TS, "white_check_mark", TEAM, False),
    ]
    if adapter.calls != expected:
        raise _fail(f"direct answer reacted {adapter.calls!r}, expected {expected!r}")

    # A delegated answer: nothing at completion; the notifier's terminal event settles it.
    adapter = _StubAdapter()
    boards[:] = [frozenset(), frozenset({CARD})]
    await module.on_processing_start(adapter, _event("is seeded-a healthy?"))
    await module.on_processing_complete(adapter, _event("is seeded-a healthy?"), success)
    sub = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": CARD}
    await module.settle_delegated(adapter, sub, "completed")
    expected = [
        (CHANNEL, ASK_TS, "eyes", TEAM, False),
        (CHANNEL, ASK_TS, "white_check_mark", TEAM, False),
    ]
    if adapter.calls != expected:
        raise _fail(f"delegated answer reacted {adapter.calls!r}, expected {expected!r}")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_adapter(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_reactions verify: both hooks guarded ahead of upstream's body; "
        "runtime reacts by kind, settles direct answers, defers delegated ones, never removes"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
