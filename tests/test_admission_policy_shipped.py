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

import fnmatch
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = REPO_ROOT / "k8s-operator" / "scripts"
POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "agent-rbac-policy.yaml"
CHART_TEMPLATE = (
    REPO_ROOT / "charts" / "kube-agents" / "templates" / "agent-rbac-admission-policy.yaml"
)
CHART_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"
PROVISION_OPERATOR = (
    REPO_ROOT / "k8s-operator" / "scripts" / "provision_03_gcp_gke_operator.sh"
)
INSTALL_GUIDE = REPO_ROOT / "INSTALL.md"
STARTUP_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-startup-test.yml"

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
        """Assert the verb, not just the path.

        A substring check for the filename passes on the comment above the
        assignment, so `kubectl apply -f` could become `kubectl diff -f` — or the
        apply could be deleted outright — with this suite still green.
        """
        script = PROVISION_OPERATOR.read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r"kubectl apply -f \"\$\{ADMISSION_POLICY_FILE\}\"",
            "the script install path (INSTALL.md Method 1, the recommended one) "
            "no longer *applies* the admission policies",
        )
        self.assertRegex(
            script,
            r'ADMISSION_POLICY_FILE="\$\{OPERATOR_DIR\}/config/admission/agent-rbac-policy\.yaml"',
            "ADMISSION_POLICY_FILE no longer points at the policy source",
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

    def test_the_step_is_in_the_execution_pipeline(self):
        """Defining the functions is not the same as running them."""
        script = PROVISION_OPERATOR.read_text(encoding="utf-8")
        self.assertRegex(
            script,
            r"run_(deploy_)?step[^\n]*verify_admission_policy execute_admission_policy",
            "verify_admission_policy/execute_admission_policy exist but nothing "
            "in the pipeline calls them",
        )


KUBECTL_STUB = """#!/usr/bin/env bash
# Records every invocation, and answers the discovery probe as the test dictates.
echo "$*" >> "$KUBECTL_LOG"
if [ "$1" = "get" ] && [ "$2" = "--raw" ]; then
  if [ "${PROBE_RC}" -ne 0 ]; then
    echo "${PROBE_STDERR}" >&2
    exit "${PROBE_RC}"
  fi
  echo "${PROBE_BODY}"
  exit 0
fi
exit 0
"""

DISCOVERY_WITH_VAP = (
    '{"kind":"APIResourceList","resources":[{"name":"validatingadmissionpolicies"},'
    '{"name":"validatingadmissionpolicybindings"}]}'
)
DISCOVERY_WITHOUT_VAP = '{"kind":"APIResourceList","resources":[{"name":"mutatingwebhookconfigurations"}]}'


def _extract_function(script: Path, name: str) -> str:
    """One top-level bash function, for evaluation without running the pipeline."""
    body = re.search(
        rf"^{re.escape(name)}\(\) \{{$.*?^\}}$",
        script.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    if body is None:
        raise AssertionError(f"{script} no longer defines a top-level {name}()")
    return body.group(0)


class AdmissionPolicyProbeTest(unittest.TestCase):
    """The probe decides whether the backstop gets installed, so it is executed here.

    Three outcomes have to stay distinguishable. Reading them off a pipeline into
    grep — the original implementation — collapsed "old cluster" and "kubectl
    broke" into one branch, so a transient API-server failure during provisioning
    left a supported cluster permanently unbackstopped while the script blamed the
    cluster's version.
    """

    def _run(self, probe_rc: int, probe_body: str = "", probe_stderr: str = ""):
        script = PROVISION_OPERATOR
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            stub = tmpdir / "kubectl"
            stub.write_text(KUBECTL_STUB, encoding="utf-8")
            stub.chmod(0o755)
            log = tmpdir / "kubectl.log"
            log.touch()

            harness = textwrap.dedent(
                f"""
                source "$SCRIPT_DIR/common.sh"
                {_extract_function(script, "admission_policy_api_status")}
                {_extract_function(script, "execute_admission_policy")}
                ADMISSION_POLICY_FILE="/tmp/does-not-need-to-exist.yaml"
                execute_admission_policy
                echo "RC=$?"
                """
            )

            env = dict(os.environ)
            env.update(
                {
                    "PATH": f"{tmpdir}:{env['PATH']}",
                    "KUBECTL_LOG": str(log),
                    "PROBE_RC": str(probe_rc),
                    "PROBE_BODY": probe_body,
                    "PROBE_STDERR": probe_stderr,
                    "CI": "1",
                    "TERM": "dumb",
                    "SCRIPT_DIR": str(SCRIPTS),
                    "VARS_FILE": str(tmpdir / "vars.sh"),
                }
            )
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            return result, log.read_text(encoding="utf-8")

    def test_a_supported_cluster_gets_the_policies_applied(self):
        result, calls = self._run(probe_rc=0, probe_body=DISCOVERY_WITH_VAP)
        self.assertIn("RC=0", result.stdout, result.stdout + result.stderr)
        self.assertIn("apply -f", calls, "the policies were never applied")

    def test_an_old_cluster_is_skipped_with_a_warning_and_no_apply(self):
        result, calls = self._run(probe_rc=0, probe_body=DISCOVERY_WITHOUT_VAP)
        self.assertIn("RC=0", result.stdout, "a pre-1.30 cluster must not fail the install")
        self.assertNotIn("apply -f", calls, "applying would fail on a cluster without the API")
        self.assertIn("1.30", result.stdout + result.stderr)

    def test_a_failed_probe_fails_the_step_instead_of_skipping(self):
        """The regression the three-state probe exists to prevent."""
        result, calls = self._run(
            probe_rc=1, probe_stderr="Unable to connect to the server: dial tcp: i/o timeout"
        )
        self.assertNotIn(
            "RC=0",
            result.stdout,
            "a probe that could not reach the API server must fail the step, not "
            "silently skip the backstop",
        )
        self.assertNotIn("apply -f", calls)

    def test_a_failed_probe_does_not_blame_the_cluster_version(self):
        """A wrong diagnosis is worse than none: it sends the operator nowhere."""
        result, _ = self._run(
            probe_rc=1, probe_stderr="error: You must be logged in to the server"
        )
        output = result.stdout + result.stderr
        self.assertNotIn(
            "needs Kubernetes 1.30+",
            output,
            "an unreachable API server was reported as an out-of-date cluster",
        )
        self.assertIn("You must be logged in to the server", output)


class CiRunsTheseAssertionsTest(unittest.TestCase):
    """An assertion whose job never starts is not a control.

    `.github/workflows/agent-startup-test.yml` is the only job that runs the
    top-level tests/ directory — `make test-python`'s globs cover agents/,
    deploy/docker/patches/ and scripts/, not tests/ — and it is path-filtered. So
    every file this suite asserts on has to be in that filter, or the regression
    it exists to catch lands without starting the job.

    That is exactly how INSTALL.md's `kubectl apply` line, the only thing
    installing the policies on the manual path, was left unguarded: the assertion
    was written and the filter was not updated.

    This test closes the loop on itself: the workflow file is in its own filter,
    so any edit that drops an entry starts the job that runs this.
    """

    def _filter_patterns(self) -> list[str]:
        workflow = yaml.safe_load(STARTUP_WORKFLOW.read_text(encoding="utf-8"))
        for step in workflow["jobs"]["test"]["steps"]:
            if "paths-filter" in str(step.get("uses", "")):
                return yaml.safe_load(step["with"]["filters"])["startup"]
        raise AssertionError(f"{STARTUP_WORKFLOW} no longer has a paths-filter step")

    def _covered(self, path: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            if pattern.endswith("/**"):
                if path.startswith(pattern[: -len("**")]):
                    return True
            # fnmatch's `*` crosses `/`, so compare depth too rather than let
            # `tests/test_*.py` appear to cover `tests/e2e/anything.py`.
            elif fnmatch.fnmatch(path, pattern) and path.count("/") == pattern.count("/"):
                return True
        return False

    def test_every_file_this_suite_asserts_on_is_in_the_ci_path_filter(self):
        patterns = self._filter_patterns()
        # The repository files this module reads. Kept explicit rather than
        # traced at runtime: a trace only sees what the current tests happen to
        # open, so it would go quiet exactly when a test is deleted.
        asserted_on = [
            str(p.relative_to(REPO_ROOT))
            for p in [
                POLICY_SRC,
                CHART_TEMPLATE,
                CHART_VALUES,
                PROVISION_OPERATOR,
                INSTALL_GUIDE,
                SCRIPTS / "common.sh",
            ]
        ]
        missing = [p for p in asserted_on if not self._covered(p, patterns)]
        self.assertEqual(
            [],
            missing,
            f"{missing} are asserted on by this suite but are not in the "
            f"{STARTUP_WORKFLOW.name} path filter, so changing them starts no job "
            "and the assertions never run",
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
