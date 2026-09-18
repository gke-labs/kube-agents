"""Tests that the two copies of hindsight-api's resources agree.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

The hindsight-api container's resources are written twice: once as
`hindsight.api.resources` in the chart's values, which the chart template
renders verbatim, and once inline in the operator's kustomize manifest, which
the dev path deploys. The template says it mirrors the kustomize directory, but
`make chart-check` compares only the CRD, RBAC and webhook copies, so nothing
kept the two resource blocks in step. These tests do, and they pin the shape
the numbers have to keep: a request small enough to schedule beside the agent
pod, below a limit the reranker can burst into during recall.
"""

import pathlib
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_VALUES = _ROOT / "charts" / "kube-agents" / "values.yaml"
_API_YAML = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "api.yaml"

# Kubernetes CPU quantities: a bare number is cores, an `m` suffix is
# thousandths of a core. Nothing here writes any other suffix.
_MILLI_SUFFIX = "m"
_MILLIS_PER_CORE = 1000


def _cpu_millis(quantity):
    """A CPU quantity as an integer number of millicores."""
    text = str(quantity)
    if text.endswith(_MILLI_SUFFIX):
        return int(text[: -len(_MILLI_SUFFIX)])
    return int(float(text) * _MILLIS_PER_CORE)


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
        # agent pod on a small node; measured steady state is about 9m.
        request = _cpu_millis(self.chart["requests"]["cpu"])
        self.assertLessEqual(
            request,
            _MILLIS_PER_CORE,
            f"hindsight-api requests {request}m CPU; the request reserves capacity "
            "the scheduler and Autopilot billing count continuously, and the "
            "process idles near 9m",
        )


if __name__ == "__main__":
    unittest.main()
