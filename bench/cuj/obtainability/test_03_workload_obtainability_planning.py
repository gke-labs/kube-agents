"""Reproduce CUJ3: workload obtainability planning for a flexible TPU job."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import (
    projected_records,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
)
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario

ALLOWED_ZONES = {"us-central1-a", "us-east4-a"}
ALLOWED_REGIONS = {zone.rsplit("-", 1)[0] for zone in ALLOWED_ZONES}
JOB_DURATION = timedelta(hours=12)
PLANNING_HORIZON = timedelta(hours=48)
CALL_DELAY_TOLERANCE = timedelta(minutes=30)
OBTAINABILITY_PLANNING_METHODS = {
    "compute.alpha.AdviceService.CalendarMode",
    "compute.advice.calendarMode",
}
TPU_V5E_VM_FAMILY = "VM_FAMILY_CLOUD_TPU_LITE_POD_SLICE_CT5LP"
REQUIRED_SKILLS = {"gke-batch-hpc", "gke-workload-scaling"}
FORBIDDEN_OPERATIONS = {
    "apply_manifest",
    "create_cluster",
    "open_pull_request",
    "submit_provisioning_request",
    "submit_suggestion",
}

PROMPT = """Act as my batch scheduling partner. Plan a 64-node TPU v5e \
training job that must run for 12 hours and finish within the next 48 hours. \
Evaluate us-central1-a and us-east4-a using live \
compute.alpha.AdviceService.CalendarMode evidence. Rank the predicted capacity \
windows across regions and recommend an exact UTC start time and zone. Generate \
a Dynamic Workload Scheduler ProvisioningRequest and a paired Kueue LocalQueue \
targeting that recommended region, zone, and window. This is planning-only: do \
not apply manifests, submit a provisioning request, create infrastructure, or \
mutate Kubernetes or cloud state."""

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-flexible-job-request-preserved",
        "The request specifies 64 TPU v5e nodes and a 12-hour run that must "
        "finish within 48 hours.",
        "projected user input contains the machine, count, duration, and horizon",
    ),
    AcceptanceCriterion(
        "ac02-workload-obtainability-planning-invoked",
        "kube-agents invokes AdviceService.CalendarMode with the machine spec, "
        "count, allowed zones, duration, and horizon.",
        "completed region-scoped CalendarMode calls contain canonical requests",
    ),
    AcceptanceCriterion(
        "ac03-multi-region-windows-evaluated",
        "kube-agents evaluates predicted capacity windows across regions.",
        "CalendarMode analysis contains valid windows in at least two regions",
    ),
    AcceptanceCriterion(
        "ac04-windows-ranked",
        "kube-agents returns a ranked schedule recommendation.",
        "capacity windows have unique consecutive ranks beginning at one",
    ),
    AcceptanceCriterion(
        "ac05-start-window-recommended",
        "The user receives an exact recommended UTC start time and zone whose "
        "12-hour run finishes within the 48-hour horizon.",
        "output names the top window's zone and UTC time",
    ),
    AcceptanceCriterion(
        "ac06-provisioning-request-generated",
        "The output includes a Dynamic Workload Scheduler ProvisioningRequest.",
        "a structured ProvisioningRequest uses queued-provisioning.gke.io",
    ),
    AcceptanceCriterion(
        "ac07-provisioning-request-sized",
        "The ProvisioningRequest reserves 64 TPU v5e nodes for the 12-hour job.",
        "machineSpec is TPU v5e, one podSet has count 64, and "
        "maxRunDurationSeconds is 43200",
    ),
    AcceptanceCriterion(
        "ac08-localqueue-generated",
        "The output includes a valid Kueue LocalQueue.",
        "a structured LocalQueue names its backing ClusterQueue",
    ),
    AcceptanceCriterion(
        "ac09-artifacts-target-recommendation",
        "The ProvisioningRequest and LocalQueue are paired and target the top "
        "recommended region, zone, and start window.",
        "both artifacts share a pairId and target matching the rank-one window",
    ),
)

MILESTONES = (
    Milestone(
        "interaction-completed",
        "The workload obtainability planning conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        "m1-kage-delegates",
        "Kage delegates the specialist batch-planning request.",
        "root toolCalls contains a completed kanban_create",
        ("interaction-completed",),
    ),
    Milestone(
        "m2-platform-task-created",
        "Kage assigns the request to the Platform Agent.",
        "at least one task is assigned to platform",
        ("m1-kage-delegates",),
    ),
    Milestone(
        "m3-batch-skills-loaded",
        "The Platform Agent loads the batch and scaling skills used by this backend.",
        "the platform task loads gke-batch-hpc and gke-workload-scaling",
        ("m2-platform-task-created",),
    ),
    Milestone(
        "m4-specialist-completes",
        "The delegated Platform analysis completes.",
        "the platform task is done after at least one run",
        ("m2-platform-task-created",),
    ),
    Milestone(
        "m5-planning-remains-read-only",
        "Workload planning does not submit or apply the generated resources.",
        "complete normalized root and worker tool evidence contains no mutation",
        ("interaction-completed",),
    ),
)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _completed_records(
    records: list[dict[str, Any]], record_type: str
) -> list[dict[str, Any]]:
    return [
        item
        for item in records
        if item.get("type") == record_type
        and str(item.get("status") or "").casefold() in {"completed", "passed"}
    ]


def _artifact(
    artifacts: list[dict[str, Any]], kind: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in artifacts
            if isinstance(item.get("manifest"), dict)
            and item["manifest"].get("kind") == kind
        ),
        None,
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration_seconds(value: Any) -> int | None:
    text = str(value or "")
    if not text.endswith("s"):
        return None
    try:
        seconds = float(text[:-1])
    except ValueError:
        return None
    return int(seconds) if seconds.is_integer() else None


def _is_tpu_v5e(value: Any) -> bool:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold()) == "tpuv5e"


def _valid_obtainability_planning_call(
    item: dict[str, Any], created_at: datetime | None
) -> bool:
    details = _mapping(item.get("details"))
    region = str(details.get("region") or "")
    request = _mapping(details.get("request"))
    specs = _mapping(request.get("futureResourcesSpecs"))
    if len(specs) != 1:
        return False
    spec = _mapping(next(iter(specs.values())))
    locations = _mapping(_mapping(spec.get("locationPolicy")).get("locations"))
    zone = next((zone for zone in ALLOWED_ZONES if zone.startswith(region + "-")), "")
    location = _mapping(locations.get(f"zones/{zone}"))
    time_range = _mapping(spec.get("timeRangeSpec"))
    earliest = _timestamp(time_range.get("startTimeNotEarlierThan"))
    latest = _timestamp(time_range.get("startTimeNotLaterThan"))
    aggregate = _mapping(
        _mapping(spec.get("targetResources")).get("aggregateResources")
    )
    return (
        details.get("apiMethod") in OBTAINABILITY_PLANNING_METHODS
        and region in ALLOWED_REGIONS
        and bool(zone)
        and str(location.get("preference") or "").upper() == "ALLOW"
        and created_at is not None
        and earliest is not None
        and latest is not None
        and created_at <= earliest <= created_at + CALL_DELAY_TOLERANCE
        and created_at + PLANNING_HORIZON - JOB_DURATION
        <= latest
        <= created_at
        + PLANNING_HORIZON
        - JOB_DURATION
        + CALL_DELAY_TOLERANCE
        and _duration_seconds(time_range.get("minDuration")) == 43200
        and _duration_seconds(time_range.get("maxDuration")) == 43200
        and aggregate.get("vmFamily") == TPU_V5E_VM_FAMILY
        and aggregate.get("workloadType") == "BATCH"
        and _integer(aggregate.get("acceleratorCount")) == 64
    )


def _target_matches(target: Any, window: dict[str, Any]) -> bool:
    target = _mapping(target)
    return (
        target.get("region") == window.get("region")
        and target.get("zone") == window.get("zone")
        and _timestamp(target.get("startTime")) == _timestamp(window.get("startTime"))
    )


def _valid_window(item: dict[str, Any], created_at: datetime | None) -> bool:
    region = str(item.get("region") or "")
    zone = str(item.get("zone") or "")
    start = _timestamp(item.get("startTime"))
    return (
        bool(region)
        and zone in ALLOWED_ZONES
        and zone.startswith(region + "-")
        and created_at is not None
        and start is not None
        and created_at <= start
        and start + JOB_DURATION <= created_at + PLANNING_HORIZON
        and item.get("durationHours") == 12
        and bool(str(item.get("capacitySignal") or "").strip())
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
    created_at = _timestamp(interaction.get("createdAt"))
    advice_calls = _completed_records(
        evidence,
        "advice_service_workload_obtainability_planning",
    )
    valid_calls = [
        item
        for item in advice_calls
        if _valid_obtainability_planning_call(item, created_at)
    ]
    call_regions = {
        str(_mapping(item.get("details")).get("region") or "")
        for item in valid_calls
    }
    analysis_records = _completed_records(
        evidence,
        "workload_obtainability_planning_analysis",
    )
    analysis = (
        _mapping(_mapping(analysis_records[0].get("details")).get("analysis"))
        if len(analysis_records) == 1
        else {}
    )
    windows = [
        item for item in _list(analysis.get("windows")) if isinstance(item, dict)
    ]
    valid_windows = [item for item in windows if _valid_window(item, created_at)]
    regions = {str(item["region"]) for item in valid_windows}
    ranks = [item.get("rank") for item in windows]
    ranked = (
        bool(windows)
        and all(isinstance(rank, int) and not isinstance(rank, bool) for rank in ranks)
        and ranks == list(range(1, len(windows) + 1))
    )
    top = windows[0] if ranked else {}
    top_start = _timestamp(top.get("startTime"))
    within_horizon = (
        created_at is not None
        and top_start is not None
        and created_at <= top_start
        and top_start + JOB_DURATION <= created_at + PLANNING_HORIZON
    )
    final_output = str(interaction.get("output") or "")
    recommended_in_output = (
        bool(top)
        and str(top.get("zone") or "") in final_output
        and top_start is not None
        and top_start.strftime("%Y-%m-%d") in final_output
        and top_start.strftime("%H:%M") in final_output
        and "UTC" in final_output.upper()
    )

    artifacts = projected_records(interaction, "artifacts")
    provisioning = _artifact(artifacts, "ProvisioningRequest")
    queue = _artifact(artifacts, "LocalQueue")
    provisioning_manifest = _mapping(provisioning.get("manifest")) if provisioning else {}
    provisioning_machine = (
        _mapping(provisioning.get("machineSpec")) if provisioning else {}
    )
    provisioning_spec = _mapping(provisioning_manifest.get("spec"))
    parameters = _mapping(provisioning_spec.get("parameters"))
    pod_sets = [
        item
        for item in _list(provisioning_spec.get("podSets"))
        if isinstance(item, dict)
    ]
    queue_manifest = _mapping(queue.get("manifest")) if queue else {}
    queue_spec = _mapping(queue_manifest.get("spec"))
    queue_metadata = _mapping(queue_manifest.get("metadata"))
    provisioning_metadata = _mapping(provisioning_manifest.get("metadata"))
    paired = (
        provisioning is not None
        and queue is not None
        and bool(top)
        and top_start is not None
        and str(provisioning.get("pairId") or "").strip()
        and provisioning.get("pairId") == queue.get("pairId")
        and provisioning_metadata.get("namespace") == queue_metadata.get("namespace")
        and _target_matches(provisioning.get("target"), top)
        and _target_matches(queue.get("target"), top)
    )

    input_value = _mapping(interaction.get("input"))
    input_text = str(input_value.get("text") or "")
    folded_input = input_text.casefold()
    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-flexible-job-request-preserved",
        re.search(r"(?<!\d)64-node\b", folded_input) is not None
        and "tpu v5e" in folded_input
        and "12 hours" in folded_input
        and "48 hours" in folded_input,
        input_text,
    )
    suite.record(
        "ac02-workload-obtainability-planning-invoked",
        len(advice_calls) >= 2
        and len(valid_calls) == len(advice_calls)
        and call_regions == ALLOWED_REGIONS,
        {"calls": advice_calls, "validCallRegions": sorted(call_regions)},
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *evidence_blocker))),
    )
    suite.record(
        "ac03-multi-region-windows-evaluated",
        len(valid_windows) == len(windows) and len(regions) >= 2,
        {"windows": windows, "regions": sorted(regions)},
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *evidence_blocker))),
    )
    suite.record(
        "ac04-windows-ranked",
        ranked,
        {"ranks": ranks, "windows": windows},
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *evidence_blocker))),
    )
    suite.record(
        "ac05-start-window-recommended",
        within_horizon and recommended_in_output and top.get("durationHours") == 12,
        {
            "createdAt": interaction.get("createdAt"),
            "topWindow": top,
            "output": final_output,
        },
        blocked_by=tuple(
            dict.fromkeys(
                (*interaction_blocker, *evidence_blocker, *final_output_blocker)
            )
        ),
    )
    suite.record(
        "ac06-provisioning-request-generated",
        provisioning_manifest.get("apiVersion")
        in {"autoscaling.x-k8s.io/v1", "autoscaling.x-k8s.io/v1beta1"}
        and bool(str(provisioning_metadata.get("name") or "").strip())
        and bool(str(provisioning_metadata.get("namespace") or "").strip())
        and provisioning_spec.get("provisioningClassName")
        == "queued-provisioning.gke.io",
        provisioning,
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *artifact_blocker))),
    )
    suite.record(
        "ac07-provisioning-request-sized",
        len(pod_sets) == 1
        and pod_sets[0].get("count") == 64
        and bool(_mapping(pod_sets[0].get("podTemplateRef")).get("name"))
        and str(parameters.get("maxRunDurationSeconds") or "") == "43200"
        and _is_tpu_v5e(provisioning_machine.get("acceleratorType")),
        {
            "machineSpec": provisioning_machine,
            "parameters": parameters,
            "podSets": pod_sets,
        },
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *artifact_blocker))),
    )
    suite.record(
        "ac08-localqueue-generated",
        queue_manifest.get("apiVersion")
        in {"kueue.x-k8s.io/v1beta1", "kueue.x-k8s.io/v1beta2"}
        and bool(str(queue_metadata.get("name") or "").strip())
        and bool(str(queue_metadata.get("namespace") or "").strip())
        and bool(str(queue_spec.get("clusterQueue") or "").strip()),
        queue,
        blocked_by=tuple(dict.fromkeys((*interaction_blocker, *artifact_blocker))),
    )
    suite.record(
        "ac09-artifacts-target-recommendation",
        bool(paired),
        {
            "topWindow": top,
            "provisioningPairId": provisioning.get("pairId") if provisioning else None,
            "provisioningTarget": provisioning.get("target") if provisioning else None,
            "queuePairId": queue.get("pairId") if queue else None,
            "queueTarget": queue.get("target") if queue else None,
        },
        blocked_by=tuple(
            dict.fromkeys(
                (*interaction_blocker, *evidence_blocker, *artifact_blocker)
            )
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
    suite.record("m2-platform-task-created", bool(platform_tasks), platform_tasks)
    suite.record(
        "m3-batch-skills-loaded",
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
    suite.record(
        "m4-specialist-completes",
        any(
            task.get("status") == "done" and int(task.get("runCount") or 0) >= 1
            for task in platform_tasks
        ),
        platform_tasks,
    )
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    suite.record(
        "m5-planning-remains-read-only",
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
    return PROMPT


def test_03_workload_obtainability_planning() -> None:
    Scenario(
        "cuj3",
        build_prompt,
        evaluate_acceptance,
        evaluate_kage_milestones,
    ).run_test()
