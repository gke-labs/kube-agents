"""Reproduce CUJ1: obtainability-aware Day-0 GKE cluster design."""

from __future__ import annotations

import re
from typing import Any

from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import (
    completed_evidence,
    projected_records,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
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
the workload's GPU or interconnect requirements. Also provide the alternative \
for standard node pools: a Node Auto-Provisioning specification allowing \
cloud.google.com/machine-family values n2, n2d, and c2d with location policy \
ANY. This is design-only: do not create a cluster, apply a manifest, open a \
pull request, or mutate cloud or Kubernetes state."""

REQUIRED_SKILLS = {"gke-cluster-creation", "gke-compute-classes"}
FORBIDDEN_OPERATIONS = {
    "create_cluster",
    "delete_cluster",
    "apply_manifest",
    "submit_suggestion",
    "open_pull_request",
}
L4_ACCELERATORS = {"l4", "nvidia-l4"}
T4_ACCELERATORS = {"t4", "nvidia-t4", "nvidia-tesla-t4"}
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

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-target-request-preserved",
        "The request specifies a GKE design for 32 A100 GPUs in us-central1.",
        "projected user input contains 32, A100, and us-central1",
    ),
    AcceptanceCriterion(
        "ac02-obtainability-api-invoked",
        "kube-agents invokes AdviceService.Capacity or "
        "GeneralCapacityRecommendation for the requested capacity.",
        "completed evidence names an allowed method and requests 32 A100 GPUs "
        "in us-central1",
    ),
    AcceptanceCriterion(
        "ac03-available-quantity-analyzed",
        "kube-agents analyzes the available quantity for the requested capacity.",
        "capacity evidence contains a numeric availableQuantity",
    ),
    AcceptanceCriterion(
        "ac04-zonal-distribution-analyzed",
        "kube-agents analyzes capacity distribution across zones.",
        "capacity evidence contains signals for at least two zones",
    ),
    AcceptanceCriterion(
        "ac05-provisioning-models-analyzed",
        "kube-agents analyzes On-Demand, Spot, and Flex provisioning signals.",
        "capacity evidence contains non-empty ON_DEMAND, SPOT, and FLEX signals",
    ),
    AcceptanceCriterion(
        "ac06-computeclass-generated",
        "The recommended path generates a GKE ComputeClass manifest.",
        "projected artifacts contain a structured manifest with kind ComputeClass",
    ),
    AcceptanceCriterion(
        "ac07-a2-primary",
        "The ComputeClass uses the A2 series as its primary priority.",
        "the first ComputeClass priority selects an A2 family or machine type",
    ),
    AcceptanceCriterion(
        "ac08-l4-t4-fallbacks",
        "The ComputeClass provides both L4 and T4 fallback options.",
        "later ComputeClass priorities contain both L4 and T4 accelerators",
    ),
    AcceptanceCriterion(
        "ac09-nap-alternative-generated",
        "The alternative path outputs a Node Auto-Provisioning specification.",
        "projected artifacts contain a structured node_auto_provisioning artifact",
    ),
    AcceptanceCriterion(
        "ac10-nap-machine-families",
        "The NAP alternative allows n2, n2d, and c2d machine families.",
        "NAP selectors include cloud.google.com/machine-family values n2, n2d, c2d",
    ),
    AcceptanceCriterion(
        "ac11-nap-location-any",
        "The NAP alternative uses location policy ANY.",
        "NAP configuration contains locationPolicy: ANY",
    ),
)


def _completed_capacity_evidence(
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in evidence
            if item.get("type") == "advice_service_capacity"
            and str(item.get("status") or "").casefold() in {"completed", "passed"}
        ),
        None,
    )


