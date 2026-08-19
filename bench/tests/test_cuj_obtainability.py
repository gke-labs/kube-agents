from __future__ import annotations

from typing import Any

from cuj.milestones import MilestoneStatus
from cuj.obtainability import test_01_cluster_design as cluster_design


def interaction(*, include_future_evidence: bool) -> dict[str, Any]:
    task = {
        "taskId": "task-1",
        "title": "Design an obtainability-aware GKE cluster",
        "assignee": "platform",
        "status": "done",
        "summary": (
            "Quota and live capacity obtainability were checked. Autopilot and "
            "Standard were compared, and a ComputeClass fallback was validated."
        ),
        "error": "",
        "runCount": 1,
    }
    observed = {
        "interactionId": "interaction-1",
        "status": "completed",
        "terminal": True,
        "tasks": [task],
        "toolCalls": [
            {"name": "kanban_create", "status": "completed", "source": "root_run"}
        ],
    }
    if include_future_evidence:
        task.update(
            {
                "skills": ["gke-cluster-creation", "gke-compute-classes"],
                "loadedSkills": ["gke-cluster-creation", "gke-compute-classes"],
                "evidence": [
                    {"type": "quota_check", "status": "completed"},
                    {"type": "advice_service_capacity", "status": "completed"},
                    {"type": "computeclass_server_dry_run", "status": "passed"},
                ],
                "result": task["summary"],
            }
        )
        observed["toolEvidenceComplete"] = True
    return observed


def test_future_evidence_contract_can_pass_every_cuj1_milestone() -> None:
    suite = cluster_design.evaluate(interaction(include_future_evidence=True))

    assert suite.passed
    assert {result.status for result in suite.results} == {MilestoneStatus.PASSED}


def test_current_portal_contract_blocks_only_unobservable_milestones() -> None:
    suite = cluster_design.evaluate(interaction(include_future_evidence=False))

    blocked = {
        result.milestone.id
        for result in suite.results
        if result.status is MilestoneStatus.BLOCKED
    }
    assert blocked == {
        "m3-design-skills-loaded",
        "m5-quota-checked",
        "m6-live-obtainability-checked",
        "m8-computeclass-validates",
        "m9-design-remains-read-only",
    }
    assert all(
        result.status is not MilestoneStatus.FAILED for result in suite.results
    )
