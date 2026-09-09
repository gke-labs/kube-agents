"""Tests Deployment rollout strategies and quota trade-offs (#975).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

Deployments with `replicas: 1` and no explicit `strategy` block default to
`RollingUpdate` with `maxSurge: 25%` (ceil = 1) and `maxUnavailable: 25%` (floor = 0).
Where a namespace `ResourceQuota` has no room for one more Pod, the surge Pod is
refused with `FailedCreate` and the rollout stalls indefinitely because the old
Pod cannot be scaled down (`maxUnavailable: 0`).

Workloads without webhook serving or heavy cold starts (`inference-replay`, `github-minter`)
define explicit rollout strategies resolving `maxUnavailable >= 1` to replace in place under quota (#975).

Single-replica workloads with admission webhooks or multi-minute model loading cold starts
(`operator`, `hindsight-api`, `vllm-gemma`) deliberately default to surge-first (`maxUnavailable: 0`
in Kustomize/examples, configurable in Helm defaulting to 0): at `replicas: 1`, `maxUnavailable: 1`
sets `minAvailable = 0` and terminates the old Pod before the replacement is Ready, causing
admission outages or minutes of memory recall / inference downtime during upgrades.

Consequently, `operator`, `hindsight-api`, and `vllm-gemma` will still stall under a zero-headroom
ResourceQuota by default. For the Helm chart workloads, `operator.rollingUpdate.maxUnavailable` and
`hindsight.api.rollingUpdate.maxUnavailable` provide the override knob to unblock rollouts under quota
when the transient outage is acceptable.
"""

import math
import pathlib
import re
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_VALUES = _ROOT / "charts" / "kube-agents" / "values.yaml"
_OPERATOR_CHART_TEMPLATE = (
    _ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"
)
_OPERATOR_KUSTOMIZE = _ROOT / "k8s-operator" / "config" / "manager" / "manager.yaml"

_HINDSIGHT_CHART_TEMPLATE = (
    _ROOT / "charts" / "kube-agents" / "templates" / "hindsight.yaml"
)
_HELPERS = _ROOT / "charts" / "kube-agents" / "templates" / "_helpers.tpl"
_HINDSIGHT_KUSTOMIZE = (
    _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "api.yaml"
)

_REPLAY_KUSTOMIZE = (
    _ROOT
    / "k8s-operator"
    / "config"
    / "integrations"
    / "inference-replay"
    / "base"
    / "deployment.yaml"
)
_REPLAY_EXAMPLE = _ROOT / "examples" / "inference-replay" / "deployment.yaml"

_VLLM_GEMMA_EXAMPLE = _ROOT / "examples" / "vllm-gemma" / "deployment.yaml"
_GITHUB_MINTER_TEMPLATE = (
    _ROOT / "charts" / "kube-agents" / "templates" / "github-minter.yaml"
)

_SCAN_ROOTS = [
    _ROOT / "k8s-operator" / "config",
    _ROOT / "examples",
]


def _extract_deployments(path):
    """Yield all Deployment documents found in a plain YAML file."""
    for doc in yaml.safe_load_all(path.read_text()):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict) or "selector" not in spec:
            continue
        yield doc


def _resolve_max_unavailable(doc):
    """Resolve a Deployment's maxUnavailable the way Kubernetes does.

    None means Recreate: no surge Pod at all, so it cannot stall on quota.
    """
    spec = doc["spec"]
    replicas = spec.get("replicas", 1)
    strategy = spec.get("strategy") or {}
    if strategy.get("type") == "Recreate":
        return None
    rolling = strategy.get("rollingUpdate") or {}

    def scaled(value, default, round_up):
        if value is None:
            value = default
        if isinstance(value, str) and value.endswith("%"):
            exact = int(value[:-1]) * replicas / 100
            return math.ceil(exact) if round_up else math.floor(exact)
        return int(value)

    surge = scaled(rolling.get("maxSurge"), "25%", True)
    unavailable = scaled(rolling.get("maxUnavailable"), "25%", False)
    if surge == 0 and unavailable == 0:
        return 1
    return unavailable


