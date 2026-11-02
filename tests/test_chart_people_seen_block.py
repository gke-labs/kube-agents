"""The chart renders `spec.telemetry.peopleSeen` from `platformAgent.telemetry.peopleSeen`.

The chart default is true, which renders nothing: the CRD defaults the field to
true. Only false renders, behind the shared CRD guard, because an installed CRD
that predates the field would prune it while the release record keeps it.
`lookup` is empty under `helm template`, so the guard is pinned as text; the
render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_people_seen_block.py' -v
"""

import pathlib
import shutil
import subprocess
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
_GUARD = ('include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.telemetry.peopleSeen" '
          '"path" (list "telemetry") "field" "peopleSeen")')
_ENDPOINT = "http://collector.example:4317"


class PeopleSeenGuardTest(unittest.TestCase):
    def setUp(self):
        self.template = _TEMPLATE.read_text(encoding="utf-8")

    def test_the_switch_is_guarded_by_the_shared_crd_guard(self):
        self.assertIn(_GUARD, self.template)

    def test_only_false_is_rendered(self):
        self.assertRegex(self.template, r'eq \(toString .*peopleSeen\) "false"')

    def test_the_chart_default_is_true(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIs(values["platformAgent"]["telemetry"]["peopleSeen"], True)


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class PeopleSeenRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )

    def _spec_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]
        self.fail("no PlatformAgent in the render")

    def test_the_default_renders_no_telemetry_block(self):
        self.assertNotIn("telemetry", self._spec_of(self._render()))

    def test_a_null_telemetry_object_renders_no_telemetry_block(self):
        spec = self._spec_of(self._render("--set-json", "platformAgent.telemetry=null"))
        self.assertNotIn("telemetry", spec)

    def test_true_renders_no_telemetry_block(self):
        spec = self._spec_of(self._render("--set", "platformAgent.telemetry.peopleSeen=true"))
        self.assertNotIn("telemetry", spec)

    def test_false_renders_the_field(self):
        spec = self._spec_of(self._render("--set", "platformAgent.telemetry.peopleSeen=false"))
        self.assertEqual(spec["telemetry"], {"peopleSeen": False})

    def test_the_endpoint_alone_renders_as_before(self):
        spec = self._spec_of(self._render("--set", f"telemetry.otlpEndpoint={_ENDPOINT}"))
        self.assertEqual(spec["telemetry"], {"otlpEndpoint": _ENDPOINT})

    def test_the_endpoint_and_the_switch_share_the_block(self):
        proc = self._render("--set", f"telemetry.otlpEndpoint={_ENDPOINT}",
                            "--set", "platformAgent.telemetry.peopleSeen=false")
        spec = self._spec_of(proc)
        self.assertEqual(spec["telemetry"], {"otlpEndpoint": _ENDPOINT, "peopleSeen": False})
        self.assertEqual(proc.stdout.count("\n  telemetry:\n"), 1)

    def test_the_schema_refuses_an_unknown_telemetry_key(self):
        proc = self._render("--set", "platformAgent.telemetry.other=1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("other", proc.stderr)


if __name__ == "__main__":
    unittest.main()
