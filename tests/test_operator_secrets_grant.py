"""The operator's `secrets` grant, and the ceiling on it.

`manager-role` holds `get` on `secrets` and nothing else. One caller needs it:
`checkShellSandboxKeys` asks whether the sandbox's authorized-keys Secret exists
so the CR can say so, because that Secret is the one mount on this branch a pod
cannot start without, and a missing one is otherwise fifteen minutes of
`ContainerCreating` with the cause on an object nobody is looking at. It reads
no value out of the Secret and the operator creates that Secret nowhere.

Trivy's KSV-0041 flags any Secret verb on a ClusterRole as CRITICAL, and its
ignore file has no way to scope an exemption to one rule -- `paths` globs are
the whole vocabulary -- so exempting this grant exempts the whole of
`k8s-operator/config/rbac/`. That is the check this file replaces. A widening to
`list`, `watch`, `create` or anything else fails here instead of passing
silently under the ignore.

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
PERMITTED_SECRET_VERBS = {"get"}
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

    def test_the_only_secret_verb_is_get(self):
        rule = _secret_rules()[0]
        self.assertEqual(set(rule.get("verbs") or []), PERMITTED_SECRET_VERBS)
        self.assertEqual(rule.get("apiGroups"), [CORE_API_GROUP])

    def test_the_secrets_rule_names_no_other_resource(self):
        # A verb set is only a ceiling for the resources it is attached to.
        # Folding `secrets` into a rule that also names, say, `configmaps` would
        # keep the verbs at `get` and still widen what a later verb addition
        # reaches.
        rule = _secret_rules()[0]
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
