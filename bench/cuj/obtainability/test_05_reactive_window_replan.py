"""Reproduce CUJ5: the reactive trigger path of the obtainability capability.

CUJ3 (test_03) grades the chat-triggered window plan; this journey grades the
capability's behavior when the trigger is an event rather than a person: a
stockout notification invalidates a previously recommended window, and the
capability re-plans without asking anyone anything. The event transport (the
Pub/Sub adapter, the Kubernetes event watcher) has its own tests; this test
delivers the incident through the portal the way an adapter's dispatch would
word it, and grades the unattended re-plan: canonical CalendarMode re-probes,
a revised recommendation or an honest "no window", regenerated paired
manifests, and no clarifying question back to a session with no person in it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import (
    delivered_answer,
    latest_artifact,
    projected_records,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
)
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario

# The invalidated window sits in one allowed zone; the re-plan may only
# consider the remaining one, so the revised recommendation is checkable.
INVALIDATED_ZONE = "us-central1-a"
REMAINING_ZONE = "europe-west4-b"
REMAINING_REGION = REMAINING_ZONE.rsplit("-", 1)[0]
INVALIDATED_START_LEAD = timedelta(hours=20)
JOB_DURATION = timedelta(hours=12)
PLANNING_HORIZON = timedelta(hours=48)
OBTAINABILITY_PLANNING_METHODS = {
    "compute.beta.AdviceService.CalendarMode",
    "compute.advice.calendarMode",
}
REQUIRED_SKILLS = {"capacity-obtainability"}
CLARIFYING_PHRASES = (
    "could you clarify",
    "can you confirm",
    "please confirm",
    "let me know if",
    "which zone would you prefer",
    "do you want me to",
)
FORBIDDEN_OPERATIONS = {
    "apply_manifest",
    "create_cluster",
    "open_pull_request",
    "submit_provisioning_request",
    "submit_suggestion",
}

PROMPT_TEMPLATE = """[automated capacity incident — no human is in this \
session; ask no clarifying questions, state any assumption you make]
Stockout notification for planned batch job tpu-training-64x-v5e: the \
reserved capacity window in {invalidated_zone} starting {invalidated_start} \
UTC is no longer obtainable. The job still needs 64 TPU v5e nodes for 12 \
hours, finishing within 48 hours of this notification. Re-plan now: probe \
the remaining allowed zone {remaining_zone} with live \
compute.beta.AdviceService.CalendarMode evidence, recommend a new exact UTC \
start time and zone or state honestly that no window exists, and regenerate \
the paired Dynamic Workload Scheduler ProvisioningRequest and Kueue \
LocalQueue for the new target. This is planning-only: do not apply \
manifests, submit a provisioning request, create infrastructure, or mutate \
Kubernetes or cloud state."""


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    interaction_blocker = (
        ()
        if interaction.get("status") == "completed"
        and interaction.get("terminal") is True
        else ("interaction did not complete successfully",)
    )
    tasks = projected_tasks(interaction)
    evidence_projected = "evidence" in interaction or any(
        "evidence" in task for task in tasks
    )
    artifacts_projected = "artifacts" in interaction or any(
        "artifacts" in task for task in tasks
    )
    evidence_blocker = (
        () if evidence_projected else ("portal projection omits evidence",)
    )
    artifact_blocker = (
        () if artifacts_projected else ("portal projection omits artifacts",)
    )
    final_output_blocker = (
        ()
        if "output" in interaction
        else ("portal interaction projection omits output",)
    )

    evidence = projected_records(interaction, "evidence")
    replan_calls = [
        item
        for item in evidence
        if item.get("type") == "advice_service_workload_obtainability_planning"
        and str(item.get("status") or "").casefold() in {"completed", "passed"}
        and isinstance(item.get("details"), dict)
        and item["details"].get("apiMethod") in OBTAINABILITY_PLANNING_METHODS
        and item["details"].get("region") == REMAINING_REGION
    ]

    answer = delivered_answer(interaction)
    folded_answer = answer.casefold()
    revised_or_honest = (
        REMAINING_ZONE in answer and "utc" in folded_answer
    ) or ("no window" in folded_answer and REMAINING_ZONE in answer)
    asked_nothing = not any(
        phrase in folded_answer for phrase in CLARIFYING_PHRASES
    )

    artifacts = projected_records(interaction, "artifacts")
    provisioning = latest_artifact(artifacts, kind="ProvisioningRequest")
    queue = latest_artifact(artifacts, kind="LocalQueue")
    provisioning_target = (
        provisioning.get("target") if isinstance(provisioning, dict) else None
    )
    queue_target = queue.get("target") if isinstance(queue, dict) else None
    retargeted = (
        isinstance(provisioning, dict)
        and isinstance(queue, dict)
        and str(provisioning.get("pairId") or "").strip() != ""
        and provisioning.get("pairId") == queue.get("pairId")
        and isinstance(provisioning_target, dict)
        and isinstance(queue_target, dict)
        and provisioning_target.get("zone") == REMAINING_ZONE
        and queue_target.get("zone") == REMAINING_ZONE
    )

    input_value = interaction.get("input")
    input_text = str(
        (input_value or {}).get("text") if isinstance(input_value, dict) else ""
    )
    folded_input = input_text.casefold()

    suite = AcceptanceCriteria(
        (
            AcceptanceCriterion(
                "ac01-incident-preserved",
                "The incident names the invalidated zone and window, the job "
                "shape, and the unattended framing.",
                "projected input carries the incident and the no-questions rule",
            ),
            AcceptanceCriterion(
                "ac02-replan-probes-remaining-zone",
                "The re-plan probes the remaining allowed zone with a "
                "canonical CalendarMode call.",
                "completed CalendarMode evidence names the remaining region",
            ),
            AcceptanceCriterion(
                "ac03-revised-recommendation-delivered",
                "The answer names a new UTC start in the remaining zone, or "
                "states honestly that no window exists there.",
                "the delivered answer carries the revised verdict",
            ),
            AcceptanceCriterion(
                "ac04-artifacts-retargeted",
                "The regenerated ProvisioningRequest and LocalQueue share a "
                "pairId and target the remaining zone.",
                "both artifacts pair and target the re-planned zone",
            ),
            AcceptanceCriterion(
                "ac05-no-clarifying-questions",
                "An unattended run asks no clarifying questions.",
                "the delivered answer contains no question back to the sender",
            ),
        )
    )
    suite.record(
        "ac01-incident-preserved",
        INVALIDATED_ZONE in input_text
        and REMAINING_ZONE in input_text
        and "64 tpu v5e" in folded_input
        and "no human is in this session" in folded_input,
        input_text,
    )
    suite.record(
        "ac02-replan-probes-remaining-zone",
        bool(replan_calls),
        {"replanCalls": replan_calls},
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *evidence_blocker))),
    )
    suite.record(
        "ac03-revised-recommendation-delivered",
        revised_or_honest,
        answer,
        blocked_by=tuple(
            dict.fromkeys((*interaction_blocker, *final_output_blocker))
        ),
    )
    suite.record(
        "ac04-artifacts-retargeted",
        retargeted,
        {
            "provisioningTarget": provisioning_target,
            "queueTarget": queue_target,
        },
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *artifact_blocker))),
    )
    suite.record(
        "ac05-no-clarifying-questions",
        asked_nothing,
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
    worker_tools_available = bool(platform_tasks) and all(
        "toolCalls" in task for task in platform_tasks
    )
    unnormalized_calls = unnormalized_tool_calls(interaction)
    routed = [
        task
        for task in platform_tasks
        if REQUIRED_SKILLS <= set(task.get("skills") or [])
        and REQUIRED_SKILLS <= set(task.get("loadedSkills") or [])
    ]
    suite = MilestoneSuite(
        (
            Milestone(
                "interaction-completed",
                "The re-plan finishes successfully with no human input.",
                "terminal completed interaction",
            ),
            Milestone(
                "m1-kage-delegates",
                "Kage delegates the incident to a specialist.",
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
                "m3-replan-remains-read-only",
                "The reactive re-plan mutates nothing.",
                "complete normalized tool evidence contains no mutation",
                ("interaction-completed",),
            ),
        )
    )
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
        "m3-replan-remains-read-only",
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
                    not worker_tools_available,
                    "portal task projection omits worker toolCalls",
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
    invalidated_start = (
        (datetime.now(UTC) + INVALIDATED_START_LEAD)
        .replace(minute=0, second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return PROMPT_TEMPLATE.format(
        invalidated_zone=INVALIDATED_ZONE,
        invalidated_start=invalidated_start,
        remaining_zone=REMAINING_ZONE,
    )


def test_05_reactive_window_replan() -> None:
    Scenario(
        "cuj5",
        build_prompt,
        evaluate_acceptance,
        evaluate_kage_milestones,
    ).run_test()
