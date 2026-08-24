"""Reusable portal interaction execution and evidence projection helpers."""

from __future__ import annotations

import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cuj.utils.evidence import EvidenceLog
from cuj.utils.portal import Portal, PortalError, portal_token

if TYPE_CHECKING:
    from cuj.utils.scenario import ScenarioConfig

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


@dataclass(frozen=True)
class InteractionRunner:
    config: ScenarioConfig
    log: EvidenceLog
    approval_choice: str = "deny"

    def run(self, prompt: str, *, session_prefix: str) -> dict[str, Any]:
        request = {
            "agentId": self.config.agent_id,
            "profile": self.config.profile,
            "sessionId": f"{session_prefix}_{uuid.uuid4().hex}",
            "input": {"text": prompt},
            "history": [],
        }
        self.log.record("request", request)

        portal = Portal(self.config.endpoint, token=portal_token())
        interaction = portal.post("interactions", request)
        self.log.record("interaction", {"poll": 0, "value": interaction})
        interaction_id = str(interaction.get("interactionId") or "")
        if not interaction_id:
            raise PortalError("portal response did not include interactionId")

        deadline = time.monotonic() + self.config.timeout
        poll = 0
        while str(interaction.get("status") or "") not in TERMINAL_STATUSES:
            if time.monotonic() >= deadline:
                interaction = {**interaction, "evaluatorTimedOut": True}
                self.log.record(
                    "interaction",
                    {"poll": poll, "value": interaction},
                )
                break
            if interaction.get("status") == "waiting_for_approval":
                interaction = portal.post(
                    "interactions/"
                    f"{urllib.parse.quote(interaction_id, safe='')}/approval",
                    {"choice": self.approval_choice},
                )
            else:
                time.sleep(self.config.poll_interval)
                interaction = portal.get(
                    f"interactions/{urllib.parse.quote(interaction_id, safe='')}"
                )
            poll += 1
            self.log.record(
                "interaction",
                {"poll": poll, "value": interaction},
            )

        return interaction


def projected_tasks(
    interaction: dict[str, Any], *, assignee: str = ""
) -> list[dict[str, Any]]:
    tasks = [
        task for task in interaction.get("tasks", []) if isinstance(task, dict)
    ]
    if assignee:
        return [task for task in tasks if task.get("assignee") == assignee]
    return tasks


def projected_records(
    interaction: dict[str, Any],
    field: str,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for source in (interaction, *projected_tasks(interaction)):
        values = source.get(field, [])
        if isinstance(values, list):
            found.extend(item for item in values if isinstance(item, dict))
    return found


def completed_evidence(interaction: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for task in projected_tasks(interaction):
        for item in task.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").casefold()
            if status in {"completed", "passed"}:
                found.add(str(item.get("type") or ""))
    return found


def projected_tool_calls(interaction: dict[str, Any]) -> list[dict[str, Any]]:
    calls = interaction.get("toolCalls", [])
    found = (
        [call for call in calls if isinstance(call, dict)]
        if isinstance(calls, list)
        else []
    )
    for task in projected_tasks(interaction):
        task_calls = task.get("toolCalls", [])
        if isinstance(task_calls, list):
            found.extend(call for call in task_calls if isinstance(call, dict))
    return found


def tool_operations(
    interaction: dict[str, Any], *, completed_only: bool = False
) -> list[str]:
    return [
        str(call.get("operation") or call.get("name") or "")
        for call in projected_tool_calls(interaction)
        if not completed_only or call.get("status") == "completed"
    ]


def unnormalized_tool_calls(interaction: dict[str, Any]) -> list[str]:
    """Tool calls whose mutation impact cannot be judged from the projection."""

    return [
        str(call.get("name") or "<unnamed>")
        for call in projected_tool_calls(interaction)
        if not str(call.get("operation") or "").strip()
    ]
