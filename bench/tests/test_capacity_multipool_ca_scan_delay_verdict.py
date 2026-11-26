"""capacity-multipool-ca-scan-delay-capacitybuffer grades three free-form lines.

The prompt gives no verdict slug and enumerates none of the answer terms. Each
objective grades one labeled line under ``fold_decoration: true``:

- ``CAUSE:`` must explain per-pool / multi-pool evaluation, simulation, or
  predicate scanning across the node pools (echoing the prompt's symptom does
  not pass).
- ``TARGETING:`` must name ``cloud.google.com/default-compute-class`` (or its
  ``-non-daemonset`` variant) on the ``TARGETING:`` line itself, unnegated and
  unhedged.
- ``HEADROOM:`` must name ``capacitybuffers.autoscaling.x-k8s.io`` or
  ``CapacityBuffer`` paired with ``autoscaling.x-k8s.io`` on the ``HEADROOM:``
  line itself (prose "capacity buffer" without the API group does not pass).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "capacity-multipool-ca-scan-delay-capacitybuffer"
    / "task.yaml"
)
CHECK_CAUSE = "cause-names-per-pool-evaluation"
CHECK_TARGETING = "targeting-names-namespace-default-compute-class"
CHECK_HEADROOM = "headroom-names-capacitybuffers-api-resource"
ALL_OBJECTIVES = (CHECK_CAUSE, CHECK_TARGETING, CHECK_HEADROOM)
TIMEOUT_SEC = 5.0

GOOD_CAUSE = (
    "CAUSE: Untargeted pods force Cluster Autoscaler to evaluate scheduling "
    "predicates per node pool across all 28 pools on each scan loop."
)
GOOD_TARGETING = "TARGETING: cloud.google.com/default-compute-class"
GOOD_HEADROOM = "HEADROOM: capacitybuffers.autoscaling.x-k8s.io"


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str, check_name: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (entry,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**entry["check"]).verify(TIMEOUT_SEC).success


def _passes_all(report: str) -> bool:
    return all(_passes(report, name) for name in ALL_OBJECTIVES)


def test_accepts_canonical_three_lines():
    report = f"{GOOD_CAUSE}\n{GOOD_TARGETING}\n{GOOD_HEADROOM}"
    assert _passes_all(report)


def test_accepts_decorated_lines_and_api_version_kind_spelling():
    report = (
        "Summary of the diagnosis.\n\n"
        "- **CAUSE:** Because the pods have no ComputeClass selector, Cluster "
        "Autoscaler simulates scheduling across every specialized GPU pool.\n"
        "- **TARGETING:** `cloud.google.com/default-compute-class-non-daemonset`\n"
        "- **HEADROOM:** `CapacityBuffer` (`autoscaling.x-k8s.io/v1beta1`).\n"
    )
    assert _passes_all(report)


def test_accepts_variant_cause_and_headroom_forms():
    for cause in (
        "CAUSE: Per-pool predicate evaluation across 28 pools delays scale-up.",
        "CAUSE: Cluster Autoscaler scans each node pool before triggering scale-up.",
        "CAUSE: Multi-pool simulation over all 28 tainted GPU node pools slows each loop.",
        "CAUSE: Untargeted pods have no selector, so CA checks every pool on each scan.",
    ):
        assert _passes(cause, CHECK_CAUSE), cause

    for targeting in (
        "TARGETING: cloud.google.com/default-compute-class",
        'TARGETING: metadata.labels["cloud.google.com/default-compute-class"]',
        "TARGETING: namespace label cloud.google.com/default-compute-class-non-daemonset",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/default-compute-class-non-daemonset",
    ):
        assert _passes(targeting, CHECK_TARGETING), targeting

    for headroom in (
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io/v1beta1",
        "HEADROOM: autoscaling.x-k8s.io/v1beta1 CapacityBuffer",
        "HEADROOM: CapacityBuffer (capacitybuffers.autoscaling.x-k8s.io)",
        "HEADROOM: CapacityBuffer (autoscaling.x-k8s.io/v1beta1).",
    ):
        assert _passes(headroom, CHECK_HEADROOM), headroom


def test_rejects_prompt_echo_and_wrong_causes():
    for bad_cause in (
        # Pure prompt echo without explaining per-pool evaluation.
        "CAUSE: The cluster has 28 specialized and tainted GPU node pools and pods have no nodeSelector.",
        # Taint mismatch without per-pool evaluation/simulation/scanning.
        "CAUSE: Untargeted pods do not match the taints on all 28 GPU node pools.",
        # Unrelated root causes.
        "CAUSE: Zonal GPU stockout delays node provisioning by 12 minutes.",
        "CAUSE: Cluster Autoscaler has a 12-minute scale-up cooldown.",
        # Negated or hedged cause.
        "CAUSE: Not per-pool evaluation; it is image pull latency.",
        "CAUSE: Per-pool evaluation across all 28 pools?",
        # Only in body prose, missing CAUSE line.
        "Cluster Autoscaler evaluates predicates across all 28 pools.",
    ):
        assert not _passes(bad_cause, CHECK_CAUSE), bad_cause


def test_rejects_wrong_negated_or_unanchored_targeting():
    for bad_targeting in (
        # Pod-level selector instead of namespace default label.
        "TARGETING: cloud.google.com/compute-class",
        # Invalid suffix.
        "TARGETING: cloud.google.com/default-compute-class-daemonset",
        # Per-Deployment or nodeSelector fix.
        "TARGETING: spec.template.spec.nodeSelector",
        "TARGETING: per-Deployment GitOps PRs with cloud.google.com/default-compute-class",
        # Negated or hedged.
        "TARGETING: do not use cloud.google.com/default-compute-class",
        "TARGETING: cloud.google.com/default-compute-class?",
        # Only mentioned in prose, not on TARGETING line.
        "Set cloud.google.com/default-compute-class on the namespace.\nTARGETING: nodeSelector",
    ):
        assert not _passes(bad_targeting, CHECK_TARGETING), bad_targeting


def test_rejects_prose_capacity_buffer_wrong_group_or_min_nodes():
    for bad_headroom in (
        # Prose phrase without API group (Jayanti's echo gap).
        "HEADROOM: capacity buffer",
        "HEADROOM: CapacityBuffer",
        # Wrong API group or missing kind.
        "HEADROOM: CapacityBuffer (autoscaling.k8s.io/v1)",
        "HEADROOM: autoscaling.x-k8s.io/v1beta1",
        "HEADROOM: horizontalpodautoscalers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io.invalid",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io/invalid",
        "HEADROOM: foo.capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io-extra",
        # Wrong mechanism, negated, or hedged.
        "HEADROOM: --min-nodes",
        "HEADROOM: do not use capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io?",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io; --min-nodes",
        # Only mentioned in prose, not on HEADROOM line.
        "Use capacitybuffers.autoscaling.x-k8s.io for bursts.\nHEADROOM: warm pool",
    ):
        assert not _passes(bad_headroom, CHECK_HEADROOM), bad_headroom


def test_rejects_verbatim_prompt_echo():
    prompt = yaml.safe_load(TASK.read_text())["prompt"]
    for check_name in ALL_OBJECTIVES:
        assert not _passes(prompt, check_name), check_name

