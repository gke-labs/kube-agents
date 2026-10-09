"""The chart renders the PlatformAgent's spec.mode from `platformAgent.mode`.

null, the default, renders no field: the operator reads an absent mode as
"today", and an install that never sets the value must render the CR it
rendered before the chart knew the key, byte for byte, or every existing
install's CR is patched on its next upgrade. Helm patches a custom resource
from the difference between its renders, so leaving the field out is also what
keeps a mode set on the CR by hand with kubectl in place across an upgrade.
"today" and "next" render as themselves; anything else is refused by the
values schema, the CRD's own enum brought forward to `helm template`.

`platformAgent.harness.tuning.maxSessions` rides along because the next lane
(hack/ci-deploy.sh, step 6b) sets the mode and the A2A session cap in one
upgrade, so the first next render sees both; it renders only when set, the way
maxInProgress does.

The text tests run everywhere; the render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_platform_agent_mode.py' -v
"""

import difflib
import json
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
_VALUES = _CHART / "values.yaml"
_SCHEMA = _CHART / "values.schema.json"
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


def _render(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "kube-agents", str(_CHART), "--namespace", "kubeagents-system", *_REQUIRED, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _cr(stdout: str) -> dict:
    crs = [d for d in yaml.safe_load_all(stdout) if d and d.get("kind") == "PlatformAgent"]
    if len(crs) != 1:
        raise AssertionError(f"expected one PlatformAgent, rendered {len(crs)}")
    return crs[0]


class ModeTemplateShapeTest(unittest.TestCase):
    """What the chart says, readable without helm."""

    def test_the_field_is_gated_on_a_set_value(self):
        # `with`, not `if hasKey` or a default: the null default and "" must
        # render nothing, and nothing may supply "today" on the chart's behalf.
        template = _TEMPLATE.read_text()
        self.assertIn('{{- with .Values.platformAgent.mode }}\n  mode: {{ . | quote }}\n  {{- end }}', template)
        self.assertNotIn('.Values.platformAgent.mode | default', template)

    def test_the_default_is_null(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertIn("mode", values["platformAgent"])
        self.assertIsNone(values["platformAgent"]["mode"])
        self.assertNotIn("maxSessions", values["platformAgent"]["harness"]["tuning"])

    def test_the_schema_admits_the_crd_enum_and_unset_only(self):
        schema = json.loads(_SCHEMA.read_text())
        mode = schema["properties"]["platformAgent"]["properties"]["mode"]
        self.assertEqual(mode, {"enum": [None, "", "today", "next"]})
        # The set values are the CRD's spec.mode enum, read from the CRD the
        # chart ships, so a third mode added there fails here until the schema
        # admits it too.
        crd = yaml.safe_load((_CHART / "crds" / "kubeagents.x-k8s.io_platformagents.yaml").read_text())
        version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
        crd_mode = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["mode"]
        self.assertEqual([m for m in mode["enum"] if m], crd_mode["enum"])


@unittest.skipUnless(shutil.which("helm"), "needs a helm binary")
class ModeRenderTest(unittest.TestCase):
    def setUp(self):
        default = _render()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.default = default.stdout

    def test_unset_renders_no_mode(self):
        self.assertNotIn("mode", _cr(self.default)["spec"])

    def test_null_and_empty_render_the_default_byte_for_byte(self):
        for spelling in ("platformAgent.mode=null", "platformAgent.mode="):
            with self.subTest(spelling=spelling):
                out = _render("--set", spelling)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout, self.default)

    def test_a_set_mode_adds_exactly_its_own_line(self):
        for mode in ("today", "next"):
            with self.subTest(mode=mode):
                out = _render("--set", f"platformAgent.mode={mode}")
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(_cr(out.stdout)["spec"]["mode"], mode)
                changed = [
                    line
                    for line in difflib.unified_diff(self.default.splitlines(), out.stdout.splitlines(), lineterm="", n=0)
                    if line[:1] in "+-" and not line.startswith(("+++", "---"))
                ]
                self.assertEqual(changed, [f'+  mode: "{mode}"'])

    def test_anything_else_is_refused_before_it_renders(self):
        for value in ("Next", "TODAY", "later", "true"):
            with self.subTest(value=value):
                out = _render("--set-string", f"platformAgent.mode={value}")
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("/platformAgent/mode", out.stderr)

    def test_max_sessions_renders_only_when_set(self):
        self.assertNotIn("tuning", _cr(self.default)["spec"]["harness"])
        out = _render("--set", "platformAgent.harness.tuning.maxSessions=6")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(_cr(out.stdout)["spec"]["harness"]["tuning"], {"maxSessions": 6})
        both = _render(
            "--set", "platformAgent.harness.tuning.maxSessions=6",
            "--set", "platformAgent.harness.tuning.maxInProgress=3",
        )
        self.assertEqual(both.returncode, 0, both.stderr)
        self.assertEqual(_cr(both.stdout)["spec"]["harness"]["tuning"], {"maxInProgress": 3, "maxSessions": 6})

    def test_max_sessions_outside_the_crd_bounds_is_refused(self):
        for value in ("0", "10001"):
            with self.subTest(value=value):
                out = _render("--set", f"platformAgent.harness.tuning.maxSessions={value}")
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("maxSessions", out.stderr)


@unittest.skipUnless(shutil.which("helm"), "needs a helm binary")
class CiDeployDocumentRenderTest(unittest.TestCase):
    """The values document hack/ci-deploy.sh's next lane hands `helm upgrade`
    (MODE_NEXT_HELM_VALUES_FORMAT, at the cap section 2b computes for the
    presubmit's four workers) renders spec.mode: next and the cap on the CR,
    so the lane's smoke exercises this value and not a patch."""

    def test_the_document_renders_spec_mode_next_and_the_cap(self):
        match = re.search(r"^readonly MODE_NEXT_HELM_VALUES_FORMAT='(.*)'$", _CI_DEPLOY.read_text(), re.MULTILINE)
        self.assertIsNotNone(match, "MODE_NEXT_HELM_VALUES_FORMAT is not in hack/ci-deploy.sh")
        with tempfile.TemporaryDirectory() as tmp:
            values_file = pathlib.Path(tmp) / "mode-next.json"
            values_file.write_text(match.group(1) % 6 + "\n")
            out = _render("--values", str(values_file))
        self.assertEqual(out.returncode, 0, out.stderr)
        spec = _cr(out.stdout)["spec"]
        self.assertEqual(spec["mode"], "next")
        self.assertEqual(spec["harness"]["tuning"], {"maxSessions": 6})


if __name__ == "__main__":
    unittest.main()
