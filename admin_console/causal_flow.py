"""Semantic projection for the agent-work causal flow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from admin_console.domain import ActivityEvent, AttributionLevel

_TRUSTED_ATTRIBUTION = {
    AttributionLevel.EXPLICIT,
    AttributionLevel.INHERITED,
}


@dataclass(frozen=True)
class CausalFlowProjection:
    """Canonical LLM work separated from raw telemetry evidence."""

    events: tuple[ActivityEvent, ...]
    llm_calls: int
    tool_calls: int
    approvals: int
    skill_loads: int
    hidden_evidence: int

    @classmethod
    def from_events(
        cls, events: Iterable[ActivityEvent]
    ) -> "CausalFlowProjection":
        selected: list[ActivityEvent] = []
        counts = {"model": 0, "tool": 0, "approval": 0, "skill": 0}
        hidden = 0
        for event in events:
            source = event.details.get("source", "")
            span_name = event.details.get("span_name", "")
            parent_name = event.details.get("parent_span_name", "")
            trusted = event.attribution in _TRUSTED_ATTRIBUTION

            kind = ""
            if source == "cloud_trace" and trusted:
                if event.action_type == "model" and span_name.startswith("api."):
                    kind = "model"
                elif event.action_type in {"tool", "approval"} and parent_name.startswith(
                    "llm."
                ):
                    kind = event.action_type
                elif event.action_type == "skill":
                    kind = "skill"

            if not kind:
                hidden += 1
                continue
            selected.append(event)
            counts[kind] += 1

        return cls(
            tuple(selected),
            counts["model"],
            counts["tool"],
            counts["approval"],
            counts["skill"],
            hidden,
        )

    @property
    def summary(self) -> str:
        shown = len(self.events)
        return (
            f"{shown} agent action(s): {self.llm_calls} LLM call(s), "
            f"{self.tool_calls} LLM-produced tool call(s), "
            f"{self.approvals} approval(s), and {self.skill_loads} skill load(s). "
            f"{self.hidden_evidence} transport, lifecycle, duplicate, or "
            "unattributed evidence record(s) hidden from this flow."
        )
