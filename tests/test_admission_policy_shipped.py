"""The agent-RBAC admission policies reach a cluster on the install paths that can carry them.

These policies spent their whole existence in examples/gitops-repo/policy/, where
nothing applied them. A policy that is not installed is not a control, so what
these tests assert is delivery, not just existence: the Helm chart renders them by
default, the composition that installs that chart does not switch them off, and the
chart's generated copy has not drifted from the source.

The install has one engine — Terraform + Helm — so the chart is the delivery path,
and there is deliberately no second one. The kustomize path (INSTALL.md Method 2)
is the exception, because `make deploy` cannot render them: they sit outside the
overlay on purpose, so that path has to apply the source file by hand and INSTALL.md
has to say so.

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

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "agent-rbac-policy.yaml"
CHART_TEMPLATE = (
    REPO_ROOT / "charts" / "kube-agents" / "templates" / "agent-rbac-admission-policy.yaml"
)
CHART_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"
COMPOSITION = REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
INSTALL_GUIDE = REPO_ROOT / "INSTALL.md"
SYNC_SCRIPT = REPO_ROOT / "hack" / "sync-chart-manifests.sh"

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
        """The failure mode that keeps these out of the kustomize overlay.

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


class InstallPathsDeliverThemTest(unittest.TestCase):
    def test_the_composition_does_not_switch_the_gate_off(self):
        """Terraform + Helm is the install engine; it installs this chart.

        The chart's default is on, so the composition delivers the policies by
        saying nothing about them. What would break that is an override, and an
        override is what this looks for — not the absence of a mention.
        """
        composition = COMPOSITION.read_text(encoding="utf-8")
        override = re.search(
            r"admissionPolicy\s*=\s*\{[^}]*enabled\s*=\s*([^\s}]+)", composition, re.S
        )
        if override is not None:
            self.assertEqual(
                "true",
                override.group(1).strip(),
                "the full-install composition overrides admissionPolicy.enabled to "
                "something other than true, so the engine's own install ships no "
                "admission backstop",
            )

    def test_the_manual_install_method_tells_the_reader_to_apply_them(self):
        """Method 2 is `make install && make deploy`, which does not include them.

        The policies are deliberately outside the kustomize overlay, so this path
        gets no backstop unless INSTALL.md says to apply the file. If that line
        goes, a reader following Method 2 ends up unbackstopped and told nothing,
        while the rest of the docs describe a backstop that ships.
        """
        install = INSTALL_GUIDE.read_text(encoding="utf-8")
        self.assertRegex(
            install,
            r"kubectl apply -f config/admission/agent-rbac-policy\.yaml",
            "INSTALL.md no longer tells the manual (Method 2) install to apply "
            "the admission policies, and nothing else on that path does",
        )


class ChartCopyHasNotDriftedTest(unittest.TestCase):
    """`make chart-check` enforces this byte-for-byte; this checks the meaning.

    Kept separate from the sync script because the two failures read differently:
    chart-check says "run make chart-sync", this says "the chart no longer
    installs the policies this repository calls the source of truth".
    """

    def test_the_chart_and_the_source_hold_the_same_objects(self):
        self.assertEqual(load_source_objects(), load_chart_objects())

    def test_the_sync_script_knows_about_the_template(self):
        """Otherwise `make chart-sync` silently stops regenerating it."""
        script = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("config/admission/agent-rbac-policy.yaml", script)
        self.assertIn("templates/agent-rbac-admission-policy.yaml", script)


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
