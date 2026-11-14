"""The chart renders no spec.mode on the PlatformAgent.

Every install runs the A2A stack, so the CR template writes no `mode` field and
values.yaml declares no `platformAgent.mode`. The values schema still declares the key,
deprecated and ignored, because the schema closes `platformAgent` to the keys it lists
and every release of an earlier chart recorded `platformAgent.mode: next` in its values:
an upgrade that re-applies those values would otherwise fail schema validation. A
leftover value renders the same manifests as the default, byte for byte, and a value
outside what the key ever accepted is still refused.

`platformAgent.harness.tuning.maxSessions` rides along: it caps the A2A session pods and
renders only when set, the way maxInProgress does.

The text tests run everywhere; the render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_platform_agent_no_mode.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_VALUES = _CHART / "values.yaml"
_SCHEMA = _CHART / "values.schema.json"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
# What the deprecated key accepted while it chose the stack; it keeps accepting exactly that.
_LEFTOVER_VALUES = (None, "", "today", "next")


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


class NoModeTemplateShapeTest(unittest.TestCase):
    """What the chart says, readable without helm."""

    def test_the_template_never_writes_a_mode(self):
        self.assertNotIn(".Values.platformAgent.mode", _TEMPLATE.read_text())
        self.assertNotIn("\n  mode:", _TEMPLATE.read_text())

    def test_values_yaml_declares_no_mode(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertNotIn("mode", values["platformAgent"])
        self.assertNotIn("maxSessions", values["platformAgent"]["harness"]["tuning"])

    def test_the_schema_keeps_the_deprecated_key_and_says_so(self):
        schema = json.loads(_SCHEMA.read_text())
        platform_agent = schema["properties"]["platformAgent"]
        # The reason the key stays: the schema refuses keys it does not list, and earlier
        # releases recorded the key in their values.
        self.assertIs(platform_agent["additionalProperties"], False)
        mode = platform_agent["properties"]["mode"]
        self.assertEqual(mode["enum"], list(_LEFTOVER_VALUES))
        self.assertIn("eprecated", mode["description"])
        self.assertIn("ignored", mode["description"])

    def test_no_template_reads_the_mode(self):
        for template in sorted((_CHART / "templates").iterdir()):
            with self.subTest(template=template.name):
                self.assertNotIn("platformAgent.mode", template.read_text())


@unittest.skipUnless(shutil.which("helm"), "needs a helm binary")
class CrdDefaultTest(unittest.TestCase):
    """With the chart writing no spec.mode, the CRD default is what persists `next`
    on a fresh object, and a rollback to a release that reads the field leans on it."""

    def test_both_crd_copies_default_the_mode_to_next(self):
        for path in (
            _CHART / "crds" / "kubeagents.x-k8s.io_platformagents.yaml",
            _REPO_ROOT / "k8s-operator" / "config" / "crd" / "bases" / "kubeagents.x-k8s.io_platformagents.yaml",
        ):
            with self.subTest(crd=path.relative_to(_REPO_ROOT).as_posix()):
                crd = yaml.safe_load(path.read_text())
                version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
                mode = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["mode"]
                self.assertEqual(mode.get("default"), "next")

    def test_the_type_declares_the_default(self):
        text = (_REPO_ROOT / "k8s-operator" / "api" / "v1alpha1" / "platformagent_types.go").read_text()
        self.assertIn("+kubebuilder:default=next", text)


class NoModeRenderTest(unittest.TestCase):
    def setUp(self):
        default = _render()
        self.assertEqual(default.returncode, 0, default.stderr)
        self.default = default.stdout

    def test_the_cr_carries_no_mode(self):
        self.assertNotIn("mode", _cr(self.default)["spec"])

    def test_a_leftover_mode_renders_the_default_byte_for_byte(self):
        for value in _LEFTOVER_VALUES:
            spelling = "platformAgent.mode=null" if value is None else f"platformAgent.mode={value}"
            with self.subTest(spelling=spelling):
                out = _render("--set", spelling)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(out.stdout, self.default)

    def test_anything_else_is_refused_before_it_renders(self):
        for value in ("Next", "TODAY", "later", "true"):
            with self.subTest(value=value):
                out = _render("--set-string", f"platformAgent.mode={value}")
                self.assertNotEqual(out.returncode, 0)
                # Helm versions spell the schema path differently; the key is in both.
                self.assertIn("platformAgent", out.stderr)
                self.assertIn("mode", out.stderr)

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


if __name__ == "__main__":
    unittest.main()
