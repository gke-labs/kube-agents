"""The operator's `secrets` grant, and the ceiling on it.

Three callers need a Secret verb on the manager role:

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
- The kube-proxy render needs `create` and `update` on every install: the
  operator mints the proxy's TLS Secret and renews it. The `mode: next` A2A
  render needs `create`/`update`/`patch`/`delete`. The operator generates the
  NATS credential Secret once and repairs a missing key, renders the config
  Secret, and deletes the config Secret on the way back to `today`; all of
  them are owner-referenced to the `PlatformAgent`.

Every one of those reads or writes a Secret in the namespace of the
PlatformAgent being reconciled, so the grant is a namespaced `Role`: the
`+kubebuilder:rbac` marker carries `namespace=`, controller-gen renders it as a
second document in `role.yaml`, and the ClusterRole document names `secrets`
nowhere. That shape is what this file pins. A `secrets` rule that reappears in
the ClusterRole is cluster-wide read of every Secret on the cluster, which is
the cluster-admin-equivalent grant the namespacing exists to remove.

The verb set was `{get}` until the A2A render landed. It widened once,
deliberately, and this file is what keeps it from widening again by accident:
the set is pinned exactly, so an addition fails here rather than passing
silently under the trivy ignore. The digest caller added no verb.

`list` and `watch` are the ones that stay refused, and they are asserted
separately below rather than left implicit in the exact-set check. Even in one
namespace they turn a by-name grant into enumeration, which is a different
power from the five above; `a2aReader()` exists precisely so every A2A Secret
read is by name and uncached, and without the enumeration verbs a cached read
cannot be reintroduced by accident.

Trivy's KSV-0041 flags any Secret verb on a Role or ClusterRole as CRITICAL,
and its ignore file has no way to scope an exemption to one rule -- `paths`
globs are the whole vocabulary -- so exempting this grant exempts the whole of
`k8s-operator/config/rbac/`. That is the check this file replaces. A widening
to `list`, `watch` or anything else, or a move back into the ClusterRole, fails
here instead of passing silently under the ignore.

`charts/kube-agents/templates/operator-rbac.yaml` is not read here: both of its
generated blocks are spliced from `role.yaml` by `hack/sync-chart-manifests.sh`,
and `make chart-check` is what holds the two together. The conformance suite's
C5 test reads the chart as well.

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
WILDCARD = "*"
CLUSTER_ROLE_KIND = "ClusterRole"
ROLE_KIND = "Role"
PERMITTED_SECRET_VERBS = {"get", "create", "update", "patch", "delete"}
REFUSED_SECRET_VERBS = {"list", "watch"}
IGNORE_PATH = REPO_ROOT / ".trivyignore.yaml"
SECRETS_MISCONFIG_ID = "KSV-0041"


def _documents() -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(ROLE_PATH.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def _of_kind(kind: str) -> list[dict]:
    return [doc for doc in _documents() if doc.get("kind") == kind]


def _namespaced_role(test: unittest.TestCase) -> dict:
    # Asserted, not indexed: against a role.yaml with no Role document the
    # failure should read as the missing grant, not as an IndexError.
    roles = _of_kind(ROLE_KIND)
    test.assertEqual(len(roles), 1, f"expected one namespaced Role in {ROLE_PATH}, found {len(roles)}")
    return roles[0]


def _secret_rules(doc: dict) -> list[dict]:
    return [
        rule
        for rule in doc.get("rules", [])
        if SECRETS_RESOURCE in (rule.get("resources") or [])
        or WILDCARD in (rule.get("resources") or [])
    ]


class OperatorSecretsGrantTest(unittest.TestCase):
    def test_the_cluster_role_names_secrets_nowhere(self):
        # The invariant the namespacing exists for: a `secrets` verb on the
        # ClusterRole, or a wildcard that would reach it, is read of every
        # Secret on the cluster whatever the verb set says.
        cluster_roles = _of_kind(CLUSTER_ROLE_KIND)
        self.assertEqual(len(cluster_roles), 1, f"expected one ClusterRole in {ROLE_PATH}")
        self.assertEqual(
            [],
            _secret_rules(cluster_roles[0]),
            "the operator's ClusterRole grants secrets; the grant belongs in the "
            "namespaced Role (marker with namespace=), not cluster-wide",
        )

    def test_secrets_are_granted_in_exactly_one_namespaced_rule(self):
        role = _namespaced_role(self)
        self.assertTrue(
            role.get("metadata", {}).get("namespace"),
            "the Role document carries no namespace; controller-gen writes one from the marker",
        )
        rules = _secret_rules(role)
        self.assertEqual(
            len(rules),
            1,
            f"expected one rule naming {SECRETS_RESOURCE}, found {len(rules)}: {rules}",
        )

    def test_the_secret_verbs_are_exactly_the_set_three_callers_need(self):
        rule = _secret_rules(_namespaced_role(self))[0]
        self.assertEqual(set(rule.get("verbs") or []), PERMITTED_SECRET_VERBS)
        self.assertEqual(rule.get("apiGroups"), [CORE_API_GROUP])

    def test_the_operator_can_never_enumerate_secrets(self):
        # Named separately from the exact-set check above because this is the
        # property, not a consequence of today's list: `list`/`watch` turn a
        # by-name grant into enumeration, which is a different power from
        # get/create/update/patch/delete by name.
        verbs = set(_secret_rules(_namespaced_role(self))[0].get("verbs") or [])
        self.assertEqual(
            set(),
            verbs & REFUSED_SECRET_VERBS,
            "the operator reads Secrets by name through a2aReader(); granting "
            "list or watch would let a cached read be added",
        )

    def test_the_secrets_rule_names_no_other_resource(self):
        # A verb set is only a ceiling for the resources it is attached to.
        # Folding `secrets` into a rule that also names, say, `configmaps` would
        # keep the verbs at `get` and still widen what a later verb addition
        # reaches.
        rule = _secret_rules(_namespaced_role(self))[0]
        self.assertEqual(rule.get("resources"), [SECRETS_RESOURCE])

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
        self.assertEqual(entries[0].get("paths"), ["k8s-operator/config/rbac/**"])


if __name__ == "__main__":
    unittest.main()
