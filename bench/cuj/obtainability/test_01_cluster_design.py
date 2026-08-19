"""Reproduce CUJ1: obtainability-aware Day-0 GKE cluster design."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cuj.milestones import Milestone, MilestoneSuite
from cuj.portal import Portal, PortalError, isolated_portal

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
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
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


@dataclass(frozen=True)
class Config:
    endpoint: str
    agent_id: str
    project_id: str
    profile: str = "default"
    timeout: float = 1600
    poll_interval: float = 2


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, str):
        path.write_text(value.rstrip() + "\n", encoding="utf-8")
    else:
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def completed_evidence(interaction: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for task in interaction.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for item in task.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").casefold()
            if status in {"completed", "passed"}:
                found.add(str(item.get("type") or ""))
    return found


def tool_operations(
    interaction: dict[str, Any], *, completed_only: bool = False
) -> list[str]:
    calls = interaction.get("toolCalls", [])
    if not isinstance(calls, list):
        return []
    return [
        str(call.get("operation") or call.get("name") or "")
        for call in calls
        if isinstance(call, dict)
        and (not completed_only or call.get("status") == "completed")
    ]


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


def opaque_tool_calls(interaction: dict[str, Any]) -> list[str]:
    calls = interaction.get("toolCalls", [])
    if not isinstance(calls, list):
        return []
    return [
        str(call.get("name") or "")
        for call in calls
        if isinstance(call, dict)
        and not str(call.get("operation") or "").strip()
        and str(call.get("name") or "").casefold() in OPAQUE_TOOLS
    ]


def evaluate(interaction: dict[str, Any]) -> MilestoneSuite:
    tasks = [item for item in interaction.get("tasks", []) if isinstance(item, dict)]
    platform_tasks = [task for task in tasks if task.get("assignee") == "platform"]
    skill_evidence_available = any(
        "skills" in task and "loadedSkills" in task for task in platform_tasks
    )
    task_evidence_available = any("evidence" in task for task in platform_tasks)
    routed = [
        task
        for task in platform_tasks
        if REQUIRED_SKILLS <= set(task.get("skills") or [])
        and REQUIRED_SKILLS <= set(task.get("loadedSkills") or [])
    ]
    completed = completed_evidence(interaction)
    operations = tool_operations(interaction)
    completed_operations = tool_operations(interaction, completed_only=True)
    result_text = "\n".join(
        str(task.get(key) or "")
        for task in tasks
        for key in ("result", "summary")
    ).casefold()
    claims_met, claims_observed = capacity_claims(result_text)
    opaque_calls = opaque_tool_calls(interaction)
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
        blocked_by=(
            ("portal interaction projection omits toolEvidenceComplete",)
            if "toolEvidenceComplete" not in interaction
            else ("tool evidence omits normalized operations",)
            if opaque_calls
            else ()
        ),
    )
    return suite


def run(config: Config, output: Path) -> MilestoneSuite:
    prompt = PROMPT.format(project_id=config.project_id)
    request = {
        "agentId": config.agent_id,
        "profile": config.profile,
        "sessionId": f"portal_cuj1_{uuid.uuid4().hex}",
        "input": {"text": prompt},
        "history": [],
    }
    dump(output / "00-request.json", request)

    portal = Portal(config.endpoint)
    interaction = portal.post("interactions", request)
    dump(output / "01-submitted.json", interaction)
    interaction_id = str(interaction.get("interactionId") or "")
    if not interaction_id:
        raise PortalError("portal response did not include interactionId")

    deadline = time.monotonic() + config.timeout
    poll = 0
    previous = interaction
    while str(interaction.get("status") or "") not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            interaction = {**interaction, "evaluatorTimedOut": True}
            break
        if interaction.get("status") == "waiting_for_approval":
            interaction = portal.post(
                f"interactions/{urllib.parse.quote(interaction_id, safe='')}/approval",
                {"choice": "deny"},
            )
        else:
            time.sleep(config.poll_interval)
            interaction = portal.get(
                f"interactions/{urllib.parse.quote(interaction_id, safe='')}"
            )
        poll += 1
        if interaction != previous:
            dump(output / "02-state-changes" / f"{poll:04d}.json", interaction)
            previous = interaction

    dump(output / "03-final-interaction.json", interaction)
    dump(
        output / "04-conversation.json",
        {
            "user": prompt,
            "kage": interaction.get("output", ""),
            "delegatedTasks": interaction.get("tasks", []),
        },
    )
    dump(
        output / "05-skill-routing.json",
        [
            {
                "taskId": task.get("taskId"),
                "assignee": task.get("assignee"),
                "skills": task.get("skills"),
                "loadedSkills": task.get("loadedSkills"),
            }
            for task in interaction.get("tasks", [])
            if isinstance(task, dict)
        ],
    )
    dump(
        output / "06-delegated-evidence.json",
        [
            {"taskId": task.get("taskId"), "evidence": task.get("evidence")}
            for task in interaction.get("tasks", [])
            if isinstance(task, dict)
        ],
    )
    suite = evaluate(interaction)
    for index, result in enumerate(suite.results, start=1):
        dump(
            output / "07-milestones" / f"{index:02d}-{result.milestone.id}.json",
            result.to_dict(),
        )
    dump(output / "08-summary.json", suite.summary())
    return suite


def config_from_env(endpoint: str) -> Config:
    required = {
        "CUJ1_AGENT_ID": os.environ.get("CUJ1_AGENT_ID", "").strip(),
        "CUJ1_PROJECT_ID": os.environ.get("CUJ1_PROJECT_ID", "").strip(),
    }
    missing = [name for name, value in required.items() if not value]
    assert not missing, f"required environment variables: {', '.join(missing)}"
    config = Config(
        endpoint=endpoint,
        agent_id=required["CUJ1_AGENT_ID"],
        project_id=required["CUJ1_PROJECT_ID"],
        profile=os.environ.get("CUJ1_PROFILE", "default"),
        timeout=float(os.environ.get("CUJ1_TIMEOUT", "1600")),
        poll_interval=float(os.environ.get("CUJ1_POLL_INTERVAL", "2")),
    )
    assert config.timeout > 0 and config.poll_interval > 0, (
        "CUJ1_TIMEOUT and CUJ1_POLL_INTERVAL must be positive"
    )
    return config


def test_01_cluster_design() -> None:
    output = Path(tempfile.mkdtemp(prefix="kube-agents-cuj1-", dir="/tmp"))
    try:
        with isolated_portal(output) as endpoint:
            suite = run(config_from_env(endpoint), output)
    except (OSError, ValueError, PortalError) as exc:
        pytest.fail(f"CUJ1 setup failed: {exc}; evidence: {output}")
    for line in suite.report_lines():
        print(line)
    print(f"Evidence: {output}")
    assert suite.passed, f"milestones not met: {suite.failure_summary()}; evidence: {output}"
