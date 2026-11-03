"""Tests that the two copies of hindsight-api's resources agree.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

The hindsight-api container's resources are written twice: once as
`hindsight.api.resources` in the chart's values, which the chart template
renders verbatim, and once inline in the operator's kustomize manifest, which
the dev path deploys. The template says it mirrors the kustomize directory, but
`make chart-check` compares only the CRD, RBAC, admission-policy and webhook
copies, so nothing kept the two resource blocks in step. These tests do, and
they pin the shape the numbers have to keep: a request small enough to
schedule beside the agent pod, below a limit the reranker can burst into
during recall.
"""

import pathlib
import sys
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _autopilot import AUTOPILOT_MAX_GIB_PER_VCPU, AUTOPILOT_MIN_GIB_PER_VCPU, gib_per_vcpu_in_band  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VALUES = _ROOT / "charts" / "kube-agents" / "values.yaml"
_API_YAML = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "api.yaml"

# Kubernetes CPU quantities: a bare number is cores, an `m` suffix is
# thousandths of a core. Nothing here writes any other suffix.
_MILLI_SUFFIX = "m"
_MILLIS_PER_CORE = 1000
# Memory quantities here are whole Gi. The request has to hold the 1.4 GB of
# model weights the API loads in process when an Autopilot cluster without
# bursting clamps the limit to it, and the pair the issue pinned.
_GI_SUFFIX = "Gi"
_PINNED_REQUESTS = {"cpu": "500m", "memory": "2Gi"}
_PINNED_LIMITS = {"cpu": "4", "memory": "4Gi"}
_MIN_MEMORY_REQUEST_GI = 2


def _cpu_millis(quantity):
    """A CPU quantity as an integer number of millicores."""
    text = str(quantity)
    if text.endswith(_MILLI_SUFFIX):
        return int(text[: -len(_MILLI_SUFFIX)])
    return int(float(text) * _MILLIS_PER_CORE)


def _memory_gi(quantity):
    """A memory quantity written in Gi as an integer number of GiB."""
    text = str(quantity)
    assert text.endswith(_GI_SUFFIX), f"{text} is not written in Gi"
    return int(text[: -len(_GI_SUFFIX)])


def _chart_resources():
    values = yaml.safe_load(_VALUES.read_text())
    return values["hindsight"]["api"]["resources"]


def _kustomize_resources():
    """The `api` container's resources from the operator's manifest.

    api.yaml is a multi-document file whose image is an unexpanded
    `${HINDSIGHT_API_IMAGE}`; safe_load_all reads it happily, since the
    substitution the deploy target does is not YAML-significant.
    """
    docs = [d for d in yaml.safe_load_all(_API_YAML.read_text()) if d]
    deployments = [
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "hindsight-api"
    ]
    assert len(deployments) == 1, f"expected one hindsight-api Deployment, got {len(deployments)}"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert "api" in by_name, f"no container named 'api' in {sorted(by_name)}"
    return by_name["api"]["resources"]


class HindsightApiResourcesTest(unittest.TestCase):
    def setUp(self):
        self.chart = _chart_resources()
        self.kustomize = _kustomize_resources()

    def test_chart_values_and_kustomize_manifest_agree(self):
        self.assertEqual(
            self.chart,
            self.kustomize,
            "hindsight.api.resources in charts/kube-agents/values.yaml and the api "
            "container's resources in k8s-operator/config/integrations/hindsight/"
            "api.yaml have drifted apart; change both, they deploy the same workload",
        )

    def test_the_cpu_request_is_below_the_limit(self):
        # The request is sized for steady state, the limit for the reranker's
        # recall bursts. A request that reaches the limit is the shape that
        # never schedules on an e2-standard-4 beside the system daemonsets.
        request = _cpu_millis(self.chart["requests"]["cpu"])
        limit = _cpu_millis(self.chart["limits"]["cpu"])
        self.assertLess(
            request,
            limit,
            f"hindsight-api requests {request}m CPU against a {limit}m limit; the "
            "request is steady state and the limit is burst headroom, so the two "
            "have to differ",
        )

    def test_the_cpu_request_is_steady_state_sized(self):
        # One core is the ceiling for a request that still co-schedules with the
        # agent pod on a small node; steady state is a few millicores
        # (docs/designs/memory.md, the hindsight-api component entry).
        request = _cpu_millis(self.chart["requests"]["cpu"])
        self.assertLessEqual(
            request,
            _MILLIS_PER_CORE,
            f"hindsight-api requests {request}m CPU; the request reserves capacity "
            "the scheduler and Autopilot billing count continuously, and the "
            "process idles at a few millicores",
        )

    def test_the_pair_is_the_pinned_one(self):
        self.assertEqual(self.chart["requests"], _PINNED_REQUESTS)
        self.assertEqual(self.chart["limits"], _PINNED_LIMITS)

    def test_the_memory_request_holds_the_model_weights_under_a_clamp(self):
        # An Autopilot cluster without Pod bursting sets the limit equal to
        # the request, and the container loads 1.4 GB of model weights in
        # process, so a request under 2Gi is OOM-killed there on cold start.
        request = _memory_gi(self.chart["requests"]["memory"])
        self.assertGreaterEqual(
            request,
            _MIN_MEMORY_REQUEST_GI,
            f"hindsight-api requests {request}Gi of memory; clamped to it, the "
            "1.4 GB of model weights do not fit",
        )

    def test_the_requests_pair_is_in_the_autopilot_band(self):
        # Outside the band Autopilot raises the smaller request, so the pod it
        # admits is larger than the manifest and the quota preflight counts.
        gib_per_vcpu = _memory_gi(self.chart["requests"]["memory"]) / (
            _cpu_millis(self.chart["requests"]["cpu"]) / _MILLIS_PER_CORE
        )
        self.assertTrue(
            gib_per_vcpu_in_band(gib_per_vcpu),
            f"hindsight-api requests {gib_per_vcpu} GiB per vCPU, outside the "
            f"{AUTOPILOT_MIN_GIB_PER_VCPU} to {AUTOPILOT_MAX_GIB_PER_VCPU} GiB per vCPU "
            "GKE Autopilot admits unchanged",
        )


if __name__ == "__main__":
    unittest.main()
