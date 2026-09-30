#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX status patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
immediately after ``apply_slack_ux_status.py``, with ``slack_presenter.py``
and ``slack_status.py`` staged beside this script (``/opt/defaults/scripts``
is not populated yet at that point in the build).

Two things are checked:

1. The adapter. ``_set_thread_status``'s first statement after its docstring
   is the flag guard handing over to ``_kage_slack_status``, and upstream's
   body still follows it, so with the flag off the setter is upstream's. The
   message-event builder calls ``note_ask``, and the import the guard names is
   bound at module level.
2. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: flag off it is inert; flag on, Hermes's phrase reaches Slack as
   ``processing`` once, the clear as ``closed``, the ask becomes the session
   title, and a card's notes post one plan, edit it, and settle it.

A refused status or plan raises nothing and logs at debug, so the build is
where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_ux_status.py"
FLAG_ENV = "KAGE_SLACK_UX"

SETTER = "_set_thread_status"
BUILDER = "_build_message_event"
GUARD_ALIAS = "_kage_slack_status"
GUARD_TARGET = "set_thread_status"
NOTE_TARGET = "note_ask"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_status"
UPSTREAM_RESOLVER = "_session_status_method"

CHANNEL = "C0KAGE"
THREAD = "1700000000.000100"
TEAM = "T0KAGE"
CARD = "t_verify"
PLAN_TS = "1700000000.000200"
PHRASE = "is thinking..."
ASK = "why is <#C1|payments> slow: check /metrics"
TITLE = "why is #payments slow, check or metrics"
STOP_BLOCK_ID = "kage_stop"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_status verify: {detail}")


def _calls(node: ast.AST, alias: str, target: str) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == target
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == alias
        for call in ast.walk(node)
    )


def _is_guard(stmt: ast.stmt) -> bool:
    """``if _kage_slack_status.enabled() and ...: return await _kage_slack_status.set_thread_status(self, ...)``."""
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    first = test.values[0] if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else test
    if not (
        isinstance(first, ast.Call)
        and isinstance(first.func, ast.Attribute)
        and first.func.attr == "enabled"
        and isinstance(first.func.value, ast.Name)
        and first.func.value.id == GUARD_ALIAS
    ):
        return False
    if len(stmt.body) != 1 or not isinstance(stmt.body[0], ast.Return):
        return False
    value = stmt.body[0].value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return False
    call = value.value
    return (
        _calls(call, GUARD_ALIAS, GUARD_TARGET)
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "self"
    )


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (SETTER, BUILDER)
    }
    setter = defs.get(SETTER)
    if setter is None:
        raise _fail(f"{ADAPTER} has no async def {SETTER}()")
    body = setter.body
    if len(body) < 3 or not _is_guard(body[1]):
        raise _fail(f"{SETTER}() does not open with the {FLAG_ENV} guard after its docstring")
    upstream = ast.Module(body=body[2:], type_ignores=[])
    if not any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == UPSTREAM_RESOLVER
        for call in ast.walk(upstream)
    ):
        raise _fail(f"{SETTER}() no longer runs upstream's body after the guard")
    builder = defs.get(BUILDER)
    if builder is None or not _calls(builder, GUARD_ALIAS, NOTE_TARGET):
        raise _fail(f"{BUILDER}() does not keep the ask for the session title")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == IMPORT_MODULE
        and any(a.name == IMPORT_NAME and a.asname == GUARD_ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{ADAPTER} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {GUARD_ALIAS}")


class _StubClient:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    async def agents_sessions_setStatus(self, **kwargs):
        self.calls.append(("setStatus", kwargs["status"]))

    async def agents_sessions_rename(self, **kwargs):
        self.calls.append(("rename", kwargs["title"]))

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("post", kwargs["blocks"]))
        return {"ts": PLAN_TS}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs["blocks"]))
        return {"ok": True}


class _StubAdapter:
    """The adapter as patched, with the flag on and an SDK that has Agent Sessions."""

    def __init__(self, module) -> None:
        self.calls: list = []
        self.module = module

    def _get_client(self, chat_id, team_id=None):
        return _StubClient(self.calls)

    async def _set_thread_status(self, chat_id, team_id, thread_ts, status, fail_label):
        await self.module.set_thread_status(
            self, chat_id, team_id, thread_ts, status, fail_label,
            lambda c: c.agents_sessions_setStatus, lambda c: c.agents_sessions_rename,
        )

    @staticmethod
    def _default_status_text(started):
        return PHRASE


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_status_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None or module._status is None:
        raise _fail("slack_presenter or slack_status did not import beside the runtime module")
    return module


async def _drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"

    # The session: the phrase becomes processing, sent once; the ask titles it; the clear closes it.
    adapter = _StubAdapter(module)
    module.note_ask(CHANNEL, THREAD, ASK)
    await adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "failed")
    await adapter._set_thread_status(CHANNEL, TEAM, THREAD, PHRASE, "failed")
    await adapter._set_thread_status(CHANNEL, TEAM, THREAD, "", "clear failed")
    expected = [("setStatus", "processing"), ("rename", TITLE), ("setStatus", "closed")]
    if adapter.calls != expected:
        raise _fail(f"the session got {adapter.calls!r}, expected {expected!r}")

    # The plan: a note posts it with Stop, the next edits it, completion settles it.
    adapter = _StubAdapter(module)
    sub = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": CARD}
    if not await module.deliver_row(adapter, sub, 1, "check payments", "reading logs"):
        raise _fail("the first note did not go on the plan")
    await module.deliver_row(adapter, sub, 2, "check payments", "reading metrics")
    await module.settle_row(adapter, sub, "completed")
    kinds = [call[0] for call in adapter.calls]
    if kinds != ["post", "setStatus", "update", "update", "setStatus"]:
        raise _fail(f"the plan made calls {kinds!r}")
    posted, settled = adapter.calls[0][1], adapter.calls[3][1]
    if posted[0].get("type") != "plan" or posted[-1].get("block_id") != STOP_BLOCK_ID:
        raise _fail(f"the plan posted {posted!r}")
    if len(settled) != 1 or settled[0]["tasks"][0]["status"] != "complete":
        raise _fail(f"the settled plan was {settled!r}")
    if adapter.calls[1] != ("setStatus", "processing") or adapter.calls[4] != ("setStatus", "closed"):
        raise _fail(f"the plan's session went {adapter.calls[1]!r} then {adapter.calls[4]!r}")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_adapter(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_status verify: status setter guarded ahead of upstream's body; "
        "runtime sends enum statuses on change, titles the session, posts, edits and settles one plan"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
