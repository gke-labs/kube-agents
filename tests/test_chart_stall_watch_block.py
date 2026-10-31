"""The chart renders `spec.harness.stallWatch` from `platformAgent.harness.stallWatch`.

The chart default is an empty map, which renders no block: the operator pins
an empty value for each knob when the CR has none, and that is the watch's
own default. A field renders only when it carries a value; an empty list is no
value, and `minStallAgeMinutes: 0` is one. The schema holds the CRD's bounds,
so a value the API server would refuse fails the render instead. The
text-structural tests run everywhere; the render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_stall_watch_block.py' -v
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

POPULATED = {
    "excludeNamespaces": ["load-test", "payments-canary"],
    "minStallAgeMinutes": 30,
    "kinds": ["deployments", "gateways.gateway.networking.k8s.io"],
}


class StallWatchBlockShapeTest(unittest.TestCase):
    """What the template and the values say, readable without helm."""

    def test_the_chart_default_is_an_empty_map(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["platformAgent"]["harness"]["stallWatch"], {})

    def test_the_block_is_guarded_against_a_crd_without_the_field(self):
        # helm upgrade does not apply crds/, so an older CRD would prune the
        # block while the release record keeps it.
        template = _TEMPLATE.read_text()
        self.assertIn('{{- if not (hasKey $harnessProps "stallWatch") }}', template)
        self.assertIn("platformAgent.harness.stallWatch is set, but the installed PlatformAgent CRD predates", template)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class StallWatchBlockRenderTest(unittest.TestCase):
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
                return document["spec"]["harness"].get("stallWatch", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, block):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"platformAgent": {"harness": {"stallWatch": block}}}, handle)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_block(self):
        self.assertEqual(self._block_of(self._render()), "ABSENT")

    def test_empty_values_render_no_block(self):
        empty = {"excludeNamespaces": [], "minStallAgeMinutes": None, "kinds": []}
        self.assertEqual(self._block_of(self._render("-f", self._values_file(empty))), "ABSENT")

    def test_a_populated_value_reaches_the_block_verbatim(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file(POPULATED))), POPULATED)

    def test_a_zero_minimum_age_is_a_value(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file({"minStallAgeMinutes": 0}))), {"minStallAgeMinutes": 0})

    def test_a_kind_that_only_starts_like_a_refused_one_renders(self):
        kinds = {"kinds": ["eventsources.argoproj.io", "secretstores.external-secrets.io", "leasesets"]}
        self.assertEqual(self._block_of(self._render("-f", self._values_file(kinds))), kinds)

    def test_all_renders_as_the_one_kind(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file({"kinds": ["all"]}))), {"kinds": ["all"]})

    def test_values_the_crd_refuses_fail_the_render(self):
        for bad in (
            {"minStallAgeMinutes": 1441},
            {"minStallAgeMinutes": -1},
            {"excludeNamespaces": ["Payments"]},
            {"excludeNamespaces": ["tenant-*"]},
            {"kinds": ["Deployments"]},
            {"kinds": ["deployments,jobs"]},
            {"kinds": ["all", "deployments"]},
            {"kinds": ["deployments", "secrets"]},
            {"kinds": ["leases"]},
            {"kinds": ["endpointslices"]},
            {"kinds": ["controllerrevisions"]},
            {"kinds": ["secrets.v1"]},
            {"kinds": ["configmaps.v1"]},
            {"kinds": ["leases.v1.coordination.k8s.io"]},
            {"kinds": ["deployments", "deployments"]},
            {"excludeNamespaces": ["payments", "payments"]},
            {"unknown": True},
        ):
            with self.subTest(bad=bad):
                proc = self._render("-f", self._values_file(bad))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("stallWatch", proc.stderr)


if __name__ == "__main__":
    unittest.main()
