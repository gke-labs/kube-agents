#!/usr/bin/env python3
"""Build-time behaviour gate for the KAGE_SLACK_UX boilerplate patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_slack_boilerplate.py``, with ``slack_presenter.py`` staged beside
this script.

Three things are checked:

1. The call sites. Both cron send lanes take ``target_text``, bound from
   ``cron_delivery_text`` over the unwrapped ``content`` and the wrapped
   ``cleaned_delivery_content``, and the wrapper itself is still built, so
   every other target keeps it. The heartbeat mode is passed through
   ``long_running_mode`` directly after it is read. The notice sends go
   through ``notice_text``.
2. The notices themselves. Each lifecycle notice is read out of the patched
   source, rendered, and handed to the runtime: on Slack with the flag on it
   must come back reworded. ``notice_text`` passes text it does not recognise
   through unchanged, so an upstream rewording would otherwise put Hermes'
   wording back on Slack with nothing failing.
3. The runtime module, loaded by path: flag off, and for any platform other
   than Slack, every helper returns its input.
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
HOME_CHANNEL_FN = "_send_home_channel_message"
STARTUP_FN = "_send_home_channel_startup_notifications"

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


def check_notices(root: Path) -> list[str]:
    """Check the notice call sites; return every notice rendered from source."""
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
    restarted = [
        node for node in ast.walk(tree)
        if _is_helper(node, "notice_text") and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant) and str(node.args[1].value).startswith(RESTARTED_PREFIX)
    ]
    if len(restarted) != 1:
        raise _fail(f"the post-restart notice is not sent through notice_text in {RUN_NOTIFICATIONS}")
    notices.append(restarted[0].args[1].value)
    home = _function(tree, HOME_CHANNEL_FN, RUN_NOTIFICATIONS)
    if not any(
        isinstance(node, ast.Assign) and _names(node.targets) == ["message"]
        and _is_helper(node.value, "notice_text") and _names(node.value.args) == ["platform", "message"]
        for node in ast.walk(home)
    ):
        raise _fail(f"{HOME_CHANNEL_FN}() does not reword its message through notice_text")
    startup = [ast.literal_eval(v) for v in _strings(_function(tree, STARTUP_FN, RUN_NOTIFICATIONS), "message")
               if isinstance(v, ast.Constant)]
    if len(startup) != 1:
        raise _fail(f"{STARTUP_FN}() has {len(startup)} literal messages, expected 1")
    notices.append(startup[0])
    return notices


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


def drive(module, notices: list[str]) -> None:
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
            for notice in notices:
                out = module.notice_text(platform, notice)
                if reworded and (out == notice or "Gateway" in out or "Hermes" in out):
                    raise _fail(f"notice_text left the Slack notice {notice!r} as {out!r}")
                if not reworded and out != notice:
                    raise _fail(f"notice_text changed {notice!r} for {platform} with the flag {'on' if on else 'off'}")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_delivery(root)
    check_heartbeat(root)
    notices = check_notices(root)
    drive(_load_runtime(root), notices)
    print(
        "slack_boilerplate verify: Slack cron targets send the unwrapped report, the heartbeat "
        f"goes generic on Slack, {len(notices)} lifecycle notices reworded on Slack; "
        "flag off and every other platform unchanged"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
