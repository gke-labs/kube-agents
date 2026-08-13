"""Presentation model for concise scheduled-execution history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from admin_console.agent_runtime import AgentCronExecution, AgentCronJob

ACTIVE_STATUSES = {"claimed", "dispatching", "pending", "running", "started"}
SUCCESS_STATUSES = {"completed", "ok", "success", "succeeded"}
FAILED_STATUSES = {"error", "failed", "crashed"}


def execution_time(execution: AgentCronExecution) -> datetime:
    return execution.started_at or execution.claimed_at or datetime.fromtimestamp(0, UTC)


@dataclass(frozen=True)
class CronExecutionGroup:
    """Executions sharing one user-facing job title."""

    title: str
    executions: tuple[AgentCronExecution, ...]

    @property
    def latest(self) -> AgentCronExecution:
        return self.executions[0]

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(sorted({execution.profile for execution in self.executions}))

    @property
    def active(self) -> int:
        return sum(
            execution.status.lower() in ACTIVE_STATUSES
            for execution in self.executions
        )

    @property
    def succeeded(self) -> int:
        return sum(
            execution.status.lower() in SUCCESS_STATUSES
            for execution in self.executions
        )

    @property
    def failed(self) -> int:
        return sum(
            execution.status.lower() in FAILED_STATUSES
            for execution in self.executions
        )


def group_cron_executions(
    executions: tuple[AgentCronExecution, ...],
    jobs_by_key: dict[tuple[str, str], AgentCronJob],
) -> tuple[CronExecutionGroup, ...]:
    """Aggregate executions by exact display title, newest first."""
    grouped: dict[str, list[AgentCronExecution]] = {}
    for execution in executions:
        job = jobs_by_key.get((execution.profile, execution.job_id))
        title = job.name if job else execution.job_id or "Unknown job"
        grouped.setdefault(title, []).append(execution)

    groups = []
    for title, items in grouped.items():
        items.sort(key=execution_time, reverse=True)
        groups.append(CronExecutionGroup(title, tuple(items)))
    groups.sort(
        key=lambda group: (
            group.active > 0,
            execution_time(group.latest),
            group.title.lower(),
        ),
        reverse=True,
    )
    return tuple(groups)
