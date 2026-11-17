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

    def _helm(self, values: str, *extra: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "values.yaml"
            path.write_text(values)
            return subprocess.run(["helm", "template", "t", str(_CHART), *_REQUIRED, "-f", str(path), *extra],
                                  capture_output=True, text=True, check=False)

    def _render(self, values: str) -> str:
        proc = self._helm(values)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    @staticmethod
    def _one(side: str, name: str, value: str) -> str:
        return (f"platformAgent:\n  harness:\n    tuning:\n      sessionResources:\n"
                f"        {side}:\n          {name}: {value}\n")

    def test_a_value_the_operator_cannot_read_is_refused(self):
        # 1e.5 passes the CRD's pattern, is stored, and then fails resource.ParseQuantity on
        # every decode of the PlatformAgent. The schema refuses it first, and the template's
        # own check refuses it when schema validation is skipped.
        for value in ['"1e.5"', '"2GB"', '"lots"', '"1.5.0"', '"1e99999999999999999999"',
                      '"-1e99999999999999999999"', '"1234567890123456"', '"8Ei"']:
            for extra in ((), ("--skip-schema-validation",)):
                with self.subTest(value=value, extra=extra):
                    proc = self._helm(self._one("limits", "memory", value), *extra)
                    self.assertNotEqual(proc.returncode, 0, proc.stdout[-300:])
                    self.assertIn("sessionResources", proc.stderr)

    def test_a_cpu_past_int64_millicores_is_refused_and_one_under_renders(self):
        # The operator reads CPU in millicores; 9.3e15 cores is past int64 there.
        proc = self._helm(self._one("limits", "cpu", '"9.3e15"'), "--skip-schema-validation")
        self.assertNotEqual(proc.returncode, 0, proc.stdout[-300:])
        self.assertIn("millicore", proc.stderr)
        self.assertIn('cpu: "9e15"', self._render(self._one("limits", "cpu", '"9e15"')))
        self.assertIn('memory: "7Ei"', self._render(self._one("limits", "memory", '"7Ei"')))

    def test_a_signed_or_exponent_quantity_renders(self):
        # The resolver treats zero and negative as unset, so they pass through; an integer
        # exponent is a quantity resource.ParseQuantity reads.
        for value, want in [('"-1Gi"', '"-1Gi"'), ('"+384Mi"', '"+384Mi"'), ('"1e3"', '"1e3"'), ("0", '"0"')]:
            with self.subTest(value=value):
                out = self._render(self._one("requests", "cpu", value))
                self.assertIn(f"cpu: {want}", out)

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
