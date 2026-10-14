#!/usr/bin/env python3
"""Build gate for the permission-refusal stop.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_permission_refusal_stop.py``. The applier proves its three anchors
matched; this drives the patched runtime:

1. ``ToolCallGuardrailConfig.from_mapping`` reads
   ``permission_refusal_halt_after`` and defaults it.
2. The real ``ToolCallGuardrailController``, fed what the shim prints for
   refusals the image's own policy module makes: at the default a refused
   write halts on the first call; at a threshold of two the first returns the
   guidance decision and the second (from ``execute_code``) the halt, with a
   successful call and an unrelated failure in between (the stock counters
   would have reset); the halt is the turn's ``halt_decision``, so
   ``agent/turn_tool_round.py``'s halt branch sees it; a new turn starts the
   count again; an API 403, a ``gcp.read-only`` refusal and a
   ``kubernetes.read-only`` refusal of a read spelling, a help read or a
   local render warn and never halt; a report echoing the lines and a
   successful read quoting a denial decide nothing; a chained write with
   exit 0, exit 126 and ``--dry-run=server`` still count; a flag refusal and
   a plain failure take the stock path.
3. ``append_toolguard_guidance`` renders the guidance into the tool result.
4. ``hermes_cli.kanban_guardrail_exit.guardrail_halt_nudge`` gives the halt its
   refusal-specific instruction, driven against the real
   ``agent.kanban_stop.build_kanban_stop_nudge``; and
   ``AIAgent._toolguard_controlled_halt_response`` returns the halt's message
   as a chat turn's reply.

Usage::

    cd /opt/hermes && python3 verify_permission_refusal_stop.py
"""

from __future__ import annotations

import os
import sys

import json

# The policy module the image ships (Dockerfile copies agents/platform/scripts/command_policy.py
# to tools/); the refusals below are what the credential-proxy shim prints for it.
from tools import command_policy
from tools.command_policy import _ACTION_BOUNDARY_NOTICE, _FLAG_BOUNDARY_NOTICE


def _result(output, exit_code=77):
    return json.dumps({"output": output, "exit_code": exit_code, "error": None})


def shim_lines(argv):
    decision = command_policy.evaluate(argv)
    assert not decision.allowed, argv
    lines = [decision.message, f"policy rule: {decision.rule_id}"]
    if decision.verb_tuple:
        lines.append("policy verb: " + " ".join(decision.verb_tuple))
    if command_policy.local_render(argv):
        lines.append("policy render: local")
    return "\n".join(lines)


def refusal(*argv, exit_code=77):
    return _result(shim_lines(list(argv)), exit_code)


SCALE = ["kubectl", "scale", "deploy/x", "--replicas=3"]
REFUSAL = refusal(*SCALE)
FLAG_REFUSAL = _result(_FLAG_BOUNDARY_NOTICE + "\npolicy rule: identity.caller-supplied-impersonation")
API_DENIED = _result("Error from server (Forbidden): pods is forbidden: User cannot list", 1)
GCLOUD_REFUSAL = _result(
    "Agents hold read-only access to Google Cloud. Propose this change as a pull request instead. "
    + _ACTION_BOUNDARY_NOTICE + "\npolicy rule: gcp.read-only\npolicy verb: container clusters update"
)
PLAIN_FAILURE = _result("error: the server doesn't have a resource type \"foo\"", 1)
OK = _result("deployment.apps/kube-dns 2/2", 0)
EVENTS = _result('LAST SEEN  MESSAGE\n2m  Error creating: pods "x" is forbidden: exceeded quota', 0)


def fail(message: str) -> int:
    print(f"verify_permission_refusal_stop: {message}", file=sys.stderr)
    return 1


