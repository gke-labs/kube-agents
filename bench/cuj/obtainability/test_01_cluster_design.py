"""Reproduce CUJ1: obtainability-aware Day-0 GKE cluster design."""

from __future__ import annotations

import re
from typing import Any

from cuj.utils.interaction import (
    completed_evidence,
    opaque_tool_calls,
    projected_tasks,
    tool_operations,
)
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario, required_env

PROMPT = """Act as my GKE design partner. Design a new production cluster named \
cuj1-a100-design in project {project_id} for a distributed training workload \
requiring 32 NVIDIA A100 GPUs in us-central1. Check project quota and current \
obtainability separately using live evidence; never treat quota, static SKU \
support, or a documented zone list as proof of live capacity. Compare Autopilot \
and Standard, recommend resilient zones and provisioning models, and produce a \
valid ComputeClass fallback design while identifying any fallback that changes \
the workload's GPU or interconnect requirements. This is design-only: do not \
create a cluster, apply a manifest, open a pull request, or mutate cloud or \
Kubernetes state."""

REQUIRED_SKILLS = {"gke-cluster-creation", "gke-compute-classes"}
FORBIDDEN_OPERATIONS = {
    "create_cluster",
    "delete_cluster",
    "apply_manifest",
    "submit_suggestion",
    "open_pull_request",
}
OPAQUE_TOOLS = {"bash", "exec", "gcloud", "git", "kubectl", "python", "shell"}
CAPACITY_TERMS = {
    "quota",
    "obtainability",
    "live capacity",
    "autopilot",
    "standard",
    "computeclass",
}
QUOTA_DISTINCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bquota\b[^.\n]{0,120}\b(?:separate|distinct|different|independent)\b"
        r"[^.\n]{0,40}\b(?:capacity|obtainability)\b",
        r"\bquota\b[^.\n]{0,80}\b(?:does not|doesn't|cannot|can't)\b"
        r"[^.\n]{0,50}\b(?:prove|guarantee|indicate|mean|establish)\b"
        r"[^.\n]{0,40}\b(?:capacity|obtainability)\b",
        r"\b(?:capacity|obtainability)\b[^.\n]{0,120}"
        r"\b(?:separate|distinct|different|independent)\b[^.\n]{0,40}\bquota\b",
    )
)
NEGATED_GUARANTEE = re.compile(
    r"\b(?:no|not|never|cannot|can't|does not|doesn't|is not|isn't|without)\b"
    r"[^.\n]{0,50}\bguarantee",
    re.IGNORECASE,
)
MILESTONES = (
    Milestone(
        "interaction-completed",
        "The Day-0 cluster-design conversation must finish successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        "m1-kage-delegates",
        "Kage must act as the user-facing coordinator and delegate the GKE "
        "design request instead of performing specialist work itself.",
        "root toolCalls contains a completed kanban_create",
        ("interaction-completed",),
    ),
    Milestone(
        "m2-platform-task-created",
        "The request must be assigned to the Platform Agent, which owns "
        "fleet-wide GKE design and provisioning guidance.",
        "at least one task assigned to platform",
        ("m1-kage-delegates",),
    ),
    Milestone(
        "m3-design-skills-loaded",
        "The Platform Agent must load the cluster-creation and ComputeClass "
        "skills needed to design a stockout-resilient topology.",
        "platform task requests and loads gke-cluster-creation and "
        "gke-compute-classes",
        ("m2-platform-task-created",),
    ),
    Milestone(
        "m4-specialist-completes",
        "The delegated Platform Agent analysis must run and complete before "
        "Kage returns the design.",
        "platform task is done after at least one run",
        ("m2-platform-task-created",),
    ),
    Milestone(
        "m5-quota-checked",
        "The design must verify project quota separately from hardware "
        "obtainability before recommending GPU capacity.",
        "completed quota_check evidence",
        ("m4-specialist-completes",),
    ),
    Milestone(
        "m6-live-obtainability-checked",
        "The design must use live AdviceService capacity evidence for the "
        "requested machine shape, quantity, and location.",
        "completed advice_service_capacity evidence",
        ("m4-specialist-completes",),
    ),
    Milestone(
        "m7-capacity-claims-qualified",
        "The recommendation must distinguish quota from live capacity, "
        "compare Autopilot and Standard, and avoid guaranteeing capacity.",
        "task result distinguishes quota, live capacity, modes, and "
        "ComputeClass without guarantees",
        ("m4-specialist-completes",),
    ),
    Milestone(
        "m8-computeclass-validates",
        "The recommended resilient topology must include a ComputeClass "
        "fallback manifest that passes server-side dry-run validation.",
        "completed computeclass_server_dry_run evidence",
        ("m4-specialist-completes",),
    ),
    Milestone(
        "m9-design-remains-read-only",
        "The Day-0 journey is design-only and must not create a cluster, "
        "apply manifests, open a pull request, or mutate infrastructure.",
        "complete root and worker tool evidence contains no mutation",
        ("interaction-completed",),
    ),
)


