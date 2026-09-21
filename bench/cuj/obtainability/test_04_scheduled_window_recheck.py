"""Reproduce CUJ4: the scheduled trigger path of the obtainability capability.

CUJ3 (test_03) grades the chat-triggered window plan. This journey grades the
capability delivery vehicle's scheduled path for the same capability
(docs/designs/capability-delivery-vehicle.md, R2): an advisory capability is
asked for rather than scheduled, and gets a schedule only as a re-check the
user opts into, reporting into the conversation that asked. The test acts as
that user: it asks for a window plan plus a one-time re-check shortly before
the recommended window, and scores only what the portal returns — the
schedule commitment must be real (a completed cron-create in tool evidence),
not prose.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import (
    delivered_answer,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
)
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario

ALLOWED_ZONES = {"us-central1-a", "europe-west4-b"}
RECHECK_LEAD = timedelta(hours=2)
REQUIRED_SKILLS = {"capacity-obtainability"}
# The spellings a completed schedule-creation tool call may normalize to.
SCHEDULE_OPERATIONS = {"cronjob", "cronjob_create", "schedule_create"}
FORBIDDEN_OPERATIONS = {
    "apply_manifest",
    "create_cluster",
    "open_pull_request",
    "submit_provisioning_request",
    "submit_suggestion",
}
# One UTC time in the delivered answer, hh:mm, e.g. "22:00 UTC" or
# "2026-09-22T22:00:00Z ... UTC".
UTC_TIME_RE = re.compile(r"\b\d{2}:\d{2}\b")

PROMPT = """Act as my batch scheduling partner. Plan a 64-node TPU v5e \
training job that must run for 12 hours and finish within the next 48 hours, \
evaluating us-central1-a and europe-west4-b with live \
compute.beta.AdviceService.CalendarMode evidence, and recommend an exact UTC \
start time and zone. Then schedule a single re-check of the recommended \
window for two hours before its start: the re-check re-probes the window, \
reports into this conversation, and must fire exactly once — never a \
recurring schedule. Tell me the re-check's exact UTC fire time and where its \
report will arrive. This is planning-only: do not apply manifests, submit a \
provisioning request, create infrastructure, or mutate Kubernetes or cloud \
state."""

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-recheck-request-preserved",
        "The request opts into a one-time re-check two hours before the "
        "recommended window, reporting into the same conversation.",
        "projected user input carries the plan and the re-check opt-in",
    ),
    AcceptanceCriterion(
        "ac02-recheck-schedule-created",
        "The re-check is a real schedule, not prose: a completed "
        "schedule-creation tool call is in the interaction's tool evidence.",
        "completed tool operations include a cron/schedule create",
    ),
    AcceptanceCriterion(
        "ac03-recheck-fire-time-stated",
        "The user is told the re-check's exact UTC fire time, two hours "
        "before the recommended window's start.",
        "the delivered answer names a UTC fire time and the two-hour lead",
    ),
    AcceptanceCriterion(
        "ac04-recheck-reports-to-thread",
        "The user is told the re-check reports into this conversation, not "
        "into a separate session.",
        "the delivered answer names this conversation as the destination",
    ),
    AcceptanceCriterion(
        "ac05-recheck-is-one-shot",
        "The re-check fires once: the answer states it, and no recurring "
        "schedule is described.",
        "the delivered answer commits to a single firing",
    ),
)

