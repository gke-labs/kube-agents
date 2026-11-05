"""The web console's PlatformAgent admission policy ships where the chart says, and agrees with the console.

k8s-operator/config/admission/web-console-cr-policy.yaml holds the web console's ServiceAccount to
the fields a2a/cmd/web-console writes on the PlatformAgent CR. The envtest in
k8s-operator/internal/controller/web_console_cr_policy_envtest_test.go runs it in a real API
server. These tests hold what that test cannot see:

- delivery: the chart renders it under one helper, with the console's ServiceAccount and Role,
  and the chart copy matches the source;
- drift: the annotation keys the policy allows are the console's constants, the ServiceAccount
  suffix it selects is the chart's, and the console's Role still grants only what the policy
  covers. A new key in the console fails here rather than at admission.

`helm` is not a dependency of this suite; .github/workflows/validate.yml renders the chart.

Run:
  python3 -m unittest discover -s tests -p 'test_web_console_cr_policy_shipped.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "web-console-cr-policy.yaml"
CHART_TEMPLATE = REPO_ROOT / "charts" / "kube-agents" / "templates" / "web-console-cr-admission-policy.yaml"
CHART_HELPERS = REPO_ROOT / "charts" / "kube-agents" / "templates" / "_helpers.tpl"
WEB_CONSOLE_TEMPLATE = REPO_ROOT / "charts" / "kube-agents" / "templates" / "web-console.yaml"
SYNC_SCRIPT = REPO_ROOT / "hack" / "sync-chart-manifests.sh"
CONSOLE_DIR = REPO_ROOT / "a2a" / "cmd" / "web-console"
# The console files that write annotations on the CR (the issue's list).
CONSOLE_WRITERS = ("models.go", "patches.go", "repositories.go")

POLICY_NAME = "kube-agents-web-console-cr"
HELPER = "kube-agents.webConsoleCRPolicyEnabled"
HELPER_GATE = '{{- if include "kube-agents.webConsoleCRPolicyEnabled" . }}'
CONSOLE_KEY_PREFIX = "kubeagents.x-k8s.io/"
# The comment lines hack/sync-chart-manifests.sh writes between the gate and the source.
GENERATED_HEADER_LINES = 4


def policy_objects(text: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(text) if d]


def chart_body() -> str:
    """The chart copy without its gate lines and the generated-file header the sync script writes."""
    lines = CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[1 + GENERATED_HEADER_LINES : -1])


def helper_body(name: str) -> str:
    text = CHART_HELPERS.read_text(encoding="utf-8")
    match = re.search(r'\{\{- define "' + re.escape(name) + r'" -\}\}(.*?)\{\{- end \}\}', text, re.S)
    if match is None:
        raise AssertionError(f"{name} is not defined in {CHART_HELPERS}")
    return match.group(1)


def console_keys() -> set[str]:
    """Every annotation-key constant in the console files that write the CR."""
    keys = set()
    for name in CONSOLE_WRITERS:
        text = (CONSOLE_DIR / name).read_text(encoding="utf-8")
        keys |= set(re.findall(r'=\s*"(' + re.escape(CONSOLE_KEY_PREFIX) + r'[^"]+)"', text))
    return keys


class DeliveryTest(unittest.TestCase):
    def test_the_template_renders_under_the_helper(self):
        self.assertEqual(CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()[0], HELPER_GATE)

    def test_the_helper_is_the_values_gate_where_the_console_and_its_role_render(self):
        body = helper_body(HELPER)
        for condition in (
            ".Values.admissionPolicy.enabled",
            'semverCompare ">=1.30.0-0" .Capabilities.KubeVersion.Version',
            ".Values.webConsole.enabled",
            ".Values.webConsole.environment.enabled",
        ):
            self.assertIn(condition, body)

    def test_the_chart_copy_matches_the_source(self):
        self.assertEqual(chart_body().strip(), POLICY_SRC.read_text(encoding="utf-8").strip())

    def test_the_sync_script_generates_it(self):
        text = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(str(POLICY_SRC.relative_to(REPO_ROOT)), text)
        self.assertIn(str(CHART_TEMPLATE.relative_to(REPO_ROOT)), text)


class PolicyContentTest(unittest.TestCase):
    def setUp(self):
        objects = policy_objects(POLICY_SRC.read_text(encoding="utf-8"))
        self.policy = next(d for d in objects if d["kind"] == "ValidatingAdmissionPolicy")
        self.binding = next(d for d in objects if d["kind"] == "ValidatingAdmissionPolicyBinding")

    def test_it_fails_closed_and_denies(self):
        self.assertEqual(self.policy["spec"]["failurePolicy"], "Fail")
        self.assertEqual(self.binding["spec"]["validationActions"], ["Deny"])

    def test_the_binding_names_the_policy(self):
        self.assertEqual(self.policy["metadata"]["name"], POLICY_NAME)
        self.assertEqual(self.binding["spec"]["policyName"], POLICY_NAME)

    def test_it_matches_platformagent_updates(self):
        (rule,) = self.policy["spec"]["matchConstraints"]["resourceRules"]
        self.assertEqual(rule["apiGroups"], ["kubeagents.x-k8s.io"])
        self.assertEqual(rule["resources"], ["platformagents"])
        self.assertEqual(rule["operations"], ["UPDATE"])

    def test_the_allowed_annotations_are_the_console_s_keys(self):
        variable = next(v for v in self.policy["spec"]["variables"] if v["name"] == "consoleAnnotations")
        allowed = set(re.findall(r"'([^']+)'", variable["expression"]))
        self.assertEqual(allowed, console_keys())
        self.assertEqual(len(allowed), 6)

    def test_it_selects_the_chart_s_console_service_account(self):
        (condition,) = self.policy["spec"]["matchConditions"]
        self.assertIn("endsWith('-web-console')", condition["expression"])
        self.assertIn(
            "serviceAccountName: {{ .Release.Name }}-web-console",
            WEB_CONSOLE_TEMPLATE.read_text(encoding="utf-8"),
        )

    def test_the_console_role_grants_only_what_the_policy_covers(self):
        """An UPDATE is what `patch` makes; a new verb such as `delete` would pass the policy by."""
        text = WEB_CONSOLE_TEMPLATE.read_text(encoding="utf-8")
        match = re.search(r'resources: \["platformagents"\]\n\s+resourceNames: .*\n\s+verbs: (\[.*\])', text)
        self.assertIsNotNone(match, "the console's platformagents rule moved")
        self.assertEqual(yaml.safe_load(match.group(1)), ["get", "patch"])


if __name__ == "__main__":
    unittest.main()
