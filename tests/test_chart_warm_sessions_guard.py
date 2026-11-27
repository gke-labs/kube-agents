"""The chart's `warmSessions` rendering is guarded against an installed
PlatformAgent CRD that predates the field, the way every other new CRD field
is: `lookup` the CRD, dig to `spec.harness.tuning`, and `fail` with the
convention's message when the key is absent. Pruned silently, the value would
never apply: a `warmSessions: 0` meant to turn the warm pool off would leave
the operator's default of one warm pod running while the release record says
otherwise. `lookup` is empty under `helm template`, so the guard can only be
pinned as text; the render tests cover what `helm template` can show: nothing
when unset, the value when set (0 included), and the schema's bounds."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'
_CRD_DIG = '"spec" "properties" "harness" "properties" "tuning" "properties"'
_FAIL_MESSAGE = "helm upgrade does not update CRDs"
_REQUIRED = ["--set", "platformAgent.harness.clusterName=x", "--set", "platformAgent.harness.location=x",
             "--set", "platformAgent.harness.projectId=x"]
_KEY = "platformAgent.harness.tuning.warmSessions"


class WarmSessionsGuardTest(unittest.TestCase):
    def setUp(self):
        self.template = _TEMPLATE.read_text(encoding="utf-8")

    def _block(self):
        block = re.search(r'\{\{- if hasKey \$tuning "warmSessions" \}\}\n(.*?)warmSessions: \{\{ \$tuning\.warmSessions \}\}\n',
                          self.template, re.DOTALL)
        self.assertIsNotNone(block, "the warmSessions block is missing from the CR template")
        return block.group(1)

    def test_warmsessions_looks_up_the_installed_crd_before_it_renders(self):
        guard = self._block()
        self.assertIn(_CRD_LOOKUP, guard)
        self.assertIn(_CRD_DIG, guard)
        self.assertIn('hasKey $tuningProps "warmSessions"', guard)

    def test_the_guard_fails_with_the_conventions_message(self):
        fail = re.search(r'\{\{- fail "([^"]*)" \}\}', self._block())
        self.assertIsNotNone(fail, "the guard has no fail")
        self.assertIn(_KEY, fail.group(1))
        self.assertIn(_FAIL_MESSAGE, fail.group(1))
        self.assertIn("charts/kube-agents/crds/", fail.group(1))

    def test_the_tuning_block_opens_for_warmsessions_alone(self):
        # A values file that sets only warmSessions must still render the tuning block.
        opener = re.search(r'\{\{- if or \(hasKey \$tuning "maxInProgress"\)(.*?)\}\}\n    tuning:\n', self.template)
        self.assertIsNotNone(opener, "the tuning block's opener is missing")
        self.assertIn('(hasKey $tuning "warmSessions")', opener.group(1))


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class WarmSessionsRenderTest(unittest.TestCase):
    def _helm(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["helm", "template", "t", str(_CHART), *_REQUIRED, *extra],
                              capture_output=True, text=True, check=False)

    def test_unset_renders_no_field(self):
        # Unset leaves the CR without the field, so the operator's default applies and an
        # install that sets nothing writes the same CR as before the field existed.
        proc = self._helm()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("warmSessions", proc.stdout)

    def test_each_value_renders_under_tuning(self):
        # 0 is the off switch, so it must render rather than read as unset.
        for value in ("0", "1", "3"):
            proc = self._helm("--set", f"{_KEY}={value}")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertRegex(proc.stdout, rf'\n    tuning:\n(      .*\n)*      warmSessions: {value}\n')

    def test_a_value_outside_the_bounds_is_refused(self):
        for value in ("-1", "101"):
            proc = self._helm("--set", f"{_KEY}={value}")
            self.assertNotEqual(proc.returncode, 0, f"warmSessions={value} rendered")
            self.assertIn("warmSessions", proc.stderr)


if __name__ == "__main__":
    unittest.main()
