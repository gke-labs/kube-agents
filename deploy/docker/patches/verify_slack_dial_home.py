#!/usr/bin/env python3
"""Build-time behaviour gate for the KUBEAGENTS_DIAL Home tab patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_slack_dial_home.py``. Two things are checked:

1. The adapter. ``SlackAdapter`` still has the members the runtime calls
   (``_get_client``, ``_is_interactive_user_authorized``, ``_event_team_id``,
   ``_ensure_dm_conversation``) and still records the workspace its first bot
   token authenticated in (``_bot_user_id``, ``_team_bot_user_ids``),
   ``_handle_app_home_opened`` hands the Home tab to the runtime before its
   Messages-only return, ``_register_bolt_handlers`` calls the runtime's
   ``register``, and the import the guards name is bound at
   module level.
2. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: switch off it is disabled; on, an admin gets the radio and their
   pick is recorded; an admin of another workspace is not one; a member who is not an admin gets the page read-only and
   their pick records nothing; a member the adapter would not answer gets one
   line; with the switch off a click on a stale dial is answered with one line
   and records nothing.

Usage::

    python3 verify_slack_dial_home.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_dial_home.py"
CLASS = "SlackAdapter"
MEMBERS = (
    "_get_client",
    "_is_interactive_user_authorized",
    "_event_team_id",
    "_ensure_dm_conversation",
    "_register_bolt_handlers",
)
#: The install's own workspace is read from these (home_team).
HOME_TEAM_WRITES = ("self._team_bot_user_ids[team_id] = bot_user_id", "self._bot_user_id = bot_user_id")
HOME_METHOD = "_handle_app_home_opened"
REGISTER_METHOD = "_register_bolt_handlers"
ALIAS = "_kage_dial_home"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"verify_slack_dial_home: {detail}")


def check_adapter(source: str) -> None:
    """Fail unless the patched adapter source wires the runtime as the applier says."""
    tree = ast.parse(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == CLASS]
    if len(classes) != 1:
        raise _fail(f"{ADAPTER} defines {len(classes)} {CLASS} classes, expected 1")
    methods = {n.name: n for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in (*MEMBERS, HOME_METHOD):
        if name not in methods:
            raise _fail(f"{CLASS}.{name} is gone; slack_dial_home.py calls it")
    for write in HOME_TEAM_WRITES:
        if write not in source:
            raise _fail(f"{ADAPTER} no longer has {write!r}; home_team() reads the install's workspace from it")
    home = ast.get_source_segment(source, methods[HOME_METHOD]) or ""
    guard, early = home.find(f"{ALIAS}.opened(self, event, body)"), home.find('!= "messages"')
    if guard < 0 or early < 0 or guard > early:
        raise _fail(f"{HOME_METHOD} does not hand the Home tab to {ALIAS}.opened before its Messages-only return")
    register = ast.get_source_segment(source, methods[REGISTER_METHOD]) or ""
    if f"{ALIAS}.register(self)" not in register:
        raise _fail(f"{REGISTER_METHOD} does not call {ALIAS}.register(self)")
    bound = any(
        isinstance(n, ast.ImportFrom) and any(a.asname == ALIAS for a in n.names) for n in tree.body
    )
    if not bound:
        raise _fail(f"{ALIAS} is not imported at module level in {ADAPTER}")


class _Client:
    def __init__(self, users: dict) -> None:
        self.users, self.published = users, []

    async def users_info(self, user):
        return {"ok": True, "user": self.users.get(user, {})}

    async def views_publish(self, user_id, view):
        self.published.append((user_id, view))
        return {"ok": True}


class _App:
    def __init__(self) -> None:
        self.actions = {}

    def action(self, action_id):
        def bind(fn):
            self.actions[action_id] = fn
            return fn

        return bind


class _Adapter:
    def __init__(self, allowed: set, users: dict) -> None:
        self.allowed, self.client, self._app = allowed, _Client(users), _App()
        self._bot_user_id, self._team_bot_user_ids = "UBOT", {"T1": "UBOT", "T2": "UBOT2"}

    async def _ensure_dm_conversation(self, chat_id, team_id=None):
        return "D" + chat_id

    def _get_client(self, chat_id, team_id=None):
        return self.client

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        return user_id in self.allowed

    @staticmethod
    def _event_team_id(event, body=None):
        return "T1"


async def _ack() -> None:
    return None


def _kinds(view: dict) -> list:
    return [b["type"] for b in view["blocks"]]


def check_runtime(module) -> None:
    """Drive the runtime with a stub adapter; fail on any behaviour the docstring does not describe."""
    with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HERMES_HOME": home}, clear=False):
        os.environ.pop(module.ADMINS_ENV, None)
        with mock.patch.dict(os.environ, {module.FLAG_ENV: "off"}):
            if module.enabled():
                raise _fail("enabled() is true with the switch off")
        with mock.patch.dict(os.environ, {module.FLAG_ENV: "on"}):
            if not module.enabled():
                raise _fail("enabled() is false with the switch on")
        os.environ[module.FLAG_ENV] = "on"
        adapter = _Adapter(
            {"UADMIN", "UMEMBER", "UOTHER"},
            {
                "UADMIN": {"is_admin": True, "team_id": "T1"},
                "UMEMBER": {"team_id": "T1"},
                "UOTHER": {"is_admin": True, "team_id": "T2"},
            },
        )
        module.register(adapter)
        handler = adapter._app.actions.get(module.LEVEL_ACTION_ID)
        if handler is None:
            raise _fail("register() bound no handler for the level action")

        asyncio.run(module.opened(adapter, {"user": "UADMIN", "channel": "D1", "tab": "home"}))
        view = adapter.client.published[-1][1]
        if view["type"] != "home" or "actions" not in _kinds(view):
            raise _fail("an admin's Home tab has no radio buttons")

        asyncio.run(module.opened(adapter, {"user": "UMEMBER", "tab": "home"}))
        if "actions" in _kinds(adapter.client.published[-1][1]):
            raise _fail("a member who is not an admin got the radio buttons")

        asyncio.run(module.opened(adapter, {"user": "UOTHER", "tab": "home"}))
        if "actions" in _kinds(adapter.client.published[-1][1]):
            raise _fail("an admin of another workspace got the radio buttons")

        asyncio.run(module.opened(adapter, {"user": "USTRANGER", "tab": "home"}))
        if adapter.client.published[-1][1]["blocks"] != [module._section(module.DENIED)]:
            raise _fail("a member the adapter would not answer was shown the dial")

        state = Path(home) / module.STATE_NAME
        pick = {"selected_option": {"value": "explain"}}
        asyncio.run(handler(_ack, {"user": {"id": "UMEMBER"}}, pick))
        if state.exists():
            raise _fail("a pick by a member who is not an admin was recorded")
        os.environ[module.FLAG_ENV] = "off"
        asyncio.run(handler(_ack, {"user": {"id": "UADMIN"}}, pick))
        if state.exists() or adapter.client.published[-1][1]["blocks"] != [module._section(module.OFF)]:
            raise _fail("a click with the switch off was not answered with the off line, or was recorded")
        os.environ[module.FLAG_ENV] = "on"
        asyncio.run(handler(_ack, {"user": {"id": "UADMIN"}}, pick))
        recorded = json.loads(state.read_text())
        if recorded.get("level") != "explain" or recorded.get("set_by") != "UADMIN":
            raise _fail(f"an admin's pick was not recorded: {recorded}")


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("slack_dial_home_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(root: Path) -> None:
    check_adapter((root / ADAPTER).read_text())
    check_runtime(_load(root / RUNTIME))
    print("verify_slack_dial_home: ok")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
