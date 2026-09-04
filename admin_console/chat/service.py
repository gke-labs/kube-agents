"""Completion-aware orchestration for black-box agent interactions."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Sequence

from admin_console.agent_chat import AgentChatError, ChatRunResult
from admin_console.agent_runtime import AgentRuntimeError, TaskUpdateResult
from admin_console.chat.backend import BackendFactory, ChatBackend
from admin_console.chat.models import (
    Interaction,
    InteractionStatus,
    TaskProjection,
    ToolCallEvidence,
    is_portal_session_id,
    utc_now,
)
from admin_console.chat.store import InteractionStore, InteractionStoreProtocol
from admin_console.telemetry import redact_evidence

ACTIVE_TASK_STATUSES = {"triage", "todo", "ready", "scheduled", "running", "review"}
FAILED_TASK_STATUSES = {"blocked", "cancelled", "crashed", "failed"}
TASK_READ_ERROR_LIMIT = 3
NONTERMINAL_INTERACTION_STATUSES = frozenset(
    {
        InteractionStatus.QUEUED,
        InteractionStatus.RUNNING,
        InteractionStatus.WAITING_FOR_APPROVAL,
        InteractionStatus.WAITING_FOR_TASKS,
    }
)


class ChatService:
    """Own the lifecycle of a root run and all observable delegated work."""

    def __init__(
        self,
        backend_factory: BackendFactory,
        *,
        store: InteractionStoreProtocol | None = None,
        poll_interval: float = 2.0,
        quiet_polls: int = 2,
        task_timeout: float = 900.0,
        max_workers: int = 4,
    ) -> None:
        self._backend_factory = backend_factory
        self.store = store or InteractionStore()
        self._poll_interval = max(0.0, poll_interval)
        self._quiet_polls = max(1, quiet_polls)
        self._task_timeout = max(1.0, task_timeout)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="portal-interaction",
        )
        self._control_executor = ThreadPoolExecutor(
            max_workers=max(2, min(4, max_workers)),
            thread_name_prefix="portal-control",
        )
        self.recovered_interactions = self.store.recover_incomplete()
        self.pruned_interactions = self.store.prune()

    def start(
        self,
        *,
        agent_id: str,
        input_text: str,
        profile: str = "default",
        session_id: str = "",
        history: Sequence[dict[str, str]] = (),
        user_email: str = "",
    ) -> Interaction:
        now = utc_now()
        interaction_id = f"ix_{uuid.uuid4().hex}"
        session_id = session_id or f"portal_{uuid.uuid4().hex}"
        if not is_portal_session_id(session_id):
            raise ValueError("session_id must identify a portal-owned session")
        interaction = Interaction(
            interaction_id=interaction_id,
            agent_id=agent_id,
            profile=profile,
            session_id=session_id,
            input_text=redact_evidence(input_text),
            status=InteractionStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        self.store.create(interaction)
        self.store.append_event(interaction_id, "interaction.queued")
        self._executor.submit(
            self._run_root,
            interaction_id,
            input_text,
            tuple(history),
            user_email,
        )
        return interaction

    def get(self, interaction_id: str) -> Interaction | None:
        return self.store.get(interaction_id)

    def approve(self, interaction_id: str, choice: str) -> Interaction:
        if choice not in {"once", "deny"}:
            raise ValueError("approval choice must be once or deny")
        interaction = self.store.transition(
            interaction_id,
            frozenset({InteractionStatus.WAITING_FOR_APPROVAL}),
            event="approval.resolved",
            event_data={"choice": choice},
            status=InteractionStatus.RUNNING,
            approval=None,
        )
        if interaction is None:
            self._required(interaction_id)
            raise ValueError("interaction is not waiting for approval")
        self._control_executor.submit(self._send_approval, interaction_id, choice)
        return self._required(interaction_id)

    def cancel(self, interaction_id: str) -> Interaction:
        updated = self.store.transition(
            interaction_id,
            NONTERMINAL_INTERACTION_STATUSES,
            event="interaction.cancelled",
            status=InteractionStatus.CANCELLED,
            error="Interaction cancelled by the caller.",
            approval=None,
        )
        if updated is None:
            return self._required(interaction_id)
        if updated.root_run_id:
            try:
                self._backend_factory().stop(
                    updated.agent_id,
                    run_id=updated.root_run_id,
                    profile=updated.profile,
                )
            except (AgentChatError, RuntimeError, ValueError):
                pass
        return updated

    def wait(self, interaction_id: str, timeout: float = 30.0) -> Interaction:
        deadline = time.monotonic() + timeout
        sequence = 0
        while True:
            interaction = self._required(interaction_id)
            if interaction.terminal or time.monotonic() >= deadline:
                return interaction
            events = self.store.events_after(interaction_id, sequence)
            if events:
                sequence = events[-1].sequence
            self.store.wait_for_change(
                interaction_id,
                after_sequence=sequence,
                timeout=min(0.25, max(0.0, deadline - time.monotonic())),
            )

    def _run_root(
        self,
        interaction_id: str,
        input_text: str,
        history: tuple[dict[str, str], ...],
        user_email: str,
    ) -> None:
        interaction = self._required(interaction_id)
        started = self.store.transition(
            interaction_id,
            frozenset({InteractionStatus.QUEUED}),
            event="interaction.started",
            status=InteractionStatus.RUNNING,
        )
        if started is None:
            return
        try:
            result = self._backend_factory().run(
                interaction.agent_id,
                prompt=input_text,
                session_id=interaction.session_id,
                history=history,
                profile=interaction.profile,
                user_email=user_email,
                timeout=600,
                on_update=lambda update: self._apply_root_update(
                    interaction_id, update
                ),
            )
            self._apply_root_result(interaction_id, result)
        except (AgentChatError, AgentRuntimeError, RuntimeError, ValueError) as exc:
            self._fail(interaction_id, exc)
        except Exception as exc:  # defensive boundary around external adapters
            self._fail(interaction_id, exc)

    def _send_approval(self, interaction_id: str, choice: str) -> None:
        interaction = self._required(interaction_id)
        if interaction.terminal:
            return
        try:
            self._backend_factory().resolve_approval(
                interaction.agent_id,
                run_id=interaction.root_run_id,
                choice=choice,
                profile=interaction.profile,
                timeout=600,
            )
        except Exception as exc:  # defensive boundary around external adapters
            self._fail(interaction_id, exc)

    def _apply_root_update(
        self,
        interaction_id: str,
        result: ChatRunResult,
    ) -> None:
        interaction = self._required(interaction_id)
        if interaction.terminal:
            if result.run_id and interaction.status == InteractionStatus.CANCELLED:
                try:
                    self._backend_factory().stop(
                        interaction.agent_id,
                        run_id=result.run_id,
                        profile=interaction.profile,
                    )
                except (AgentChatError, RuntimeError, ValueError):
                    pass
            return
        changes = {
            "root_run_id": result.run_id or interaction.root_run_id,
            "tool_calls": self._merge_tool_evidence(
                interaction.tool_calls,
                result.events,
            ),
        }
        if result.status == "running":
            updated = self.store.transition(
                interaction_id,
                frozenset({InteractionStatus.RUNNING}),
                **changes,
            )
            if updated is None:
                self._stop_cancelled_result(interaction_id, result.run_id)
            return
        if result.status == "waiting_for_approval":
            updated = self.store.transition(
                interaction_id,
                frozenset({InteractionStatus.RUNNING}),
                event="approval.requested",
                event_data=result.approval or {},
                **changes,
                status=InteractionStatus.WAITING_FOR_APPROVAL,
                approval=result.approval or {},
            )
            if updated is None:
                self._stop_cancelled_result(interaction_id, result.run_id)

    def _stop_cancelled_result(self, interaction_id: str, run_id: str) -> None:
        interaction = self._required(interaction_id)
        if interaction.status != InteractionStatus.CANCELLED or not run_id:
            return
        try:
            self._backend_factory().stop(
                interaction.agent_id,
                run_id=run_id,
                profile=interaction.profile,
            )
        except (AgentChatError, RuntimeError, ValueError):
            pass

    def _apply_root_result(
        self,
        interaction_id: str,
        result: ChatRunResult,
    ) -> None:
        interaction = self._required(interaction_id)
        if interaction.terminal:
            return
        changes = {
            "root_run_id": result.run_id or interaction.root_run_id,
            "tool_calls": self._merge_tool_evidence(
                interaction.tool_calls,
                result.events,
            ),
        }
        if result.status == "waiting_for_approval":
            self._apply_root_update(interaction_id, result)
            return
        if result.status != "completed":
            error = result.error or f"Root agent run ended with status {result.status}."
            status = (
                InteractionStatus.CANCELLED
                if result.status == "cancelled"
                else InteractionStatus.FAILED
            )
            self.store.transition(
                interaction_id,
                NONTERMINAL_INTERACTION_STATUSES,
                event=f"interaction.{status.value}",
                event_data={"error": error},
                **changes,
                status=status,
                output=result.output,
                error=error,
                diagnostics=(
                    "Inspect the root run and gateway logs before retrying.",
                ),
            )
            return

        updated = self.store.transition(
            interaction_id,
            frozenset({InteractionStatus.RUNNING}),
            event="root.completed",
            event_data={"output": result.output},
            **changes,
            status=InteractionStatus.WAITING_FOR_TASKS,
            output=result.output,
        )
        if updated is None:
            return
        self._settle_tasks(interaction_id)

    def _settle_tasks(self, interaction_id: str) -> None:
        deadline = time.monotonic() + self._task_timeout
        quiet = 0
        consecutive_read_errors = 0
        previous: tuple[TaskProjection, ...] | None = None
        while time.monotonic() < deadline:
            interaction = self._required(interaction_id)
            if interaction.terminal:
                return
            try:
                result = self._backend_factory().get_task_updates(
                    interaction.agent_id,
                    session_id=interaction.session_id,
                    limit=100,
                )
            except Exception as exc:  # task evidence is required for completion
                consecutive_read_errors += 1
                if consecutive_read_errors >= TASK_READ_ERROR_LIMIT:
                    self._fail(
                        interaction_id,
                        exc,
                        diagnostic=(
                            "The root run ended, but delegated task state could not be "
                            "read after repeated attempts; evaluation is incomplete."
                        ),
                    )
                    return
                time.sleep(self._poll_interval)
                continue

            consecutive_read_errors = 0

            tasks = self._project_tasks(result)
            if tasks != previous:
                self.store.update(interaction_id, tasks=tasks)
                self.store.append_event(
                    interaction_id,
                    "tasks.observed",
                    {"tasks": [task.to_dict() for task in tasks]},
                )
                previous = tasks
                quiet = 0

            statuses = {task.status.strip().lower() for task in tasks}
            settled = not result.truncated and not statuses & ACTIVE_TASK_STATUSES
            if not settled:
                quiet = 0
            else:
                quiet += 1

            if quiet >= self._quiet_polls:
                failed = [
                    task
                    for task in tasks
                    if task.status.strip().lower() in FAILED_TASK_STATUSES
                ]
                if failed:
                    detail = "; ".join(
                        f"{task.task_id}: {task.error or task.status}"
                        for task in failed
                    )
                    self.store.transition(
                        interaction_id,
                        frozenset({InteractionStatus.WAITING_FOR_TASKS}),
                        event="interaction.failed",
                        event_data={"error": detail},
                        status=InteractionStatus.FAILED,
                        error=f"Delegated work failed: {detail}",
                        diagnostics=(
                            "Open Task Kanban for the failed task and inspect its latest run.",
                        ),
                    )
                else:
                    self.store.transition(
                        interaction_id,
                        frozenset({InteractionStatus.WAITING_FOR_TASKS}),
                        event="interaction.completed",
                        status=InteractionStatus.COMPLETED,
                    )
                return
            time.sleep(self._poll_interval)

        self.store.transition(
            interaction_id,
            frozenset({InteractionStatus.WAITING_FOR_TASKS}),
            event="interaction.timed_out",
            status=InteractionStatus.TIMED_OUT,
            error="Delegated work did not reach a terminal state before the deadline.",
            diagnostics=(
                "Inspect active tasks in Task Kanban, then retry with a longer evaluator timeout.",
            ),
        )

    @staticmethod
    def _project_tasks(result: TaskUpdateResult) -> tuple[TaskProjection, ...]:
        return tuple(
            TaskProjection(
                task_id=task.task_id,
                title=task.title,
                assignee=task.assignee,
                status=task.status,
                summary=task.summary,
                error=task.error,
                run_count=task.run_count,
            )
            for task in result.tasks
        )

    @staticmethod
    def _merge_tool_evidence(
        existing: tuple[ToolCallEvidence, ...],
        events: tuple[dict, ...],
    ) -> tuple[ToolCallEvidence, ...]:
        evidence = list(existing)
        for event in events:
            event_type = str(event.get("event") or "")
            if event_type not in {"tool.started", "tool.completed", "tool.failed"}:
                continue
            name = str(event.get("tool") or "").strip()
            if not name:
                continue
            if event_type == "tool.started":
                evidence.append(ToolCallEvidence(name, "started"))
                continue
            status = (
                "failed"
                if event_type == "tool.failed" or bool(event.get("error"))
                else "completed"
            )
            for index, item in enumerate(evidence):
                if item.name == name and item.status == "started":
                    evidence[index] = replace(item, status=status)
                    break
            else:
                evidence.append(ToolCallEvidence(name, status))
        return tuple(evidence)

    def _fail(
        self,
        interaction_id: str,
        exc: Exception,
        *,
        diagnostic: str = "Check the verified connection and gateway health, then retry.",
    ) -> None:
        current = self._required(interaction_id)
        if current.terminal:
            return
        guidance = str(getattr(exc, "guidance", "") or diagnostic)
        message = str(exc) or type(exc).__name__
        self.store.transition(
            interaction_id,
            NONTERMINAL_INTERACTION_STATUSES,
            event="interaction.failed",
            event_data={"error": message},
            status=InteractionStatus.FAILED,
            error=message,
            diagnostics=(guidance,),
        )

    def _required(self, interaction_id: str) -> Interaction:
        interaction = self.store.get(interaction_id)
        if interaction is None:
            raise KeyError(interaction_id)
        return interaction
