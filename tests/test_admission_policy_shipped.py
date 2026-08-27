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

# The template's first line, which gates it twice over. Matched exactly rather
# than loosely: load_chart_objects() strips this line to parse the rest, so a
# change to it has to be seen here before the stripping can be trusted.
TEMPLATE_GATE = (
    "{{- if and .Values.admissionPolicy.enabled "
    '(semverCompare ">=1.30.0-0" .Capabilities.KubeVersion.Version) }}'
)

EXPECTED_OBJECTS = {
    ("ValidatingAdmissionPolicy", "kube-agents-agent-readonly"),
    ("ValidatingAdmissionPolicyBinding", "kube-agents-agent-readonly"),
    ("ValidatingAdmissionPolicy", "kube-agents-agent-binding-scope"),
    ("ValidatingAdmissionPolicyBinding", "kube-agents-agent-binding-scope"),
}


# The one difference the chart's copy is allowed to have from the source, and
# why: the two install paths run the controller under different
# ServiceAccounts, and the policy's operator exemption has to name the one that
# will actually reconcile. hack/sync-chart-manifests.sh applies exactly this
# rewrite. Spelled out here rather than pattern-matched so that a second
# substitution appearing in the generator fails the drift comparison instead of
# being absorbed by a looser rule.
CONTROLLER_USER_SRC = "'system:serviceaccount:kubeagents-system:kube-agents-operator-sa'"
CONTROLLER_USER_CHART = (
    "'system:serviceaccount:{{ .Release.Namespace }}:{{ .Release.Name }}-operator-sa'"
)


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
    if lines[0] != TEMPLATE_GATE or lines[-1] != "{{- end }}":
        raise AssertionError(
            "the chart template is no longer 'gate + generated source + end'; "
            "this test's stripping is invalid, so re-read it before trusting it"
        )
    body = "\n".join(lines[1:-1])
    # Undo the generator's one substitution before parsing, so what is compared
    # against the source is like for like and any *other* templating still trips
    # the check below.
    body = body.replace(CONTROLLER_USER_CHART, CONTROLLER_USER_SRC)
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

    def test_the_template_is_gated_on_the_version_that_serves_the_api(self):
        """Chart.yaml accepts 1.29; ValidatingAdmissionPolicy is v1 only from 1.30.

        Without the version half of the gate, a default `helm install` on a
        cluster the chart says it supports fails on `no matches for kind` --
        and through Terraform that fails the whole apply, not just the
        policies. `helm template --kube-version 1.29.0` rendering nothing is
        the behaviour; this asserts the mechanism that produces it, since the
        suite has no helm to render with.
        """
        first = CHART_TEMPLATE.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(TEMPLATE_GATE, first)
        self.assertIn(
            "1.30.0",
            first,
            "the version floor moved; ValidatingAdmissionPolicy reached v1 in 1.30",
        )

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

    def test_the_chart_copy_names_the_identity_that_release_reconciles_as(self):
        """The exemption is useless if it names the wrong ServiceAccount.

        The chart runs the controller as `<release>-operator-sa` in the release
        namespace; the source file names the kustomize path's
        `kubeagents-controller`. A chart copied verbatim would exempt an
        identity that does not exist on a chart install, and every reconcile
        would be evaluated against the rule the exemption exists to skip.
        """
        raw = CHART_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            CONTROLLER_USER_CHART,
            raw,
            "the chart's admission policy does not name the release's own operator "
            "ServiceAccount, so the operator exemption cannot match on a chart install",
        )
        self.assertNotIn(
            CONTROLLER_USER_SRC,
            raw,
            "the chart still carries the source file's literal ServiceAccount name; "
            "the sync substitution did not run",
        )

    def test_the_sync_script_knows_about_the_template(self):
        """Otherwise `make chart-sync` silently stops regenerating it."""
        script = SYNC_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("config/admission/agent-rbac-policy.yaml", script)
        self.assertIn("templates/agent-rbac-admission-policy.yaml", script)


