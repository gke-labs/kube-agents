#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX button-click patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after the patches in the same ``RUN`` have applied, with ``slack_presenter.py``
staged beside this script (``/opt/defaults/scripts`` is not populated yet at
that point in the build).

Two things are checked:

1. The adapter. ``SlackAdapter`` still has the members the runtime calls:
   ``_begin_interaction(ack, body, action, kind)`` returning the eight fields
   :func:`slack_ux_clicks.answer` unpacks, in that order, plus
   ``_is_ignored_channel``, ``_slack_allowed_channels``, ``_slack_disable_dms``,
   ``_get_client``, ``_handle_slack_message``, ``_is_interactive_user_authorized``,
   ``_channel_gate_allows`` and ``_slack_message_matches_mention_patterns``, sets
   ``_bot_user_id`` and ``_team_bot_user_ids`` in ``__init__``, plus ``_client_for`` for ``slack_ux_incident``;
   the adapter file still defines ``_slack_mention_detection_text(event)`` at module level and still reads the
   ``_hermes_force_process`` marker the click's message carries.
   ``_register_bolt_handlers`` still wires the plugin
   action handlers, and the flag guard calling
   ``_kage_slack_clicks.register(self)`` follows that call directly. The import
   the guard names is bound at module level.
2. The runtime module, loaded by path from ``gateway/`` and driven with a stub
   adapter: flag off it registers nothing; flag on, an authorized choice click
   rewrites the message, echoes, and reaches the message handler as the
   clicker's message in the thread; an unauthorized one does none of that.

A click that reaches nothing raises nothing: Slack shows the button as
clicked and the gateway logs at debug. The build is where it is caught.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import inspect
import os
import sys
from pathlib import Path

ADAPTER = "plugins/platforms/slack/adapter.py"
RUNTIME = "gateway/slack_ux_clicks.py"
FLAG_ENV = "KAGE_SLACK_UX"

METHOD = "_register_bolt_handlers"
PLUGIN_WIRING = "_register_plugin_action_handlers"
GUARD_ALIAS = "_kage_slack_clicks"
IMPORT_MODULE = "gateway"
IMPORT_NAME = "slack_ux_clicks"

ADAPTER_CLASS = "SlackAdapter"
#: The adapter members ``slack_ux_clicks`` calls, and ``_client_for``, which
#: ``slack_ux_incident``'s alert edit calls; the stubs supply them, so only this
#: check ties them to upstream.
RUNTIME_MEMBERS = (
    "_begin_interaction", "_is_ignored_channel", "_slack_allowed_channels", "_slack_disable_dms",
    "_get_client", "_handle_slack_message", "_client_for", "_is_interactive_user_authorized",
    "_channel_gate_allows", "_slack_message_matches_mention_patterns",
)
#: The adapter file's module-level functions the runtime calls, and how: positional arguments, keywords.
RUNTIME_FUNCTIONS = {"_slack_mention_detection_text": (1, ())}
#: The instance attributes the runtime reads, set in ``__init__``.
RUNTIME_ATTRIBUTES = ("_bot_user_id", "_team_bot_user_ids")
#: The members the runtime awaits; every other one it calls plainly.
ASYNC_MEMBERS = ("_begin_interaction", "_handle_slack_message", "_channel_gate_allows")
#: The event key whose ``.get()`` makes the message handler skip the mention
#: requirement for a click's turn. Matched in the AST, so quoting does not matter.
FORCE_MARKER = "_hermes_force_process"
BEGIN_INTERACTION = "_begin_interaction"
BEGIN_POSITIONAL = ("self", "ack", "body", "action", "kind")
#: What ``_begin_interaction`` returns, unpacked positionally by ``answer()``.
BEGIN_RETURNS = ("team_id", "action_id", "value", "message", "msg_ts", "channel_id", "user_name", "user_id")
#: How the runtime calls the other members: positional arguments after ``self``, and keywords.
CALL_SHAPES = {
    "_is_ignored_channel": ((1, ()),),
    "_slack_allowed_channels": ((0, ()),),
    "_slack_disable_dms": ((0, ()),),
    "_get_client": ((1, ("team_id",)),),
    "_client_for": ((2, ()),),
    "_handle_slack_message": ((1, ()),),
    "_is_interactive_user_authorized": ((1, ("channel_id", "team_id")),),
    "_slack_message_matches_mention_patterns": ((1, ()),),
    "_channel_gate_allows": ((0, (
        "channel_id", "routing_text", "bot_uid", "is_mentioned", "is_thread_reply", "event_thread_ts", "user_id",
        "team_id", "is_dm", "force_process",
    )),),
}

