from __future__ import annotations

from copy import deepcopy
from typing import Any

from cuj.obtainability import test_03_calendar_mode as calendar_mode
from cuj.utils.acceptance_criteria import AcceptanceResult, AcceptanceStatus
from cuj.utils.milestones import MilestoneStatus


TOP_WINDOW = {
    "rank": 1,
    "region": "us-east4",
    "zone": "us-east4-a",
    "startTime": "2026-08-20T22:00:00Z",
    "durationHours": 12,
    "capacitySignal": "high",
}


def interaction(*, include_future_evidence: bool) -> dict[str, Any]:
    task = {
        "taskId": "task-calendar",
        "title": "Plan a TPU v5e capacity window",
        "assignee": "platform",
        "status": "done",
        "summary": "Calendar planning completed.",
        "error": "",
        "runCount": 1,
    }
    observed = {
        "interactionId": "interaction-calendar",
        "createdAt": "2026-08-19T10:00:00Z",
        "status": "completed",
        "terminal": True,
        "input": {
            "text": (
                "Plan a 64-node TPU v5e job that runs for 12 hours and "
                "finishes within 48 hours."
            )
        },
        "output": "Delegated CalendarMode planning.",
        "tasks": [task],
        "toolCalls": [
            {"name": "kanban_create", "status": "completed", "source": "root_run"}
        ],
    }
    if not include_future_evidence:
        return observed

    task.update(
        {
            "skills": ["gke-batch-hpc", "gke-workload-scaling"],
            "loadedSkills": ["gke-batch-hpc", "gke-workload-scaling"],
            "evidence": [
                {
                    "type": "advice_service_calendar_mode",
                    "status": "completed",
                    "details": {
                        "apiMethod": "compute.advice.calendarMode",
                        "region": "us-central1",
                        "request": {
                            "futureResourcesSpecs": {
                                "training-job": {
                                    "locationPolicy": {
                                        "locations": {
                                            "zones/us-central1-a": {
                                                "preference": "ALLOW"
                                            }
                                        }
                                    },
                                    "timeRangeSpec": {
                                        "startTimeNotEarlierThan": (
                                            "2026-08-19T10:00:00Z"
                                        ),
                                        "startTimeNotLaterThan": (
                                            "2026-08-20T22:00:00Z"
                                        ),
                                        "minDuration": "43200s",
                                        "maxDuration": "43200s",
                                    },
                                    "targetResources": {
                                        "aggregateResources": {
                                            "vmFamily": (
                                                calendar_mode.TPU_V5E_VM_FAMILY
                                            ),
                                            "workloadType": "BATCH",
                                            "acceleratorCount": "64",
                                        }
                                    },
                                }
                            }
                        },
                    },
                },
                {
                    "type": "advice_service_calendar_mode",
                    "status": "completed",
                    "details": {
                        "apiMethod": "compute.alpha.AdviceService.CalendarMode",
                        "region": "us-east4",
                        "request": {
                            "futureResourcesSpecs": {
                                "training-job": {
                                    "locationPolicy": {
                                        "locations": {
                                            "zones/us-east4-a": {
                                                "preference": "ALLOW"
                                            }
                                        }
                                    },
                                    "timeRangeSpec": {
                                        "startTimeNotEarlierThan": (
                                            "2026-08-19T10:00:00Z"
                                        ),
                                        "startTimeNotLaterThan": (
                                            "2026-08-20T22:00:00Z"
                                        ),
                                        "minDuration": "43200s",
                                        "maxDuration": "43200s",
                                    },
                                    "targetResources": {
                                        "aggregateResources": {
                                            "vmFamily": (
                                                calendar_mode.TPU_V5E_VM_FAMILY
                                            ),
                                            "workloadType": "BATCH",
                                            "acceleratorCount": "64",
                                        }
                                    },
                                }
                            }
                        },
                    },
                },
                {
                    "type": "calendar_mode_analysis",
                    "status": "completed",
                    "details": {
                        "analysis": {
                            "windows": [
                                deepcopy(TOP_WINDOW),
                                {
                                    "rank": 2,
                                    "region": "us-central1",
                                    "zone": "us-central1-a",
                                    "startTime": "2026-08-20T18:00:00Z",
                                    "durationHours": 12,
                                    "capacitySignal": "medium",
                                },
                            ]
                        },
                    },
                }
            ],
            "artifacts": [
                {
                    "id": "tpu-calendar-provisioning-request",
                    "type": "kubernetes_manifest",
                    "pairId": "tpu-calendar-plan",
                    "machineSpec": {"acceleratorType": "TPU_V5E"},
                    "target": {
                        "region": "us-east4",
                        "zone": "us-east4-a",
                        "startTime": "2026-08-20T22:00:00Z",
                    },
                    "manifest": {
                        "apiVersion": "autoscaling.x-k8s.io/v1",
                        "kind": "ProvisioningRequest",
                        "metadata": {
                            "name": "tpu-v5e-calendar",
                            "namespace": "training",
                        },
                        "spec": {
                            "provisioningClassName": "queued-provisioning.gke.io",
                            "parameters": {"maxRunDurationSeconds": "43200"},
                            "podSets": [
                                {
                                    "count": 64,
                                    "podTemplateRef": {"name": "tpu-v5e-template"},
                                }
                            ],
                        },
                    },
                },
                {
                    "id": "tpu-calendar-local-queue",
                    "type": "kubernetes_manifest",
                    "pairId": "tpu-calendar-plan",
                    "target": {
                        "region": "us-east4",
                        "zone": "us-east4-a",
                        "startTime": "2026-08-20T22:00:00Z",
                    },
                    "manifest": {
                        "apiVersion": "kueue.x-k8s.io/v1beta1",
                        "kind": "LocalQueue",
                        "metadata": {
                            "name": "tpu-calendar",
                            "namespace": "training",
                        },
                        "spec": {"clusterQueue": "dws-tpu-cluster-queue"},
                    },
                },
            ],
            "toolCalls": [],
        }
    )
    observed["finalOutput"] = (
        "Capacity is recommended in us-east4-a starting at "
        "2026-08-20 22:00 UTC."
    )
    observed["toolEvidenceComplete"] = True
    return observed


