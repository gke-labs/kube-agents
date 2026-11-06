"""The chart renders `platformAgent.deployment.incidentWorkloadDedupSeconds` into the CR's env.

The key becomes the `INCIDENT_WORKLOAD_DEDUP_SECONDS` entry in `spec.deployment.env`,
with a string value. It defaults to 3600, so a default render carries that one entry;
`0` turns folding off and renders as `"0"`; `null` renders no entry and leaves the
operator's own default. An entry with the same name in `platformAgent.deployment.env`
wins, so the name occurs one time only: the CRD keys env on name, and the API server
refuses a duplicate. A `valueFrom` entry for the name fails the render while the key is
set, because the operator forwards only literal values. The schema refuses a negative
number, a number above a day, a fraction and a string.

Run: python3 -m unittest discover -s tests -p 'test_chart_incident_workload_dedup.py' -v
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
]
_KEY = "incidentWorkloadDedupSeconds"
_ENV_NAME = "INCIDENT_WORKLOAD_DEDUP_SECONDS"
# The default the chart, the operator (platformagent_manifests.go) and the daemon
# (session_kv_server.py) agree on, and the schema's ceiling: a day, the daemon's cap.
_DEFAULT_SECONDS = 3600
_MAX_SECONDS = 86400
_DEFAULT_ENTRY = {"name": _ENV_NAME, "value": str(_DEFAULT_SECONDS)}
_VALUE_FROM = {"configMapKeyRef": {"name": "windows", "key": "fold"}}
# hack/ci-deploy.sh sets the window to 150 s through the env list, as it always did.
_CI_DEPLOY_FLAGS = [
    "--set-string", "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING",
    "--set-string", "platformAgent.deployment.env[0].value=0",
    "--set-string", "platformAgent.deployment.env[1].name=ALERT_DAILY_LIMIT_DRIFT",
    "--set-string", "platformAgent.deployment.env[1].value=0",
    "--set-string", f"platformAgent.deployment.env[2].name={_ENV_NAME}",
    "--set-string", "platformAgent.deployment.env[2].value=150",
]


def _values(seconds="unset", env=None):
    deployment = {}
    if seconds != "unset":
        deployment[_KEY] = seconds
    if env is not None:
        deployment["env"] = env
    return {"platformAgent": {"deployment": deployment}}


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared at the default and bounded."""

    def test_values_yaml_defaults_to_an_hour(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"][_KEY], _DEFAULT_SECONDS)

    def test_the_schema_bounds_the_key_to_a_day(self):
        schema = json.loads(_SCHEMA.read_text())
        node = schema["properties"]["platformAgent"]["properties"]["deployment"]["properties"][_KEY]
        self.assertEqual(node, {"type": ["integer", "null"], "minimum": 0, "maximum": _MAX_SECONDS})

    def test_the_operator_and_daemon_carry_the_same_default(self):
        manifests = (_REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go").read_text()
        # gofmt may re-pad the `=` column, so the spacing is not pinned.
        self.assertRegex(manifests, rf'incidentWorkloadDedupDefaultSeconds\s*=\s*"{_DEFAULT_SECONDS}"')
        daemon = (_REPO_ROOT / "agents" / "platform" / "scripts" / "session_kv_server.py").read_text()
        self.assertIn(f"INCIDENT_WORKLOAD_DEDUP_DEFAULT_SECONDS = {_DEFAULT_SECONDS}\n", daemon)
        self.assertIn(f"INCIDENT_WORKLOAD_DEDUP_MAX_SECONDS = {_MAX_SECONDS}\n", daemon)


@unittest.skipUnless(_HELM, "helm is not installed")
class IncidentWorkloadDedupRenderTest(unittest.TestCase):
    def _render(self, values=None, extra_args=()):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            # A values file, unlike `--set`, gives the chart float64 numbers and nulls.
            yaml.safe_dump(values or {}, fh)
            fh.flush()
            return subprocess.run(
                [_HELM, "template", "r", str(_CHART), "-s", _CR_TEMPLATE, *_REQUIRED,
                 "-f", fh.name, *extra_args],
                capture_output=True, text=True,
            )

    def _deployment(self, values=None, extra_args=()):
        res = self._render(values, extra_args)
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = [d for d in yaml.safe_load_all(res.stdout) if d and d.get("kind") == "PlatformAgent"]
        self.assertEqual(len(docs), 1, res.stdout)
        return docs[0]["spec"]["deployment"]

    def test_a_default_render_carries_the_hour(self):
        self.assertEqual(self._deployment()["env"], [_DEFAULT_ENTRY])

    def test_zero_renders_as_zero(self):
        self.assertEqual(self._deployment(_values(0))["env"], [{"name": _ENV_NAME, "value": "0"}])

    def test_null_renders_no_entry(self):
        self.assertNotIn("env", self._deployment(_values(None)))

    def test_the_maximum_renders_in_digits(self):
        deployment = self._deployment(_values(_MAX_SECONDS))
        self.assertEqual(deployment["env"], [{"name": _ENV_NAME, "value": str(_MAX_SECONDS)}])

    def test_the_entry_follows_the_ceilings(self):
        deployment = self._deployment({"platformAgent": {"deployment": {"alertDailyLimits": {"drift": 20}}}})
        self.assertEqual(deployment["env"], [{"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "20"}, _DEFAULT_ENTRY])

    def test_an_explicit_env_entry_wins_over_the_key(self):
        deployment = self._deployment(_values(900, [{"name": _ENV_NAME, "value": "0"}]))
        self.assertEqual(deployment["env"], [{"name": _ENV_NAME, "value": "0"}])

    def test_an_explicit_env_entry_wins_over_the_default(self):
        deployment = self._deployment(_values(env=[{"name": _ENV_NAME, "value": "600"}]))
        self.assertEqual(deployment["env"], [{"name": _ENV_NAME, "value": "600"}])

    def test_a_value_from_entry_fails_the_render_while_the_key_is_set(self):
        res = self._render(_values(env=[{"name": _ENV_NAME, "valueFrom": _VALUE_FROM}]))
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(f"platformAgent.deployment.{_KEY}", res.stderr)
        self.assertIn(_ENV_NAME, res.stderr)
        self.assertIn("forwards only literal values", res.stderr)

    def test_a_value_from_entry_renders_unchanged_with_the_key_null(self):
        # The chart passes it through. The operator drops it, as for any valueFrom.
        deployment = self._deployment(_values(None, [{"name": _ENV_NAME, "valueFrom": _VALUE_FROM}]))
        self.assertEqual(deployment["env"], [{"name": _ENV_NAME, "valueFrom": _VALUE_FROM}])

    def test_an_unrelated_env_entry_and_the_default_both_render(self):
        deployment = self._deployment(_values(env=[{"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"}]))
        self.assertEqual(deployment["env"], [_DEFAULT_ENTRY, {"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"}])

    def test_the_ci_deploy_flags_win_over_the_default(self):
        env = self._deployment(extra_args=_CI_DEPLOY_FLAGS)["env"]
        self.assertEqual([e for e in env if e["name"] == _ENV_NAME], [{"name": _ENV_NAME, "value": "150"}])

    def test_the_schema_refuses_a_bad_value(self):
        for seconds in (-1, _MAX_SECONDS + 1, "hour", 2.5):
            with self.subTest(seconds=seconds):
                res = self._render(_values(seconds))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(_KEY, res.stderr)


if __name__ == "__main__":
    unittest.main()
