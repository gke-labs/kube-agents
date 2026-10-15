"""Install the permission-refusal stop into Hermes's tool guardrails.

Three anchored edits, two in ``agent/tool_guardrails.py`` and one in ``run_agent.py``:

1. ``ToolCallGuardrailController.after_call`` — the stock method is renamed
   ``_kube_stock_after_call`` and a new ``after_call`` runs it first, then asks
   ``hermes_cli.permission_refusal_stop.observe_refusal`` whether a refusal
   replaces its decision (guidance, or the halt the halt branch in
   ``agent/turn_tool_round.py`` already routes through the kanban-aware exit).
   The stock counters therefore see every call.
2. ``ToolCallGuardrailConfig.from_mapping`` — read
   ``tool_loop_guardrails.permission_refusal_halt_after`` onto the config, so
   the threshold sits beside the other guardrail thresholds in a profile's
   ``config.yaml``. The config is a frozen dataclass; the attribute is added
   with ``object.__setattr__`` rather than a new field, so ``asdict`` and the
   stock constructor are unchanged.
3. ``AIAgent._toolguard_controlled_halt_response`` — a permission-refusal halt
   returns its own message as the chat turn's reply, instead of the stock
   "stopped retrying after N non-progressing attempts" text, which is about
   loops and tells the user to change strategy.

Each edit leaves a marker the applier refuses on a second run.
See the module docstring in permission_refusal_stop.py for the defect.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "permission-refusal-stop"
GUARDRAILS_RELATIVE = "agent/tool_guardrails.py"
RUN_AGENT_RELATIVE = "run_agent.py"

# The signature of after_call, unique in the file.
AFTER_CALL_ANCHOR = (
    "    def after_call(\n"
    "        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,\n"
    "        *, failed: bool | None = None,\n"
    "    ) -> ToolGuardrailDecision:\n"
)
AFTER_CALL_WRAPPER = """    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: str | None,
        *, failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        # kube-agents patch: the stock counters run first; a definitive
        # permission refusal then overrides their decision (counted across
        # the turn, halting at the configured number), and a refusal that is
        # only guidance replaces an allow/warn. See hermes_cli/permission_refusal_stop.py.
        decision = self._kube_stock_after_call(tool_name, args, result, failed=failed)
        try:
            from hermes_cli.permission_refusal_stop import observe_refusal as _kube_observe_refusal

            if failed is None:
                failed, _ = classify_tool_failure(tool_name, result)
            _kube_refusal = _kube_observe_refusal(
                self, tool_name, _coerce_args(args), result, decision.signature,
                stock=decision, failed=failed, decide=self._decide, decision_cls=ToolGuardrailDecision,
            )
        except Exception:
            _kube_refusal = None
        return decision if _kube_refusal is None else _kube_refusal

"""
AFTER_CALL_MARKER = "_kube_observe_refusal("

FROM_MAPPING_ANCHOR = (
    '        return cls(loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")), **flags, **thresholds)\n'
)
FROM_MAPPING_PATCHED = """        config = cls(loop_caps=LoopCapConfig.from_mapping(data.get("loop_caps")), **flags, **thresholds)
        # kube-agents patch: the permission-refusal halt threshold, read by
        # hermes_cli/permission_refusal_stop.py; the default lives there.
        object.__setattr__(
            config, "kube_permission_refusal_halt_after",
            _int_at_least(data.get("permission_refusal_halt_after"), 1, 1),
        )
        return config
"""
FROM_MAPPING_MARKER = "kube_permission_refusal_halt_after"

HALT_RESPONSE_ANCHOR = (
    "    def _toolguard_controlled_halt_response(self, decision: ToolGuardrailDecision) -> str:\n"
    "        return (\n"
)
HALT_RESPONSE_PATCHED = (
    "    def _toolguard_controlled_halt_response(self, decision: ToolGuardrailDecision) -> str:\n"
    "        # kube-agents patch: a permission-refusal halt is the reply, not a loop report.\n"
    "        if decision.code == \"permission_refusal_halt\" and decision.message:\n"
    "            return decision.message\n"
    "        return (\n"
)
HALT_RESPONSE_MARKER = 'decision.code == "permission_refusal_halt"'

AFTER_CALL_PATCHED = AFTER_CALL_WRAPPER + AFTER_CALL_ANCHOR.replace("def after_call(", "def _kube_stock_after_call(")


def apply(root: Path) -> None:
    guardrails = patchlib.Patch(root, GUARDRAILS_RELATIVE, prefix=PREFIX)
    guardrails.refuse_if_patched(AFTER_CALL_MARKER, FROM_MAPPING_MARKER)
    guardrails.substitute(AFTER_CALL_ANCHOR, AFTER_CALL_PATCHED, label="after_call wrapper")
    guardrails.substitute(FROM_MAPPING_ANCHOR, FROM_MAPPING_PATCHED, label="from_mapping threshold")

    run_agent = patchlib.Patch(root, RUN_AGENT_RELATIVE, prefix=PREFIX)
    run_agent.refuse_if_patched(HALT_RESPONSE_MARKER)
    run_agent.substitute(HALT_RESPONSE_ANCHOR, HALT_RESPONSE_PATCHED, label="chat halt reply")

    guardrails.commit("after_call runs the stock counters, then the permission-refusal classifier; from_mapping reads the threshold")
    run_agent.commit("a permission-refusal halt is the chat turn's reply")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: apply_permission_refusal_stop.py <hermes root>", file=sys.stderr)
        return 64
    apply(Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
