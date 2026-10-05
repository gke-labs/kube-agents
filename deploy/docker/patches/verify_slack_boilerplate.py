#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX boilerplate patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_slack_boilerplate.py``, with ``slack_presenter.py`` staged beside
this script.

Four things are checked:

1. The call sites. Both cron send lanes take ``target_text``, bound from
   ``cron_delivery_text`` over the unwrapped ``content`` and the wrapped
   ``cleaned_delivery_content``, and the wrapper itself is still built, so
   every other target keeps it. The heartbeat mode is passed through
   ``long_running_mode`` directly after it is read. The interrupting notices
   still go out as ``adapter.send(chat_id, msg)``, which on Slack is the
   hooked ``SlackAdapter.send``. The post-restart send and the
   home-channel startup send each sit behind a ``drop_notice`` guard that
   returns or continues first, and ``_send_home_channel_message``, which the
   session-database warnings share, carries no guard; their own loop does. The
   busy-input onboarding hint is gated on ``drop_notice``, and
   ``SlackAdapter.send`` and ``SlackAdapter.edit_message`` pass their content
   through ``system_text``, after the DM target and the outbound check.
   Every name a hook reads is bound where it runs (``patchlib.unbound``):
   most are evaluated with the flag off too, so an upstream rename of
   ``content`` or ``turn_ctx`` would otherwise raise ``NameError`` on every
   cron delivery, Slack send or heartbeat. ``cron_delivery_text`` reads its
   target's attributes through ``getattr`` with a default, so upstream's
   target class must still declare each one: renamed, Slack would quietly get
   the wrapped report back with nothing failing.
2. The notices themselves. Each interrupting notice is read out of the patched
   source, rendered, and handed to ``system_text``: with the flag on it must
   come back reworded. ``system_text`` passes text it does not recognise
   through unchanged, so an upstream rewording would otherwise put Hermes'
   wording back on Slack with nothing failing.
