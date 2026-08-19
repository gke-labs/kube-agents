from __future__ import annotations

from typing import Any

from cuj.obtainability import test_01_cluster_design as cluster_design
from cuj.utils.acceptance_criteria import AcceptanceResult, AcceptanceStatus
from cuj.utils.milestones import MilestoneResult, MilestoneStatus


def interaction(*, include_future_evidence: bool) -> dict[str, Any]:
    task = {
        "taskId": "task-1",
        "title": "Design an obtainability-aware GKE cluster",
        "assignee": "platform",
        "status": "done",
        "summary": "Obtainability-aware cluster design completed.",
        "error": "",
        "runCount": 1,
    }
    observed = {
        "interactionId": "interaction-1",
        "status": "completed",
        "terminal": True,
        "input": {
            "text": "Design a GKE cluster for 32 NVIDIA A100 GPUs in us-central1."
        },
        "output": "Delegated the cluster design to the Platform Agent.",
        "tasks": [task],
        "toolCalls": [
            {"name": "kanban_create", "status": "completed", "source": "root_run"}
        ],
    }
    if include_future_evidence:
        final_output = (
            "Quota is separate from live capacity and obtainability. Autopilot and "
            "Standard were compared, and a ComputeClass fallback was validated."
        )
        task.update(
            {
                "skills": ["gke-cluster-creation", "gke-compute-classes"],
                "loadedSkills": ["gke-cluster-creation", "gke-compute-classes"],
                "evidence": [
                    {"type": "quota_check", "status": "completed"},
                    {
                        "type": "advice_service_capacity",
                        "status": "completed",
                        "details": {
                            "apiMethod": "compute.alpha.AdviceService.Capacity",
                            "request": {
                                "region": "us-central1",
                                "acceleratorType": "A100",
                                "acceleratorCount": 32,
                            },
                            "analysis": {
                                "availableQuantity": 24,
                                "zones": [
                                    {
                                        "zone": "us-central1-a",
                                        "availableQuantity": 8,
                                    },
                                    {
                                        "zone": "us-central1-c",
                                        "availableQuantity": 16,
                                    },
                                ],
                                "provisioningModels": {
                                    "ON_DEMAND": "low",
                                    "SPOT": "medium",
                                    "FLEX": "high",
                                },
                            },
                        },
                    },
                    {"type": "computeclass_server_dry_run", "status": "passed"},
                ],
                "artifacts": [
                    {
                        "id": "computeclass",
                        "type": "kubernetes_manifest",
                        "manifest": {
                            "apiVersion": "cloud.google.com/v1",
                            "kind": "ComputeClass",
                            "spec": {
                                "priorities": [
                                    {"machineFamily": "a2"},
                                    {
                                        "gpu": {"type": "nvidia-l4", "count": 1}
                                    },
                                    {
                                        "gpu": {
                                            "type": "nvidia-tesla-t4",
                                            "count": 1,
                                        }
                                    },
                                ]
                            },
                        },
                    },
                    {
                        "id": "nap",
                        "type": "node_auto_provisioning",
                        "manifest": {
                            "spec": {
                                "nodeAffinity": {
                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                        "nodeSelectorTerms": [
                                            {
                                                "matchExpressions": [
                                                    {
                                                        "key": "cloud.google.com/machine-family",
                                                        "operator": "In",
                                                        "values": [
                                                            "n2",
                                                            "n2d",
                                                            "c2d",
                                                        ],
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                },
                                "location": {"locationPolicy": "ANY"},
                            }
                        },
                    },
                ],
                "result": final_output,
                "toolCalls": [],
            }
        )
        observed["finalOutput"] = final_output
        observed["toolEvidenceComplete"] = True
    return observed


def acceptance_result(
    observed: dict[str, Any], criterion_id: str
) -> AcceptanceResult:
    return next(
        result
        for result in cluster_design.evaluate_acceptance(observed).results
        if result.criterion.id == criterion_id
    )


def milestone_result(observed: dict[str, Any], milestone_id: str) -> MilestoneResult:
    return next(
        result
        for result in cluster_design.evaluate_kage_milestones(observed).results
        if result.milestone.id == milestone_id
    )


def test_future_evidence_contract_passes_every_cuj1_acceptance_criterion() -> None:
    criteria = cluster_design.evaluate_acceptance(
        interaction(include_future_evidence=True)
    )

    assert criteria.passed
    assert len(criteria.results) == len(cluster_design.ACCEPTANCE_CRITERIA) == 11
    assert {result.status for result in criteria.results} == {
        AcceptanceStatus.PASSED
    }


def test_backend_milestones_do_not_define_acceptance() -> None:
    observed = interaction(include_future_evidence=False)
    milestones = cluster_design.evaluate_kage_milestones(observed)
    criteria = cluster_design.evaluate_acceptance(observed)

    assert any(result.status is MilestoneStatus.PASSED for result in milestones.results)
    assert not criteria.passed
    assert {
        result.criterion.id
        for result in criteria.results
        if result.status is AcceptanceStatus.PASSED
    } == {"ac01-target-request-preserved"}
    assert all(
        result.status is not AcceptanceStatus.FAILED for result in criteria.results
    )


def test_acceptance_is_independent_of_backend_assignee() -> None:
    observed = interaction(include_future_evidence=True)
    observed["tasks"][0]["assignee"] = "another-agent-backend"

    assert cluster_design.evaluate_acceptance(observed).passed
    assert not cluster_design.evaluate_kage_milestones(observed).passed


def test_acceptance_supports_root_level_backend_evidence() -> None:
    observed = interaction(include_future_evidence=True)
    task = observed["tasks"].pop()
    observed["evidence"] = task["evidence"]
    observed["artifacts"] = task["artifacts"]

    assert cluster_design.evaluate_acceptance(observed).passed


def test_capacity_acceptance_rejects_wrong_method_and_missing_model() -> None:
    observed = interaction(include_future_evidence=True)
    details = observed["tasks"][0]["evidence"][1]["details"]
    details["apiMethod"] = "compute.instances.list"
    del details["analysis"]["provisioningModels"]["FLEX"]

    assert (
        acceptance_result(observed, "ac02-obtainability-api-invoked").status
        is AcceptanceStatus.FAILED
    )
    assert (
        acceptance_result(observed, "ac05-provisioning-models-analyzed").status
        is AcceptanceStatus.FAILED
    )


def test_computeclass_acceptance_checks_priority_order_and_both_fallbacks() -> None:
    observed = interaction(include_future_evidence=True)
    priorities = observed["tasks"][0]["artifacts"][0]["manifest"]["spec"][
        "priorities"
    ]
    priorities[0] = {"machineFamily": "g2"}
    priorities.pop()

    assert (
        acceptance_result(observed, "ac07-a2-primary").status
        is AcceptanceStatus.FAILED
    )
    assert (
        acceptance_result(observed, "ac08-l4-t4-fallbacks").status
        is AcceptanceStatus.FAILED
    )


def test_computeclass_acceptance_ignores_descriptive_notes() -> None:
    observed = interaction(include_future_evidence=True)
    observed["tasks"][0]["artifacts"][0]["manifest"]["spec"]["priorities"] = [
        {"machineFamily": "g2", "note": "A2 unavailable"},
        {"machineFamily": "n2", "note": "L4 and T4 unavailable"},
    ]

    assert (
        acceptance_result(observed, "ac07-a2-primary").status
        is AcceptanceStatus.FAILED
    )
    assert (
        acceptance_result(observed, "ac08-l4-t4-fallbacks").status
        is AcceptanceStatus.FAILED
    )


def test_nap_acceptance_checks_families_and_location_policy() -> None:
    observed = interaction(include_future_evidence=True)
    nap = observed["tasks"][0]["artifacts"][1]["manifest"]
    expression = nap["spec"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]
    expression["values"] = ["n2"]
    nap["spec"]["location"]["locationPolicy"] = "BALANCED"

    assert (
        acceptance_result(observed, "ac10-nap-machine-families").status
        is AcceptanceStatus.FAILED
    )
    assert (
        acceptance_result(observed, "ac11-nap-location-any").status
        is AcceptanceStatus.FAILED
    )


def test_nap_acceptance_ignores_selector_shaped_unrelated_fields() -> None:
    observed = interaction(include_future_evidence=True)
    observed["tasks"][0]["artifacts"][1]["manifest"] = {
        "spec": {
            "unrelated": {
                "key": "cloud.google.com/machine-family",
                "operator": "In",
                "values": ["n2", "n2d", "c2d"],
            },
            "comment": {"locationPolicy": "ANY"},
        }
    }

    for criterion_id in (
        "ac09-nap-alternative-generated",
        "ac10-nap-machine-families",
        "ac11-nap-location-any",
    ):
        assert (
            acceptance_result(observed, criterion_id).status
            is AcceptanceStatus.FAILED
        )


def test_zonal_distribution_requires_distinct_zones() -> None:
    observed = interaction(include_future_evidence=True)
    analysis = observed["tasks"][0]["evidence"][1]["details"]["analysis"]
    analysis["zones"] = [
        {"zone": "us-central1-a", "availableQuantity": 8},
        {"zone": "us-central1-a", "availableQuantity": 16},
    ]

    assert (
        acceptance_result(observed, "ac04-zonal-distribution-analyzed").status
        is AcceptanceStatus.FAILED
    )


def test_capacity_claims_reject_conflation_and_reversed_guarantee() -> None:
    observed = interaction(include_future_evidence=True)
    observed["finalOutput"] = (
        "Quota is the same as live capacity and obtainability. Autopilot Standard "
        "ComputeClass. Capacity is guaranteed."
    )

    result = milestone_result(observed, "m7-capacity-claims-qualified")

    assert result.status is MilestoneStatus.FAILED


def test_opaque_tool_evidence_cannot_certify_read_only_behavior() -> None:
    observed = interaction(include_future_evidence=True)
    observed["toolCalls"].append({"name": "kubectl", "status": "completed"})

    result = milestone_result(observed, "m9-design-remains-read-only")

    assert result.status is MilestoneStatus.BLOCKED
    assert result.blocked_by == ("tool evidence omits normalized operations",)


def test_worker_mutation_fails_read_only_milestone() -> None:
    observed = interaction(include_future_evidence=True)
    observed["tasks"][0]["toolCalls"] = [
        {"operation": "apply_manifest", "status": "completed"}
    ]

    result = milestone_result(observed, "m9-design-remains-read-only")

    assert result.status is MilestoneStatus.FAILED
