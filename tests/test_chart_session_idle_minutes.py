"""The chart's session idle times (#2371): `platformAgent.harness.tuning.sessionIdleMinutes`
and `dmSessionIdleMinutes` render onto the PlatformAgent as integers, only when set, and each
sits behind the chart's standard guard against an installed CRD that predates it: `lookup` the
CRD, dig to `spec.harness.tuning`, and `fail` when the key is absent. `lookup` is empty under
`helm template`, so the guard is pinned as text."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


class SessionIdleMinutesGuardTest(unittest.TestCase):
    def test_both_keys_are_guarded_by_the_installed_crd(self):
        text = _TEMPLATE.read_text()
        block = text[text.index('range $key := list "sessionIdleMinutes" "dmSessionIdleMinutes"'):]
        block = block[:block.index("{{ $key }}:")]
        self.assertIn('lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"', block)
        self.assertIn('"spec" "properties" "harness" "properties" "tuning" "properties"', block)
        self.assertIn("if not (hasKey $tuningProps $key)", block)
        self.assertIn("helm upgrade does not update CRDs", block)


@unittest.skipUnless(_HELM, "helm is not installed")
class SessionIdleMinutesRenderTest(unittest.TestCase):
    def _helm(self, values: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "values.yaml"
            path.write_text(values)
            return subprocess.run([_HELM, "template", "t", str(_CHART), *_REQUIRED, "-f", str(path),
                                   "--show-only", "templates/platform-agent-cr.yaml"],
                                  capture_output=True, text=True, check=False)

    def test_set_values_render_as_integers(self):
        proc = self._helm("platformAgent:\n  harness:\n    tuning:\n      sessionIdleMinutes: 45\n"
                          "      dmSessionIdleMinutes: 3\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("      sessionIdleMinutes: 45\n", proc.stdout)
        self.assertIn("      dmSessionIdleMinutes: 3\n", proc.stdout)

    def test_unset_renders_neither(self):
        proc = self._helm("{}\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("IdleMinutes", proc.stdout)

    def test_the_schema_refuses_zero_a_string_and_over_a_week(self):
        for value in ["0", '"5m"', "10081"]:
            with self.subTest(value=value):
                proc = self._helm(f"platformAgent:\n  harness:\n    tuning:\n      dmSessionIdleMinutes: {value}\n")
                self.assertNotEqual(proc.returncode, 0, proc.stdout[-200:])
                self.assertIn("dmSessionIdleMinutes", proc.stderr)


if __name__ == "__main__":
    unittest.main()