def result(observed: dict[str, Any], criterion_id: str) -> AcceptanceResult:
    return next(
        item
        for item in calendar_mode.evaluate_acceptance(observed).results
        if item.criterion.id == criterion_id
    )


def evidence(
    observed: dict[str, Any], record_type: str
) -> list[dict[str, Any]]:
    return [
        item
        for item in observed["tasks"][0]["evidence"]
        if item["type"] == record_type
    ]


def test_future_contract_passes_every_calendar_mode_acceptance_criterion() -> None:
    criteria = calendar_mode.evaluate_acceptance(
        interaction(include_future_evidence=True)
    )

    assert criteria.passed
    assert len(criteria.results) == len(calendar_mode.ACCEPTANCE_CRITERIA) == 9
    assert {item.status for item in criteria.results} == {AcceptanceStatus.PASSED}


def test_current_projection_blocks_unobservable_calendar_mode_criteria() -> None:
    criteria = calendar_mode.evaluate_acceptance(
        interaction(include_future_evidence=False)
    )

    assert {
        item.criterion.id
        for item in criteria.results
        if item.status is AcceptanceStatus.PASSED
    } == {"ac01-flexible-job-request-preserved"}
    assert all(item.status is not AcceptanceStatus.FAILED for item in criteria.results)


def test_calendar_acceptance_is_backend_independent() -> None:
    observed = interaction(include_future_evidence=True)
    task = observed["tasks"].pop()
    observed["evidence"] = task["evidence"]
    observed["artifacts"] = task["artifacts"]

    assert calendar_mode.evaluate_acceptance(observed).passed
    assert not calendar_mode.evaluate_kage_milestones(observed).passed


def test_calendar_request_requires_exact_method_and_workload() -> None:
    observed = interaction(include_future_evidence=True)
    details = evidence(observed, "advice_service_calendar_mode")[0]["details"]
    details["apiMethod"] = "fake.compute.advice.calendarMode"
    spec = next(iter(details["request"]["futureResourcesSpecs"].values()))
    spec["targetResources"]["aggregateResources"]["acceleratorCount"] = "63"

    assert (
        result(observed, "ac02-calendar-mode-invoked").status
        is AcceptanceStatus.FAILED
    )


