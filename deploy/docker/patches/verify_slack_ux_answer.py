#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX answer-fold patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes``, after the patches in
the same ``RUN`` have applied, with ``slack_presenter.py`` staged beside this
script (``/opt/defaults/scripts`` is not populated yet at that point).

Three things are checked:

1. The notifier. ``_send_event`` hands ``_progress_deliver`` the incident
   adapter built around ``_kage_slack_answer.adapter_for(adapter,
   self.platform_str, ev, self.task, sub)``, and that name is bound at module
   level.
2. The runtime module, loaded by path from ``gateway/``: flag off,
   ``adapter_for`` returns the notifier's own adapter; flag on, a finished
   card's answer posts once, in the card's thread, as its first sentence in
   bold and the rest folded as the Slack plugin's own ``block_kit`` renders
   it, and a report that opens with a heading takes the upstream send.
3. The adapter. ``SlackAdapter`` still has the members the runtime calls, the
   one it awaits still async, and still sets ``_bot_message_ts``. The stub
   below supplies them, so only this check ties them to upstream; a missing
   one would fall back to the upstream send at runtime, folding nothing.

A wrong adapter here raises nothing at runtime: the answer is posted whole as
before. The build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

NOTIFIER = "gateway/kanban_watchers_notifier.py"
RUNTIME = "gateway/slack_ux_answer.py"
BLOCK_KIT = "plugins/platforms/slack/block_kit.py"
ADAPTER = "plugins/platforms/slack/adapter.py"
ADAPTER_CLASS = "SlackAdapter"
#: The adapter members the runtime calls, the ones it awaits, and the attribute it adds to.
RUNTIME_MEMBERS = (
    "_extra_flag", "_outbound_blocked", "_dm_target", "_metadata_team_id", "_resolve_thread_ts",
    "_client_for", "_workspace_message_marker", "format_message",
)
ASYNC_MEMBERS = ("_dm_target",)
RUNTIME_ATTRIBUTE = "_bot_message_ts"
FLAG_ENV = "KAGE_SLACK_UX"

METHOD = "_send_event"
DELIVER = "_progress_deliver"
INCIDENT_ALIAS = "_kage_slack_incident"
ALIAS = "_kage_slack_answer"
FACTORY = "adapter_for"
EXPECTED_ARGS = "adapter, self.platform_str, ev, self.task, sub"

CHANNEL = "C0KAGE"
THREAD_TS = "1700000000.000100"
POSTED_TS = "1700000000.000200"
HEADLINE = "Checkout is slow because the payments pool is at its limit."
REST = "The pool has 4 nodes and all of them are above 90% CPU.\n\n- Scale the pool to 6 nodes.\n- Then watch p99 latency."
ANSWER = f"{HEADLINE} {REST}"
REPORT = "## What's wrong\n\nCheckout is slow.\n\n## Why\n\nThe pool is full."


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_answer verify: {detail}")


def _is_factory(node: ast.AST, alias: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == FACTORY
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == alias
    )


