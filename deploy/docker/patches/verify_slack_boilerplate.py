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
   ``long_running_mode`` directly after it is read. The interrupting notice
   send goes through ``notice_text``. The post-restart send and the
   home-channel startup send each sit behind a ``drop_notice`` guard that
   returns or continues first, and ``_send_home_channel_message``, which the
   session-database warnings share, carries no guard; their own loop does. The
   busy-input onboarding hint is gated on ``drop_notice``, and
   ``SlackAdapter.send`` passes its content through ``system_text`` once the
   DM target is resolved.
2. The notices themselves. Each interrupting notice is read out of the patched
   source, rendered, and handed to the runtime: on Slack with the flag on it
   must come back reworded. ``notice_text`` passes text it does not recognise
   through unchanged, so an upstream rewording would otherwise put Hermes'
   wording back on Slack with nothing failing.
3. The other system replies, rendered from the patched source the same way:
   the busy acks, the drain and restart refusals, the force-stop reply, the
   background-task update and both provider authentication failures. With the
   flag on, ``system_text`` must reword every one, with no emoji, "Gateway",
   "agent" or exception text left; ``system_text`` passes unrecognised text
   through, so this is what catches an upstream rewording.
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

FLAG_ENV = "KAGE_SLACK_UX"
ALIAS = "_kage_slack_boilerplate"
RUNTIME = "gateway/slack_boilerplate.py"

DELIVERY = "cron/scheduler_delivery.py"
DELIVER_FN = "_deliver_result"
SEND_LANES = ("_deliver_via_live_adapter", "_deliver_standalone")
TARGET_TEXT = "target_text"
WRAPPER_HEADER = "Cronjob Response: "

RUN_TURN = "gateway/run_turn.py"
HEARTBEAT_FN = "_run_agent_notify_long_running"
HEARTBEAT_MODE = "_long_running_mode"

RUN_SHUTDOWN = "gateway/run_shutdown.py"
NOTICE_SEND_FN = "_send_notice_logged"
SHUTDOWN_FN = "_notify_active_sessions_of_shutdown"
CRON_INTERRUPT_FN = "_notify_interrupted_cron_jobs"

RUN_NOTIFICATIONS = "gateway/run_notifications.py"
RESTARTED_PREFIX = "♻ Gateway restarted"
RESTART_FN = "_send_restart_notification"
HOME_CHANNEL_FN = "_send_home_channel_message"
STARTUP_FN = "_send_home_channel_startup_notifications"
STATUS_METADATA = "turn_ctx._status_thread_metadata"
THREAD_METADATA = {"thread_id": "1700000000.000100"}

SESSION_DB_FN = "_send_session_db_warning_notifications"

RUN_BUSY = "gateway/run_busy.py"
BUSY_ACK_FN = "_compose_busy_ack_message"
BUSY_HINT = "busy_input_hint_gateway"
BUSY_HEADS = ("⏳ Queued for the next turn", "⚡ Interrupting current task")
BUSY_DETAILS = ("", " (3 min elapsed, running: terminal)")
DRAIN_FN = "_send_busy_drain_notice"

RUN_INBOUND = "gateway/run_inbound.py"
RUN_TURN_RUNNER = "gateway/run_turn_runner.py"
RUN = "gateway/run.py"
BACKGROUND_FN = "_format_process_running_message"
BACKGROUND_CMDS = ("make build", "")
BACKGROUND_OUTPUTS = ("", "step 3/9")
AUTH_PREFIX = "⚠️ Provider authentication failed"
#: Stands in for the provider exception; Slack must never see it.
AUTH_ERROR = "401 stand-in provider error"

SLACK_ADAPTER = "plugins/platforms/slack/adapter.py"
SLACK_CLASS = "SlackAdapter"

#: (file, literal prefix, how many literals or f-strings carry it) for the
#: replies read straight out of a module; ``self._status_action_gerund()`` is
#: rendered for each of CRON_ACTIONS.
SYSTEM_LITERALS = (
    (RUN_BUSY, "⏳ Gateway", 2),
    (RUN_INBOUND, "⏳ Gateway", 3),
    (RUN_INBOUND, "⏳ This agent is draining", 1),
    (RUN_INBOUND, "⏳ Another turn is still running", 1),
    (RUN_INBOUND, "⚡ Force-stopped", 1),
    (RUN_TURN_RUNNER, AUTH_PREFIX, 1),
    (RUN, AUTH_PREFIX, 1),
)
#: Left on a reworded reply, any of these means the rewording missed.
SYSTEM_LEFTOVERS = ("⏳", "⚡", "⚠️", "Gateway", "agent", AUTH_ERROR)

#: Stands in for the names the interrupted-cron-job notice interpolates.
CRON_JOB_NAME = "inventory"
CRON_ACTIONS = ("restarting", "shutting down")

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
                and _names(second.value.args) == ["turn_ctx.source", HEARTBEAT_MODE, STATUS_METADATA]
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


