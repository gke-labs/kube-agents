"""The chart renders `spec.harness.retention` from `platformAgent.harness.retention`.

The chart default is an empty map, which renders no block: the operator renders
its own default retention (defaultSessionRetentionDays) when the CR has none.
`sessionDays` renders only when set. The schema holds the CRD's bounds
(90-365), so a value the API server would refuse fails the render instead. The
text-structural tests run everywhere; the render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_retention_block.py' -v
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


class RetentionBlockShapeTest(unittest.TestCase):
    """What the template and the values say, readable without helm."""

    def test_the_chart_default_is_an_empty_map(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["platformAgent"]["harness"]["retention"], {})

    def test_the_block_is_guarded_against_a_crd_without_the_field(self):
        # helm upgrade does not apply crds/, so an older CRD would prune the
        # block while the release record keeps it.
        template = _TEMPLATE.read_text()
        self.assertIn('{{- if not (hasKey $harnessProps "retention") }}', template)
        self.assertIn("platformAgent.harness.retention is set, but the installed PlatformAgent CRD predates", template)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class RetentionBlockRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )

    def _block_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]["harness"].get("retention", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, block):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"platformAgent": {"harness": {"retention": block}}}, handle)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_block(self):
        self.assertEqual(self._block_of(self._render()), "ABSENT")

    def test_an_unset_day_count_renders_no_block(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file({"sessionDays": None}))), "ABSENT")

    def test_a_set_day_count_reaches_the_block(self):
        for days in (90, 180, 365):
            with self.subTest(days=days):
                block = self._block_of(self._render("-f", self._values_file({"sessionDays": days})))
                self.assertEqual(block, {"sessionDays": days})

    def test_values_the_crd_refuses_fail_the_render(self):
        for bad in ({"sessionDays": 0}, {"sessionDays": 89}, {"sessionDays": 366}, {"sessionDays": "7"}, {"unknown": True}):
            with self.subTest(bad=bad):
                proc = self._render("-f", self._values_file(bad))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("retention", proc.stderr)


if __name__ == "__main__":
    unittest.main()
