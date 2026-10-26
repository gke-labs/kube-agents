"""The chart creates the AgentProfile namespace on a "next" install (#3655).

Profile workers run in `<release namespace>-profiles`. The chart creates it, not the
operator: the operator renders only its quota and network policy, inside it, with the
namespaced rights it already holds. A "today" install, the default, gets no such
namespace. `platformAgent.harness.tuning.profiles` reaches the CR, and the values
schema holds it to the CRD's bounds.

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
    def test_a_today_install_gets_no_profile_namespace(self):
        self.assertEqual(_docs(_NS_TEMPLATE), [])
        self.assertEqual(_docs(_NS_TEMPLATE, "platformAgent.mode=today"), [])

    def test_a_next_install_gets_the_release_namespace_with_profiles_appended(self):
        docs = _docs(_NS_TEMPLATE, "platformAgent.mode=next")
        self.assertEqual(len(docs), 1, docs)
        self.assertEqual(docs[0]["kind"], "Namespace")
        self.assertEqual(docs[0]["metadata"]["name"], f"{_RELEASE_NS}-profiles")
        # The markers the operator requires before it treats the namespace as
        # the profile namespace (profileNamespaceExists in the operator).
        self.assertEqual(docs[0]["metadata"]["labels"]["app.kubernetes.io/component"], "profiles")
        self.assertEqual(docs[0]["metadata"]["annotations"]["meta.helm.sh/release-namespace"], _RELEASE_NS)

    def test_a_release_namespace_too_long_for_the_suffix_fails_at_render(self):
        args = [_HELM, "template", "ka", str(_CHART), "-n", "a" * 55, "--set", "platformAgent.mode=next", *_REQUIRED]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("longer than 63 characters", result.stderr)

    def test_profile_tuning_reaches_the_cr(self):
        cr = _docs(_CR_TEMPLATE, "platformAgent.mode=next",
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
