"""Reusable portal interaction execution and evidence projection helpers."""

from __future__ import annotations

import json
import re
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
        # Polling repeats the whole projection every couple of seconds, and
        # an unchanged repeat says nothing a reader needs: a 15-minute run
        # wrote 275 KB of near-identical payloads and buried the four moments
        # that mattered. Only transitions are recorded from here, plus the
        # terminal state, which the summary and every evaluator read.
        previous = json.dumps(interaction, sort_keys=True, default=str)
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
            current = json.dumps(interaction, sort_keys=True, default=str)
            if current != previous:
                self.log.record(
                    "interaction",
                    {"poll": poll, "value": interaction},
                )
                previous = current

        self.log.record("interaction_final", {"poll": poll, "value": interaction})
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


#: Paragraphs a coordinator emits when it hands work off — "Delegated to the
#: platform agent … I've started this as task t_… The answer will post into
#: this thread as soon as it's ready." — carry no answer content. Evaluators
#: must score the reply that follows them, or a journey whose specialist never
#: reported back would be judged on boilerplate.
_DELEGATION_ACK = re.compile(
    r"\b(?:delegated|delegating|routed|assigned|handed off|started)\b"
    r"[^.\n]{0,120}"
    r"(?:\bplatform agent\b|\btask t_[0-9a-f]+\b|\bwill post\b)"
    r"|\bwill post\b[^.\n]{0,60}\b(?:thread|here)\b",
    re.IGNORECASE,
)


def substantive_output(interaction: dict[str, Any]) -> str:
    """The user-visible answer with leading delegation acknowledgments removed.

    Acknowledgments are dropped sentence by sentence rather than paragraph by
    paragraph: a coordinator that opens its answer with "Delegated to the
    platform agent. Here is the design: ..." must keep the design, while an
    interaction that only ever acknowledged returns the empty string — that
    silence is the finding, not something to paper over.
    """

    text = str(interaction.get("output") or "")
    kept: list[str] = []
    skipping = True
    for paragraph in re.split(r"\n\s*\n", text):
        if not skipping:
            kept.append(paragraph)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip())
        remainder = list(sentences)
        while remainder and _DELEGATION_ACK.search(remainder[0]):
            remainder.pop(0)
        if remainder:
            skipping = False
            kept.append(" ".join(remainder))
    return "\n\n".join(kept).strip()


def latest_artifact(
    artifacts: list[dict[str, Any]],
    *,
    kind: str = "",
    artifact_type: str = "",
) -> dict[str, Any] | None:
    """The most recent artifact matching a manifest kind and/or record type.

    Latest wins: a worker that attaches a corrected manifest supersedes its
    earlier attempt, exactly as a re-uploaded file would. Both CUJ scenarios
    read artifacts through this, so they cannot grade the same recorder
    behavior in opposite directions.
    """

    for artifact in reversed(artifacts):
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            continue
        if kind and manifest.get("kind") != kind:
            continue
        if artifact_type and artifact.get("type") != artifact_type:
            continue
        return artifact
    return None


def unnormalized_tool_calls(interaction: dict[str, Any]) -> list[str]:
    """Tool calls whose mutation impact cannot be judged from the projection."""

    return [
        str(call.get("name") or "<unnamed>")
        for call in projected_tool_calls(interaction)
        if not str(call.get("operation") or "").strip()
    ]