def check_notices(root: Path) -> list[str]:
    """Check the notice call sites; return every interrupting notice rendered from source."""
    notices: list[str] = []

    tree = _tree(root, RUN_SHUTDOWN)
    if not _binds_alias(tree):
        raise _fail(f"{RUN_SHUTDOWN} does not import gateway.slack_boilerplate as {ALIAS}")
    send = _function(tree, NOTICE_SEND_FN, RUN_SHUTDOWN)
    if not any(
        isinstance(node, ast.Call) and ast.unparse(node.func) == "adapter.send"
        and len(node.args) == 2 and _is_helper(node.args[1], "notice_text")
        and _names(node.args[1].args) == ["platform_str", "msg"]
        for node in ast.walk(send)
    ):
        raise _fail(f"{NOTICE_SEND_FN}() does not send notice_text(platform_str, msg)")
    for value in _strings(_function(tree, SHUTDOWN_FN, RUN_SHUTDOWN), "msg"):
        notices.append(ast.literal_eval(value))
    if len(notices) != 2:
        raise _fail(f"{SHUTDOWN_FN}() has {len(notices)} msg literals, expected 2")
    cron = _strings(_function(tree, CRON_INTERRUPT_FN, RUN_SHUTDOWN), "msg")
    if len(cron) != 1:
        raise _fail(f"{CRON_INTERRUPT_FN}() has {len(cron)} msg assignments, expected 1")
    rendered = compile(ast.Expression(body=cron[0]), RUN_SHUTDOWN, "eval")
    for action in CRON_ACTIONS:
        notices.append(eval(rendered, {}, {"job": {"name": CRON_JOB_NAME}, "job_id": "id", "action": action}))

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


def check_system(root: Path) -> list[str]:
    """Check the system-reply call sites; return every such reply rendered from source."""
    replies: list[str] = []
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
        for node, action in itertools.product(nodes, CRON_ACTIONS):
            self = SimpleNamespace(_status_action_gerund=lambda action=action: action)
            replies.append(_render(node, relative, {"self": self, "exc": AUTH_ERROR}))

    tree = _tree(root, RUN_BUSY)
    if not _binds_alias(tree):
        raise _fail(f"{RUN_BUSY} does not import gateway.slack_boilerplate as {ALIAS}")
    compose = _function(tree, BUSY_ACK_FN, RUN_BUSY)
    heads = {
        node.value.elts[0].value: node.value.elts[1].value for node in ast.walk(compose)
        if isinstance(node, ast.Assign) and _names(node.targets) == ["(head, tail)"]
        and isinstance(node.value, ast.Tuple) and all(isinstance(e, ast.Constant) for e in node.value.elts)
    }
    for head in BUSY_HEADS:
        if head not in heads:
            raise _fail(f"{BUSY_ACK_FN}() no longer builds the {head!r} ack")
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
    sends = [
        node for cls in classes for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "send"
    ]
    if len(sends) != 1:
        raise _fail(f"{SLACK_ADAPTER} has {len(sends)} {SLACK_CLASS}.send methods, expected 1")
    if not any(
        isinstance(first, ast.Assign) and _names(first.targets) == ["chat_id"] and "_dm_target" in ast.unparse(first)
        and isinstance(second, ast.Assign) and _names(second.targets) == ["content"]
        and _is_helper(second.value, "system_text") and _names(second.value.args) == ["content"]
        for first, second in itertools.pairwise(sends[0].body)
    ):
        raise _fail(f"{SLACK_CLASS}.send does not pass content through system_text after _dm_target")
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
            mode = module.long_running_mode(source(platform), "raw", None)
            if mode != ("generic" if reworded else "raw"):
                raise _fail(f"long_running_mode for {platform} with the flag {'on' if on else 'off'}: {mode!r}")
            mode = module.long_running_mode(source(platform), "raw", THREAD_METADATA)
            if mode != ("off" if reworded else "raw"):
                raise _fail(f"long_running_mode under a status line for {platform}: {mode!r}")
            if module.long_running_mode(source(platform), "off", None) != "off":
                raise _fail(f"long_running_mode turned an off heartbeat on for {platform}")
            if module.drop_notice(platform) != reworded:
                raise _fail(f"drop_notice for {platform} with the flag {'on' if on else 'off'}")
            for notice in notices:
                out = module.notice_text(platform, notice)
                if reworded and (out == notice or "Gateway" in out or "Hermes" in out):
                    raise _fail(f"notice_text left the Slack notice {notice!r} as {out!r}")
                if not reworded and out != notice:
                    raise _fail(f"notice_text changed {notice!r} for {platform} with the flag {'on' if on else 'off'}")
        for reply in replies:
            out = module.system_text(reply)
            if on and (out == reply or any(word in out for word in SYSTEM_LEFTOVERS)):
                raise _fail(f"system_text left the Slack reply {reply!r} as {out!r}")
            if not on and out is not reply:
                raise _fail(f"system_text changed {reply!r} with the flag off")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_delivery(root)
    check_heartbeat(root)
    notices = check_notices(root)
    replies = check_system(root)
    drive(_load_runtime(root), notices, replies)
    print(
        "slack_boilerplate verify: Slack cron targets send the unwrapped report, the heartbeat "
        f"drops under the status line and goes generic elsewhere on Slack, {len(notices)} interrupting "
        f"notices and {len(replies)} system replies reworded, and both back-online notices, the "
        "session-database warnings and the busy-input hint kept off Slack; "
        "flag off and every other platform unchanged"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
