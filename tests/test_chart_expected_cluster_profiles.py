"""The chart renders `spec.storage.expectedClusterProfiles` from `platformAgent.storage.expectedClusterProfiles`.

The default, `null`, renders no field. A set count renders as an integer, alone or
beside `size`. The schema holds the CRD's bounds (1-20000). A count also needs a
CRD that knows the field: `helm upgrade` never applies `crds/`, and a CRD can have
`spec.storage` without it, so the template has its own guard, pinned as text here
because `lookup` is empty under `helm template`.

The quota preflight counts the agent's claim at the operator's formula, so it
carries the operator's two budget constants. The last test pins them equal to
`dataVolumeBaseSize` and `perClusterProfileStorage` in the controller.

Run: python3 -m unittest discover -s tests -p 'test_chart_expected_cluster_profiles.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_GO_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.storage.expectedClusterProfiles"
_UNITS = {"Gi": 1024**3, "Mi": 1024**2}


def _go_bytes(name: str) -> int:
    match = re.search(rf'^\s*{name}\s*=\s*"(\d+)(Gi|Mi)"$', _GO_MANIFESTS.read_text(), re.MULTILINE)
    if match is None:
        raise AssertionError(f"{name} is not a Gi/Mi string constant in platformagent_manifests.go")
    return int(match.group(1)) * _UNITS[match.group(2)]


class ExpectedClusterProfilesShapeTest(unittest.TestCase):
    def test_the_chart_default_is_null(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIsNone(values["platformAgent"]["storage"]["expectedClusterProfiles"])

    def test_the_count_has_its_own_crd_guard(self):
        template = _TEMPLATE.read_text()
        self.assertIn('(dig "storage" "properties" (dict) $specProps) "expectedClusterProfiles"', template)
        self.assertIn("platformAgent.storage.expectedClusterProfiles needs a PlatformAgent CRD that has", template)

    def test_the_preflight_budget_equals_the_operator_constants(self):
        helpers = _HELPERS.read_text()
        match = re.search(r"\$need := add (\d+) \(mul \(\. \| int64\) (\d+)\)", helpers)
        self.assertIsNotNone(match, "the quota preflight's formula line moved; update this test with it")
        self.assertEqual(int(match.group(1)), _go_bytes("dataVolumeBaseSize"))
        self.assertEqual(int(match.group(2)), _go_bytes("perClusterProfileStorage"))


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ExpectedClusterProfilesRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )

    def _storage_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"].get("storage", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def test_the_default_renders_no_storage(self):
        self.assertEqual(self._storage_of(self._render()), "ABSENT")

    def test_a_count_renders_as_an_integer(self):
        for count in (1, 200, 20000):
            with self.subTest(count=count):
                storage = self._storage_of(self._render("--set", f"{_VALUE_PATH}={count}"))
                self.assertEqual(storage, {"expectedClusterProfiles": count})

    def test_a_count_and_a_size_render_together(self):
        storage = self._storage_of(self._render("--set", f"{_VALUE_PATH}=200", "--set-string",
                                                "platformAgent.storage.size=100Gi"))
        self.assertEqual(storage, {"size": "100Gi", "expectedClusterProfiles": 200})

    def test_values_the_crd_refuses_fail_the_render(self):
        for bad in ("0", "20001", "-5"):
            with self.subTest(bad=bad):
                proc = self._render("--set", f"{_VALUE_PATH}={bad}")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("expectedClusterProfiles", proc.stderr)


if __name__ == "__main__":
    unittest.main()