3. The other system replies, rendered from the patched source the same way:
   the busy acks, the drain and restart refusals, the mid-turn slash-command
   and ``/steer`` replies, the force-stop reply, the background-task update and
   every provider error reply, plus the ``/restart`` and ``/stop`` replies read
   out of ``locales/en.yaml`` (each key must still be looked up in
   ``gateway/slash_commands.py``). With the flag on, ``system_text`` must
   reword every one, with no emoji, "Gateway", "gateway", "agent", "Hermes",
   ``/stop`` or exception text left; this is what catches an upstream
   rewording. The two replies built around an exception, the ``/steer``
   failure and the provider authentication failure, must each be the upstream
   argument of an ``error_reply`` call, so Slack gets the plain reply whatever
   the exception holds. The words a drain reply or the interrupted-cron-job notice
   interpolates are read out of upstream too (``_status_action_gerund()`` and
   the notice's ``action`` binding), and each must be one the runtime rewords.
4. The runtime module, loaded by path: on Slack with the flag on,
   ``drop_notice`` is true, so neither back-online notice reaches Slack; flag
   off, and for any platform other than Slack, every helper returns its input
   and ``drop_notice`` is false.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import patchlib

FLAG_ENV = "KAGE_SLACK_UX"
ALIAS = "_kage_slack_boilerplate"
RUNTIME = "gateway/slack_boilerplate.py"

DELIVERY = "cron/scheduler_delivery.py"
DELIVER_FN = "_deliver_result"
SEND_LANES = ("_deliver_via_live_adapter", "_deliver_standalone")
TARGET_TEXT = "target_text"
WRAPPER_HEADER = "Cronjob Response: "
TARGET_CLASS = "_TargetDelivery"
TARGET_FACTORY = "_prepare_target_delivery"
TARGET_LOCAL = "t"
#: Names a return annotation may carry besides ``TARGET_CLASS``: the forms of
#: "or None" (``None`` itself parses as a constant, not a name). Anything else
#: names a second class whose fields go unchecked.
OPTIONAL_NAMES = {"Optional", "Union", "typing"}
#: What ``cron_delivery_text`` reads off a target. ``drive`` below passes a
#: SimpleNamespace, so only this check ties the names to upstream's class. Only
#: ``platform_name`` changes what is sent; the other two feed a log line, and are
#: pinned anyway so a read the helper makes is never left to a default.
TARGET_FIELDS = ("platform_name", "chat_id", "job")

RUN_TURN = "gateway/run_turn.py"
HEARTBEAT_FN = "_run_agent_notify_long_running"
HEARTBEAT_MODE = "_long_running_mode"

RUN_SHUTDOWN = "gateway/run_shutdown.py"
NOTICE_SEND_FN = "_send_notice_logged"
NOTICE_SEND_ARGS = ["chat_id", "msg"]
SHUTDOWN_FN = "_notify_active_sessions_of_shutdown"
CRON_INTERRUPT_FN = "_notify_interrupted_cron_jobs"

RUN_NOTIFICATIONS = "gateway/run_notifications.py"
RESTARTED_PREFIX = "♻ Gateway restarted"
RESTART_FN = "_send_restart_notification"
HOME_CHANNEL_FN = "_send_home_channel_message"
STARTUP_FN = "_send_home_channel_startup_notifications"

SESSION_DB_FN = "_send_session_db_warning_notifications"

RUN_BUSY = "gateway/run_busy.py"
BUSY_ACK_FN = "_compose_busy_ack_message"
BUSY_HINT = "busy_input_hint_gateway"
BUSY_HEADS = (
    "⏳ Queued for the next turn", "⚡ Interrupting current task", "⏩ Steered into current run",
    "↪ Redirected current run", "⏳ Subagent working", "⏳ Compressing context",
)
#: The shared tail of the subagent and compression acks, a class attribute.
BUSY_DEMOTED_TAIL = "_BUSY_DEMOTED_TAIL"
BUSY_DETAILS = ("", " (3 min elapsed, running: terminal)")
DRAIN_FN = "_send_busy_drain_notice"

#: What the mid-turn slash-command and /steer replies interpolate.
BUSY_ENV = {"name": "model", "preview": "check the ingress too", "verb": "refine"}

#: Hermes' English catalog, where the /restart and /stop replies live, the
#: module that looks each key up, and the arguments each is formatted with.
LOCALES = "locales/en.yaml"
SLASH_COMMANDS = "gateway/slash_commands.py"
LOCALE_REPLIES = {
    "gateway.draining": {"count": 2},
    "gateway.restart.in_progress": {},
    "gateway.restart.restarting": {},
    "gateway.stop.stopped": {},
    "gateway.stop.stopped_pending": {},
}

RUN_INBOUND = "gateway/run_inbound.py"
RUN_TURN_RUNNER = "gateway/run_turn_runner.py"
RUN = "gateway/run.py"
SLASH_COMMANDS_GOALS = "gateway/slash_commands_goals.py"
#: The method whose return value the drain replies interpolate.
ACTION_FN = "_status_action_gerund"
BACKGROUND_FN = "_format_process_running_message"
BACKGROUND_CMDS = ("make build", "")
BACKGROUND_OUTPUTS = ("", "step 3/9")
AUTH_PREFIX = "⚠️ Provider authentication failed"
#: Stands in for the provider exception; Slack must never see it.
AUTH_ERROR = "401 stand-in provider error"

#: (file, prefix) of each reply built around an exception, and the runtime
#: helper that must build it.
ERROR_SITES = ((RUN_BUSY, "⚠️ Steer failed"), (RUN_TURN_RUNNER, AUTH_PREFIX))
ERROR_HELPER = f"{ALIAS}.error_reply"
#: Stand-ins for exceptions no bounded display-time pattern could match.
ERROR_SHAPES = (f"{AUTH_ERROR}\nTraceback line two", AUTH_ERROR + "x" * 600)

SLACK_ADAPTER = "plugins/platforms/slack/adapter.py"
SLACK_CLASS = "SlackAdapter"
#: Each hooked method, and what the statement just before the hook contains.
SLACK_METHODS = {"send": "self._dm_target(", "edit_message": "return blocked"}

#: (file, literal prefix, how many literals or f-strings carry it) for the
#: replies read straight out of a module; ``self._status_action_gerund()`` is
#: rendered for each value upstream's method can return.
SYSTEM_LITERALS = (
    (RUN_BUSY, "⏳ Gateway", 2),
    (RUN_BUSY, "⏳ Agent is running", 1),
    (RUN_BUSY, "⏩ Steer queued", 1),
    (RUN_BUSY, "Agent still starting — /steer", 1),
    (RUN_BUSY, "No active agent — /steer", 1),
    (RUN_BUSY, "⚠️ Steer failed", 1),
    (RUN_BUSY, "Agent is running —", 2),
    (RUN_INBOUND, "⏳ Gateway", 3),
    (RUN_INBOUND, "⏳ This agent is draining", 1),
    (RUN_INBOUND, "⏳ Another turn is still running", 1),
    (RUN_INBOUND, "⚡ Force-stopped", 1),
    (RUN_TURN_RUNNER, AUTH_PREFIX, 1),
    (RUN, AUTH_PREFIX, 1),
    (RUN, "⚠️ The model provider rejected", 1),
    (RUN, "⏱️ The model provider is rate-limiting", 1),
    (RUN, "⚠️ The model server is not responding", 1),
    (RUN, "⚠️ The model provider failed after retries", 1),
    (RUN, "Agent is running —", 3),
    (SLASH_COMMANDS_GOALS, "Agent is running —", 1),
)
#: Left on a reworded reply, any of these means the rewording missed.
SYSTEM_LEFTOVERS = (
    "⏳", "⚡", "⚠️", "⏱️", "⏩", "↪", "♻", "/stop", "Gateway", "agent", "gateway", "Hermes", "hermes", AUTH_ERROR,
)

#: Each patched file, and how many hooks the applier put in it: a statement
#: calling the runtime, or a ``drop_notice`` test with its guard body.
HOOKS = {
    DELIVERY: 1, RUN_TURN: 1, RUN_NOTIFICATIONS: 3, RUN_BUSY: 2, RUN_TURN_RUNNER: 1, SLACK_ADAPTER: 2,
}
#: Statements with a body of their own; a hook is never one of these.
COMPOUND = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Match,
)