CHANNEL = "C0KAGE"
TEAM = "T0KAGE"
USER = "U0KAGE"
MESSAGE_TS = "1700000000.000200"
THREAD = "1700000000.000100"
ACTION_TS = "1700000001.000300"
LABEL = "Raise to 512Mi"
CHOICE_ID = "kage.choice.0"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_clicks verify: {detail}")


def _is_self_call(stmt: ast.stmt, name: str) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Attribute)
        and stmt.value.func.attr == name
        and isinstance(stmt.value.func.value, ast.Name)
        and stmt.value.func.value.id == "self"
    )


def _is_guard(stmt: ast.stmt) -> bool:
    """``if _kage_slack_clicks.enabled(): _kage_slack_clicks.register(self)``."""
    if not isinstance(stmt, ast.If) or stmt.orelse or len(stmt.body) != 1:
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
    body = stmt.body[0]
    if not (isinstance(body, ast.Expr) and isinstance(body.value, ast.Call)):
        return False
    call = body.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "register"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == GUARD_ALIAS
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "self"
    )


def _members(cls: ast.ClassDef) -> dict[str, ast.AST]:
    """The class body's methods and assigned names (upstream builds some getters by assignment)."""
    found: dict[str, ast.AST] = {}
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            found.update({t.id: stmt for t in stmt.targets if isinstance(t, ast.Name)})
    return found


def _method_args(tree: ast.Module, member: ast.AST) -> ast.arguments | None:
    """The arguments of a method, or of the function a module-level factory returns for it."""
    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # A decorator (``@property``, ``@staticmethod``) changes how the call binds.
        return None if member.decorator_list else member.args
    if not (isinstance(member, ast.Assign) and isinstance(member.value, ast.Call)
            and isinstance(member.value.func, ast.Name)):
        return None
    factory = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == member.value.func.id), None
    )
    if factory is None:
        return None
    nested = {n.name: n for n in factory.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    returned = [n.value.id for n in factory.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)]
    return nested[returned[0]].args if len(returned) == 1 and returned[0] in nested else None


def _signature(args: ast.arguments) -> inspect.Signature:
    """The signature ``args`` declares, with annotations dropped and every default ``None``."""
    bare = copy.deepcopy(args)
    for arg in [*bare.posonlyargs, *bare.args, *bare.kwonlyargs, bare.vararg, bare.kwarg]:
        if arg is not None:
            arg.annotation = None
    bare.defaults = [ast.Constant(None) for _ in bare.defaults]
    bare.kw_defaults = [None if d is None else ast.Constant(None) for d in bare.kw_defaults]
    scope: dict = {}
    exec(f"def member({ast.unparse(bare)}): pass", scope)  # noqa: S102 — a signature, no body
    return inspect.signature(scope["member"])


def _accepts(args: ast.arguments, positional: int, keywords: tuple[str, ...]) -> bool:
    """Whether a method with ``args`` binds ``self`` plus the given call as Python would.

    A keyword that binds only into ``**kwargs`` (a positional-only parameter of that name)
    counts as refused: the call runs, but the parameter it meant to set does not get it.
    """
    signature = _signature(args)
    try:
        bound = signature.bind(None, *[None] * positional, **dict.fromkeys(keywords))
    except TypeError:
        return False
    extra = next(
        (bound.arguments.get(p.name, {}) for p in signature.parameters.values() if p.kind is p.VAR_KEYWORD), {}
    )
    return not set(keywords) & set(extra)


