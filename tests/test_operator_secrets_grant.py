"""The operator's `secrets` grant, and the ceiling on it.

Five callers need a Secret verb on `manager-role`:

- `checkShellSandboxKeys` needs `get`. It asks whether the sandbox's
  authorized-keys Secret exists so the CR can say so, because that Secret is the
  one mount a pod cannot start without, and a missing one is otherwise fifteen
  minutes of `ContainerCreating` with the cause on an object nobody is looking
  at. It reads no value out of the Secret and creates that Secret nowhere.
- `stampSecretEnvHash` needs `get`, and unlike the caller above it **reads the
  values**: it digests the Secret keys the agent pod consumes as environment onto
  the pod template, so that rotating one rolls the pod
  (`k8s-operator/internal/controller/platformagent_secret_hash.go`). Which
  Secrets it reads is decided by the rendered pod spec, so a CR- or
  plugin-supplied `SecretKeyRef` is covered. Every read is by name and uncached.
- The `mode: next` A2A render needs `create`/`update`/`patch`/`delete`. The
  operator generates the NATS credential Secret once and repairs a missing key,
  renders the config Secret, and deletes the config Secret on the way back to
  `today`; all of them are owner-referenced to the `PlatformAgent`.
- `ensureKubeProxyTLSSecret` needs `get`/`create`/`update` to manage the
  `<agent>-kube-proxy-tls` Secret.
- `buildMinimalPlatformRole` grants `get`/`list`/`watch` on `secrets` to
  `kubeagents:minimal:<ns>:<name>` so `<agent>-kube-proxy` can read `Secret`
  objects and serve redacted metadata (`KUBE_PROXY_REDACT_SECRETS=true`).
  Kubernetes RBAC escalation prevention requires `manager-role` to hold
  `get`/`list`/`watch` on `secrets` before it can grant those verbs on a
  managed `ClusterRole`.

Even though `manager-role` holds `list` and `watch` for RBAC escalation
prevention, the operator itself must never list or watch `Secret` objects:
every controller Secret read goes by name through `a2aReader()`, and
`test_the_operator_never_watches_or_lists_secrets_in_controller_code` below
forbids `&corev1.SecretList`, `Owns(&corev1.Secret`, and
`Watches(&corev1.Secret` in controller Go files.

Trivy's KSV-0041 flags any Secret verb on a ClusterRole as CRITICAL, and its
ignore file has no way to scope an exemption to one rule -- `paths` globs are
the whole vocabulary -- so exempting this grant exempts `k8s-operator/config/rbac/`
and the rendered golden fixtures in
`k8s-operator/internal/testing/testdata/platform/expected/`. That is the check
this file replaces: it pins the verb set and `+kubebuilder:rbac` marker, forbids
controller `Secret` list/watch calls, and fails if the Trivy exemption widens
past those two directories.

`charts/kube-agents/templates/operator-rbac.yaml` is not read here: it is
spliced from `role.yaml` by `hack/sync-chart-manifests.sh`, and `make
chart-check` is what holds the two together.

Run:
  python3 -m unittest discover -s tests -p 'test_operator_secrets_grant.py' -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_PATH = REPO_ROOT / "k8s-operator" / "config" / "rbac" / "role.yaml"
CHART_PATH = REPO_ROOT / "charts" / "kube-agents"
CR_TEMPLATE_PATH = CHART_PATH / "templates" / "platform-agent-cr.yaml"
REQUIRED_HELM_SETS = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
DISALLOW_SECRETS_GUARD = (
    'include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.security.disallowSecretsRead" '
    '"path" (list "security") "field" "disallowSecretsRead")'
)
CORE_API_GROUP = ""
SECRETS_RESOURCE = "secrets"
PERMITTED_SECRET_VERBS = {"get", "list", "watch", "create", "update", "patch", "delete"}
CONTROLLER_DIR = REPO_ROOT / "k8s-operator" / "internal" / "controller"
IGNORE_PATH = REPO_ROOT / ".trivyignore.yaml"
SECRETS_MISCONFIG_ID = "KSV-0041"


def _secret_rules() -> list[dict]:
    role = yaml.safe_load(ROLE_PATH.read_text(encoding="utf-8"))
    return [
        rule
        for rule in role.get("rules", [])
        if SECRETS_RESOURCE in (rule.get("resources") or [])
    ]


class OperatorSecretsGrantTest(unittest.TestCase):
    def test_secrets_are_granted_in_exactly_one_rule(self):
        rules = _secret_rules()
        self.assertEqual(
            len(rules),
            1,
            f"expected one rule naming {SECRETS_RESOURCE}, found {len(rules)}: {rules}",
        )

    def test_the_secret_verbs_are_exactly_the_permitted_set(self):
        rule = _secret_rules()[0]
        self.assertEqual(set(rule.get("verbs") or []), PERMITTED_SECRET_VERBS)
        self.assertEqual(rule.get("apiGroups"), [CORE_API_GROUP])

    def test_the_operator_never_watches_or_lists_secrets_in_controller_code(self):
        # `list` and `watch` are held on `manager-role` only so the operator
        # passes Kubernetes RBAC escalation prevention when reconciling
        # `buildMinimalPlatformRole` (which grants `get`/`list`/`watch` on
        # `secrets` to `<agent>-kube-proxy`, where Secret payloads are stripped).
        # The operator's own Go code must never add a Secret informer watch or
        # call `SecretList`.
        for go_file in sorted(CONTROLLER_DIR.glob("*.go")):
            if go_file.name.endswith("_test.go"):
                continue
            source = go_file.read_text(encoding="utf-8")
            self.assertNotIn(
                "SecretList",
                source,
                f"{go_file.name} lists Secrets; operator Secret reads must stay by-name via APIReader/a2aReader",
            )
            self.assertNotIn(
                "Owns(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via Owns(&corev1.Secret...); operator must not start a Secret informer",
            )
            self.assertNotIn(
                "Watches(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via Watches(&corev1.Secret...); operator must not start a Secret informer",
            )
            self.assertNotIn(
                "WatchesMetadata(&corev1.Secret",
                source,
                f"{go_file.name} watches Secrets via WatchesMetadata(&corev1.Secret...); operator must not start a Secret informer",
            )

    def test_the_secrets_marker_names_no_other_resource(self):
        # controller-gen merges rules with identical apiGroups and verbs in
        # role.yaml, so check the +kubebuilder:rbac marker in
        # platformagent_controller.go directly to ensure `secrets` stays on its
        # own marker line and is never folded into another resource's marker.
        rule = _secret_rules()[0]
        self.assertNotIn("*", rule.get("resources") or [])
        controller_src = (CONTROLLER_DIR / "platformagent_controller.go").read_text(encoding="utf-8")
        secret_markers = [
            line.strip()
            for line in controller_src.splitlines()
            if "+kubebuilder:rbac:" in line and "secrets" in line
        ]
        self.assertEqual(len(secret_markers), 1)
        for marker in secret_markers:
            self.assertIn("resources=secrets,", marker, f"secrets marker shares resources: {marker}")

    def test_the_trivy_exemption_stays_scoped_to_the_expected_directories(self):
        ignores = yaml.safe_load(IGNORE_PATH.read_text(encoding="utf-8"))
        entries = [
            entry
            for entry in ignores.get("misconfigurations", [])
            if entry.get("id") == SECRETS_MISCONFIG_ID
        ]
        self.assertEqual(
            len(entries),
            1,
            f"expected one {SECRETS_MISCONFIG_ID} exemption, found {len(entries)}",
        )
        self.assertEqual(
            entries[0].get("paths"),
            [
                "k8s-operator/config/rbac/**",
                "k8s-operator/internal/testing/testdata/platform/expected/**",
            ],
        )

    def test_chart_guards_and_defaults_disallow_secrets_read_to_null(self):
        template = CR_TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn(DISALLOW_SECRETS_GUARD, template)
        values = yaml.safe_load((CHART_PATH / "values.yaml").read_text(encoding="utf-8"))
        self.assertIsNone(values["platformAgent"]["security"]["disallowSecretsRead"])

    @unittest.skipUnless(shutil.which("helm"), "helm not installed")
    def test_chart_renders_disallow_secrets_read_when_set(self):
        def render_security(*extra: str) -> dict:
            proc = subprocess.run(
                [
                    "helm", "template", "test-release", str(CHART_PATH),
                    *REQUIRED_HELM_SETS, *extra,
                    "--show-only", "templates/platform-agent-cr.yaml",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for doc in yaml.safe_load_all(proc.stdout):
                if isinstance(doc, dict) and doc.get("kind") == "PlatformAgent":
                    return doc["spec"]["security"]
            self.fail("no PlatformAgent in rendered output")

        self.assertNotIn("disallowSecretsRead", render_security())
        self.assertIs(
            render_security("--set", "platformAgent.security.disallowSecretsRead=true").get("disallowSecretsRead"),
            True,
        )
        self.assertIs(
            render_security("--set", "platformAgent.security.disallowSecretsRead=false").get("disallowSecretsRead"),
            False,
        )

    def test_rendered_kube_proxy_enforces_secret_redaction_when_minimal_role_grants_secrets(self):
        # Every golden fixture where kubeagents:minimal grants `secrets` must
        # also render `<agent>-kube-proxy` with `KUBE_PROXY_REDACT_SECRETS=true`
        # and keep the agent pod's `automountServiceAccountToken` disabled while
        # routing `KUBE_PROXY_URL` to `<agent>-kube-proxy`.
        golden_dir = REPO_ROOT / "k8s-operator" / "internal" / "testing" / "testdata" / "platform" / "expected"
        checked = 0
        for fixture in sorted(golden_dir.glob("*.yaml")):
            docs = [d for d in yaml.safe_load_all(fixture.read_text(encoding="utf-8")) if isinstance(d, dict)]
            minimal_roles = [
                d for d in docs
                if d.get("kind") == "ClusterRole"
                and str(d.get("metadata", {}).get("name", "")).startswith("kubeagents:minimal:")
            ]
            if not minimal_roles:
                continue
            grants_secrets = any(
                SECRETS_RESOURCE in (rule.get("resources") or [])
                for rule in (minimal_roles[0].get("rules") or [])
            )
            if not grants_secrets:
                continue
            checked += 1
            deployments = {
                d["metadata"]["name"]: d
                for d in docs
                if d.get("kind") == "Deployment"
            }
            kube_proxy_deploys = [
                d for name, d in deployments.items() if name.endswith("-kube-proxy")
            ]
            self.assertEqual(
                len(kube_proxy_deploys),
                1,
                f"{fixture.name}: expected one *-kube-proxy Deployment alongside kubeagents:minimal secrets grant",
            )
            kp_containers = kube_proxy_deploys[0]["spec"]["template"]["spec"]["containers"]
            kp_env = {
                item["name"]: item.get("value")
                for item in kp_containers[0].get("env", [])
            }
            self.assertEqual(
                kp_env.get("KUBE_PROXY_REDACT_SECRETS"),
                "true",
                f"{fixture.name}: kube-proxy Deployment must set KUBE_PROXY_REDACT_SECRETS=true when secrets are granted",
            )
        self.assertGreater(checked, 0, "expected at least one golden fixture granting secrets on kubeagents:minimal")


if __name__ == "__main__":
    unittest.main()