def main() -> int:
    from agent.tool_guardrails import (
        ToolCallGuardrailConfig,
        ToolCallGuardrailController,
        append_toolguard_guidance,
    )
    from hermes_cli import permission_refusal_stop as stop

    # 1. The knob.
    default = ToolCallGuardrailConfig.from_mapping({})
    if getattr(default, stop.HALT_AFTER_ATTR, None) != stop.DEFAULT_HALT_AFTER:
        return fail("from_mapping did not default the halt threshold")
    tuned = ToolCallGuardrailConfig.from_mapping({"permission_refusal_halt_after": 2})
    if getattr(tuned, stop.HALT_AFTER_ATTR, None) != 2:
        return fail("from_mapping did not read permission_refusal_halt_after")

    # 2a. The controller at the default: the first refusal halts.
    controller = ToolCallGuardrailController(default)
    halt = controller.after_call("terminal", {"command": "kubectl scale deploy x --replicas=3"}, REFUSAL)
    if (halt.action, halt.code, halt.count) != ("halt", stop.HALT_CODE, 1) or controller.halt_decision is not halt:
        return fail(f"the first refusal at the default decided {halt!r}")

    # 2b. At two: guidance first, the halt second, across a success and a plain failure.
    controller = ToolCallGuardrailController(tuned)
    first = controller.after_call("terminal", {"command": "kubectl scale deploy x --replicas=3"}, REFUSAL)
    if (first.action, first.code, first.count) != ("warn", stop.WARN_CODE, 1):
        return fail(f"first refusal decided {first!r}")
    if controller.halt_decision is not None:
        return fail("first refusal halted")
    controller.after_call("terminal", {"command": "kubectl get deploy x"}, OK)
    controller.after_call("terminal", {"command": "kubectl get foo"}, PLAIN_FAILURE)
    second = controller.after_call("execute_code", {"code": "subprocess.run(['kubectl','patch','deploy/x'])"}, refusal("kubectl", "patch", "deploy/x", "-p", "{}"))
    if (second.action, second.code, second.count) != ("halt", stop.HALT_CODE, 2):
        return fail(f"second refusal decided {second!r}")
    if controller.halt_decision is not second:
        return fail("the halt is not the turn's halt decision")
    controller.reset_for_turn()
    again = controller.after_call("terminal", {"command": "kubectl scale deploy x --replicas=3"}, REFUSAL)
    if again.action != "warn" or again.count != 1:
        return fail(f"a new turn did not restart the count: {again!r}")
    api = controller.after_call("terminal", {"command": "kubectl get pods -n other"}, API_DENIED)
    if (api.action, api.code) != ("warn", stop.API_WARN_CODE) or controller.halt_decision is not None:
        return fail(f"an API 403 decided {api!r}")
    for argv in (
        ["kubectl", "config", "view"],
        ["kubectl", "scale", "--help"],
        ["kubectl", "exec", "-n", "kube-system", "deploy/x", "--", "sh", "-c", "set -e; cat /etc/resolv.conf"],
        ["kubectl", "create", "deployment", "x", "--image=y", "--dry-run=client", "-o", "yaml"],
    ):
        read = controller.after_call("terminal", {"command": " ".join(argv)}, refusal(*argv))
        if (read.action, read.code) != ("warn", stop.ADVISORY_WARN_CODE) or controller.halt_decision is not None:
            return fail(f"{argv!r} refused decided {read!r}")
    gcloud = controller.after_call("terminal", {"command": "gcloud container clusters update x"}, GCLOUD_REFUSAL)
    if (gcloud.action, gcloud.code) != ("warn", stop.ADVISORY_WARN_CODE) or controller.halt_decision is not None:
        return fail(f"a gcp.read-only refusal decided {gcloud!r}")
    events = controller.after_call("terminal", {"command": "kubectl get events -n tenant"}, EVENTS)
    if events.code in (stop.WARN_CODE, stop.ADVISORY_WARN_CODE, stop.API_WARN_CODE, stop.HALT_CODE):
        return fail(f"a successful read quoting a denial decided {events!r}")
    # A report that echoes the refusal's own lines decides nothing; one that only
    # names the command and the rule beside a saved refusal is counted (a stated limit).
    report = controller.after_call(
        "terminal",
        {"command": "cat <<'EOF' | tee /tmp/pr.md\nkubectl scale deploy x --replicas=3 was refused:\n" + shim_lines(SCALE) + "\nEOF"},
        _result(shim_lines(SCALE), 0),
    )
    if report.code in (stop.WARN_CODE, stop.ADVISORY_WARN_CODE, stop.API_WARN_CODE, stop.HALT_CODE) or controller.halt_decision is not None:
        return fail(f"a report quoting the refusal decided {report!r}")
    for command, result in (
        ("kubectl scale deploy x --replicas=3; kubectl get deploy x", refusal(*SCALE, exit_code=0)),
        ("kubectl create deployment x --image=y --dry-run=client -o yaml | kubectl apply -f -",
         _result(shim_lines(["kubectl", "create", "deployment", "x", "--image=y", "--dry-run=client", "-o", "yaml"]) + "\n" + shim_lines(["kubectl", "apply", "-f", "-"]))),
        ("kubectl scale deploy x --replicas=3", refusal(*SCALE, exit_code=126)),
        ("kubectl scale deploy x --replicas=3 --dry-run=server", refusal("kubectl", "scale", "deploy/x", "--replicas=3", "--dry-run=server")),
    ):
        decided = controller.after_call("terminal", {"command": command}, result)
        if decided.code not in (stop.WARN_CODE, stop.HALT_CODE):
            return fail(f"{command!r} decided {decided!r}")
    for result in (FLAG_REFUSAL, PLAIN_FAILURE, OK):
        decision = controller.after_call("terminal", {"command": "kubectl get x"}, result)
        if decision.code in (stop.WARN_CODE, stop.ADVISORY_WARN_CODE, stop.API_WARN_CODE, stop.HALT_CODE):
            return fail(f"{result[:40]!r} took the refusal path: {decision!r}")

    # 3. The guidance reaches the tool result.
    rendered = append_toolguard_guidance(REFUSAL, first)
    if stop.WARN_CODE not in rendered or "do not retry it through another command" not in rendered:
        return fail("guidance was not appended to the tool result")

    # 4. The nudge names the refusal, and the chat reply is the halt's message.
    from agent.kanban_stop import build_kanban_stop_nudge
    from hermes_cli.kanban_guardrail_exit import guardrail_halt_nudge
    from run_agent import AIAgent

    if AIAgent._toolguard_controlled_halt_response(None, halt) != stop.HALT_MESSAGE:
        return fail("the chat halt response is not the refusal message")
    if "stopped retrying" not in AIAgent._toolguard_controlled_halt_response(None, first):
        return fail("a non-refusal halt lost the stock response")

    os.environ["HERMES_KANBAN_TASK"] = "t_verify01"
    try:
        nudge = guardrail_halt_nudge(build_kanban_stop_nudge, messages=[], attempts=0, decision=second)
    finally:
        os.environ.pop("HERMES_KANBAN_TASK", None)
    if not nudge or "permission" not in nudge.lower() or "kanban_block" not in nudge:
        return fail(f"the halt nudge does not name the refusal: {nudge!r}")

    print("verify_permission_refusal_stop: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