def _computeclass_artifact(
    artifacts: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return next(
        (
            artifact
            for artifact in artifacts
            if isinstance(artifact.get("manifest"), dict)
            and artifact["manifest"].get("kind") == "ComputeClass"
        ),
        None,
    )


def _nap_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next(
        (
            artifact
            for artifact in artifacts
            if artifact.get("type") == "node_auto_provisioning"
            and isinstance(artifact.get("manifest"), dict)
        ),
        None,
    )


def _machine_families(spec: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    affinity = spec.get("nodeAffinity", {})
    required = (
        affinity.get("requiredDuringSchedulingIgnoredDuringExecution", {})
        if isinstance(affinity, dict)
        else {}
    )
    terms = (
        required.get("nodeSelectorTerms", []) if isinstance(required, dict) else []
    )
    for term in terms if isinstance(terms, list) else []:
        expressions = term.get("matchExpressions", []) if isinstance(term, dict) else []
        for expression in expressions if isinstance(expressions, list) else []:
            if not isinstance(expression, dict):
                continue
            if expression.get("key") != "cloud.google.com/machine-family":
                continue
            if str(expression.get("operator") or "").casefold() != "in":
                continue
            values = expression.get("values", [])
            if isinstance(values, list):
                found.update(str(entry).casefold() for entry in values)
    return found


def _is_a2_priority(priority: Any) -> bool:
    if not isinstance(priority, dict):
        return False
    family = str(priority.get("machineFamily") or "").casefold()
    machine_type = str(priority.get("machineType") or "").casefold()
    return family == "a2" or machine_type.startswith("a2-")


def _accelerator_type(priority: Any) -> str:
    if not isinstance(priority, dict):
        return ""
    gpu = priority.get("gpu", {})
    if not isinstance(gpu, dict):
        return ""
    return str(gpu.get("type") or "").casefold()


def _location_policy(spec: dict[str, Any]) -> str:
    location = spec.get("location", {})
    if not isinstance(location, dict):
        return ""
    return str(location.get("locationPolicy") or "").upper()


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


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    tasks = projected_tasks(interaction)
    interaction_blocker = (
        ()
        if interaction.get("status") == "completed"
        and interaction.get("terminal") is True
        else ("interaction did not complete successfully",)
    )
    evidence_projected = "evidence" in interaction or any(
        "evidence" in task for task in tasks
    )
    artifacts_projected = "artifacts" in interaction or any(
        "artifacts" in task for task in tasks
    )
    evidence = projected_records(interaction, "evidence")
    artifacts = projected_records(interaction, "artifacts")
    evidence_blocker = tuple(
        dict.fromkeys(
            (
                *interaction_blocker,
                *(
                    ()
                    if evidence_projected
                    else ("portal task projection omits evidence",)
                ),
            )
        )
    )
    artifact_blocker = tuple(
        dict.fromkeys(
            (
                *interaction_blocker,
                *(
                    ()
                    if artifacts_projected
                    else ("portal task projection omits artifacts",)
                ),
            )
        )
    )
    capacity = _completed_capacity_evidence(evidence)
    details = capacity.get("details", {}) if capacity else {}
    details = details if isinstance(details, dict) else {}
    method = str(details.get("apiMethod") or "")
    capacity_request = details.get("request", {})
    capacity_request = (
        capacity_request if isinstance(capacity_request, dict) else {}
    )
    analysis = details.get("analysis", {})
    analysis = analysis if isinstance(analysis, dict) else {}
    available_quantity = analysis.get("availableQuantity")
    zones = analysis.get("zones")
    zonal_signals = (
        [
            item
            for item in zones
            if isinstance(item, dict)
            and str(item.get("zone") or "").strip()
            and any(
                key in item
                for key in ("availableQuantity", "obtainability", "signal")
            )
        ]
        if isinstance(zones, list)
        else []
    )
    distinct_zones = {str(item["zone"]) for item in zonal_signals}
    models = analysis.get("provisioningModels", {})
    models = models if isinstance(models, dict) else {}
    normalized_models = {
        re.sub(r"[^a-z]", "", str(key).casefold()): value
        for key, value in models.items()
    }

    computeclass = _computeclass_artifact(artifacts)
    computeclass_manifest = computeclass.get("manifest", {}) if computeclass else {}
    spec = (
        computeclass_manifest.get("spec", {})
        if isinstance(computeclass_manifest, dict)
        else {}
    )
    priorities = spec.get("priorities", []) if isinstance(spec, dict) else []
    priorities = priorities if isinstance(priorities, list) else []
    fallback_accelerators = {
        _accelerator_type(priority) for priority in priorities[1:]
    }

    nap = _nap_artifact(artifacts)
    nap_manifest = nap.get("manifest", {}) if nap else {}
    nap_spec = (
        nap_manifest.get("spec", {}) if isinstance(nap_manifest, dict) else {}
    )
    nap_spec = nap_spec if isinstance(nap_spec, dict) else {}
    families = _machine_families(nap_spec)
    location_policy = _location_policy(nap_spec)

    user_input = interaction.get("input", {})
    user_input = user_input if isinstance(user_input, dict) else {}
    input_text = str(user_input.get("text") or "")
    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-target-request-preserved",
        bool(re.search(r"\b32\b", input_text))
        and "a100" in input_text.casefold()
        and "us-central1" in input_text.casefold(),
        input_text,
    )
    suite.record(
        "ac02-obtainability-api-invoked",
        (
            method.endswith("AdviceService.Capacity")
            or method.endswith("AdviceService.GeneralCapacityRecommendation")
        )
        and str(capacity_request.get("region") or "").casefold() == "us-central1"
        and "a100"
        in str(capacity_request.get("acceleratorType") or "").casefold()
        and capacity_request.get("acceleratorCount") == 32,
        {
            "apiMethod": method,
            "request": capacity_request,
            "status": capacity.get("status") if capacity else None,
        },
        blocked_by=evidence_blocker,
    )
    suite.record(
        "ac03-available-quantity-analyzed",
        isinstance(available_quantity, (int, float))
        and not isinstance(available_quantity, bool),
        available_quantity,
        blocked_by=evidence_blocker,
    )
    suite.record(
        "ac04-zonal-distribution-analyzed",
        len(distinct_zones) >= 2 and len(zonal_signals) == len(zones),
        {"zones": zones, "distinctZones": sorted(distinct_zones)},
        blocked_by=evidence_blocker,
    )
    required_models = {"ondemand", "spot", "flex"}
    suite.record(
        "ac05-provisioning-models-analyzed",
        required_models <= normalized_models.keys()
        and all(normalized_models[item] not in (None, "", [], {}) for item in required_models),
        models,
        blocked_by=evidence_blocker,
    )
    suite.record(
        "ac06-computeclass-generated",
        computeclass is not None
        and computeclass_manifest.get("apiVersion") == "cloud.google.com/v1"
        and isinstance(spec, dict)
        and bool(priorities),
        computeclass,
        blocked_by=artifact_blocker,
    )
    suite.record(
        "ac07-a2-primary",
        _is_a2_priority(priorities[0]) if priorities else False,
        priorities[0] if priorities else None,
        blocked_by=artifact_blocker,
    )
    suite.record(
        "ac08-l4-t4-fallbacks",
        bool(L4_ACCELERATORS & fallback_accelerators)
        and bool(T4_ACCELERATORS & fallback_accelerators),
        {
            "priorities": priorities[1:],
            "acceleratorTypes": sorted(fallback_accelerators),
        },
        blocked_by=artifact_blocker,
    )
    suite.record(
        "ac09-nap-alternative-generated",
        nap is not None and bool(families) and bool(location_policy),
        nap,
        blocked_by=artifact_blocker,
    )
    suite.record(
        "ac10-nap-machine-families",
        {"n2", "n2d", "c2d"} <= families,
        sorted(families),
        blocked_by=artifact_blocker,
    )
    suite.record(
        "ac11-nap-location-any",
        location_policy == "ANY",
        location_policy,
        blocked_by=artifact_blocker,
    )
    return suite


def evaluate_kage_milestones(interaction: dict[str, Any]) -> MilestoneSuite:
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
    final_output_available = "output" in interaction
    result_text = str(interaction.get("output") or "")
    claims_met, claims_observed = capacity_claims(result_text)
    unnormalized_calls = unnormalized_tool_calls(interaction)
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
        blocked_by=()
        if final_output_available
        else ("portal interaction projection omits output",),
    )
    suite.record(
        "m8-computeclass-validates",
        "computeclass_server_dry_run" in completed,
        sorted(completed),
        blocked_by=evidence_blocker,
    )
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    suite.record(
        "m9-design-remains-read-only",
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
                    not worker_tool_evidence_available,
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
    return PROMPT.format(project_id=required_env("CUJ_PROJECT_ID"))


def test_01_cluster_design() -> None:
    Scenario(
        "cuj1",
        build_prompt,
        evaluate_acceptance,
        evaluate_kage_milestones,
    ).run_test()