def check_notifier(root: Path) -> None:
    path = root / NOTIFIER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    methods = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == METHOD]
    if len(methods) != 1:
        raise _fail(f"{NOTIFIER} has {len(methods)} async def {METHOD}(), expected 1")
    delivers = [
        n for n in ast.walk(methods[0])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == DELIVER
    ]
    if len(delivers) != 1:
        raise _fail(f"{METHOD}() calls {DELIVER}() {len(delivers)} times, expected 1")
    args = delivers[0].args
    incident = args[1] if len(args) > 1 else None
    inner = incident.args[0] if _is_factory(incident, INCIDENT_ALIAS) and incident.args else None
    if not (_is_factory(inner, ALIAS) and ", ".join(ast.unparse(a) for a in inner.args) == EXPECTED_ARGS):
        raise _fail(
            f"{DELIVER}()'s adapter argument is not {INCIDENT_ALIAS}.{FACTORY}() around "
            f"{ALIAS}.{FACTORY}({EXPECTED_ARGS})"
        )
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "gateway"
        and any(a.name == "slack_ux_answer" and a.asname == ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{NOTIFIER} does not import gateway.slack_ux_answer as {ALIAS}")


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == ADAPTER_CLASS]
    if len(classes) != 1:
        raise _fail(f"{ADAPTER} has {len(classes)} class {ADAPTER_CLASS}, expected 1")
    defs = {
        n.name: isinstance(n, ast.AsyncFunctionDef)
        for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [name for name in RUNTIME_MEMBERS if name not in defs]
    if missing:
        raise _fail(f"{ADAPTER_CLASS} no longer defines {', '.join(missing)}")
    wrong = [name for name in RUNTIME_MEMBERS if defs[name] != (name in ASYNC_MEMBERS)]
    if wrong:
        raise _fail(f"{ADAPTER_CLASS}.{', '.join(wrong)} changed between sync and async")
    sets = any(
        isinstance(n, ast.Attribute) and n.attr == RUNTIME_ATTRIBUTE and isinstance(n.ctx, ast.Store)
        for n in ast.walk(classes[0])
    )
    if not sets:
        raise _fail(f"{ADAPTER_CLASS} no longer sets self.{RUNTIME_ATTRIBUTE}")


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_answer_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


class _StubAdapter:
    """The SlackAdapter surface the folder reaches, recording what it is asked to do."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self._bot_message_ts: set = set()

    def _extra_flag(self, key):
        return key == "rich_blocks"

    def _outbound_blocked(self, chat_id, label):
        return None

    async def _dm_target(self, chat_id, metadata):
        return chat_id

    def _metadata_team_id(self, metadata):
        return None

    def _resolve_thread_ts(self, reply_to, metadata):
        return (metadata or {}).get("thread_id") or reply_to

    def _workspace_message_marker(self, team_id, ts):
        return ts

    def _client_for(self, chat_id, metadata):
        adapter = self

        class _Client:
            async def chat_postMessage(self, **kwargs):
                adapter.log.append(("chat_postMessage", kwargs))
                return {"ts": POSTED_TS}

        return _Client()

    async def send(self, chat_id, content, metadata=None):
        self.log.append(("send", content))
        return SimpleNamespace(success=True, message_id=None, error=None)


def _plugin_fold(root: Path) -> list[dict]:
    """``REST`` as the Slack plugin's block_kit renders it, loaded apart from the runtime's copy."""
    spec = importlib.util.spec_from_file_location("slack_ux_answer_verify_block_kit", root / BLOCK_KIT)
    block_kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(block_kit)
    return block_kit.sanitize_blocks(block_kit.render_blocks(REST, mrkdwn_fn=None))


async def _drive(module, expected_fold: list[dict]) -> None:
    event = SimpleNamespace(kind="completed")
    task = SimpleNamespace(result=ANSWER)
    sub = {"chat_id": CHANNEL, "thread_id": THREAD_TS}
    metadata = {"thread_id": THREAD_TS}
    os.environ.pop(FLAG_ENV, None)
    try:
        adapter = _StubAdapter()
        if module.adapter_for(adapter, "slack", event, task, sub) is not adapter:
            raise _fail(f"adapter_for() wrapped the adapter with {FLAG_ENV} unset")
        os.environ[FLAG_ENV] = "1"
        wrapped = module.adapter_for(adapter, "slack", event, task, sub)
        if wrapped is adapter:
            raise _fail("adapter_for() did not take a completed card on Slack")
        result = await wrapped.send(CHANNEL, ANSWER, metadata=metadata)
        if [entry[0] for entry in adapter.log] != ["chat_postMessage"] or result.message_id != POSTED_TS:
            raise _fail(f"the answer delivery made {adapter.log!r}")
        post = adapter.log[0][1]
        headline, fold = post["blocks"]
        bold = headline["elements"][0]["elements"]
        if post.get("thread_ts") != THREAD_TS or bold != [{"type": "text", "text": HEADLINE, "style": {"bold": True}}]:
            raise _fail(f"the answer post was {post!r}")
        if fold.get("type") != "container" or not expected_fold or fold.get("child_blocks") != expected_fold:
            raise _fail("the rest was not folded through the Slack plugin's block_kit")
        adapter = _StubAdapter()
        await module.adapter_for(adapter, "slack", event, task, sub).send(CHANNEL, REPORT, metadata=metadata)
        if adapter.log != [("send", REPORT)]:
            raise _fail(f"a report opening with a heading made {adapter.log!r}")
    finally:
        os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_notifier(root)
    check_adapter(root)
    module = _load_runtime(root)
    asyncio.run(_drive(module, _plugin_fold(root)))
    print(
        "slack_ux_answer verify: the notifier's deliver takes adapter_for()'s adapter inside the incident one; "
        "off it is the notifier's own, on a finished answer posts its first sentence bold and the rest folded"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
