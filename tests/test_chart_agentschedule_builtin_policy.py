"""The chart's AgentSchedule builtin policy exempts the operator the chart runs.

k8s-operator/config/admission/agentschedule-builtin-policy.yaml refuses anyone
but the operator who makes or unmakes a built-in AgentSchedule. The envtest
runs the source file, whose operator identities are the kustomize path's; the
chart's copy has hack/sync-chart-manifests.sh substitute the release's. If the
substitution and the operator Deployment's ServiceAccount drifted apart, the
chart would refuse its own operator, so this renders both and holds them equal.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_agentschedule_builtin_policy.py' -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _rendered_manifests import HELM, rendered_chart  # noqa: E402

CI_ENV = "CI"
NO_HELM_ON_CI = "helm is not on PATH but CI is set: the policy would pass unread on a runner. Install helm rather than skipping."
POLICY = "kube-agents-agentschedule-builtin"


class TheBuiltinPolicyExemptsTheChartsOperator(unittest.TestCase):
    def setUp(self) -> None:
        if not HELM:
            if os.environ.get(CI_ENV):
                self.fail(NO_HELM_ON_CI)
            self.skipTest("helm is not on PATH")
        self.docs = rendered_chart()

    def test_the_exempt_username_is_the_operator_deployments_service_account(self) -> None:
        policy = next(
            (d for d in self.docs if d.get("kind") == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == POLICY),
            None,
        )
        self.assertIsNotNone(policy, f"the chart renders no {POLICY} policy")
        # templates/operator-deployment.yaml: <release>-controller-manager.
        operator = next(
            (d for d in self.docs if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("-controller-manager")),
            None,
        )
        self.assertIsNotNone(operator, "the chart renders no operator Deployment")
        sa = operator["spec"]["template"]["spec"]["serviceAccountName"]
        namespace = operator["metadata"].get("namespace", "default")
        username = f"system:serviceaccount:{namespace}:{sa}"
        expression = policy["spec"]["matchConditions"][0]["expression"]
        self.assertIn(f"'{username}'", expression, f"the policy doesn't exempt the chart's operator {username}")


if __name__ == "__main__":
    unittest.main()
