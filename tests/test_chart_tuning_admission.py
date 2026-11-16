"""The chart renders `spec.harness.tuning.admission` from `platformAgent.harness.tuning.admission`.

The chart default renders no block: the operator renders `kanban.admission` with
its own defaults when the CR carries none, so an install that sets nothing is
unchanged by the key's existence. The block passes through as a map, the way
`profiles` does, and the schema holds the CRD's bounds, so a value the API server
would refuse fails the render instead. The shape tests run everywhere; the render
tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_tuning_admission.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_SCHEMA = _CHART / "values.schema.json"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
# The CRD's bounds on each field (k8s-operator/api/v1alpha1/common_types.go,
# AdmissionSpec); the schema repeats them so `helm template` refuses what the
# API server would.
_BOUNDS = {
    "memoryBudgetMiB": (512, 65536),
    "workerRssCapMiB": (256, 65536),
    "queueBoundSeconds": (10, 3600),
}
_FULL_BLOCK = {
    "memoryBudgetMiB": 3072,
    "workerRssCapMiB": 1024,
    "queueBoundSeconds": 300,
    "failClosed": False,
}


class AdmissionBlockShapeTest(unittest.TestCase):
    """What the template and the schema say, readable without helm."""

    def test_the_block_passes_through_as_a_map(self):
        template = _TEMPLATE.read_text()
        self.assertIn("{{- with $tuning.admission }}", template)
        # The gate that decides whether `tuning:` is rendered at all has to see
        # the key, or a values file that sets only admission renders nothing.
        gate = next(line for line in template.splitlines() if 'hasKey $tuning "maxInProgress"' in line)
        self.assertIn("$tuning.admission", gate)

    def test_the_block_is_guarded_against_a_crd_without_the_field(self):
        # helm upgrade does not apply crds/, so an older CRD would prune the
        # block while the release record keeps it.
        template = _TEMPLATE.read_text()
        self.assertIn('{{- if not (hasKey $tuningProps "admission") }}', template)
        self.assertIn("platformAgent.harness.tuning.admission is set, but the installed PlatformAgent CRD predates", template)

    def test_the_schema_holds_the_crd_bounds(self):
        schema = json.loads(_SCHEMA.read_text())
        tuning = schema["properties"]["platformAgent"]["properties"]["harness"]["properties"]["tuning"]
        admission = tuning["properties"]["admission"]
        self.assertFalse(admission["additionalProperties"])
        for field, (minimum, maximum) in _BOUNDS.items():
            with self.subTest(field=field):
                self.assertEqual(admission["properties"][field]["type"], "integer")
                self.assertEqual(admission["properties"][field]["minimum"], minimum)
                self.assertEqual(admission["properties"][field]["maximum"], maximum)
        self.assertEqual(admission["properties"]["failClosed"], {"type": "boolean"})


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class AdmissionBlockRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )

    def _tuning_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]["harness"].get("tuning", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, tuning):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump({"platformAgent": {"harness": {"tuning": tuning}}}, handle)
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_tuning(self):
        self.assertEqual(self._tuning_of(self._render()), "ABSENT")

    def test_admission_alone_renders_the_block(self):
        tuning = self._tuning_of(self._render("-f", self._values_file({"admission": _FULL_BLOCK})))
        self.assertEqual(tuning, {"admission": _FULL_BLOCK})

    def test_a_partial_block_renders_only_what_is_set(self):
        tuning = self._tuning_of(self._render("-f", self._values_file({"admission": {"workerRssCapMiB": 2048}})))
        self.assertEqual(tuning, {"admission": {"workerRssCapMiB": 2048}})

    def test_admission_renders_beside_the_other_tuning_keys(self):
        tuning = self._tuning_of(self._render(
            "-f", self._values_file({"maxInProgress": 3, "admission": {"memoryBudgetMiB": 4096}}),
        ))
        self.assertEqual(tuning, {"maxInProgress": 3, "admission": {"memoryBudgetMiB": 4096}})

    def test_values_the_crd_refuses_fail_the_render(self):
        for field, (minimum, maximum) in _BOUNDS.items():
            for value in (minimum - 1, maximum + 1):
                with self.subTest(field=field, value=value):
                    proc = self._render("-f", self._values_file({"admission": {field: value}}))
                    self.assertNotEqual(proc.returncode, 0, proc.stdout)
                    self.assertIn(field, proc.stderr)

    def test_an_unknown_key_fails_the_render(self):
        proc = self._render("-f", self._values_file({"admission": {"memoryBudgetMib": 2048}}))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("memoryBudgetMib", proc.stderr)


if __name__ == "__main__":
    unittest.main()