#: Stands in for the job name the interrupted-cron-job notice interpolates;
#: its ``action`` is read out of upstream.
CRON_JOB_NAME = "inventory"
CRON_ACTION = "action"

CHANNEL = "C0KAGE"
REPORT = "Your fleet: 3 clusters, all healthy."
WRAPPED = f"{WRAPPER_HEADER}inventory\n(job_id: abc123)\n-------------\n\n{REPORT}\n\nTo stop or manage this job"


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_boilerplate verify: {detail}")


def _tree(root: Path, relative: str) -> ast.Module:
    path = root / relative
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    return ast.parse(path.read_text())


def _function(tree: ast.Module, name: str, relative: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise _fail(f"{relative} has no def {name}()")


def _is_helper(node: ast.AST, helper: str) -> bool:
    """``_kage_slack_boilerplate.<helper>(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == helper
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == ALIAS
    )


def _names(nodes) -> list[str]:
    return [ast.unparse(n) for n in nodes]


def _binds_alias(tree: ast.AST) -> bool:
    return any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "gateway"
        and any(a.name == "slack_boilerplate" and a.asname == ALIAS for a in stmt.names)
        for stmt in ast.walk(tree)
    )


def _reads_alias(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == ALIAS and isinstance(n.ctx, ast.Load) for n in ast.walk(node))


def _hooks(tree: ast.Module) -> list[ast.AST]:
    """What the applier inserted: each simple statement reading the alias, and each ``if`` testing it.

    An ``if`` contributes its test, and its body when that ends in a ``return``
    or ``continue`` (the ``drop_notice`` guards, whose bodies are ours); the
    busy-input hint's body is upstream's.
    """
    hooks: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _reads_alias(node.test):
            hooks.append(node.test)
            if isinstance(node.body[-1], ast.Return | ast.Continue):
                hooks.extend(node.body)
        elif isinstance(node, ast.stmt) and not isinstance(node, COMPOUND) and _reads_alias(node):
            hooks.append(node)
    return hooks


def check_bound(root: Path) -> None:
    """Every name a hook reads is bound where it runs, in each of the six patched files."""
    for relative, expected in HOOKS.items():
        tree = _tree(root, relative)
        hooks = _hooks(tree)
        found = sum(not isinstance(node, ast.stmt) or _reads_alias(node) for node in hooks)
        if found != expected:
            raise _fail(f"{relative} has {found} hooks, expected {expected}")
        for node in hooks:
            unbound = patchlib.unbound(tree, node)
            if unbound:
                raise _fail(
                    f"{relative}:{node.lineno} reads {', '.join(unbound)}, which nothing binds there; "
                    "an upstream rename the anchor did not cover"
                )


def check_delivery(root: Path) -> None:
    fn = _function(_tree(root, DELIVERY), DELIVER_FN, DELIVERY)
    if not _binds_alias(fn):
        raise _fail(f"{DELIVER_FN}() does not import gateway.slack_boilerplate as {ALIAS}")
    binds = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and [ast.unparse(t) for t in node.targets] == [TARGET_TEXT]
        and _is_helper(node.value, "cron_delivery_text")
    ]
    if len(binds) != 1:
        raise _fail(f"{DELIVER_FN}() binds {TARGET_TEXT} from cron_delivery_text {len(binds)} times, expected 1")
    args = _names(binds[0].value.args)
    if args[:3] != ["t", "content", "cleaned_delivery_content"]:
        raise _fail(f"cron_delivery_text is called with {args!r}")
    for lane in SEND_LANES:
        calls = [
            node for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == lane
        ]
        if len(calls) != 1 or len(calls[0].args) < 2 or ast.unparse(calls[0].args[1]) != TARGET_TEXT:
            raise _fail(f"{DELIVER_FN}() does not send {TARGET_TEXT} through {lane}()")
    if WRAPPER_HEADER not in ast.unparse(fn):
        raise _fail(f"{DELIVER_FN}() no longer builds the cron wrapper; the Chat relay routes on it")


def _binds(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, ast.Store)
    if isinstance(node, ast.arg):
        return node.arg == name
    if isinstance(node, ast.alias):
        return (node.asname or node.name.split(".")[0]) == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    if isinstance(node, (ast.ExceptHandler, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                         ast.MatchAs, ast.MatchStar)):
        return node.name == name
    return False


def check_target_fields(root: Path) -> None:
    # Fields declared on the class itself only: a field moved to a base class or
    # turned into a property refuses the build, loudly, until this is re-derived.
    tree = _tree(root, DELIVERY)
    deliver = _function(tree, DELIVER_FN, DELIVERY)
    # Every binding of t, whatever its form (a nested def's parameter, an
    # except or import alias, a def, a match capture), so a second binding from
    # anything but the factory refuses the build.
    stores = [node for node in ast.walk(deliver) if _binds(node, TARGET_LOCAL)]
    binds = [
        node for node in ast.walk(deliver)
        if isinstance(node, ast.Assign) and [ast.unparse(t) for t in node.targets] == [TARGET_LOCAL]
        and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == TARGET_FACTORY
    ]
    if len(stores) != 1 or len(binds) != 1:
        raise _fail(f"{DELIVER_FN}() no longer binds {TARGET_LOCAL} once, from {TARGET_FACTORY}()")
    factory = _function(tree, TARGET_FACTORY, DELIVERY)
    returns = factory.returns
    if isinstance(returns, ast.Constant) and isinstance(returns.value, str):
        returns = ast.parse(returns.value, mode="eval").body
    named = set() if returns is None else {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(returns) if isinstance(node, (ast.Name, ast.Attribute))
    }
    if named - OPTIONAL_NAMES != {TARGET_CLASS}:
        raise _fail(f"{TARGET_FACTORY}() is no longer annotated to return {TARGET_CLASS}")
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == TARGET_CLASS
    ]
    if len(classes) != 1:
        raise _fail(f"{DELIVERY} defines {TARGET_CLASS} {len(classes)} times, expected 1")
    fields = {
        node.target.id for node in classes[0].body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    missing = [name for name in TARGET_FIELDS if name not in fields]
    if missing:
        raise _fail(
            f"{TARGET_CLASS} no longer declares {', '.join(missing)}, which cron_delivery_text "
            "reads through getattr with a default; renamed upstream, it reads the default with nothing failing"
        )


def check_heartbeat(root: Path) -> None:
    fn = _function(_tree(root, RUN_TURN), HEARTBEAT_FN, RUN_TURN)
    for first, second in itertools.pairwise(fn.body):
        if (
            isinstance(first, ast.Assign)
            and _names(first.targets) == [HEARTBEAT_MODE]
            and "_display_surface_mode" in ast.unparse(first.value)
        ):
            if (
                isinstance(second, ast.Assign)
                and _names(second.targets) == [HEARTBEAT_MODE]
                and _is_helper(second.value, "long_running_mode")
                and _names(second.value.args) == ["turn_ctx.source", HEARTBEAT_MODE]
            ):
                return
            raise _fail(f"{HEARTBEAT_FN}() does not pass its mode through long_running_mode next")
    raise _fail(f"{HEARTBEAT_FN}() no longer reads {HEARTBEAT_MODE} from _display_surface_mode")


def _strings(fn: ast.AST, target: str) -> list[ast.expr]:
    return [
        node.value for node in ast.walk(fn)
        if isinstance(node, ast.Assign) and _names(node.targets) == [target]
    ]


def _guard_line(fn: ast.AST, exit_type: type) -> int:
    """The line of ``fn``'s ``if drop_notice(platform):`` whose body ends in ``exit_type``."""
    guards = [
        node.lineno for node in ast.walk(fn)
        if isinstance(node, ast.If) and _is_helper(node.test, "drop_notice")
        and _names(node.test.args) == ["platform"] and isinstance(node.body[-1], exit_type)
        and not (exit_type is ast.Return and ast.unparse(node.body[-1]) != "return None")
    ]
    if len(guards) != 1:
        raise _fail(f"{fn.name}() has {len(guards)} drop_notice(platform) guards, expected 1")
    return guards[0]


def _literal_values(node: ast.expr, where: str) -> tuple[str, ...]:
    """Every value ``node`` can take, through conditional expressions; string literals only."""
    if isinstance(node, ast.IfExp):
        return _literal_values(node.body, where) + _literal_values(node.orelse, where)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    raise _fail(f"{where} is no longer built from string literals: {ast.unparse(node)}")


def action_gerunds(root: Path) -> tuple[str, ...]:
    """Every value upstream's ``_status_action_gerund()`` can return."""
    fn = _function(_tree(root, RUN), ACTION_FN, RUN)
    returns = [node.value for node in ast.walk(fn) if isinstance(node, ast.Return) and node.value is not None]
    if not returns:
        raise _fail(f"{RUN} {ACTION_FN}() returns nothing")
    return tuple(dict.fromkeys(v for node in returns for v in _literal_values(node, f"{ACTION_FN}()")))


def cron_actions(root: Path) -> tuple[str, ...]:
    """Every value the interrupted-cron-job notice's ``action`` can take."""
    fn = _function(_tree(root, RUN_SHUTDOWN), CRON_INTERRUPT_FN, RUN_SHUTDOWN)
    binds = [
        node.value for node in ast.walk(fn)
        if isinstance(node, ast.Assign) and _names(node.targets) == [CRON_ACTION]
    ]
    if len(binds) != 1:
        raise _fail(f"{CRON_INTERRUPT_FN}() binds {CRON_ACTION} {len(binds)} times, expected 1")
    return _literal_values(binds[0], f"{CRON_INTERRUPT_FN}()'s {CRON_ACTION}")


def check_action_words(runtime, root: Path) -> None:
    """Each interpolated action word upstream produces must be one the runtime rewords."""
    for words, table, name in (
        (action_gerunds(root), runtime.DRAIN_WHO, "DRAIN_WHO"),
        (cron_actions(root), runtime.CRON_INTERRUPTED_WHY, "CRON_INTERRUPTED_WHY"),
    ):
        unknown = [word for word in words if word not in table]
        if unknown:
            raise _fail(f"upstream interpolates {unknown}, which {name} in {RUNTIME} does not reword")


def check_notices(root: Path) -> list[str]:
    """Check the notice call sites; return every interrupting notice rendered from source."""
    notices: list[str] = []

    tree = _tree(root, RUN_SHUTDOWN)
    send = _function(tree, NOTICE_SEND_FN, RUN_SHUTDOWN)
    if not any(
        isinstance(node, ast.Call) and ast.unparse(node.func) == "adapter.send"
        and _names(node.args) == NOTICE_SEND_ARGS
        for node in ast.walk(send)
    ):
        raise _fail(f"{NOTICE_SEND_FN}() no longer sends the notice as adapter.send(chat_id, msg)")
    for value in _strings(_function(tree, SHUTDOWN_FN, RUN_SHUTDOWN), "msg"):
        notices.append(ast.literal_eval(value))
    if len(notices) != 2:
        raise _fail(f"{SHUTDOWN_FN}() has {len(notices)} msg literals, expected 2")
    cron = _strings(_function(tree, CRON_INTERRUPT_FN, RUN_SHUTDOWN), "msg")
    if len(cron) != 1:
        raise _fail(f"{CRON_INTERRUPT_FN}() has {len(cron)} msg assignments, expected 1")
    rendered = compile(ast.Expression(body=cron[0]), RUN_SHUTDOWN, "eval")
    for action in cron_actions(root):
        notices.append(eval(rendered, {}, {"job": {"name": CRON_JOB_NAME}, "job_id": "id", CRON_ACTION: action}))

    tree = _tree(root, RUN_NOTIFICATIONS)
    if not _binds_alias(tree):
        raise _fail(f"{RUN_NOTIFICATIONS} does not import gateway.slack_boilerplate as {ALIAS}")
    restart = _function(tree, RESTART_FN, RUN_NOTIFICATIONS)
    sends = [
        node.lineno for node in ast.walk(restart)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "transport.send" and len(node.args) == 3
        and isinstance(node.args[2], ast.Constant) and str(node.args[2].value).startswith(RESTARTED_PREFIX)
    ]
    if len(sends) != 1:
        raise _fail(f"{RESTART_FN}() no longer sends the post-restart notice as one transport.send literal")
    if _guard_line(restart, ast.Return) > sends[0]:
        raise _fail(f"{RESTART_FN}() checks drop_notice after the post-restart notice is sent")
    startup = _function(tree, STARTUP_FN, RUN_NOTIFICATIONS)
    home_sends = [
        node.lineno for node in ast.walk(startup)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == f"self.{HOME_CHANNEL_FN}"
    ]
    if len(home_sends) != 1:
        raise _fail(f"{STARTUP_FN}() calls {HOME_CHANNEL_FN}() {len(home_sends)} times, expected 1")
    if _guard_line(startup, ast.Continue) > home_sends[0]:
        raise _fail(f"{STARTUP_FN}() checks drop_notice after the startup notice is sent")
    home = _function(tree, HOME_CHANNEL_FN, RUN_NOTIFICATIONS)
    if ALIAS in ast.unparse(home):
        raise _fail(f"{HOME_CHANNEL_FN}() is patched; the session-database warnings share it")
    return notices


def _render(node: ast.expr, relative: str, env: dict) -> str:
    return eval(compile(ast.Expression(body=node), relative, "eval"), {}, env)


def _leading_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return None


def check_error_sites(root: Path) -> None:
    """Each exception-bearing reply is built through ``error_reply``, the upstream text as its second argument."""
    for relative, prefix in ERROR_SITES:
        tree = _tree(root, relative)
        if not _binds_alias(tree):
            raise _fail(f"{relative} does not import gateway.slack_boilerplate as {ALIAS}")
        literals = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr) and (_leading_text(node) or "").startswith(prefix)
        ]
        wrapped = {
            id(node.args[1]) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == ERROR_HELPER and len(node.args) >= 3
        }
        if not literals or any(id(node) not in wrapped for node in literals):
            raise _fail(
                f"{relative} builds a {prefix!r} reply outside {ERROR_HELPER}(), so its exception can reach Slack"
            )


