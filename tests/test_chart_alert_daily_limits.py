"""The chart renders `platformAgent.deployment.alertDailyLimits` into the CR's env.

Each key that is set becomes one `ALERT_DAILY_LIMIT_<KEY>` entry in
`spec.deployment.env`, with a string value. A default render has no env. An entry
with the same name in `platformAgent.deployment.env` wins, so the name occurs one
time only: the CRD keys env on name, and the API server refuses a duplicate. A
`valueFrom` entry for a name whose key is set fails the render, because the operator
forwards only literal values. The schema refuses a negative number, a number above
the int32 maximum, a string and an unknown key.

Run: python3 -m unittest discover -s tests -p 'test_chart_alert_daily_limits.py' -v
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
# The four `--set-string` flags that hack/ci-deploy.sh passes for the eval install.
_CI_DEPLOY_FLAGS = [
    "--set-string", "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING",
    "--set-string", "platformAgent.deployment.env[0].value=0",
    "--set-string", "platformAgent.deployment.env[1].name=ALERT_DAILY_LIMIT_DRIFT",
    "--set-string", "platformAgent.deployment.env[1].value=0",
]
_CI_DEPLOY_ENV = [
    {"name": "ALERT_DAILY_LIMIT_WARNING", "value": "0"},
    {"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "0"},
]
_VALUE_FROM = {"configMapKeyRef": {"name": "ceilings", "key": "drift"}}
# The schema maximum for each key: the int32 maximum. The template casts each value
# to int64, and a value above the int64 range becomes negative, which removes the cap.
_LIMIT_MAX = 2147483647


def _values(limits=None, env=None):
    deployment = {}
    if limits is not None:
        deployment["alertDailyLimits"] = limits
    if env is not None:
        deployment["env"] = env
    return {"platformAgent": {"deployment": deployment}}


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, empty, and closed."""

    def test_values_yaml_carries_an_empty_map(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"]["alertDailyLimits"], {})

    def test_the_schema_names_the_four_buckets_and_nothing_else(self):
        schema = json.loads(_SCHEMA.read_text())
        node = schema["properties"]["platformAgent"]["properties"]["deployment"]["properties"]["alertDailyLimits"]
        self.assertIs(node["additionalProperties"], False)
        self.assertEqual(sorted(node["properties"]), ["critical", "drift", "info", "warning"])
        for bucket, spec in node["properties"].items():
            with self.subTest(bucket=bucket):
                self.assertEqual(spec, {"type": ["integer", "null"], "minimum": 0, "maximum": _LIMIT_MAX})


@unittest.skipUnless(_HELM, "helm is not installed")
class AlertDailyLimitsRenderTest(unittest.TestCase):
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

    def test_a_default_render_has_no_env(self):
        self.assertNotIn("env", self._deployment())

    def test_null_keys_render_no_env(self):
        deployment = self._deployment(_values({"critical": None, "drift": None}))
        self.assertNotIn("env", deployment)

    def test_two_keys_render_exactly_two_string_entries(self):
        deployment = self._deployment(_values({"critical": 50, "drift": 20}))
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_CRITICAL", "value": "50"},
            {"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "20"},
        ])

    def test_zero_renders_as_zero(self):
        deployment = self._deployment(_values({"info": 0}))
        self.assertEqual(deployment["env"], [{"name": "ALERT_DAILY_LIMIT_INFO", "value": "0"}])

    def test_a_large_value_renders_in_digits(self):
        # Helm reads the number as float64, and toString on a float64 writes 1e+07.
        deployment = self._deployment(_values({"warning": 10000000}))
        self.assertEqual(deployment["env"], [{"name": "ALERT_DAILY_LIMIT_WARNING", "value": "10000000"}])

    def test_the_maximum_renders_in_digits(self):
        deployment = self._deployment(_values({"drift": _LIMIT_MAX}))
        self.assertEqual(deployment["env"], [{"name": "ALERT_DAILY_LIMIT_DRIFT", "value": str(_LIMIT_MAX)}])

    def test_an_explicit_env_entry_wins_over_the_key(self):
        deployment = self._deployment(_values(
            {"warning": 30},
            [{"name": "ALERT_DAILY_LIMIT_WARNING", "value": "0"}],
        ))
        self.assertEqual(deployment["env"], [{"name": "ALERT_DAILY_LIMIT_WARNING", "value": "0"}])

    def test_a_value_from_entry_for_a_set_key_fails_the_render(self):
        # The operator forwards only literal values, so it drops the entry, and
        # the key loses to it. The render fails rather than lose both.
        res = self._render(_values(
            {"drift": 20},
            [{"name": "ALERT_DAILY_LIMIT_DRIFT", "valueFrom": _VALUE_FROM}],
        ))
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("platformAgent.deployment.alertDailyLimits.drift", res.stderr)
        self.assertIn("ALERT_DAILY_LIMIT_DRIFT", res.stderr)
        self.assertIn("forwards only literal values", res.stderr)

    def test_a_value_from_entry_for_an_unset_key_renders_unchanged(self):
        # The chart passes it through. The operator drops it, as for any valueFrom.
        deployment = self._deployment(_values(
            {"critical": 25},
            [{"name": "ALERT_DAILY_LIMIT_DRIFT", "valueFrom": _VALUE_FROM}],
        ))
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_CRITICAL", "value": "25"},
            {"name": "ALERT_DAILY_LIMIT_DRIFT", "valueFrom": _VALUE_FROM},
        ])

    def test_an_unrelated_env_entry_and_one_key_both_render(self):
        deployment = self._deployment(_values(
            {"critical": 25},
            [{"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"}],
        ))
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_CRITICAL", "value": "25"},
            {"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"},
        ])

    def test_env_in_a_second_values_document_keeps_the_ceilings(self):
        # The Terraform composition passes extra_helm_values as a second values
        # document. A map deep-merges across documents, so its env list does not
        # erase the ceilings.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as extra:
            yaml.safe_dump(_values(env=[{"name": "EOD_EXCLUDE_NAMESPACES", "value": ""}]), extra)
            extra.flush()
            deployment = self._deployment(_values({"drift": 20}), ["-f", extra.name])
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "20"},
            {"name": "EOD_EXCLUDE_NAMESPACES", "value": ""},
        ])

    def test_the_ci_deploy_flags_render_unchanged(self):
        deployment = self._deployment(extra_args=_CI_DEPLOY_FLAGS)
        self.assertEqual(deployment["env"], _CI_DEPLOY_ENV)

    def test_the_ci_deploy_flags_win_over_the_same_keys(self):
        deployment = self._deployment(_values({"warning": 30, "drift": 20}), _CI_DEPLOY_FLAGS)
        self.assertEqual(deployment["env"], _CI_DEPLOY_ENV)

    def test_the_schema_refuses_a_bad_value(self):
        for limits in ({"warning": -1}, {"warning": _LIMIT_MAX + 1}, {"warning": "ten"}, {"warning": 2.5},
                       {"total": 10}):
            with self.subTest(limits=limits):
                res = self._render(_values(limits))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("alertDailyLimits", res.stderr)


if __name__ == "__main__":
    unittest.main()
