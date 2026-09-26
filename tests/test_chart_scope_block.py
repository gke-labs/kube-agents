"""The chart renders `spec.scope` from `platformAgent.scope`, in three states.

null, the chart default, renders no block: the CR declares nothing, and a scope
the CR already carries is left alone while no earlier revision rendered the
block, because Helm patches a custom resource from the difference between its
rendered manifests (a render without the block after one with it removes the
field, which the reconcile reads as no declaration). A map renders the block as
it is, empty lists included, because the reconcile reads an emptied projects
list as the declaration that drops projects and a missing block as no
declaration (docs/designs/multi-project-scope.md §7); the composition always
passes one. The text-structural tests run everywhere; the render tests need a
helm binary, which the agent-startup job lacks.

Run: python3 -m unittest discover -s tests -p 'test_chart_scope_block.py' -v
"""

import pathlib
import re
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
    "projects": ["payments-prod", "payments-staging"],
    "exclude": {
        "projects": ["*-sandbox"],
        "clusters": [
            {"projectId": "payments-staging", "location": "us-central1", "clusterName": "scratch-cluster"},
        ],
    },
}
EMPTY = {"projects": [], "exclude": {"projects": [], "clusters": []}}


class ScopeBlockShapeTest(unittest.TestCase):
    """What the template says, readable without helm."""

    def setUp(self):
        self.template = _TEMPLATE.read_text()

    def test_the_block_is_gated_on_a_present_map_and_nothing_else(self):
        # `with` would drop an empty map; hasKey is true for the null default.
        # Neither may gate the block.
        block = re.search(r"\{\{- \$scope := \.Values\.platformAgent\.scope \}\}\n(.*?)\{\{- end \}\}",
                          self.template, re.DOTALL)
        self.assertIsNotNone(block, "the scope block is missing from the CR template")
        body = block.group(1)
        self.assertIn('{{- if kindIs "map" $scope }}', body)
        self.assertNotIn("compactFields", body)
        self.assertNotIn("{{- with", body)

    def test_every_list_renders_even_when_empty(self):
        for key in ("projects: {{ $scope.projects | default list | toJson }}",
                    "projects: {{ $scopeExclude.projects | default list | toJson }}",
                    "clusters: {{ $scopeExclude.clusters | default list | toJson }}"):
            with self.subTest(line=key):
                self.assertIn(key, self.template)

    def test_the_chart_default_is_null(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIn("scope", values["platformAgent"])
        self.assertIsNone(values["platformAgent"]["scope"])


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ScopeBlockRenderTest(unittest.TestCase):
    def _render(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True,
        )
        return proc

    def _scope_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"].get("scope", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, scope):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"platformAgent": {"scope": scope}}, handle)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_block(self):
        self.assertEqual(self._scope_of(self._render()), "ABSENT")

    def test_an_empty_map_renders_a_present_block_with_empty_lists(self):
        for args in (["--set-json", "platformAgent.scope={}"],
                     ["-f", self._values_file({})],
                     ["-f", self._values_file(EMPTY)]):
            with self.subTest(args=args):
                self.assertEqual(self._scope_of(self._render(*args)), EMPTY)

    def test_a_populated_value_reaches_the_block_verbatim(self):
        self.assertEqual(self._scope_of(self._render("-f", self._values_file(POPULATED))), POPULATED)

    def test_a_null_set_by_the_caller_renders_no_block(self):
        # A null deletes the key from the coalesced values, the same as never
        # setting it: absent block, nothing declared.
        proc = self._render("-f", self._values_file(POPULATED), "--set-json", "platformAgent.scope=null")
        self.assertEqual(self._scope_of(proc), "ABSENT")

    def test_an_unknown_key_under_scope_is_refused_by_the_schema(self):
        proc = self._render("--set", "platformAgent.scope.folders={123}")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("scope", proc.stderr)