MILESTONES = (
    Milestone(
        "interaction-completed",
        "The plan-plus-re-check conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        "m1-kage-delegates",
        "Kage delegates the specialist planning request.",
        "root toolCalls contains a completed kanban_create",
        ("interaction-completed",),
    ),
    Milestone(
        "m2-obtainability-skill-loaded",
        "The delegated task loads the obtainability capability's skill.",
        "the platform task loads capacity-obtainability",
        ("m1-kage-delegates",),
    ),
    Milestone(
        "m3-planning-remains-read-only",
        "Scheduling the re-check mutates nothing beyond the schedule itself.",
        "complete normalized tool evidence contains no forbidden mutation",
        ("interaction-completed",),
    ),
)


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    interaction_blocker = (
        ()
        if interaction.get("status") == "completed"
        and interaction.get("terminal") is True
        else ("interaction did not complete successfully",)
    )
    tool_evidence_blocker = (
        ()
        if interaction.get("toolEvidenceComplete") is True
        else ("portal projection omits complete tool evidence",)
    )
    final_output_blocker = (
        ()
        if "output" in interaction
        else ("portal interaction projection omits output",)
    )

    completed_operations = tool_operations(interaction, completed_only=True)
    schedule_created = bool(SCHEDULE_OPERATIONS.intersection(completed_operations))

    answer = delivered_answer(interaction)
    folded_answer = answer.casefold()
    fire_time_stated = (
        bool(UTC_TIME_RE.search(answer))
        and "utc" in folded_answer
        and ("two hours" in folded_answer or "2 hours" in folded_answer)
    )
    reports_to_thread = (
        "this conversation" in folded_answer
        or "this thread" in folded_answer
        or "same conversation" in folded_answer
        or "same thread" in folded_answer
    )
    one_shot = (
        "once" in folded_answer or "one-time" in folded_answer
    ) and "every" not in folded_answer

    input_value = interaction.get("input")
    input_text = str(
        (input_value or {}).get("text") if isinstance(input_value, dict) else ""
    )
    folded_input = input_text.casefold()

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-recheck-request-preserved",
        "64-node" in folded_input
        and "tpu v5e" in folded_input
        and "re-check" in folded_input
        and "two hours before" in folded_input
        and "this conversation" in folded_input
        and "exactly once" in folded_input,
        input_text,
    )
    suite.record(
        "ac02-recheck-schedule-created",
        schedule_created,
        {"completedOperations": sorted(completed_operations)},
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *tool_evidence_blocker))
        ),
    )
    suite.record(
        "ac03-recheck-fire-time-stated",
        fire_time_stated,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    suite.record(
        "ac04-recheck-reports-to-thread",
        reports_to_thread,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    suite.record(
        "ac05-recheck-is-one-shot",
        one_shot,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    return suite


def evaluate_kage_milestones(interaction: dict[str, Any]) -> MilestoneSuite:
    platform_tasks = projected_tasks(interaction, assignee="platform")
    operations = tool_operations(interaction)
    completed_operations = tool_operations(interaction, completed_only=True)
    unnormalized_calls = unnormalized_tool_calls(interaction)
    routed = [
        task
        for task in platform_tasks
        if REQUIRED_SKILLS <= set(task.get("skills") or [])
        and REQUIRED_SKILLS <= set(task.get("loadedSkills") or [])
    ]
    suite = MilestoneSuite(MILESTONES)
    suite.record(
        "interaction-completed",
        interaction.get("status") == "completed"
        and interaction.get("terminal") is True,
        interaction.get("status"),
    )
    suite.record(
        "m1-kage-delegates",
        "kanban_create" in completed_operations,
        completed_operations,
    )
    suite.record(
        "m2-obtainability-skill-loaded",
        bool(routed),
        [
            {
                "taskId": task.get("taskId"),
                "skills": task.get("skills"),
                "loadedSkills": task.get("loadedSkills"),
            }
            for task in platform_tasks
        ],
        blocked_by=()
        if any("skills" in task and "loadedSkills" in task for task in platform_tasks)
        else ("portal task projection omits skills/loadedSkills",),
    )
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    suite.record(
        "m3-planning-remains-read-only",
        interaction.get("toolEvidenceComplete") is True
        and not mutations
        and not unnormalized_calls,
        {
            "toolEvidenceComplete": interaction.get("toolEvidenceComplete"),
            "operations": operations,
            "mutations": mutations,
            "unnormalizedTools": unnormalized_calls,
        },
        # An observed mutation outranks any missing-evidence reason.
        blocked_by=()
        if mutations
        else tuple(
            reason
            for condition, reason in (
                (
                    "toolEvidenceComplete" not in interaction,
                    "portal interaction projection omits toolEvidenceComplete",
                ),
                (
                    bool(unnormalized_calls),
                    "tool evidence omits normalized operations",
                ),
            )
            if condition
        ),
    )
    return suite


def build_prompt() -> str:
    return PROMPT


def test_04_scheduled_window_recheck() -> None:
    Scenario(
        "cuj4",
        build_prompt,
        evaluate_acceptance,
        evaluate_kage_milestones,
    ).run_test()
