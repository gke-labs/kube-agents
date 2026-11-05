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
the whole vocabulary -- so exempting this grant exempts the whole of
`k8s-operator/config/rbac/`. That is the check this file replaces: it pins the
verb set and `+kubebuilder:rbac` marker, forbids controller `Secret` list/watch
calls, and fails if the Trivy exemption widens past the `rbac` directory.

`charts/kube-agents/templates/operator-rbac.yaml` is not read here: it is
spliced from `role.yaml` by `hack/sync-chart-manifests.sh`, and `make
chart-check` is what holds the two together.

Run:
  python3 -m unittest discover -s tests -p 'test_operator_secrets_grant.py' -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_PATH = REPO_ROOT / "k8s-operator" / "config" / "rbac" / "role.yaml"
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
        self.assertGreaterEqual(len(secret_markers), 1)
        for marker in secret_markers:
            self.assertIn("resources=secrets,", marker, f"secrets marker shares resources: {marker}")

    def test_the_trivy_exemption_stays_scoped_to_the_rbac_directory(self):
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


if __name__ == "__main__":
    unittest.main()
