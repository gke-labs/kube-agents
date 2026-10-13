#!/usr/bin/env python3
"""Build gate for the permission-refusal stop.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_permission_refusal_stop.py``. The applier proves its two anchors
matched; this drives the patched runtime:

1. ``ToolCallGuardrailConfig.from_mapping`` reads
   ``permission_refusal_halt_after`` and defaults it.
2. The real ``ToolCallGuardrailController``: at the default a command-policy
   refusal halts on the first call; at a threshold of two the first returns
   the guidance decision and the second the halt, with a successful call and
   an unrelated failure in between (the stock counters would have reset); the
   halt is the turn's ``halt_decision``, so ``agent/turn_tool_round.py``'s
   halt branch sees it; a new turn starts the count again; an API 403 and a
   ``gcp.read-only`` refusal warn and never halt; a flag refusal and a plain
   failure take the stock path.
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

REFUSAL = (
    '{"output": "Agents hold read-only access to Kubernetes. Propose this change as a pull '
    "request instead. This is a permission boundary, not an error to work around: if this "
    "command would change cluster or cloud state, that change is refused however it is "
    'attempted, so do not retry it through another command, tool, flag, or identity.\\npolicy '
    'rule: kubernetes.read-only\\n", "exit_code": 77}'
)
FLAG_REFUSAL = (
    '{"output": "This is a permission boundary, not an error to work around: the command may be '
    'retried without the flag, but what the flag itself does is withheld.\\npolicy rule: '
    'identity.caller-supplied-impersonation\\n", "exit_code": 77}'
)
API_DENIED = '{"output": "Error from server (Forbidden): pods is forbidden: User cannot list", "exit_code": 1}'
GCLOUD_REFUSAL = (
    '{"output": "Agents hold read-only access to Google Cloud. Propose this change as a pull request '
    "instead. This is a permission boundary, not an error to work around: if this command would change "
    'cluster or cloud state, that change is refused however it is attempted.\\npolicy rule: gcp.read-only\\n", '
    '"exit_code": 77}'
)
PLAIN_FAILURE = '{"output": "error: the server doesn\'t have a resource type \\"foo\\"", "exit_code": 1}'
OK = '{"output": "deployment.apps/kube-dns 2/2", "exit_code": 0}'


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
    second = controller.after_call("terminal", {"command": "kubectl patch deploy x -p y"}, REFUSAL)
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
    gcloud = controller.after_call("terminal", {"command": "gcloud artifacts docker tags list x"}, GCLOUD_REFUSAL)
    if (gcloud.action, gcloud.code) != ("warn", stop.GCLOUD_WARN_CODE) or controller.halt_decision is not None:
        return fail(f"a gcp.read-only refusal decided {gcloud!r}")
    for result in (FLAG_REFUSAL, PLAIN_FAILURE, OK):
        decision = controller.after_call("terminal", {"command": "kubectl get x"}, result)
        if decision.code in (stop.WARN_CODE, stop.GCLOUD_WARN_CODE, stop.API_WARN_CODE, stop.HALT_CODE):
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
