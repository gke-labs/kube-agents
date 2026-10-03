"""Tests that every Deployment the install ships can roll under a full quota (#1506).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

#977 and #1267 fixed the same class twice: a single-replica Deployment whose
rollout needs a surge Pod stalls forever when a namespace `ResourceQuota` has
no headroom, because `maxUnavailable: 0` forbids scaling the old Pod down
first. The suites they left (`test_litellm_rollout_quota.py`,
`test_deployments_rollout_quota.py`) name workloads by file and read chart
templates by regex over template text. Nothing there reads what the chart
renders or what the operator emits, so a new workload in either source arrives
untested. This suite reads the output side instead:

    helm template ...                          every Deployment the chart renders
    k8s-operator/.../testdata/platform/expected  every Deployment the golden cases
                                               emit (golden_test.go keeps these
                                               equal to the operator's output)

The golden half reaches only what the golden cases render. All seven set
`spec.mode: default`, so the `mode: next` stack is in none of them: the A2A
gateway's strategy is asserted by `TestBuildA2AGatewayIdentityAndOwnerWiring`
in `k8s-operator/internal/controller/`, and the two surge-first pairs -- the
A2A auth callout and the capability verifier, two replicas each -- are asserted
by nothing here. A `mode: next` golden case would bring all three into this
sweep.

Every Deployment in both must be `Recreate` or resolve `maxUnavailable >= 1`,
unless its name is in `_SURGE_FIRST_BY_DESIGN`, where the entry carries the
reason. Each exception is also asserted to still render surge-first, so a
workload that later grows a quota-safe strategy has to leave the list.

StatefulSets are excluded on purpose: a StatefulSet rolling update deletes a
Pod before creating its replacement, so a Pod-count quota cannot deadlock it.

The `CI` rule: without `helm` on PATH the render half skips on a laptop and
fails on a runner. A skip there would let the chart half of this suite vanish
from every pull request while the sweep reports green around it.
"""

import functools
import os
import pathlib
import shutil
import subprocess
import unittest

import yaml

from test_deployments_rollout_quota import _resolve_max_unavailable

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_GOLDEN_DIR = (
    _ROOT
    / "k8s-operator"
    / "internal"
    / "testing"
    / "testdata"
    / "platform"
    / "expected"
)

_RELEASE_NAME = "test-release"
_HELM_SET_VALUES = (
    "platformAgent.harness.clusterName=ci-cluster",
    "platformAgent.harness.location=us-central1",
    "platformAgent.harness.projectId=ci-project",
    "hindsight.enabled=true",
    "githubMinter.enabled=true",
    "githubMinter.org=example-org",
    "githubMinter.repo=example-repo",
)

# What the values above render today: github-token-minter, hindsight-api,
# litellm and the operator. An empty or partial render must not pass as "no
# offenders", so the count is asserted, and a new chart Deployment fails here
# until its author looks at its strategy and bumps the number.
_EXPECTED_RENDERED_DEPLOYMENTS = 4
# Seven golden cases, each emitting an agent gateway and a credential proxy.
_EXPECTED_GOLDEN_DEPLOYMENTS = 14

# Single-replica workloads that deliberately keep `maxUnavailable: 0` and so
# stall under a zero-headroom quota. The reasons are the ones
# `test_deployments_rollout_quota.py` documents; the chart exposes
# `operator.rollingUpdate.maxUnavailable` and
# `hindsight.api.rollingUpdate.maxUnavailable` as the override for installs
# that would rather take the outage. Keys are rendered names, so the operator's
# is bound to `_RELEASE_NAME`.
_SURGE_FIRST_BY_DESIGN = {
    "hindsight-api": (
        "1.4 GB image and a multi-minute model-loading cold start: at replicas 1, "
        "maxUnavailable 1 takes the long-term memory store offline for the whole load"
    ),
    f"{_RELEASE_NAME}-controller-manager": (
        "admission webhook backend with failurePolicy Fail: at replicas 1, "
        "maxUnavailable 1 terminates the only webhook server before its replacement "
        "is Ready, and every guarded write in the namespace fails until it is"
    ),
}