class PolicyContentTest(unittest.TestCase):
    """Only the denials that are actually in the policies — see the module docstring.

    These assertions read the CEL as text. There is no CEL runtime in this
    repository, so what they catch is a denial being deleted or weakened in a
    way that changes the string; a semantically broken rewrite that keeps the
    substring passes. Do not read a green here as "the expressions were
    evaluated". The one thing that does evaluate them is the API server, which
    compiles every expression when the policy object is admitted -- so a
    `kubectl apply --dry-run=server` of the source file is the check that
    covers syntax, and enforcement on a live cluster covers the rest.

    Worth stating because it has already cost one round: the failure mode a
    reader most expects these to guard -- an exemption that lets a denied
    object through -- is precisely the one they cannot see. Nothing here
    evaluates a matchCondition against an object, so the first version of the
    operator exemption, which any manifest could satisfy by setting a label,
    was green in this suite while being a complete bypass of the policy it
    sat in. Treat "the assertions pass" as "the text still says what it said",
    and get the semantics from the live run.
    """

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

    def test_the_operator_exemption_keys_on_the_authenticated_user(self):
        """A label exemption is a hole, not an exemption.

        matchConditions are ANDed and a false one drops the object from the
        policy entirely, so any exemption an author can satisfy from inside the
        manifest is a bypass of the whole rule rather than a carve-out from it.
        The first version of this exemption keyed on a label the controller
        stamps; setting that label on a hand-written ClusterRoleBinding to
        `developer-team-agent` walked straight past the denial. `request.userInfo`
        is filled in by the API server from the authenticated request, so no
        object can carry it.
        """
        conditions = self.by_name["kube-agents-agent-binding-scope"]["spec"]["matchConditions"]
        joined = " ".join(c["expression"] for c in conditions)
        self.assertIn(
            "request.userInfo.username",
            joined,
            "the operator exemption no longer keys on the authenticated user",
        )
        self.assertNotIn(
            "metadata.labels",
            joined,
            "the binding-scope policy reads a label again; anything the manifest can "
            "carry is a bypass of the policy, not an exemption from it",
        )

    def test_the_operator_own_bindings_are_exempt_from_the_binding_scope_policy(self):
        """The policy asserted an invariant the controller violates by construction.

        `reconcileRBAC` unconditionally mints `kubeagents:minimal:<ns>:<name>`
        as a ClusterRoleBinding whose subject is
        `spec.security.serviceAccountName`. The default value ends in `-agent`,
        so the binding-scope policy evaluates every reconcile on every install;
        an agent configured with the namespace-tier ServiceAccount name from
        the shipped GitOps example would have that reconcile denied. The
        operator has no namespace-tier path -- it only ever binds the platform
        tier -- so exempting what it stamps removes a contradiction rather than
        a control.
        """
        conditions = self.by_name["kube-agents-agent-binding-scope"]["spec"]["matchConditions"]
        joined = " ".join(c["expression"] for c in conditions)
        for username in [
            "system:serviceaccount:kubeagents-system:kubeagents-controller",
            "system:serviceaccount:kubeagents-system:kube-agents-operator-sa",
        ]:
            with self.subTest(username=username):
                self.assertIn(
                    username,
                    joined,
                    "the binding-scope policy no longer exempts this install path's "
                    "controller, so a reconcile can be denied by the policy the same "
                    "install ships",
                )

    def test_the_binding_scope_policy_selects_on_the_subject_not_a_label(self):
        """The one selector an author cannot drop from the manifest.

        A label may narrow what the policy examines -- the operator exemption
        above is exactly that -- but it may never be what brings an object into
        scope in the first place, because an author who omits it then walks
        past the policy. So this checks the selecting condition by name rather
        than the conditions as a block: `binds-agent-sa` has to key on the
        subject, and any label test has to live in a different condition whose
        only effect is to exclude.
        """
        conditions = self.by_name["kube-agents-agent-binding-scope"]["spec"]["matchConditions"]
        by_name = {c.get("name"): c["expression"] for c in conditions}
        self.assertIn(
            "binds-agent-sa", by_name, "the subject selector moved or was renamed"
        )
        self.assertIn("object.subjects", by_name["binds-agent-sa"])
        self.assertNotIn(
            "metadata.labels",
            by_name["binds-agent-sa"],
            "the policy now selects on a label, which an author can simply omit",
        )
        for name, expression in by_name.items():
            if name == "binds-agent-sa" or "metadata.labels" not in expression:
                continue
            with self.subTest(condition=name):
                self.assertTrue(
                    expression.lstrip().startswith("!("),
                    f"matchCondition {name!r} tests a label without being a negation, so it "
                    "may be selecting on one rather than excluding on one",
                )


if __name__ == "__main__":
    unittest.main()
