"""The chart renders `platformAgent.deployment.incidentMaxOpenPullRequests` into the CR's env.

Above 0 the key becomes one `INCIDENT_TRIAGE_MAX_OPEN_PULL_REQUESTS` entry in
`spec.deployment.env`, with a string value. 0, the default, and null render no
entry, so a default render has no env. An entry with the same name in
`platformAgent.deployment.env` wins, and a `valueFrom` entry for it fails the
render while the key is above 0, because the operator forwards only literal
values. The schema refuses a negative number, a number above the int32 maximum
and a string.

Run: python3 -m unittest discover -s tests -p 'test_chart_incident_max_open_pull_requests.py' -v
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
_NAME = "INCIDENT_TRIAGE_MAX_OPEN_PULL_REQUESTS"
_VALUE_FROM = {"configMapKeyRef": {"name": "limits", "key": "pull-requests"}}
_LIMIT_MAX = 2147483647


def _values(limit=None, env=None, limits=None):
    deployment = {}
    if limit is not None:
        deployment["incidentMaxOpenPullRequests"] = limit
    if env is not None:
        deployment["env"] = env
    if limits is not None:
        deployment["alertDailyLimits"] = limits
    return {"platformAgent": {"deployment": deployment}}


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, 0, and bounded."""

    def test_values_yaml_carries_zero(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"]["incidentMaxOpenPullRequests"], 0)

    def test_the_schema_bounds_the_key(self):
        schema = json.loads(_SCHEMA.read_text())
        node = schema["properties"]["platformAgent"]["properties"]["deployment"]["properties"]
        self.assertEqual(
            node["incidentMaxOpenPullRequests"], {"type": ["integer", "null"], "minimum": 0, "maximum": _LIMIT_MAX}
        )


@unittest.skipUnless(_HELM, "helm is not installed")
class IncidentMaxOpenPullRequestsRenderTest(unittest.TestCase):
    def _render(self, values=None):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            # A values file, unlike `--set`, gives the chart float64 numbers and nulls.
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

    def test_zero_and_null_render_no_env(self):
        self.assertNotIn("env", self._deployment())
        self.assertNotIn("env", self._deployment(_values(0)))
        self.assertNotIn("env", self._deployment({"platformAgent": {"deployment": {"incidentMaxOpenPullRequests": None}}}))

    def test_a_limit_renders_one_string_entry(self):
        self.assertEqual(self._deployment(_values(5))["env"], [{"name": _NAME, "value": "5"}])

    def test_the_maximum_renders_in_digits(self):
        # Helm reads the number as float64, and toString on a float64 writes 2.147483647e+09.
        self.assertEqual(self._deployment(_values(_LIMIT_MAX))["env"], [{"name": _NAME, "value": str(_LIMIT_MAX)}])

    def test_it_follows_the_alert_ceilings_and_precedes_the_env_list(self):
        deployment = self._deployment(_values(
            3, [{"name": "INCIDENT_TRIAGE_OPEN_PULL_REQUEST", "value": "true"}], {"drift": 20},
        ))
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "20"},
            {"name": _NAME, "value": "3"},
            {"name": "INCIDENT_TRIAGE_OPEN_PULL_REQUEST", "value": "true"},
        ])

    def test_an_explicit_env_entry_wins_over_the_key(self):
        deployment = self._deployment(_values(5, [{"name": _NAME, "value": "1"}]))
        self.assertEqual(deployment["env"], [{"name": _NAME, "value": "1"}])

    def test_a_value_from_entry_fails_the_render_while_the_key_is_set(self):
        res = self._render(_values(5, [{"name": _NAME, "valueFrom": _VALUE_FROM}]))
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("platformAgent.deployment.incidentMaxOpenPullRequests", res.stderr)
        self.assertIn("forwards only literal values", res.stderr)

    def test_a_value_from_entry_passes_through_at_zero(self):
        deployment = self._deployment(_values(0, [{"name": _NAME, "valueFrom": _VALUE_FROM}]))
        self.assertEqual(deployment["env"], [{"name": _NAME, "valueFrom": _VALUE_FROM}])

    def test_the_schema_refuses_a_bad_value(self):
        for limit in (-1, _LIMIT_MAX + 1, "ten", 2.5):
            with self.subTest(limit=limit):
                res = self._render(_values(limit))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("incidentMaxOpenPullRequests", res.stderr)


if __name__ == "__main__":
    unittest.main()
