"""The chart's `sessionResources` rendering (#2371) is guarded against an installed
PlatformAgent CRD that predates the field, the way every other new CRD field
is: `lookup` the CRD, dig to `spec.harness.tuning`, and `fail` with the
convention's message when the key is absent. Pruned silently, the value would
never apply: the session pods would keep the default requests and limits while
the release record says otherwise. `lookup` is empty under `helm template`, so the
guard can only be pinned as text."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "platform-agent-cr.yaml"
_CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'
_CRD_DIG = '"spec" "properties" "harness" "properties" "tuning" "properties"'
_FAIL_MESSAGE = "helm upgrade does not update CRDs"


class SessionResourcesGuardTest(unittest.TestCase):
    def setUp(self):
        self.template = _TEMPLATE.read_text(encoding="utf-8")

    def _block(self):
        block = re.search(r'\{\{- if \$sessionQuantities \}\}\n(.*?)sessionResources:\n',
                          self.template, re.DOTALL)
        self.assertIsNotNone(block, "the sessionResources block is missing from the CR template")
        return block.group(1)

    def test_sessionresources_looks_up_the_installed_crd_before_it_renders(self):
        guard = self._block()
        self.assertIn(_CRD_LOOKUP, guard)
        self.assertIn(_CRD_DIG, guard)
        self.assertIn('hasKey $tuningProps "sessionResources"', guard)

    def test_the_guard_fails_with_the_conventions_message(self):
        fail = re.search(r'\{\{- fail "([^"]*)" \}\}', self._block())
        self.assertIsNotNone(fail, "the guard has no fail")
        self.assertIn("platformAgent.harness.tuning.sessionResources", fail.group(1))
        self.assertIn(_FAIL_MESSAGE, fail.group(1))
        self.assertIn("charts/kube-agents/crds/", fail.group(1))


_CHART = _REPO_ROOT / "charts" / "kube-agents"
_REQUIRED = ["--set", "platformAgent.harness.clusterName=x", "--set", "platformAgent.harness.location=x",
             "--set", "platformAgent.harness.projectId=x"]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class SessionResourcesRenderTest(unittest.TestCase):
    """The value renders through the sizing overrides' helpers: every quantity quoted, so a
    values file's float reaches the CRD's int-or-string quantity as a string, and a null leaf
    dropped (Helm's way to unset a key)."""

    def _render(self, values: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(values)
        proc = subprocess.run(["helm", "template", "t", str(_CHART), *_REQUIRED, "-f", f.name],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_a_float_is_quoted_and_a_null_leaf_dropped(self):
        out = self._render("platformAgent:\n  harness:\n    tuning:\n      sessionResources:\n"
                           "        requests:\n          cpu: 0.5\n          memory: null\n"
                           "        limits:\n          memory: 1Gi\n")
        block = out[out.index("sessionResources:"):].split("\n")[:5]
        self.assertEqual([line.strip() for line in block],
                         ["sessionResources:", "limits:", 'memory: "1Gi"', "requests:", 'cpu: "0.5"'])

    def test_an_override_of_only_nulls_renders_nothing(self):
        out = self._render("platformAgent:\n  harness:\n    tuning:\n      sessionResources:\n"
                           "        requests:\n          memory: null\n")
        self.assertNotIn("sessionResources:", out)


if __name__ == "__main__":
    unittest.main()
