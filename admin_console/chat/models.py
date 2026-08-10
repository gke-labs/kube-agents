"""Runtime-neutral models for one complete user interaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class InteractionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_TASKS = "waiting_for_tasks"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = {
    InteractionStatus.COMPLETED,
    InteractionStatus.FAILED,
    InteractionStatus.CANCELLED,
    InteractionStatus.TIMED_OUT,
}


@dataclass(frozen=True)
class TaskProjection:
    task_id: str
    title: str
    assignee: str
    status: str
    summary: str = ""
    error: str = ""
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskId": self.task_id,
            "title": self.title,
            "assignee": self.assignee,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "runCount": self.run_count,
        }


@dataclass(frozen=True)
class InteractionEvent:
    sequence: int
    event: str
    occurred_at: datetime
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event": self.event,
            "occurredAt": self.occurred_at.isoformat(),
            "data": self.data,
        }


@dataclass(frozen=True)
class Interaction:
    interaction_id: str
    agent_id: str
    profile: str
    session_id: str
    input_text: str
    status: InteractionStatus
    created_at: datetime
    updated_at: datetime
    root_run_id: str = ""
    output: str = ""
    error: str = ""
    diagnostics: tuple[str, ...] = ()
    approval: dict[str, Any] | None = None
    tasks: tuple[TaskProjection, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "interactionId": self.interaction_id,
            "agentId": self.agent_id,
            "profile": self.profile,
            "sessionId": self.session_id,
            "input": {"text": self.input_text},
            "status": self.status.value,
            "terminal": self.terminal,
            "createdAt": self.created_at.isoformat(),
            "updatedAt": self.updated_at.isoformat(),
            "rootRunId": self.root_run_id or None,
            "output": self.output,
            "error": self.error,
            "diagnostics": list(self.diagnostics),
            "approval": self.approval,
            "tasks": [task.to_dict() for task in self.tasks],
        }
