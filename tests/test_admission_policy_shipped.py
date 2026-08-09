"""The agent-RBAC admission policies reach a cluster on both install paths.

These policies spent their whole existence in examples/gitops-repo/policy/, where
nothing applied them. A policy that is not installed is not a control, so what
these tests assert is delivery, not just existence: the Helm chart renders them by
default, the provisioning script applies them, and the chart's generated copy has
not drifted from the source the script uses.

Deliberately NOT asserted, because it is not true: that the policies make agent
RBAC read-only. They cannot read a referenced role's rules cross-object, and the
content policy only selects manifests carrying the `kube-agents/tier` label. See
the header of k8s-operator/config/admission/agent-rbac-policy.yaml. What is
asserted here about their content is only that the three denials that are in them
stay in them.

`helm` is not a dependency of this suite (it is not installed on the runner that
executes it), so the chart's default-on behaviour is checked against the template
and values files rather than a real render. The CI job in .github/workflows/
validate.yml renders the chart with helm and greps for the policies; that is the
end-to-end half.

Run:
  python3 -m unittest discover -s tests -p 'test_admission_policy_shipped.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "agent-rbac-policy.yaml"
CHART_TEMPLATE = (
    REPO_ROOT / "charts" / "kube-agents" / "templates" / "agent-rbac-admission-policy.yaml"
)
CHART_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"
PROVISION_OPERATOR = (
    REPO_ROOT / "k8s-operator" / "scripts" / "provision_03_gcp_gke_operator.sh"
)

VALUES_GATE = "admissionPolicy"

EXPECTED_OBJECTS = {
    ("ValidatingAdmissionPolicy", "kube-agents-agent-readonly"),
    ("ValidatingAdmissionPolicyBinding", "kube-agents-agent-readonly"),
    ("ValidatingAdmissionPolicy", "kube-agents-agent-binding-scope"),
    ("ValidatingAdmissionPolicyBinding", "kube-agents-agent-binding-scope"),
}


def load_source_objects() -> list[dict]:
    return [d for d in yaml.safe_load_all(POLICY_SRC.read_text(encoding="utf-8")) if d]


def load_chart_objects() -> list[dict]:
    """The chart template with its one Go-template construct removed.

    The template is generated as `{{- if <gate> }}` + the source + `{{- end }}`,
    with no other templating, so dropping those two lines leaves loadable YAML.
    Asserting that both lines are present is part of the point: if the gate ever
    grows a condition this stops matching and the test fails rather than quietly
    parsing something else.
    """
    lines = CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()
    if lines[0] != "{{- if .Values.admissionPolicy.enabled }}" or lines[-1] != "{{- end }}":
        raise AssertionError(
            "the chart template is no longer 'gate + generated source + end'; "
            "this test's stripping is invalid, so re-read it before trusting it"
        )
    body = "\n".join(lines[1:-1])
    if "{{" in body:
        raise AssertionError(f"unexpected Go templating inside the generated body: {body[:200]}")
    return [d for d in yaml.safe_load_all(body) if d]


class ChartShipsThePoliciesTest(unittest.TestCase):
    def test_the_chart_has_a_template_for_them(self):
        self.assertTrue(
            CHART_TEMPLATE.is_file(),
            f"{CHART_TEMPLATE} is missing — a normal `helm install` no longer "
            "gets the agent-RBAC admission policies",
        )

    def test_the_chart_renders_all_four_objects(self):
        rendered = {(d["kind"], d["metadata"]["name"]) for d in load_chart_objects()}
        self.assertEqual(EXPECTED_OBJECTS, rendered)

    def test_each_binding_points_at_a_policy_that_exists(self):
        """The failure mode that made the kustomize install path unusable.

        A name transform that rewrites metadata.name without rewriting
        spec.policyName leaves a binding referencing nothing. Nothing rejects
        that at apply time; the policies simply stop being enforced.
        """
        objects = load_chart_objects()
        policies = {
            d["metadata"]["name"] for d in objects if d["kind"] == "ValidatingAdmissionPolicy"
        }
        bindings = [d for d in objects if d["kind"] == "ValidatingAdmissionPolicyBinding"]
        self.assertTrue(bindings, "no bindings — the policies would be inert")
        for binding in bindings:
            with self.subTest(binding=binding["metadata"]["name"]):
                self.assertIn(binding["spec"]["policyName"], policies)

    def test_the_gate_defaults_to_on(self):
        values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
        self.assertIn(
            VALUES_GATE,
            values,
            f"values.yaml has no `{VALUES_GATE}` key, so the chart template's gate "
            "renders nothing and a default install ships no policies",
        )
        self.assertIs(
            True,
            values[VALUES_GATE].get("enabled"),
            "the admission policies must be installed by default; an opt-in "
            "security backstop is one nobody opts into",
        )


class ScriptInstallShipsThePoliciesTest(unittest.TestCase):
    def test_the_operator_provisioning_step_applies_the_source_file(self):
        script = PROVISION_OPERATOR.read_text(encoding="utf-8")
        self.assertIn(
            "config/admission/agent-rbac-policy.yaml",
            script,
            "the script install path (INSTALL.md Method 1, the recommended one) "
            "no longer applies the admission policies",
        )

    def test_the_step_is_in_the_execution_pipeline(self):
        """Defining the functions is not the same as running them."""
        script = PROVISION_OPERATOR.read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r"run_(deploy_)?step[^\n]*verify_admission_policy execute_admission_policy",
            "verify_admission_policy/execute_admission_policy exist but nothing "
            "in the pipeline calls them",
        )


class ChartCopyHasNotDriftedTest(unittest.TestCase):
    """`make chart-check` enforces this byte-for-byte; this checks the meaning.

    Kept separate from the sync script because the two failures read differently:
    chart-check says "run make chart-sync", this says "the two install paths now
    install different policies".
    """

    def test_chart_and_script_install_the_same_objects(self):
        self.assertEqual(load_source_objects(), load_chart_objects())


class PolicyContentTest(unittest.TestCase):
    """Only the denials that are actually in the policies — see the module docstring."""

    def setUp(self):
        # Policies only: a binding shares its policy's name, so an unfiltered
        # name->object map would silently hand back whichever came last.
        self.by_name = {
            d["metadata"]["name"]: d
            for d in load_source_objects()
            if d["kind"] == "ValidatingAdmissionPolicy"
        }

    def test_both_policies_fail_closed(self):
        for name in ["kube-agents-agent-readonly", "kube-agents-agent-binding-scope"]:
            with self.subTest(policy=name):
                self.assertEqual("Fail", self.by_name[name]["spec"]["failurePolicy"])

    def test_both_bindings_deny_rather_than_warn(self):
        for doc in load_source_objects():
            if doc["kind"] != "ValidatingAdmissionPolicyBinding":
                continue
            with self.subTest(binding=doc["metadata"]["name"]):
                self.assertEqual(
                    ["Deny"],
                    doc["spec"]["validationActions"],
                    "Warn/Audit lets the write through; the binding must Deny",
                )

    def test_the_read_verb_allowlist_is_an_allowlist(self):
        """A denylist of known-bad verbs would admit every verb added later."""
        expressions = " ".join(
            v["expression"]
            for v in self.by_name["kube-agents-agent-readonly"]["spec"]["validations"]
        )
        self.assertIn("v in ['get','list','watch']", expressions.replace("\n", " "))

    def test_secrets_are_denied(self):
        expressions = " ".join(
            v["expression"]
            for v in self.by_name["kube-agents-agent-readonly"]["spec"]["validations"]
        )
        self.assertIn("secrets", expressions)

    def test_the_binding_scope_policy_selects_on_the_subject_not_a_label(self):
        """The one selector an author cannot drop from the manifest."""
        conditions = self.by_name["kube-agents-agent-binding-scope"]["spec"]["matchConditions"]
        joined = " ".join(c["expression"] for c in conditions)
        self.assertIn("object.subjects", joined)
        self.assertNotIn("metadata.labels", joined)


if __name__ == "__main__":
    unittest.main()
