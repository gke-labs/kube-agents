#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX incident-triage patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes``, after the patches in
the same ``RUN`` have applied, with ``slack_presenter.py`` staged beside this
script (``/opt/defaults/scripts`` is not populated yet at that point).

Two things are checked:

1. The notifier. ``_send_event`` hands ``_progress_deliver`` the adapter
   ``_kage_slack_incident.adapter_for(adapter, self.platform_str, ev,
   self.task, sub)`` returns, and that name is bound at module level.
2. The runtime module, loaded by path from ``gateway/``: flag off,
   ``adapter_for`` returns the notifier's own adapter; flag on, a triage
   report for an open alert thread edits the alert with one button per
   option, the recommended one primary, and the report folded as the Slack
   plugin's own ``block_kit`` renders it, and sends nothing else.

A wrong adapter here raises nothing at runtime: the report is posted under
the alert as before. The build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

NOTIFIER = "gateway/kanban_watchers_notifier.py"
RUNTIME = "gateway/slack_ux_incident.py"
BLOCK_KIT = "plugins/platforms/slack/block_kit.py"
FLAG_ENV = "KAGE_SLACK_UX"
DB_PATH_ENV = "SESSION_KV_DB_PATH"

METHOD = "_send_event"
DELIVER = "_progress_deliver"
ALIAS = "_kage_slack_incident"
FACTORY = "adapter_for"
EXPECTED_ARGS = "adapter, self.platform_str, ev, self.task, sub"

CHANNEL = "C0KAGE"
ALERT_TS = "1700000000.000100"
REPORT = (
    "## What's wrong\n\n"
    "payments-api in seeded-debug keeps crashing: secret payments-db-creds has been missing since 14:02.\n\n"
    "## Why\n\n"
    "- The pod fails on start reading `payments-db-creds`.\n\n"
    "## What to do\n\n"
    "- **Option A (Roll back to 14:02):** Revert the Deployment to the last revision.\n"
    "- **Option B (Restore the secret):** Recreate payments-db-creds from the GitOps repo.\n"
    "- ✅ **Recommended: Option B** — it fixes the cause.\n"
    "- **To authorize:** reply **'apply'** to open a GitOps Pull Request with the recommended fix, "
    "or name one directly with **'apply Option A'** / **'apply Option B'**.\n"
)


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_incident verify: {detail}")


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
    factory = args[1] if len(args) > 1 else None
    if not (
        isinstance(factory, ast.Call)
        and isinstance(factory.func, ast.Attribute)
        and factory.func.attr == FACTORY
        and isinstance(factory.func.value, ast.Name)
        and factory.func.value.id == ALIAS
        and ", ".join(ast.unparse(a) for a in factory.args) == EXPECTED_ARGS
    ):
        raise _fail(f"{DELIVER}()'s adapter argument is not {ALIAS}.{FACTORY}({EXPECTED_ARGS})")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "gateway"
        and any(a.name == "slack_ux_incident" and a.asname == ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{NOTIFIER} does not import gateway.slack_ux_incident as {ALIAS}")


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_incident_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


class _StubAdapter:
    def __init__(self) -> None:
        self.log: list[tuple] = []

    def _client_for(self, chat_id, metadata):
        adapter = self

        class _Client:
            async def chat_update(self, **kwargs):
                adapter.log.append(("chat_update", kwargs))

        return _Client()

    async def send(self, chat_id, content, metadata=None):
        self.log.append(("send", chat_id))


def _seed(db_path: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
        conn.execute("CREATE TABLE incidents (chat_id TEXT, thread_id TEXT, report TEXT)")
        meta = {"platform": "slack", "chat_id": CHANNEL, "thread_id": ALERT_TS}
        conn.execute("INSERT INTO session_metadata VALUES (?, ?)", ("k8s-evt-0000abcd", json.dumps(meta)))
        conn.commit()


def _plugin_fold(root: Path) -> list[dict]:
    """``REPORT`` as the Slack plugin's block_kit renders it, loaded apart from the runtime's copy."""
    spec = importlib.util.spec_from_file_location("slack_ux_incident_verify_block_kit", root / BLOCK_KIT)
    block_kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(block_kit)
    return block_kit.sanitize_blocks(block_kit.render_blocks(REPORT.strip(), mrkdwn_fn=None))


async def _drive(module, db_path: str, expected_fold: list[dict]) -> None:
    adapter = _StubAdapter()
    event = SimpleNamespace(kind="completed")
    task = SimpleNamespace(result=REPORT)
    sub = {"chat_id": CHANNEL, "thread_id": ALERT_TS}
    os.environ.pop(FLAG_ENV, None)
    os.environ[DB_PATH_ENV] = db_path
    try:
        if module.adapter_for(adapter, "slack", event, task, sub) is not adapter:
            raise _fail(f"adapter_for() wrapped the adapter with {FLAG_ENV} unset")
        os.environ[FLAG_ENV] = "1"
        wrapped = module.adapter_for(adapter, "slack", event, task, sub)
        if wrapped is adapter:
            raise _fail("adapter_for() did not take an open alert's triage report")
        await wrapped.send(CHANNEL, "report", metadata={"thread_id": ALERT_TS})
        if [entry[0] for entry in adapter.log] != ["chat_update"]:
            raise _fail(f"the triage delivery made {adapter.log!r}")
        update = adapter.log[0][1]
        buttons = [e for b in update["blocks"] if b.get("type") == "actions" for e in b["elements"]]
        labels = [b["text"]["text"] for b in buttons]
        values = [b.get("value") for b in buttons[:2]]
        if update["ts"] != ALERT_TS or labels[:2] != [
            "Roll back to 14:02", "Restore the secret (recommended)",
        ] or values != ["apply Option A: Roll back to 14:02", "apply Option B: Restore the secret"]:
            raise _fail(f"the alert edit was {update!r}")
        if buttons[1].get("style") != "primary" or "style" in buttons[0]:
            raise _fail("the recommended option is not the primary button")
        fold = update["blocks"][-1]
        if fold.get("type") != "container" or not expected_fold or fold.get("child_blocks") != expected_fold:
            raise _fail("the report was not folded through the Slack plugin's block_kit")
    finally:
        os.environ.pop(FLAG_ENV, None)
        os.environ.pop(DB_PATH_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_notifier(root)
    # The runtime imports gateway.kanban_notifier when it decides.
    sys.path.insert(0, str(root))
    module = _load_runtime(root)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "session_kv.db")
        _seed(db_path)
        asyncio.run(_drive(module, db_path, _plugin_fold(root)))
    print(
        "slack_ux_incident verify: the notifier's deliver takes adapter_for()'s adapter; "
        "off it is the notifier's own, on an open alert's triage edits the alert with its options"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
