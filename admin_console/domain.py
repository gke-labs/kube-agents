"""Domain models shared by console pages and telemetry providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class TriggerKind(StrEnum):
    HUMAN = "human"
    CRON = "cron"
    EVENT = "event"
    RETRY = "retry"
    AGENT_FOLLOWUP = "agent_followup"
    UNKNOWN = "unknown"


class AttributionLevel(StrEnum):
    EXPLICIT = "explicit"
    INHERITED = "inherited"
    INFERRED = "inferred"
    MISSING = "missing"


@dataclass(frozen=True)
class ActivityEvent:
    event_id: str
    occurred_at: datetime
    interaction_id: str
    trigger_kind: TriggerKind
    action_type: str
    action_name: str
    status: str
    summary: str
    agent_name: str
    platform: str = ""
    user_id: str = ""
    session_id: str = ""
    task_id: str = ""
    parent_task_id: str = ""
    tool_name: str = ""
    cluster: str = ""
    namespace: str = ""
    resource: str = ""
    duration_ms: int = 0
    attribution: AttributionLevel = AttributionLevel.EXPLICIT
    trace_id: str = ""
    details: dict[str, str] = field(default_factory=dict)


class TelemetryProvider(Protocol):
    """Read-only normalized telemetry interface."""

    def list_activity(self) -> list[ActivityEvent]: ...