def check_system(root: Path) -> list[str]:
    """Check the system-reply call sites; return every such reply rendered from source."""
    replies: list[str] = []
    gerunds = action_gerunds(root)
    for relative, prefix, expected in SYSTEM_LITERALS:
        tree = _tree(root, relative)
        pieces = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
        nodes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Constant | ast.JoinedStr) and id(node) not in pieces
            and (_leading_text(node) or "").startswith(prefix)
        ]
        if len(nodes) != expected:
            raise _fail(f"{relative} has {len(nodes)} replies starting {prefix!r}, expected {expected}")
        for node, action in itertools.product(nodes, gerunds):
            self = SimpleNamespace(_status_action_gerund=lambda action=action: action)
            replies.append(_render(node, relative, {"self": self, "exc": AUTH_ERROR, **BUSY_ENV}))

    tree = _tree(root, RUN_BUSY)
    if not _binds_alias(tree):
        raise _fail(f"{RUN_BUSY} does not import gateway.slack_boilerplate as {ALIAS}")
    compose = _function(tree, BUSY_ACK_FN, RUN_BUSY)
    demoted = [
        ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and _names(node.targets) == [BUSY_DEMOTED_TAIL]
    ]
    if len(demoted) != 1:
        raise _fail(f"{RUN_BUSY} defines {BUSY_DEMOTED_TAIL} {len(demoted)} times, expected 1")
    tails = {f"self.{BUSY_DEMOTED_TAIL}": demoted[0]}
    heads = {
        node.value.elts[0].value: (
            node.value.elts[1].value if isinstance(node.value.elts[1], ast.Constant)
            else tails.get(ast.unparse(node.value.elts[1]))
        )
        for node in ast.walk(compose)
        if isinstance(node, ast.Assign) and _names(node.targets) == ["(head, tail)"]
        and isinstance(node.value, ast.Tuple) and isinstance(node.value.elts[0], ast.Constant)
    }
    for head in BUSY_HEADS:
        if heads.get(head) is None:
            raise _fail(f"{BUSY_ACK_FN}() no longer builds the {head!r} ack from literals")
        replies.extend(f"{head}{detail}{heads[head]}" for detail in BUSY_DETAILS)
    if not any(
        isinstance(node, ast.If) and BUSY_HINT in ast.unparse(node)
        and "is_seen(" in ast.unparse(node.test)
        and f"{ALIAS}.drop_notice(event.source.platform)" in ast.unparse(node.test)
        for node in ast.walk(compose)
    ):
        raise _fail(f"{BUSY_ACK_FN}() appends the busy-input hint without checking drop_notice")

    tree = _tree(root, RUN_NOTIFICATIONS)
    background = _function(tree, BACKGROUND_FN, RUN_NOTIFICATIONS)
    header = _strings(background, "header")
    ret = [node.value for node in ast.walk(background) if isinstance(node, ast.Return)]
    if len(header) != 1 or len(ret) != 1:
        raise _fail(f"{BACKGROUND_FN}() no longer builds one header and returns once")
    for cmd, output in itertools.product(BACKGROUND_CMDS, BACKGROUND_OUTPUTS):
        env = {"header": _render(header[0], RUN_NOTIFICATIONS, {"short_cmd": cmd}), "new_output": output}
        replies.append(_render(ret[0], RUN_NOTIFICATIONS, env))
    warnings = _function(tree, SESSION_DB_FN, RUN_NOTIFICATIONS)
    warning_sends = [
        node.lineno for node in ast.walk(warnings)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == f"self.{HOME_CHANNEL_FN}"
    ]
    if len(warning_sends) != 1:
        raise _fail(f"{SESSION_DB_FN}() calls {HOME_CHANNEL_FN}() {len(warning_sends)} times, expected 1")
    if _guard_line(warnings, ast.Continue) > warning_sends[0]:
        raise _fail(f"{SESSION_DB_FN}() checks drop_notice after the warning is sent")

    tree = _tree(root, SLACK_ADAPTER)
    if not _binds_alias(tree):
        raise _fail(f"{SLACK_ADAPTER} does not import gateway.slack_boilerplate as {ALIAS}")
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == SLACK_CLASS]
    for method, before in SLACK_METHODS.items():
        defs = [
            node for cls in classes for node in cls.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == method
        ]
        if len(defs) != 1:
            raise _fail(f"{SLACK_ADAPTER} has {len(defs)} {SLACK_CLASS}.{method} methods, expected 1")
        if not any(
            before in ast.unparse(first)
            and isinstance(second, ast.Assign) and _names(second.targets) == ["content"]
            and _is_helper(second.value, "system_text") and _names(second.value.args) == ["content"]
            for first, second in itertools.pairwise(defs[0].body)
        ):
            raise _fail(f"{SLACK_CLASS}.{method} does not pass content through system_text after {before}")
    return replies