def _init_attributes(cls: ast.ClassDef) -> set[str]:
    """The ``self.<name>`` attributes ``__init__`` assigns, annotated or not."""
    init = next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    if init is None:
        return set()
    targets = [t for n in ast.walk(init) if isinstance(n, ast.Assign) for t in n.targets]
    targets = [e for t in targets for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
    targets += [n.target for n in ast.walk(init) if isinstance(n, ast.AnnAssign)]
    return {
        t.attr for t in targets
        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
    }


def check_members(tree: ast.Module) -> None:
    """The adapter members the runtime calls exist and accept its calls, and ``_begin_interaction`` has its shape."""
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == ADAPTER_CLASS]
    if len(classes) != 1:
        raise _fail(f"{ADAPTER} has {len(classes)} class {ADAPTER_CLASS}, expected 1")
    members = _members(classes[0])
    missing = [name for name in RUNTIME_MEMBERS if name not in members]
    if missing:
        raise _fail(f"{ADAPTER_CLASS} no longer has {', '.join(missing)}, which the runtime calls")
    unset = [name for name in RUNTIME_ATTRIBUTES if name not in _init_attributes(classes[0])]
    if unset:
        raise _fail(f"{ADAPTER_CLASS}.__init__ no longer sets {', '.join(unset)}, which the runtime reads")
    functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, (positional, keywords) in RUNTIME_FUNCTIONS.items():
        function = functions.get(name)
        if function is None or function.decorator_list:
            raise _fail(f"{ADAPTER} no longer defines {name}() at module level, which the runtime calls")
        if isinstance(function, ast.AsyncFunctionDef):
            raise _fail(f"{ADAPTER}'s {name} is now async; the runtime calls it")
        # _accepts binds a leading self, which a function's first parameter takes instead.
        if not _accepts(function.args, positional - 1, keywords):
            raise _fail(f"{ADAPTER}'s {name} no longer accepts {positional} positional argument(s), as the runtime calls it")
    for name, calls in CALL_SHAPES.items():
        args = _method_args(tree, members[name])
        if args is None:
            raise _fail(f"{ADAPTER_CLASS}.{name} is no longer a method the runtime can call")
        for positional, keywords in calls:
            if not _accepts(args, positional, keywords):
                raise _fail(
                    f"{ADAPTER_CLASS}.{name} no longer accepts {positional} positional argument(s)"
                    f" and {keywords!r}, as the runtime calls it"
                )
    for name in RUNTIME_MEMBERS:
        member = members[name]
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        awaited = name in ASYNC_MEMBERS
        if isinstance(member, ast.AsyncFunctionDef) != awaited:
            state, use = ("no longer", "awaits") if awaited else ("now", "calls")
            raise _fail(f"{ADAPTER_CLASS}.{name} is {state} async; the runtime {use} it")
    begin = members[BEGIN_INTERACTION]
    if not isinstance(begin, (ast.FunctionDef, ast.AsyncFunctionDef)) or begin.decorator_list:
        raise _fail(f"{ADAPTER_CLASS}.{BEGIN_INTERACTION} is no longer a method")
    positional = tuple(a.arg for a in [*begin.args.posonlyargs, *begin.args.args])
    if positional != BEGIN_POSITIONAL:
        raise _fail(f"{BEGIN_INTERACTION} takes {positional!r}, slack_ux_clicks passes {BEGIN_POSITIONAL!r}")
    if not _accepts(begin.args, len(BEGIN_POSITIONAL) - 1, ()):
        raise _fail(f"{BEGIN_INTERACTION} requires a keyword argument slack_ux_clicks does not pass")
    returned = [
        tuple(e.id if isinstance(e, ast.Name) else ast.unparse(e) for e in n.value.elts)
        for n in ast.walk(begin)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
    ]
    if returned != [BEGIN_RETURNS]:
        raise _fail(f"{BEGIN_INTERACTION} returns {returned!r}, slack_ux_clicks unpacks {BEGIN_RETURNS!r}")


def _reads_force_marker(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == FORCE_MARKER
        for node in ast.walk(tree)
    )


def check_adapter(root: Path) -> None:
    path = root / ADAPTER
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    tree = ast.parse(path.read_text())
    check_members(tree)
    if not _reads_force_marker(tree):
        raise _fail(f"{ADAPTER} no longer reads .get({FORCE_MARKER!r}), which a click's message relies on")
    methods = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == METHOD]
    if len(methods) != 1:
        raise _fail(f"{ADAPTER} has {len(methods)} def {METHOD}(), expected 1")
    body = methods[0].body
    wired = [i for i, stmt in enumerate(body) if _is_self_call(stmt, PLUGIN_WIRING)]
    if len(wired) != 1:
        raise _fail(f"{METHOD}() no longer calls self.{PLUGIN_WIRING}() once")
    after = wired[0] + 1
    if after >= len(body) or not _is_guard(body[after]):
        raise _fail(f"{METHOD}() does not run the {FLAG_ENV} guard right after self.{PLUGIN_WIRING}()")
    bound = any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == IMPORT_MODULE
        and any(a.name == IMPORT_NAME and a.asname == GUARD_ALIAS for a in stmt.names)
        for stmt in tree.body
    )
    if not bound:
        raise _fail(f"{ADAPTER} does not import {IMPORT_MODULE}.{IMPORT_NAME} as {GUARD_ALIAS}")


