"""capacity-multipool-ca-scan-delay-capacitybuffer grades three free-form lines.

The prompt gives no verdict slug and enumerates none of the answer terms. Each
objective grades one labeled line under ``fold_decoration: true``:

- ``CAUSE:`` must explain per-pool / multi-pool evaluation, simulation,
  iteration, or predicate scanning across the node pools or node groups
  (echoing the prompt's symptom or blaming stockout/quota/taints does not pass).
- ``TARGETING:`` must name ``cloud.google.com/default-compute-class`` (or its
  ``-non-daemonset`` variant) on the ``TARGETING:`` line itself, unnegated,
  unhedged, and without pairing another key, flag, or per-Deployment
  alternative.
- ``HEADROOM:`` must name ``capacitybuffer(s).autoscaling.x-k8s.io``,
  ``autoscaling.x-k8s.io(/v...)/CapacityBuffer(s)``, or ``CapacityBuffer(s)``
  paired with ``autoscaling.x-k8s.io`` on the ``HEADROOM:`` line itself (prose
  "capacity buffer" without the API group, wrong-group CRDs, and ``min-nodes`` /
  ``minNodeCount`` do not pass).
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


def test_expected_output_passes_all_objectives():
    expected = yaml.safe_load(TASK.read_text())["expected_output"]
    assert _passes_all(expected), expected


def test_accepts_decorated_lines_and_api_version_kind_spelling():
    report = (
        "Summary of the diagnosis.\n\n"
        "- **CAUSE:** Because the pods have no ComputeClass selector, Cluster "
        "Autoscaler simulates scheduling across every specialized GPU pool.\n"
        "- **TARGETING:** `cloud.google.com/default-compute-class-non-daemonset`\n"
        "- **HEADROOM:** `CapacityBuffer` (`autoscaling.x-k8s.io/v1beta1`).\n"
    )
    assert _passes_all(report)


def test_accepts_variant_cause_targeting_and_headroom_forms():
    for cause in (
        "CAUSE: Per-pool predicate evaluation across 28 pools delays scale-up.",
        "CAUSE: Cluster Autoscaler scans each node pool before triggering scale-up.",
        "CAUSE: Multi-pool simulation over all 28 tainted GPU node pools slows each loop.",
        "CAUSE: Untargeted pods force Cluster Autoscaler to evaluate scheduling predicates across all 28 specialized and tainted GPU node pools.",
        "CAUSE: Cluster Autoscaler simulates scheduling across all 28 specialized, tainted GPU node pools.",
        "CAUSE: Cluster Autoscaler evaluates predicates across 28 specialized/tainted GPU node pools.",
        "CAUSE: Cluster Autoscaler evaluates scheduling predicates across all 28 node groups on every scan loop.",
        "CAUSE: Cluster Autoscaler must evaluate scheduling predicates across multiple node pools.",
        "CAUSE: Multi-pool Cluster Autoscaler scan delay from untargeted pods.",
        "CAUSE: Untargeted pods have no predicate filtering, so Cluster Autoscaler evaluates all 28 pools.",
        "CAUSE: With no scan pruning, Cluster Autoscaler simulates scheduling across all 28 pools.",
        "CAUSE: Untargeted pods have no selector, so there is no evaluation shortcut; CA scans all 28 pools.",
        "CAUSE: Not a zonal stockout — Cluster Autoscaler evaluates scheduling predicates across every node pool.",
        "CAUSE: Not a zonal GPU stockout — Cluster Autoscaler evaluates scheduling predicates across every node pool.",
        "CAUSE: With no quota limit, Cluster Autoscaler evaluates scheduling predicates across all 28 node pools.",
        "CAUSE: Not a quota issue; Cluster Autoscaler evaluates scheduling predicates across all 28 node pools.",
    ):
        assert _passes(cause, CHECK_CAUSE), cause

    for targeting in (
        "TARGETING: cloud.google.com/default-compute-class",
        'TARGETING: metadata.labels["cloud.google.com/default-compute-class"]',
        "TARGETING: namespace label cloud.google.com/default-compute-class-non-daemonset",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/default-compute-class-non-daemonset",
        "TARGETING: cloud.google.com/default-compute-class-non-daemonset or cloud.google.com/default-compute-class",
        "TARGETING: `cloud.google.com/default-compute-class` or `cloud.google.com/default-compute-class-non-daemonset`",
        'TARGETING: "cloud.google.com/default-compute-class" or "cloud.google.com/default-compute-class-non-daemonset"',
        "TARGETING: cloud.google.com/default-compute-class (or cloud.google.com/default-compute-class-non-daemonset)",
    ):
        assert _passes(targeting, CHECK_TARGETING), targeting

    for headroom in (
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffer.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io/v1beta1",
        "HEADROOM: autoscaling.x-k8s.io/v1beta1/CapacityBuffer",
        "HEADROOM: autoscaling.x-k8s.io/CapacityBuffer",
        "HEADROOM: autoscaling.x-k8s.io/v1beta1 CapacityBuffer",
        "HEADROOM: CapacityBuffer (capacitybuffers.autoscaling.x-k8s.io)",
        "HEADROOM: CapacityBuffer (autoscaling.x-k8s.io/v1beta1).",
        "HEADROOM: apiVersion: autoscaling.x-k8s.io/v1beta1; kind: CapacityBuffer",
        "HEADROOM: CapacityBuffer (autoscaling.x-k8s.io/v1beta1; provisioningStrategy: buffer.x-k8s.io/active-capacity)",
    ):
        assert _passes(headroom, CHECK_HEADROOM), headroom


def test_rejects_prompt_echo_and_wrong_causes():
    for bad_cause in (
        # Pure prompt echo without explaining per-pool evaluation.
        "CAUSE: The cluster has 28 specialized and tainted GPU node pools and pods have no nodeSelector.",
        # Taint mismatch without per-pool evaluation/simulation/scanning.
        "CAUSE: Untargeted pods do not match the taints on all 28 GPU node pools.",
        "CAUSE: Untargeted pods fail taint checks on all 28 GPU node pools.",
        # Unrelated root causes, even when naming all pools or scan, or after negating another cause.
        "CAUSE: Zonal GPU stockout delays node provisioning by 12 minutes.",
        "CAUSE: Zonal GPU stockout across all 28 node pools delays the Cluster Autoscaler scan.",
        "CAUSE: With no quota limit, a zonal GPU stockout across all 28 node pools delays the Cluster Autoscaler scan.",
        "CAUSE: Not a stockout; Cluster Autoscaler has a 12-minute cooldown across all 28 node pools on each scan.",
        "CAUSE: With no quota exhaustion, image pull latency across all 28 GPU node pools delays the scan.",
        "CAUSE: Not a zonal stockout; there is a GPU stockout across all 28 node pools on each scan.",
        "CAUSE: Quota checks failed across all 28 GPU node pools.",
        "CAUSE: Quota limits across all 28 GPU node pools delay the Cluster Autoscaler scan.",
        "CAUSE: Cluster Autoscaler has a 12-minute scale-up cooldown.",
        # Negated or hedged cause.
        "CAUSE: Not per-pool evaluation; it is image pull latency.",
        "CAUSE: Not the per-pool evaluation; it is image pull latency.",
        "CAUSE: Cluster Autoscaler does not evaluate all 28 node pools; it is a stockout.",
        "CAUSE: Per-pool evaluation across all 28 pools is not the cause; it is a stockout.",
        "CAUSE: Per-pool evaluation across all 28 pools?",
        # Only in body prose, missing CAUSE line.
        "Cluster Autoscaler evaluates predicates across all 28 pools.",
    ):
        assert not _passes(bad_cause, CHECK_CAUSE), bad_cause


def test_rejects_wrong_negated_or_unanchored_targeting():
    for bad_targeting in (
        # Pod-level selector instead of namespace default label.
        "TARGETING: cloud.google.com/compute-class",
        # Invalid suffix or path segment.
        "TARGETING: cloud.google.com/default-compute-class-daemonset",
        "TARGETING: cloud.google.com/default-compute-class/invalid",
        "TARGETING: cloud.google.com/default-compute-class.invalid",
        # Per-Deployment, nodeSelector, or wrong alternative paired with or (on either side or with invalid suffix).
        "TARGETING: spec.template.spec.nodeSelector",
        "TARGETING: per-Deployment GitOps PRs with cloud.google.com/default-compute-class",
        "TARGETING: per deployment manifests with cloud.google.com/default-compute-class",
        "TARGETING: cloud.google.com/default-compute-class or node_selector",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/compute-class",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/default-compute-class-daemonset",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/default-compute-class/invalid",
        "TARGETING: cloud.google.com/default-compute-class or cloud.google.com/default-compute-class.invalid",
        "TARGETING: cloud.google.com/machine-family or cloud.google.com/default-compute-class",
        "TARGETING: --enable-default-compute-class or cloud.google.com/default-compute-class",
        "TARGETING: foo.cloud.google.com/default-compute-class or cloud.google.com/default-compute-class-non-daemonset",
        # Negated or hedged.
        "TARGETING: do not use cloud.google.com/default-compute-class",
        "TARGETING: avoid cloud.google.com/default-compute-class",
        "TARGETING: cloud.google.com/default-compute-class?",
        # Only mentioned in prose or on another line, not on TARGETING line.
        "Set cloud.google.com/default-compute-class on the namespace.\nTARGETING: nodeSelector",
        "TARGETING:\nHEADROOM: cloud.google.com/default-compute-class",
    ):
        assert not _passes(bad_targeting, CHECK_TARGETING), bad_targeting


def test_rejects_prose_capacity_buffer_wrong_group_or_min_nodes():
    for bad_headroom in (
        # Prose phrase without API group.
        "HEADROOM: capacity buffer",
        "HEADROOM: CapacityBuffer",
        # Wrong API group, invalid version, prefixed/suffixed name, or other CRD.
        "HEADROOM: CapacityBuffer (autoscaling.k8s.io/v1)",
        "HEADROOM: autoscaling.x-k8s.io/v1beta1",
        "HEADROOM: horizontalpodautoscalers.autoscaling.x-k8s.io",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io.invalid",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io/invalid",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io/vinvalid",
        "HEADROOM: foo.capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: foo.capacitybuffers (autoscaling.x-k8s.io/v1beta1)",
        "HEADROOM: capacitybuffers.invalid (autoscaling.x-k8s.io/v1beta1)",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io-extra",
        "HEADROOM: CapacityBuffer (buffer.x-k8s.io), ProvisioningRequest (autoscaling.x-k8s.io/v1)",
        "HEADROOM: CapacityQuota (autoscaling.x-k8s.io/v1beta1) for capacitybuffer",
        # Wrong mechanism, negated, or hedged.
        "HEADROOM: --min-nodes",
        "HEADROOM: capacitybuffers.autoscaling.x-k8s.io or minNodeCount",
        "HEADROOM: do not use capacitybuffers.autoscaling.x-k8s.io",
        "HEADROOM: avoid capacitybuffers.autoscaling.x-k8s.io",
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