def test_calendar_request_must_cover_the_feasible_start_horizon() -> None:
    observed = interaction(include_future_evidence=True)
    for call in evidence(observed, "advice_service_calendar_mode"):
        spec = next(
            iter(call["details"]["request"]["futureResourcesSpecs"].values())
        )
        spec["timeRangeSpec"].update(
            {
                "startTimeNotEarlierThan": "2026-08-19T11:00:00Z",
                "startTimeNotLaterThan": "2026-08-19T12:00:00Z",
            }
        )

    assert (
        result(observed, "ac02-calendar-mode-invoked").status
        is AcceptanceStatus.FAILED
    )


def test_calendar_request_accepts_completed_call_retries() -> None:
    observed = interaction(include_future_evidence=True)
    calls = evidence(observed, "advice_service_calendar_mode")
    observed["tasks"][0]["evidence"].append(deepcopy(calls[0]))

    assert (
        result(observed, "ac02-calendar-mode-invoked").status
        is AcceptanceStatus.PASSED
    )


def test_calendar_windows_require_multiple_regions_and_consecutive_ranks() -> None:
    observed = interaction(include_future_evidence=True)
    windows = evidence(observed, "calendar_mode_analysis")[0]["details"][
        "analysis"
    ]["windows"]
    windows[1]["region"] = "us-east4"
    windows[1]["rank"] = 3

    assert (
        result(observed, "ac03-multi-region-windows-evaluated").status
        is AcceptanceStatus.FAILED
    )
    assert result(observed, "ac04-windows-ranked").status is AcceptanceStatus.FAILED


def test_recommended_window_must_be_delivered_within_horizon() -> None:
    observed = interaction(include_future_evidence=True)
    top = evidence(observed, "calendar_mode_analysis")[0]["details"][
        "analysis"
    ]["windows"][0]
    top["startTime"] = "2026-08-22T22:00:00Z"
    observed["finalOutput"] = "Try a future window when capacity is available."

    assert (
        result(observed, "ac05-start-window-recommended").status
        is AcceptanceStatus.FAILED
    )


def test_recommended_window_requires_the_exact_date() -> None:
    observed = interaction(include_future_evidence=True)
    observed["finalOutput"] = (
        "Capacity is recommended in us-east4-a starting at "
        "2026-08-21 22:00 UTC."
    )

    assert (
        result(observed, "ac05-start-window-recommended").status
        is AcceptanceStatus.FAILED
    )


def test_failed_interaction_cannot_satisfy_acceptance() -> None:
    observed = interaction(include_future_evidence=True)
    observed["status"] = "failed"

    assert not calendar_mode.evaluate_acceptance(observed).passed
    assert (
        result(observed, "ac02-calendar-mode-invoked").status
        is AcceptanceStatus.BLOCKED
    )


def test_provisioning_request_requires_dws_size_and_duration() -> None:
    observed = interaction(include_future_evidence=True)
    provisioning = observed["tasks"][0]["artifacts"][0]
    spec = provisioning["manifest"]["spec"]
    provisioning["machineSpec"]["acceleratorType"] = "not-tpu-v5e"
    provisioning["manifest"]["metadata"]["name"] = ""
    spec["provisioningClassName"] = "check-capacity.autoscaling.x-k8s.io"
    spec["parameters"]["maxRunDurationSeconds"] = "43199"
    spec["podSets"][0]["count"] = 63

    assert (
        result(observed, "ac06-provisioning-request-generated").status
        is AcceptanceStatus.FAILED
    )
    assert (
        result(observed, "ac07-provisioning-request-sized").status
        is AcceptanceStatus.FAILED
    )


def test_localqueue_and_provisioning_request_must_target_top_window() -> None:
    observed = interaction(include_future_evidence=True)
    queue = observed["tasks"][0]["artifacts"][1]
    queue["manifest"]["spec"]["clusterQueue"] = ""
    queue["pairId"] = "different-plan"
    queue["target"]["zone"] = "us-central1-a"

    assert (
        result(observed, "ac08-localqueue-generated").status
        is AcceptanceStatus.FAILED
    )
    assert (
        result(observed, "ac09-artifacts-target-recommendation").status
        is AcceptanceStatus.FAILED
    )


def test_kage_milestones_remain_diagnostic() -> None:
    milestones = calendar_mode.evaluate_kage_milestones(
        interaction(include_future_evidence=False)
    )

    assert any(item.status is MilestoneStatus.PASSED for item in milestones.results)
    assert not milestones.passed