def check_locale(root: Path) -> list[str]:
    """Return the /restart and /stop replies rendered from Hermes' English catalog."""
    import yaml

    path = root / LOCALES
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    catalog = yaml.safe_load(path.read_text()) or {}
    looked_up = {
        node.value for node in ast.walk(_tree(root, SLASH_COMMANDS))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    replies = []
    for key, kwargs in LOCALE_REPLIES.items():
        value = catalog
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if not isinstance(value, str):
            raise _fail(f"{LOCALES} has no {key}")
        if key not in looked_up:
            raise _fail(f"{SLASH_COMMANDS} no longer looks up {key}")
        replies.append(value.format(**kwargs))
    return replies


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_boilerplate_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None:
        raise _fail("slack_presenter did not import beside the runtime module")
    return module


def drive(module, notices: list[str], replies: list[str]) -> None:
    def extract_media(text):
        return [], text

    def target(platform):
        return SimpleNamespace(platform_name=platform, chat_id=CHANNEL, job={"id": "abc123"})

    def source(platform):
        return SimpleNamespace(platform=SimpleNamespace(value=platform))

    for flag in (None, "1"):
        if flag is None:
            os.environ.pop(FLAG_ENV, None)
        else:
            os.environ[FLAG_ENV] = flag
        on = flag is not None
        for platform in ("slack", "chat", "google_chat"):
            reworded = on and platform == "slack"
            text = module.cron_delivery_text(target(platform), REPORT, WRAPPED, extract_media)
            if text != (REPORT if reworded else WRAPPED):
                raise _fail(f"cron_delivery_text for {platform} with the flag {'on' if on else 'off'}: {text!r}")
            mode = module.long_running_mode(source(platform), "raw")
            if mode != ("generic" if reworded else "raw"):
                raise _fail(f"long_running_mode for {platform} with the flag {'on' if on else 'off'}: {mode!r}")
            if module.long_running_mode(source(platform), "off") != "off":
                raise _fail(f"long_running_mode turned an off heartbeat on for {platform}")
            if module.drop_notice(platform) != reworded:
                raise _fail(f"drop_notice for {platform} with the flag {'on' if on else 'off'}")
        for notice in notices:
            out = module.system_text(notice)
            if on and (out == notice or any(word in out for word in SYSTEM_LEFTOVERS)):
                raise _fail(f"system_text left the Slack notice {notice!r} as {out!r}")
            if not on and out is not notice:
                raise _fail(f"system_text changed the notice {notice!r} with the flag off")
        for error in ERROR_SHAPES:
            upstream = f"{AUTH_PREFIX}: {error}"
            for platform in ("slack", "chat"):
                out = module.error_reply(platform, upstream, module.AUTH_FAILED, module.AUTH_FAILED_LOG, error)
                expected = module.AUTH_FAILED if on and platform == "slack" else upstream
                if out != expected:
                    raise _fail(f"error_reply for {platform} with the flag {'on' if on else 'off'}: {out!r}")
        for reply in replies:
            out = module.system_text(reply)
            if on and (out == reply or any(word in out for word in SYSTEM_LEFTOVERS)):
                raise _fail(f"system_text left the Slack reply {reply!r} as {out!r}")
            if not on and out is not reply:
                raise _fail(f"system_text changed {reply!r} with the flag off")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_delivery(root)
    check_target_fields(root)
    check_heartbeat(root)
    notices = check_notices(root)
    replies = check_system(root) + check_locale(root)
    check_error_sites(root)
    check_bound(root)
    runtime = _load_runtime(root)
    check_action_words(runtime, root)
    drive(runtime, notices, replies)
    print(
        "slack_boilerplate verify: Slack cron targets send the unwrapped report, the heartbeat "
        f"goes generic on Slack, {len(notices)} interrupting "
        f"notices and {len(replies)} system replies reworded, exception text kept out of the "
        "steer and auth failure replies where they are built, and both back-online notices, the "
        "session-database warnings and the busy-input hint kept off Slack; every name the hooks read "
        "is bound, and upstream's cron target still declares every field the helper reads; flag off and "
        "every other platform unchanged"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