def _deployments(text):
    """Every Deployment document in a multi-document YAML string, by name."""
    found = {}
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict) or "selector" not in spec:
            continue
        found[(doc.get("metadata") or {}).get("name")] = doc
    return found


@functools.lru_cache(maxsize=1)
def _render_chart():
    """The chart's rendered manifests, or None when helm is absent off CI.

    Cached: three tests read the same render, and helm takes about half a
    second per call. A raise is not cached, so the CI guard fires on every
    call that reaches it.
    """
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            raise AssertionError(
                "helm is not on PATH but CI is set: the chart half of this suite "
                "would silently skip on a runner. Install helm rather than skipping."
            )
        return None
    args = ["helm", "template", _RELEASE_NAME, str(_CHART)]
    for value in _HELM_SET_VALUES:
        args += ["--set", value]
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _golden_deployments():
    """Every Deployment across the operator's golden files, keyed by file and name."""
    found = {}
    for path in sorted(_GOLDEN_DIR.glob("*.yaml")):
        for name, doc in _deployments(path.read_text()).items():
            found[f"{path.name}:{name}"] = doc
    return found


class RenderedDeploymentsRollUnderAFullQuota(unittest.TestCase):
    def _assert_quota_safe(self, deployments):
        offenders = []
        for key, doc in sorted(deployments.items()):
            name = (doc.get("metadata") or {}).get("name")
            if name in _SURGE_FIRST_BY_DESIGN:
                continue
            resolved = _resolve_max_unavailable(doc)
            if resolved is not None and resolved < 1:
                offenders.append(f"{key} (resolves maxUnavailable to {resolved})")
        self.assertEqual(
            [],
            offenders,
            "every Deployment must be Recreate or resolve maxUnavailable >= 1 so it can "
            "roll under a full namespace ResourceQuota, or sit in _SURGE_FIRST_BY_DESIGN "
            "with the reason (#1506)",
        )

    def test_chart_render_yields_the_expected_deployments(self):
        rendered = _render_chart()
        if rendered is None:
            self.skipTest("helm not installed and CI is unset")
        deployments = _deployments(rendered)
        self.assertEqual(
            len(deployments),
            _EXPECTED_RENDERED_DEPLOYMENTS,
            f"helm rendered {sorted(deployments)}; a new chart Deployment needs its "
            "strategy checked and this count updated",
        )

    def test_every_rendered_chart_deployment_is_quota_safe_or_listed(self):
        rendered = _render_chart()
        if rendered is None:
            self.skipTest("helm not installed and CI is unset")
        self._assert_quota_safe(_deployments(rendered))

    def test_golden_files_hold_the_expected_deployments(self):
        deployments = _golden_deployments()
        self.assertEqual(
            len(deployments),
            _EXPECTED_GOLDEN_DEPLOYMENTS,
            f"the goldens hold {sorted(deployments)}; a new golden case or a new "
            "operator-owned Deployment needs its strategy checked and this count updated",
        )

    def test_every_golden_operator_deployment_is_quota_safe_or_listed(self):
        self._assert_quota_safe(_golden_deployments())

    def test_every_exception_is_still_rendered_surge_first(self):
        """An entry whose workload no longer surges first is stale; drop it.

        Only the chart render carries the two listed workloads today, so a
        missing helm skips this the same way; with CI set the render raises.
        """
        rendered = _render_chart()
        if rendered is None:
            self.skipTest("helm not installed and CI is unset")
        deployments = _deployments(rendered)
        for name, reason in sorted(_SURGE_FIRST_BY_DESIGN.items()):
            with self.subTest(deployment=name):
                self.assertIn(
                    name,
                    deployments,
                    f"_SURGE_FIRST_BY_DESIGN names {name!r} but the chart renders no "
                    "such Deployment: the workload was renamed or removed, so the entry "
                    "shields nothing",
                )
                self.assertEqual(
                    _resolve_max_unavailable(deployments[name]),
                    0,
                    f"{name} is listed as surge-first by design ({reason}) but no longer "
                    "resolves maxUnavailable to 0; remove it from _SURGE_FIRST_BY_DESIGN",
                )


if __name__ == "__main__":
    unittest.main()
