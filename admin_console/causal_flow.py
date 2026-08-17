"""Semantic projection for the agent-work causal flow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from admin_console.domain import ActivityEvent


@dataclass(frozen=True)
class SourceEvidence:
    """One raw OTel source attribute and where it was observed."""

    field: str
    value: str
    scope: str


@dataclass(frozen=True)
class CausalSource:
    """Stable aggregation identity plus all available OTel origin evidence."""

    key: str
    source_type: str
    source_id: str
    primary: SourceEvidence | None
    evidence: tuple[SourceEvidence, ...]
    session_id: str

    @staticmethod
    def _raw(
        details: dict[str, str], attribute: str
    ) -> SourceEvidence | None:
        direct = str(details.get(f"otel.{attribute}", ""))
        if direct:
            return SourceEvidence(attribute, direct, "span")
        inherited = str(details.get(f"otel.trace.{attribute}", ""))
        if inherited:
            return SourceEvidence(attribute, inherited, "trace")
        return None

    @staticmethod
    def _cron_job(session_id: str) -> str:
        match = re.fullmatch(r"cron_(.+)_\d{8}_\d{6}", session_id)
        return match.group(1) if match else ""

    @classmethod
    def from_event(cls, event: ActivityEvent) -> "CausalSource":
        details = event.details
        attributes = (
            "session.id",
            "user.id",
            "hermes.sender.id",
            "chat.platform",
            "trigger.kind",
            "hermes.session.kind",
        )
        evidence = tuple(
            item
            for attribute in attributes
            if (item := cls._raw(details, attribute)) is not None
        )
        by_field = {item.field: item for item in evidence}
        session = by_field.get("session.id")
        if session is None and event.session_id:
            session = SourceEvidence("session.id", event.session_id, "event")
            evidence = (session, *evidence)
        session_id = session.value if session else ""
        cron_job = cls._cron_job(session_id)

        primary = by_field.get("user.id") or by_field.get("hermes.sender.id")
        if primary:
            source_type, source_id = "user", primary.value
        elif primary := by_field.get("chat.platform"):
            source_type, source_id = "platform", primary.value
        elif cron_job:
            source_type, source_id = "cron", cron_job
            primary = session
        elif primary := by_field.get("trigger.kind"):
            source_type, source_id = "trigger", primary.value
        elif primary := by_field.get("hermes.session.kind"):
            source_type, source_id = "session_kind", primary.value
        elif session:
            source_type, source_id = "session", session.value
            primary = session
        else:
            source_type, source_id = "unknown", "unknown"
            primary = None
        return cls(
            key=f"{source_type}\0{source_id}",
            source_type=source_type,
            source_id=source_id,
            primary=primary,
            evidence=evidence,
            session_id=session_id,
        )


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
            kind = ""
            if source == "cloud_trace":
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
            "non-work evidence record(s) hidden from this flow."
        )
