"""The A2A gateway's pod admission policy ships where the chart says, and agrees with the pod it admits.

k8s-operator/config/admission/a2a-gateway-pod-policy.yaml holds the pods the A2A gateway creates
to the session pod a2a/gateway/spawn.go builds. The envtest in
k8s-operator/internal/controller/a2a_gateway_pod_policy_envtest_test.go runs it in a real API
server. These tests hold what that test cannot see:

- delivery: the chart renders it under one helper, the operator's A2A_GATEWAY_POD_POLICY is set
  from that same helper, the chart copy matches the source, and the manual install applies it;
- drift: the names the policy hard-codes (the ServiceAccount suffixes, the token audiences, the
  session labels) are the ones the gateway and the operator use, so a rename on either side fails
  here rather than refusing every session pod at admission.

`helm` is not a dependency of this suite; .github/workflows/validate.yml renders the chart.

Run:
  python3 -m unittest discover -s tests -p 'test_a2a_gateway_pod_policy_shipped.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "a2a-gateway-pod-policy.yaml"
CHART_TEMPLATE = REPO_ROOT / "charts" / "kube-agents" / "templates" / "a2a-gateway-pod-admission-policy.yaml"
CHART_HELPERS = REPO_ROOT / "charts" / "kube-agents" / "templates" / "_helpers.tpl"
OPERATOR_DEPLOYMENT = REPO_ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"
SYNC_SCRIPT = REPO_ROOT / "hack" / "sync-chart-manifests.sh"
INSTALL_GUIDE = REPO_ROOT / "INSTALL.md"
SPAWN = REPO_ROOT / "a2a" / "gateway" / "spawn.go"
CREDENTIALS = REPO_ROOT / "a2a" / "lib" / "credentials.go"
A2A_MANIFESTS = REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_a2a_manifests.go"

POLICY_NAME = "kube-agents-a2a-gateway-pod"
HELPER = "kube-agents.a2aGatewayPodPolicyEnabled"
HELPER_GATE = '{{- if include "kube-agents.a2aGatewayPodPolicyEnabled" . }}'
OPERATOR_ENV = "A2A_GATEWAY_POD_POLICY"


def policy_objects(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def chart_body() -> str:
    """The chart copy without its gate lines and generated-file header, as YAML."""
    lines = CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.startswith("{{"))


def go_const(path: Path, name: str) -> str:
    match = re.search(rf'\b{name}\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{name} not found in {path}")
    return match.group(1)


class ChartDeliveryTest(unittest.TestCase):
    def test_the_template_renders_under_the_helper(self):
        self.assertEqual(HELPER_GATE, CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()[0])

    def test_the_helper_is_the_values_gate_on_a_cluster_that_serves_the_api(self):
        helpers = CHART_HELPERS.read_text(encoding="utf-8")
        match = re.search(r'\{\{- define "' + re.escape(HELPER) + r'" -\}\}\n(.*?)\n\{\{- end \}\}', helpers, re.DOTALL)
        self.assertIsNotNone(match, f"{HELPER} is not defined in _helpers.tpl")
        body = match.group(1)
        self.assertIn(".Values.admissionPolicy.enabled", body)
        self.assertIn('semverCompare ">=1.30.0-0" .Capabilities.KubeVersion.Version', body)

    def test_the_operator_env_is_set_from_the_same_helper(self):
        lines = OPERATOR_DEPLOYMENT.read_text(encoding="utf-8").splitlines()
        env = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {OPERATOR_ENV}")
        opening = max(i for i in range(env) if lines[i].strip().startswith("{{- if "))
        self.assertEqual(HELPER_GATE, lines[opening].strip())
        self.assertFalse(
            any(line.strip() == "{{- end }}" for line in lines[opening + 1 : env]),
            f"{OPERATOR_ENV} is not inside the helper's if",
        )
        self.assertEqual('value: "true"', lines[env + 1].strip())

    def test_the_chart_copy_matches_the_source(self):
        self.assertEqual(policy_objects(POLICY_SRC.read_text(encoding="utf-8")), policy_objects(chart_body()))

    def test_the_sync_script_generates_it(self):
        script = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(str(POLICY_SRC.relative_to(REPO_ROOT)), script)
        self.assertIn(str(CHART_TEMPLATE.relative_to(REPO_ROOT)), script)

    def test_the_manual_install_applies_it(self):
        self.assertIn(
            "kubectl apply -f config/admission/a2a-gateway-pod-policy.yaml",
            INSTALL_GUIDE.read_text(encoding="utf-8"),
        )


class PolicyContentTest(unittest.TestCase):
    def setUp(self):
        objects = policy_objects(POLICY_SRC.read_text(encoding="utf-8"))
        self.policy = next(d for d in objects if d["kind"] == "ValidatingAdmissionPolicy")
        self.binding = next(d for d in objects if d["kind"] == "ValidatingAdmissionPolicyBinding")
        self.text = POLICY_SRC.read_text(encoding="utf-8")

    def test_it_fails_closed_and_denies(self):
        self.assertEqual("Fail", self.policy["spec"]["failurePolicy"])
        self.assertEqual(["Deny"], self.binding["spec"]["validationActions"])

    def test_the_binding_names_the_policy(self):
        self.assertEqual(POLICY_NAME, self.policy["metadata"]["name"])
        self.assertEqual(POLICY_NAME, self.binding["spec"]["policyName"])

    def test_it_matches_pod_create(self):
        (rule,) = self.policy["spec"]["matchConstraints"]["resourceRules"]
        self.assertEqual(["pods"], rule["resources"])
        self.assertEqual(["CREATE"], rule["operations"])


class DriftTest(unittest.TestCase):
    """The policy's literals are the gateway's and the operator's."""

    def setUp(self):
        self.text = POLICY_SRC.read_text(encoding="utf-8")

    def test_the_token_audiences(self):
        for audience in (
            go_const(CREDENTIALS, "BusTokenAudience"),
            go_const(SPAWN, "credentialProxySessionAudience"),
        ):
            with self.subTest(audience=audience):
                self.assertIn(f"'{audience}'", self.text)

    def test_the_session_labels(self):
        for key, value in (
            (go_const(SPAWN, "labelPartOf"), go_const(SPAWN, "partOfValue")),
            (go_const(SPAWN, "labelRole"), go_const(SPAWN, "sessionRole")),
        ):
            with self.subTest(label=key):
                self.assertIn(f"object.metadata.labels['{key}'] == '{value}'", self.text)

    def test_the_service_account_suffixes(self):
        manifests = A2A_MANIFESTS.read_text(encoding="utf-8")
        self.assertIn('return agent.Name + "-a2a-session"', manifests)
        self.assertRegex(manifests, r'func a2aGatewayName\(agent \*agentv1alpha1\.PlatformAgent\) string \{ return agent\.Name \+ "-a2a-gateway" \}')
        self.assertIn("endsWith('-a2a-gateway')", self.text)
        self.assertIn("+ '-a2a-session'", self.text)


if __name__ == "__main__":
    unittest.main()
