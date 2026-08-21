"""Offline regression tests for the CUJ acceptance and milestone evaluators.

These feed synthetic interaction projections shaped like the portal's
``Interaction.to_dict()`` into the pure evaluator functions, so predicate
regressions surface in milliseconds instead of a live half-hour run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCH_ROOT))

from cuj.obtainability import test_01_cluster_design as cuj1  # noqa: E402
from cuj.obtainability import (  # noqa: E402
    test_03_workload_obtainability_planning as cuj3,
)

QUALIFIED_ANSWER = (
    "Quota is separate from live capacity and obtainability, so both were "
    "checked. Autopilot and Standard were compared; the recommended path is a "
    "ComputeClass fallback design."
)


def _task(**overrides: Any) -> dict[str, Any]:
    task = {
        "taskId": "t_1",
        "title": "GKE cluster design",
        "assignee": "platform",
        "status": "done",
        "summary": "Completed the design report.",
        "error": "",
        "runCount": 1,
    }
    task.update(overrides)
    return task


def _interaction(**overrides: Any) -> dict[str, Any]:
    """A projection with exactly the keys the portal emits today."""

    interaction = {
        "interactionId": "i_1",
        "agentId": "platform-agent",
        "profile": "default",
        "sessionId": "s_1",
        "input": {
            "text": "Design a cluster for 32 NVIDIA A100 GPUs in us-central1."
        },
        "status": "completed",
        "terminal": True,
        "createdAt": "2026-08-21T00:00:00+00:00",
        "updatedAt": "2026-08-21T00:10:00+00:00",
        "rootRunId": "r_1",
        "output": QUALIFIED_ANSWER,
        "error": "",
        "diagnostics": [],
        "approval": None,
        "tasks": [_task()],
        "toolCalls": [
            {"name": "kanban_create", "status": "completed", "source": "root"}
        ],
    }
    interaction.update(overrides)
    return interaction


def _results_by_id(suite: Any) -> dict[str, dict[str, Any]]:
    return {result.to_dict()["id"]: result.to_dict() for result in suite.results}


def test_cuj1_acceptance_reports_only_pass_or_fail() -> None:
    acceptance = cuj1.evaluate_acceptance(_interaction())
    results = _results_by_id(acceptance)
    assert results["ac01-target-request-preserved"]["status"] == "passed"
    assert not acceptance.passed
    statuses = {result["status"] for result in results.values()}
    assert statuses == {"passed", "failed"}
    for result in results.values():
        assert "blockedBy" not in result
        assert isinstance(result["missingProof"], list)


def test_cuj1_reads_the_output_key_the_portal_emits() -> None:
    suite = cuj1.evaluate_kage_milestones(_interaction())
    m7 = _results_by_id(suite)["m7-capacity-claims-qualified"]
    assert m7["status"] == "passed", m7
    assert m7["missingProof"] == []


def test_cuj1_acceptance_gates_on_terminal_interaction() -> None:
    interaction = _interaction(
        status="waiting_for_tasks",
        terminal=False,
        tasks=[_task(evidence=[], artifacts=[])],
    )
    results = _results_by_id(cuj1.evaluate_acceptance(interaction))
    for identifier, result in results.items():
        if identifier == "ac01-target-request-preserved":
            continue
        assert result["status"] == "failed"
        assert "interaction did not complete successfully" in result["missingProof"]


def test_read_only_milestone_fails_on_observed_mutation() -> None:
    interaction = _interaction(
        tasks=[
            _task(
                toolCalls=[
                    {
                        "name": "apply",
                        "operation": "apply_manifest",
                        "status": "completed",
                    }
                ]
            )
        ],
        toolCalls=[{"name": "kubectl", "status": "completed", "source": "root"}],
    )
    for suite, milestone_id in (
        (cuj1.evaluate_kage_milestones(interaction), "m9-design-remains-read-only"),
        (cuj3.evaluate_kage_milestones(interaction), "m5-planning-remains-read-only"),
    ):
        result = _results_by_id(suite)[milestone_id]
        assert result["status"] == "failed", result
        # The observed violation must not be filed as an observability gap.
        assert result["missingProof"] == []
        assert "apply_manifest" in result["observed"]["mutations"]


def test_read_only_milestone_fails_on_unnormalized_call() -> None:
    interaction = _interaction(
        toolEvidenceComplete=True,
        tasks=[_task(toolCalls=[])],
        toolCalls=[
            {
                "name": "mcp__gke__create_cluster",
                "status": "completed",
                "source": "root",
            }
        ],
    )
    result = _results_by_id(cuj1.evaluate_kage_milestones(interaction))[
        "m9-design-remains-read-only"
    ]
    assert result["status"] == "failed", result
    assert "tool evidence omits normalized operations" in result["missingProof"]
    assert "mcp__gke__create_cluster" in result["observed"]["unnormalizedTools"]
