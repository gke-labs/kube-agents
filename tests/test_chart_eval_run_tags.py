"""The chart renders `platformAgent.deployment.evalRunTags` into the CR's env.

`true` becomes one `KUBEAGENTS_EVAL_RUN_TAGS=1` entry in `spec.deployment.env`;
`false`, the default, renders nothing, so a default render is unchanged. An
literal entry with the same name in `platformAgent.deployment.env` wins; a
`valueFrom` one with the key `true` fails the render. The schema
refuses anything but a boolean.

Run: python3 -m unittest discover -s tests -p 'test_chart_eval_run_tags.py' -v
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
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
    "--set", "platformAgent.deployment.incidentWorkloadDedupSeconds=null",
]
_NAME = "KUBEAGENTS_EVAL_RUN_TAGS"
_VALUE_FROM = {"secretKeyRef": {"name": "eval-flags", "key": "tags"}}


def _values(flag=None, env=None):
    deployment = {}
    if flag is not None:
        deployment["evalRunTags"] = flag
    if env is not None:
        deployment["env"] = env
    return {"platformAgent": {"deployment": deployment}}


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    def test_values_yaml_carries_false(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertIs(values["platformAgent"]["deployment"]["evalRunTags"], False)

    def test_the_schema_types_the_key(self):
        schema = json.loads(_SCHEMA.read_text())
        node = schema["properties"]["platformAgent"]["properties"]["deployment"]["properties"]
        self.assertEqual(node["evalRunTags"], {"type": "boolean"})


@unittest.skipUnless(_HELM, "helm is not installed")
class EvalRunTagsRenderTest(unittest.TestCase):
    def _render(self, values=None):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            yaml.safe_dump(values or {}, fh)
            fh.flush()
            return subprocess.run(
                [_HELM, "template", "r", str(_CHART), "-s", _CR_TEMPLATE, *_REQUIRED, "-f", fh.name],
                capture_output=True, text=True,
            )

    def _deployment(self, values=None):
        res = self._render(values)
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = [d for d in yaml.safe_load_all(res.stdout) if d and d.get("kind") == "PlatformAgent"]
        self.assertEqual(len(docs), 1, res.stdout)
        return docs[0]["spec"]["deployment"]

    def test_default_and_false_render_no_env(self):
        self.assertNotIn("env", self._deployment())
        self.assertNotIn("env", self._deployment(_values(False)))

    def test_true_renders_one_string_entry(self):
        self.assertEqual(self._deployment(_values(True))["env"], [{"name": _NAME, "value": "1"}])

    def test_an_explicit_env_entry_wins(self):
        deployment = self._deployment(_values(True, [{"name": _NAME, "value": "0"}]))
        self.assertEqual(deployment["env"], [{"name": _NAME, "value": "0"}])

    def test_a_value_from_entry_with_the_key_true_fails_the_render(self):
        env = [{"name": _NAME, "valueFrom": _VALUE_FROM}]
        res = self._render(_values(True, env))
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("evalRunTags is true", res.stderr)

    def test_a_value_from_entry_with_the_key_false_renders(self):
        env = [{"name": _NAME, "valueFrom": _VALUE_FROM}]
        self.assertEqual(self._deployment(_values(False, env))["env"], env)

    def test_the_schema_refuses_a_non_boolean(self):
        for flag in ("true", 1):
            with self.subTest(flag=flag):
                res = self._render(_values(flag))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("evalRunTags", res.stderr)


if __name__ == "__main__":
    unittest.main()
