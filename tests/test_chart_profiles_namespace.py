"""The chart creates the AgentProfile namespace on every install (#3655).

Profile workers run in `<release namespace>-profiles`. The chart creates it, not the
operator: the operator renders only its quota and network policy, inside it, with the
namespaced rights it already holds. A release without a PlatformAgent
(`platformAgent.enabled=false`) gets no such namespace. `platformAgent.harness.tuning.profiles`
reaches the CR, and the values schema holds it to the CRD's bounds.

Run: python3 -m unittest discover -s tests -p 'test_chart_profiles_namespace.py' -v
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_NS_TEMPLATE = "templates/profiles-namespace.yaml"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_RBAC_TEMPLATE = "templates/operator-rbac.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_RELEASE_NS = "kubeagents-system"


def _helm(template: str, *overrides: str) -> subprocess.CompletedProcess:
    args = [_HELM, "template", "ka", str(_CHART), "-n", _RELEASE_NS, "--show-only", template, *_REQUIRED]
    for override in overrides:
        args += ["--set", override]
    return subprocess.run(args, capture_output=True, text=True)


def _docs(template: str, *overrides: str) -> list:
    result = _helm(template, *overrides)
    if result.returncode != 0 and "could not find template" in result.stderr:
        return []  # --show-only on a template that rendered nothing
    result.check_returncode()
    return [d for d in yaml.safe_load_all(result.stdout) if d]


@unittest.skipUnless(_HELM, "helm not on PATH")
class ProfilesNamespaceRenderTest(unittest.TestCase):
    def test_a_release_without_a_platform_agent_gets_no_profile_namespace(self):
        self.assertEqual(_docs(_NS_TEMPLATE, "platformAgent.enabled=false"), [])

    def test_the_default_install_gets_the_release_namespace_with_profiles_appended(self):
        docs = _docs(_NS_TEMPLATE)
        self.assertEqual(len(docs), 1, docs)
        self.assertEqual(docs[0]["kind"], "Namespace")
        self.assertEqual(docs[0]["metadata"]["name"], f"{_RELEASE_NS}-profiles")
        # The markers the operator requires before it treats the namespace as
        # the profile namespace (profileNamespaceExists in the operator).
        self.assertEqual(docs[0]["metadata"]["labels"]["app.kubernetes.io/component"], "profiles")
        self.assertEqual(docs[0]["metadata"]["annotations"]["meta.helm.sh/release-namespace"], _RELEASE_NS)

    def test_the_operator_gets_its_write_role_in_the_profile_namespace_whatever_the_mode(self):
        # The operator renders into the profile namespace on every install
        # (the A2A stack renders whatever spec.mode says), so its namespaced
        # Role and RoleBinding must render there whenever the namespace does.
        # Gated on mode next, a mode-today install left the operator with no
        # write verbs in a namespace it writes to, and every pass failed to
        # apply the profile-namespace resources.
        profile_ns = f"{_RELEASE_NS}-profiles"
        for mode in (None, "today", "next"):
            with self.subTest(mode=mode):
                overrides = [] if mode is None else [f"platformAgent.mode={mode}"]
                docs = _docs(_RBAC_TEMPLATE, *overrides)
                in_ns = {(d["kind"], d["metadata"]["name"]): d for d in docs if d["metadata"].get("namespace") == profile_ns}
                role = in_ns.get(("Role", "ka-operator-namespaced-role"))
                binding = in_ns.get(("RoleBinding", "ka-operator-namespaced-rolebinding"))
                self.assertIsNotNone(role, sorted(in_ns))
                self.assertIsNotNone(binding, sorted(in_ns))
                self.assertEqual(binding["roleRef"]["name"], "ka-operator-namespaced-role")
                self.assertEqual(binding["subjects"][0]["name"], "ka-operator-sa")
                netpol_verbs = {
                    v
                    for rule in role["rules"]
                    if "networkpolicies" in rule.get("resources", [])
                    and any(group == "networking.k8s.io" for group in rule.get("apiGroups", []))
                    for v in rule["verbs"]
                }
                self.assertTrue({"create", "patch", "update", "delete"} <= netpol_verbs, role["rules"])

    def test_without_a_platform_agent_there_is_no_profile_namespace_role(self):
        docs = _docs(_RBAC_TEMPLATE, "platformAgent.enabled=false")
        self.assertFalse(
            [d for d in docs if d["metadata"].get("namespace") == f"{_RELEASE_NS}-profiles"],
            "no profile namespace, so nothing of the operator's renders into it",
        )

    def test_a_release_namespace_too_long_for_the_suffix_fails_at_render(self):
        args = [_HELM, "template", "ka", str(_CHART), "-n", "a" * 55, *_REQUIRED]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("longer than 63 characters", result.stderr)

    def test_profile_tuning_reaches_the_cr(self):
        cr = _docs(_CR_TEMPLATE,
                   "platformAgent.harness.tuning.profiles.ceiling=5",
                   "platformAgent.harness.tuning.profiles.warmReplicas=0")[0]
        self.assertEqual(cr["spec"]["harness"]["tuning"]["profiles"], {"ceiling": 5, "warmReplicas": 0})

    def test_the_values_schema_holds_profile_tuning_to_the_crd_bounds(self):
        for override in ("platformAgent.harness.tuning.profiles.ceiling=0",
                         "platformAgent.harness.tuning.profiles.ceiling=1001",
                         "platformAgent.harness.tuning.profiles.warmReplicas=101",
                         "platformAgent.harness.tuning.profiles.other=1"):
            result = _helm(_CR_TEMPLATE, override)
            self.assertNotEqual(result.returncode, 0, f"{override} rendered")
            self.assertIn("profiles", result.stderr, f"{override}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