class _App:
    def __init__(self) -> None:
        self.listeners: list[tuple] = []

    def action(self, matcher):
        def wire(handler):
            self.listeners.append((matcher, handler))
            return handler

        return wire


class _Client:
    def __init__(self, log: list) -> None:
        self.log = log

    async def chat_update(self, **kwargs):
        self.log.append(("chat_update", kwargs))

    async def chat_postMessage(self, **kwargs):
        self.log.append(("chat_postMessage", kwargs))


class _StubAdapter:
    def __init__(self, authorized: bool) -> None:
        self.authorized = authorized
        self.log: list[tuple] = []
        self._app = _App()
        self._bot_user_id = "U0BOT"
        self._team_bot_user_ids: dict[str, str] = {}

    async def _begin_interaction(self, ack, body, action, kind, *, team_scoped=True):
        await ack()
        if not self.authorized:
            return None
        message = body["message"]
        return (TEAM, action["action_id"], action["value"], message, message["ts"], CHANNEL, "someone", USER)

    def _is_ignored_channel(self, channel_id):
        return False

    def _slack_allowed_channels(self):
        return set()

    def _slack_disable_dms(self):
        return False

    def _get_client(self, chat_id, team_id=None):
        return _Client(self.log)

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        return self.authorized

    def _slack_message_matches_mention_patterns(self, text):
        return False

    async def _channel_gate_allows(
        self, *, channel_id, routing_text, bot_uid, is_mentioned, is_thread_reply, event_thread_ts, user_id,
        team_id, is_dm, force_process,
    ):
        return True

    async def _handle_slack_message(self, event, payload=None):
        self.log.append(("message", event))


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_clicks_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


async def _ack() -> None:
    return None


def _click():
    body = {
        "message": {
            "ts": MESSAGE_TS,
            "thread_ts": THREAD,
            "text": "fallback",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "*pick one*"}},
                {"type": "actions", "elements": [{"type": "button", "action_id": CHOICE_ID, "value": LABEL}]},
            ],
        }
    }
    return body, {
        "action_id": CHOICE_ID, "text": {"type": "plain_text", "text": LABEL}, "value": LABEL,
        "action_ts": ACTION_TS,
    }


async def _drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"
    try:
        adapter = _StubAdapter(authorized=True)
        module.register(adapter)
        if len(adapter._app.listeners) != 2:
            raise _fail(f"register() wired {len(adapter._app.listeners)} listeners, expected 2")
        await module.answer(adapter, _ack, *_click(), module.CHOICE_KIND)
        kinds = [entry[0] for entry in adapter.log]
        if kinds != ["chat_update", "chat_postMessage", "message"]:
            raise _fail(f"an authorized choice click made {kinds!r}")
        update, echo, turn = (entry[1] for entry in adapter.log)
        if any(b.get("type") == "actions" for b in update["blocks"]):
            raise _fail("the answered choice buttons are still on the message")
        if echo["thread_ts"] != THREAD or f"<@{USER}>" not in echo["text"]:
            raise _fail(f"the echo was {echo!r}")
        expected = {"user": USER, "text": LABEL, "channel": CHANNEL, "thread_ts": THREAD, "ts": ACTION_TS}
        if {k: turn.get(k) for k in expected} != expected:
            raise _fail(f"the turn was {turn!r}")

        module._answered.clear()
        stranger = _StubAdapter(authorized=False)
        await module.answer(stranger, _ack, *_click(), module.CHOICE_KIND)
        if stranger.log:
            raise _fail(f"an unauthorized click made {stranger.log!r}")
    finally:
        os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_adapter(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_clicks verify: registration guarded after the plugin handlers; "
        "an authorized choice click is echoed and runs as the clicker's turn, an unauthorized one does nothing"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
