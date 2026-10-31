"""The chart renders `spec.storage.size` from `platformAgent.storage.size`.

The default, `null`, renders no `storage` block, so the operator sizes both data
claims from its own default and a later raise of that default reaches the CR. A
set size renders `storage.size` as a string. The schema refuses a value that is
not a Kubernetes quantity, and the render refuses zero, which passes the
quantity grammar but which the operator ignores for its default, and a size
above 64Ti, the largest persistent disk.

A size also has to reach a CRD that knows the field. `helm upgrade` never applies
`crds/`, so on an install whose live CRD predates `spec.storage` the API server
prunes the block and the claims stay at the default while the release record
carries the size. The template guards that as it guards the scoped pool: `lookup`
the installed CRD and fail the render when `spec` lacks `storage`. `lookup`
returns nothing under `helm template`, so the guard is pinned as text here.

Run: python3 -m unittest discover -s tests -p 'test_chart_storage_size.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_CHART_CRD = _CHART / "crds" / "kubeagents.x-k8s.io_platformagents.yaml"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.storage.size"
_CRD_LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'
_CRD_DIG = 'dig "schema" "openAPIV3Schema" "properties" "spec" "properties" (dict) .'
_CRD_REMEDY = "kubectl apply --server-side -f charts/kube-agents/crds/"
_ZERO_MESSAGE = "reads as zero whole bytes; the data volume size must be at least one byte"
_TOO_LARGE_MESSAGE = "the data volume size must be at most 64Ti"


def _storage_block(template: str) -> str:
    """The template text from the size's `with` to the block it renders."""
    block = re.search(
        r"\{\{- with \(\.Values\.platformAgent\.storage \| default dict\)\.size \}\}\n(.*?)\n  storage:\n    size: \{\{ \. \| quote \}\}\n  \{\{- end \}\}",
        template,
        re.DOTALL,
    )
    if block is None:
        raise AssertionError("the storage block is missing from the CR template")
    return block.group(1)


class StorageSizeShapeTest(unittest.TestCase):
    """What the template, values and schema say, readable without helm."""

    def setUp(self):
        self.template = _TEMPLATE.read_text()

    def test_the_chart_default_is_null(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["platformAgent"]["storage"], {"size": None})

    def test_the_schema_closes_the_block_and_checks_the_quantity(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        storage = schema["properties"]["platformAgent"]["properties"]["storage"]
        self.assertIs(storage["additionalProperties"], False)
        self.assertEqual(set(storage["properties"]), {"size"})
        pattern = re.compile(storage["properties"]["size"]["pattern"])
        for good in ("", "80Gi", "1Ti", "51200Mi", "1.5Ti", "100G", "5e10"):
            with self.subTest(good=good):
                self.assertIsNotNone(pattern.search(good))
        for bad in ("80GB", "-1Gi", "+80Gi", "80 Gi", "Gi", "80gi"):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.search(bad))

    def test_the_crd_guard_looks_up_the_installed_crd_and_names_the_remedy(self):
        block = _storage_block(self.template)
        self.assertIn(_CRD_LOOKUP, block)
        self.assertIn(_CRD_DIG, block)
        fail = re.search(r'\{\{- if not \(hasKey \$specProps "storage"\) \}\}\n\s*\{\{- fail "([^"]*)" \}\}', block)
        self.assertIsNotNone(fail, "the CRD guard does not fail on a missing spec.storage")
        self.assertIn(_VALUE_PATH, fail.group(1))
        self.assertIn("helm upgrade does not update CRDs", fail.group(1))
        self.assertIn(_CRD_REMEDY, fail.group(1))

    def test_the_guard_reads_the_key_the_shipped_crd_declares(self):
        # The guard digs spec.properties and looks for "storage"; the CRD the
        # chart ships must declare exactly that, with size under it, or the
        # guard refuses every install that sets the value.
        crd = yaml.safe_load(_CHART_CRD.read_text())
        version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
        spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        self.assertIn("storage", spec)
        self.assertIn("size", spec["storage"]["properties"])
        # No CRD default: a default would write 50Gi into every CR, and a later
        # raise of the operator default would not reach it.
        self.assertNotIn("default", spec["storage"]["properties"]["size"])


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class StorageSizeRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True,
        )

    def _spec_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]
        self.fail("no PlatformAgent in the render")

    def test_the_default_renders_no_storage(self):
        self.assertNotIn("storage", self._spec_of(self._render()))

    def test_an_empty_or_null_value_renders_no_storage(self):
        for args in (["--set", f"{_VALUE_PATH}="],
                     ["--set-json", f"{_VALUE_PATH}=null"],
                     ["--set-json", "platformAgent.storage=null"]):
            with self.subTest(args=args):
                self.assertNotIn("storage", self._spec_of(self._render(*args)))

    def test_a_set_size_renders_it_as_a_string(self):
        for size in ("80Gi", "1Ti", "51200Mi", "64Ti", "65536Gi"):
            with self.subTest(size=size):
                spec = self._spec_of(self._render("--set", f"{_VALUE_PATH}={size}"))
                self.assertEqual(spec["storage"], {"size": size})

    def test_the_schema_refuses_a_value_that_is_not_a_quantity(self):
        for args in (["--set", f"{_VALUE_PATH}=80GB"],
                     ["--set", f"{_VALUE_PATH}=-1Gi"],
                     ["--set", f"{_VALUE_PATH}=80"],
                     ["--set", "platformAgent.storage.sizeGi=80"]):
            with self.subTest(args=args):
                proc = self._render(*args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("/platformAgent/storage", proc.stderr)

    def test_a_zero_size_fails_the_render_by_name(self):
        for size in ("0", "0Gi", "0.0Ti", "0.5"):
            with self.subTest(size=size):
                proc = self._render("--set-string", f"{_VALUE_PATH}={size}")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(_VALUE_PATH, proc.stderr)
                self.assertIn(_ZERO_MESSAGE, proc.stderr)


    def test_a_size_above_64ti_fails_the_render_by_name(self):
        for size in ("65537Gi", "65Ti", "9Ei"):
            with self.subTest(size=size):
                proc = self._render("--set-string", f"{_VALUE_PATH}={size}")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(_VALUE_PATH, proc.stderr)
                self.assertIn(_TOO_LARGE_MESSAGE, proc.stderr)


if __name__ == "__main__":
    unittest.main()