def _has_chart_rolling_update_strategy(template_text):
    """Verify that a Helm chart template contains a rollingUpdate strategy block with maxUnavailable >= 1."""
    has_strategy = "strategy:" in template_text
    has_rolling = (
        "type: RollingUpdate" in template_text or "rollingUpdate:" in template_text
    )
    has_max_unavail = (
        re.search(
            r"maxUnavailable:\s*([1-9]\d*|\{\{.+?\}\})", template_text
        )
        is not None
    )
    return has_strategy and has_rolling and has_max_unavail


def _unconditional_reassignments(template, variable):
    """`{{ $var = ... }}` lines that are not guarded by an `if` on the same line.

    The template reassigns a fencepost on purpose, to substitute the chart
    default when the value is unusable, and every such reassignment sits inside
    a single-line `{{- if ... }}...{{- end }}`. One that does not is a value
    pinned for every install regardless of values.yaml — which is the #749
    defect wearing the shape of a fix, and it is invisible to a check that only
    reads the declaration.
    """
    return [
        line.strip()
        for line in template.splitlines()
        if re.search(rf"{re.escape(variable)}\s*=[^=]", line)
        and ":=" not in line
        and not re.search(r"\{\{-?\s*if\b", line)
    ]


class DeploymentsRolloutSurvivesAFullQuota(unittest.TestCase):
    def test_operator_kustomize_preserves_surge_first_webhook_strategy(self):
        docs = list(_extract_deployments(_OPERATOR_KUSTOMIZE))
        self.assertEqual(len(docs), 1, f"expected 1 Deployment in {_OPERATOR_KUSTOMIZE}")
        max_unavail = _resolve_max_unavailable(docs[0])
        self.assertEqual(
            max_unavail,
            0,
            f"{_OPERATOR_KUSTOMIZE.relative_to(_ROOT)} must resolve maxUnavailable to 0 "
            "to prevent admission webhook outages during upgrades under failurePolicy: Fail",
        )

    def test_operator_values_yaml_defaults_surge_first(self):
        values = yaml.safe_load(_VALUES.read_text())
        ru = values.get("operator", {}).get("rollingUpdate", {})
        self.assertEqual(
            ru.get("maxUnavailable"),
            0,
            "charts/kube-agents/values.yaml: operator.rollingUpdate.maxUnavailable must "
            "default to 0 to preserve admission webhook availability during upgrades",
        )
        self.assertEqual(
            ru.get("maxSurge"),
            1,
            "charts/kube-agents/values.yaml: operator.rollingUpdate.maxSurge must default to 1",
        )

    def test_operator_chart_template_renders_configurable_strategy(self):
        text = _OPERATOR_CHART_TEMPLATE.read_text()
        self.assertIn(".Values.operator.rollingUpdate", text)
        self.assertIn('include "kube-agents.rollingUpdateFenceposts"', text)
        self.assertIn("maxSurge: {{ $rollingUpdate.maxSurge }}", text)
        self.assertIn("maxUnavailable: {{ $rollingUpdate.maxUnavailable }}", text)

    def test_hindsight_chart_template_renders_configurable_strategy(self):
        text = _HINDSIGHT_CHART_TEMPLATE.read_text()
        self.assertIn(".Values.hindsight.api.rollingUpdate", text)
        self.assertIn('include "kube-agents.rollingUpdateFenceposts"', text)
        self.assertIn("maxSurge: {{ $rollingUpdate.maxSurge }}", text)
        self.assertIn("maxUnavailable: {{ $rollingUpdate.maxUnavailable }}", text)

    def test_helpers_define_shared_rolling_update_guard(self):
        text = _HELPERS.read_text()
        self.assertIn('define "kube-agents.rollingUpdateFenceposts"', text)
        self.assertIn(
            "maxSurge (%v) and maxUnavailable (%v) may not both be zero", text
        )
        for variable in ("$surge", "$unavail"):
            self.assertEqual(
                [],
                _unconditional_reassignments(text, variable),
                f"charts/kube-agents/templates/_helpers.tpl reassigns {variable} outside a "
                "conditional, which pins the fencepost for every install regardless of "
                "values.yaml — the substitution of a default has to stay guarded",
            )

    def test_hindsight_values_yaml_defaults_surge_first(self):
        values = yaml.safe_load(_VALUES.read_text())
        ru = values.get("hindsight", {}).get("api", {}).get("rollingUpdate", {})
        self.assertEqual(
            ru.get("maxUnavailable"),
            0,
            "charts/kube-agents/values.yaml: hindsight.api.rollingUpdate.maxUnavailable must "
            "default to 0 to prevent memory store downtime during cold start rollouts",
        )
        self.assertEqual(
            ru.get("maxSurge"),
            1,
            "charts/kube-agents/values.yaml: hindsight.api.rollingUpdate.maxSurge must default to 1",
        )

    def test_hindsight_kustomize_preserves_surge_first_strategy(self):
        docs = list(_extract_deployments(_HINDSIGHT_KUSTOMIZE))
        self.assertEqual(len(docs), 1, f"expected 1 Deployment in {_HINDSIGHT_KUSTOMIZE}")
        max_unavail = _resolve_max_unavailable(docs[0])
        self.assertEqual(
            max_unavail,
            0,
            f"{_HINDSIGHT_KUSTOMIZE.relative_to(_ROOT)} must resolve maxUnavailable to 0 "
            "to prevent taking the long-term memory store offline during model loading cold starts",
        )

    def test_replay_kustomize_sets_rollout_strategy(self):
        docs = list(_extract_deployments(_REPLAY_KUSTOMIZE))
        self.assertEqual(len(docs), 1, f"expected 1 Deployment in {_REPLAY_KUSTOMIZE}")
        max_unavail = _resolve_max_unavailable(docs[0])
        self.assertIsNotNone(max_unavail)
        self.assertGreaterEqual(
            max_unavail,
            1,
            f"{_REPLAY_KUSTOMIZE.relative_to(_ROOT)} must resolve maxUnavailable >= 1",
        )

    def test_replay_example_sets_rollout_strategy(self):
        docs = list(_extract_deployments(_REPLAY_EXAMPLE))
        self.assertEqual(len(docs), 1, f"expected 1 Deployment in {_REPLAY_EXAMPLE}")
        max_unavail = _resolve_max_unavailable(docs[0])
        self.assertIsNotNone(max_unavail)
        self.assertGreaterEqual(
            max_unavail,
            1,
            f"{_REPLAY_EXAMPLE.relative_to(_ROOT)} must resolve maxUnavailable >= 1",
        )

    def test_vllm_gemma_example_preserves_surge_first_strategy(self):
        docs = list(_extract_deployments(_VLLM_GEMMA_EXAMPLE))
        self.assertEqual(
            len(docs), 1, f"expected 1 Deployment in {_VLLM_GEMMA_EXAMPLE}"
        )
        max_unavail = _resolve_max_unavailable(docs[0])
        self.assertEqual(
            max_unavail,
            0,
            f"{_VLLM_GEMMA_EXAMPLE.relative_to(_ROOT)} must resolve maxUnavailable to 0 "
            "to prevent multi-minute inference outages while pulling and loading Gemma models",
        )

    def test_github_minter_chart_template_sets_rollout_strategy(self):
        text = _GITHUB_MINTER_TEMPLATE.read_text()
        self.assertTrue(
            _has_chart_rolling_update_strategy(text),
            f"{_GITHUB_MINTER_TEMPLATE.relative_to(_ROOT)} must define a RollingUpdate "
            "strategy with maxUnavailable >= 1 to allow replacing pods under full quota",
        )

    def test_no_generic_workload_deployment_manifest_resolves_zero_max_unavailable(self):
        offenders = []
        for root in _SCAN_ROOTS:
            for path in sorted(root.rglob("*.yaml")):
                for doc in _extract_deployments(path):
                    # Exclude single-replica workloads that deliberately surge-first:
                    # - operator controller-manager (admission webhook backend)
                    # - hindsight-api (1.4 GB image + 5m model loading cold start)
                    # - vllm-gemma (multi-minute Gemma weight loading)
                    if path in (_OPERATOR_KUSTOMIZE, _HINDSIGHT_KUSTOMIZE, _VLLM_GEMMA_EXAMPLE):
                        continue
                    resolved = _resolve_max_unavailable(doc)
                    if resolved is not None and resolved < 1:
                        name = (doc.get("metadata") or {}).get("name")
                        offenders.append(
                            f"{path.relative_to(_ROOT)} (deployment: {name}, resolves to {resolved})"
                        )
        self.assertEqual(
            [],
            offenders,
            "generic workload Deployments must resolve maxUnavailable to at least 1, "
            "so they can roll under a full namespace quota (#975).",
        )


if __name__ == "__main__":
    unittest.main()
