"""The answer objective of ``fanout-absent-cluster-cards-complete`` reads the
replica count in the words the answer uses.

Every fixture is graded through ``ReportContainsVerifier.verify()`` on a
staged transcript, phrases, patterns and the forbidden pattern together, as
the run grades it. The positives are the spellings the recorded nightly
answers wrote for a two-replica Deployment: the kubectl columns read out, the
READY column, and the field named with its value. The negatives name the
cluster without the count, give another count, or leave seeded-a unchecked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "fanout-absent-cluster-cards-complete" / "task.yaml"
OBJECTIVE = "the-answer-names-the-cluster"
TIMEOUT_SEC = 5
HEAD = "checkout-gateway is on seeded-a. "

# Each names the workload and the cluster and gives the count of two.
COUNTS = [
    "checkout-gateway runs on seeded-a with 2 replicas.",
    "checkout-gateway is on seeded-a, with a replica count of 2.",
    # The recorded spellings.
    "Deployment `checkout-gateway` is running in namespace `seeded-reliability` on seeded-a, with `2` desired, `2` ready and `2` available replicas.",
    "checkout-gateway on seeded-a has 2/2 ready, 2 up-to-date, and 2 available replicas with both pods running.",
    "checkout-gateway on seeded-a: all 2 desired replicas are ready and available.",
    "checkout-gateway on seeded-a: replica count is 2.",
    "checkout-gateway on seeded-a: replica count: 2.",
    "The platform agent found checkout-gateway running 2 replica pods on seeded-a.",
    "The checkout-gateway deployment on seeded-a shows spec.replicas=2, readyReplicas=2, availableReplicas=2 and updatedReplicas=2.",
    "checkout-gateway runs on seeded-a and is fully available. Its spec.replicas is 2, status.readyReplicas is 2.",
    "checkout-gateway by cluster:\n| seeded-a | Present | `2/2` ready (`2` desired, `2` available) |\n| seeded-b | Not present | `0` |",
    "checkout-gateway (seeded-a): **2 replicas**, both pods `1/1` Ready.",
    'The card returned {"cluster": "seeded-a", "desiredReplicas": 2, "readyReplicas": 2, "present": true} for checkout-gateway.',
]

# Each names the cluster and does not give the count of two, or leaves it unchecked.
NOT_COUNTS = [
    "checkout-gateway runs on seeded-a. Both pods are 1/1 Ready.",
    "checkout-gateway runs on seeded-a with 3 replicas (3 desired, 3 ready).",
    "checkout-gateway was not found on seeded-b, seeded-c or seeded-d. seeded-a: 0 (seeded-reliability not found).",
    "checkout-gateway was not found on seeded-b or seeded-c; seeded-a is unchecked, so I cannot give the replica count yet.",
    "checkout-gateway has 2 replicas on seeded-b; seeded-a was not checked.",
    "checkout-gateway is on seeded-a. 3 replicas, 2 ready.",
    "checkout-gateway on seeded-a has 3 desired, 2 ready and 2 available replicas.",
]


def _check() -> dict:
    spec = yaml.safe_load(TASK.read_text())
    entries = [e for e in spec["verification_spec"] if e["name"] == OBJECTIVE]
    assert len(entries) == 1, f"{OBJECTIVE} is not in {TASK}"
    return entries[0]["check"]


def _passes(report: str) -> bool:
    transcript.set(report, [], final_message=report)
    try:
        result = ReportContainsVerifier(**_check()).verify(timeout_sec=TIMEOUT_SEC)
    finally:
        transcript.clear()
    assert result.status != "error", result.reason
    return result.success


@pytest.mark.parametrize("report", COUNTS)
def test_a_two_replica_answer_passes_however_it_is_spelled(report):
    assert _passes(report), report


@pytest.mark.parametrize("report", NOT_COUNTS)
def test_an_answer_without_the_count_fails(report):
    assert not _passes(report), report