def capacity_claims(result_text: str) -> tuple[bool, dict[str, Any]]:
    folded = result_text.casefold()
    present = sorted(term for term in CAPACITY_TERMS if term in folded)
    distinguishes_quota = any(
        pattern.search(result_text) for pattern in QUOTA_DISTINCTION_PATTERNS
    )
    guarantee_claims = [
        sentence.strip()
        for sentence in re.split(r"[.!?\n]+", result_text)
        if "capacity" in sentence.casefold()
        and "guarantee" in sentence.casefold()
        and not NEGATED_GUARANTEE.search(sentence)
    ]
    absolute_claims = [
        claim
        for claim in ("100% obtainability", "100% startup reliability")
        if claim in folded
    ]
    observed = {
        "present": present,
        "distinguishesQuota": distinguishes_quota,
        "forbidden": [*absolute_claims, *guarantee_claims],
    }
    return (
        len(present) == len(CAPACITY_TERMS)
        and distinguishes_quota
        and not observed["forbidden"],
        observed,
    )


def evaluate(interaction: dict[str, Any]) -> MilestoneSuite:
    tasks = projected_tasks(interaction)
    platform_tasks = projected_tasks(interaction, assignee="platform")
    skill_evidence_available = any(
        "skills" in task and "loadedSkills" in task for task in platform_tasks
    )
    task_evidence_available = any("evidence" in task for task in platform_tasks)
    worker_tool_evidence_available = bool(platform_tasks) and all(
        "toolCalls" in task for task in platform_tasks
    )
    routed = [
        task
        for task in platform_tasks
        if REQUIRED_SKILLS <= set(task.get("skills") or [])
        and REQUIRED_SKILLS <= set(task.get("loadedSkills") or [])
    ]
    completed = completed_evidence(interaction)
    operations = tool_operations(interaction)
    completed_operations = tool_operations(interaction, completed_only=True)
    result_text = str(interaction.get("output") or "")
    claims_met, claims_observed = capacity_claims(result_text)
    opaque_calls = opaque_tool_calls(interaction, OPAQUE_TOOLS)
    suite = MilestoneSuite(MILESTONES)
    suite.record(
        "interaction-completed",
        interaction.get("status") == "completed" and interaction.get("terminal") is True,
        interaction.get("status"),
    )
    suite.record(
        "m1-kage-delegates",
        "kanban_create" in completed_operations,
        completed_operations,
    )
    suite.record("m2-platform-task-created", bool(platform_tasks), tasks)
    suite.record(
        "m3-design-skills-loaded",
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
        if skill_evidence_available
        else ("portal task projection omits skills/loadedSkills",),
    )
    suite.record(
        "m4-specialist-completes",
        any(
            task.get("status") == "done" and int(task.get("runCount") or 0) >= 1
            for task in platform_tasks
        ),
        platform_tasks,
    )
    evidence_blocker = (
        ()
        if task_evidence_available
        else ("portal task projection omits evidence",)
    )
    suite.record(
        "m5-quota-checked",
        "quota_check" in completed,
        sorted(completed),
        blocked_by=evidence_blocker,
    )
    suite.record(
        "m6-live-obtainability-checked",
        "advice_service_capacity" in completed,
        sorted(completed),
        blocked_by=evidence_blocker,
    )
    suite.record(
        "m7-capacity-claims-qualified",
        claims_met,
        claims_observed,
    )
    suite.record(
        "m8-computeclass-validates",
        "computeclass_server_dry_run" in completed,
        sorted(completed),
        blocked_by=evidence_blocker,
    )
    suite.record(
        "m9-design-remains-read-only",
        interaction.get("toolEvidenceComplete") is True
        and not FORBIDDEN_OPERATIONS.intersection(operations),
        {
            "toolEvidenceComplete": interaction.get("toolEvidenceComplete"),
            "operations": operations,
            "opaqueTools": opaque_calls,
        },
        blocked_by=tuple(
            reason
            for condition, reason in (
                (
                    "toolEvidenceComplete" not in interaction,
                    "portal interaction projection omits toolEvidenceComplete",
                ),
                (
                    not worker_tool_evidence_available,
                    "portal task projection omits worker toolCalls",
                ),
                (bool(opaque_calls), "tool evidence omits normalized operations"),
            )
            if condition
        ),
    )
    return suite


def build_prompt() -> str:
    return PROMPT.format(project_id=required_env("CUJ1_PROJECT_ID"))


def test_01_cluster_design() -> None:
    Scenario("cuj1", build_prompt, evaluate).run_test()
