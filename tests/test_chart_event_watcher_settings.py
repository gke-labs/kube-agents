"""The chart renders `platformAgent.deployment.eventWatcher` into the CR's env.

Each key that is set becomes one `WATCHER_<NAME>` entry in `spec.deployment.env`, with a
string value, after the alert ceilings and before `platformAgent.deployment.env`. A default
render has no env. An entry with the same name in `platformAgent.deployment.env` wins, a
`valueFrom` entry included, so the name occurs one time only. The schema refuses a value that
is not a Go duration, a count of at least 1 or a boolean as the key needs, a zero dedup window
or scale-up hold, and an unknown key. Every name the chart renders is one that
`deploy/shared/start-services.sh` reads and checks.

Run: python3 -m unittest discover -s tests -p 'test_chart_event_watcher_settings.py' -v
"""

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
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_ENTRYPOINT = _REPO_ROOT / "deploy" / "shared" / "start-services.sh"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_FROM = {"configMapKeyRef": {"name": "watcher", "key": "span"}}
# Each key, the variable it becomes, and a value of its kind that is not the default.
_SETTINGS = {
    "dedupWindow": ("WATCHER_DEDUP_WINDOW", "12h"),
    "backoffMinCount": ("WATCHER_BACKOFF_MIN_COUNT", 4),
    "imagePullTransientMinCount": ("WATCHER_IMAGEPULL_TRANSIENT_MIN_COUNT", 2),
    "failedSchedulingMinCount": ("WATCHER_FAILEDSCHEDULING_MIN_COUNT", 8),
    "failedSchedulingMinSpan": ("WATCHER_FAILEDSCHEDULING_MIN_SPAN", "7m"),
    "scaleUpHold": ("WATCHER_SCALEUP_HOLD", "20m"),
    "settle": ("WATCHER_SETTLE", "90s"),
    "quietAfter": ("WATCHER_QUIET_AFTER", "1h30m"),
    "autopilotScaleToZeroHold": ("WATCHER_AUTOPILOT_SCALE_TO_ZERO_HOLD", False),
}
_RENDERED = {"12h": "12h", 4: "4", 2: "2", 8: "8", "7m": "7m", "20m": "20m", "90s": "90s",
             "1h30m": "1h30m", False: "false"}


def _values(settings=None, env=None, limits=None):
    deployment = {}
    if settings is not None:
        deployment["eventWatcher"] = settings
    if env is not None:
        deployment["env"] = env
    if limits is not None:
        deployment["alertDailyLimits"] = limits
    return {"platformAgent": {"deployment": deployment}}


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, empty, closed, and reaches the watcher."""

    def test_values_yaml_carries_an_empty_map(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"]["eventWatcher"], {})

    def test_the_schema_names_the_nine_settings_and_nothing_else(self):
        schema = json.loads(_SCHEMA.read_text())
        node = schema["properties"]["platformAgent"]["properties"]["deployment"]["properties"]["eventWatcher"]
        self.assertIs(node["additionalProperties"], False)
        self.assertEqual(sorted(node["properties"]), sorted(_SETTINGS))

    def test_every_rendered_name_is_one_the_entrypoint_checks(self):
        # start-services.sh reads each variable, falls back to a named default, and
        # passes it to the watcher. A name it does not read would do nothing.
        text = _ENTRYPOINT.read_text()
        checked = set(re.findall(r"^normalize_(?:duration|count|boolean) (WATCHER_[A-Z_]+) ", text, re.M))
        self.assertLessEqual({name for name, _ in _SETTINGS.values()}, checked)

    def test_the_template_names_every_setting(self):
        template = (_CHART / _CR_TEMPLATE).read_text()
        for key, (name, _) in _SETTINGS.items():
            with self.subTest(key=key):
                self.assertIn(f'(list "{key}" "{name}")', template)


@unittest.skipUnless(_HELM, "helm is not installed")
class EventWatcherSettingsRenderTest(unittest.TestCase):
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
        self.assertNotIn("env", self._deployment(_values({"settle": None, "backoffMinCount": None})))

    def test_each_key_renders_its_variable_as_a_string(self):
        for key, (name, value) in _SETTINGS.items():
            with self.subTest(key=key):
                deployment = self._deployment(_values({key: value}))
                self.assertEqual(deployment["env"], [{"name": name, "value": _RENDERED[value]}])

    def test_all_keys_render_in_a_fixed_order(self):
        deployment = self._deployment(_values({key: value for key, (_, value) in _SETTINGS.items()}))
        self.assertEqual(deployment["env"], [
            {"name": name, "value": _RENDERED[value]} for name, value in _SETTINGS.values()
        ])

    def test_zero_turns_a_duration_off(self):
        for zero in (0, "0", "0s"):
            with self.subTest(zero=zero):
                deployment = self._deployment(_values({"quietAfter": zero}))
                self.assertEqual(deployment["env"], [{"name": "WATCHER_QUIET_AFTER", "value": str(zero)}])

    def test_a_large_count_renders_in_digits(self):
        # Helm reads the number as float64, and toString on a float64 writes 1e+07.
        deployment = self._deployment(_values({"failedSchedulingMinCount": 10000000}))
        self.assertEqual(deployment["env"], [{"name": "WATCHER_FAILEDSCHEDULING_MIN_COUNT", "value": "10000000"}])

    def test_a_set_flag_renders_like_a_values_file(self):
        res = self._deployment(extra_args=[
            "--set", "platformAgent.deployment.eventWatcher.backoffMinCount=5",
            "--set", "platformAgent.deployment.eventWatcher.autopilotScaleToZeroHold=false",
            "--set", "platformAgent.deployment.eventWatcher.settle=0",
        ])
        self.assertEqual(res["env"], [
            {"name": "WATCHER_BACKOFF_MIN_COUNT", "value": "5"},
            {"name": "WATCHER_SETTLE", "value": "0"},
            {"name": "WATCHER_AUTOPILOT_SCALE_TO_ZERO_HOLD", "value": "false"},
        ])

    def test_the_settings_follow_the_alert_ceilings_and_precede_env(self):
        deployment = self._deployment(_values(
            {"settle": "3m"},
            [{"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"}],
            {"drift": 20},
        ))
        self.assertEqual(deployment["env"], [
            {"name": "ALERT_DAILY_LIMIT_DRIFT", "value": "20"},
            {"name": "WATCHER_SETTLE", "value": "3m"},
            {"name": "EOD_EXCLUDE_NAMESPACES", "value": "kube-system"},
        ])

    def test_an_explicit_env_entry_wins_over_the_key(self):
        deployment = self._deployment(_values(
            {"failedSchedulingMinSpan": "7m"},
            [{"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "value": "5m"}],
        ))
        self.assertEqual(deployment["env"], [{"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "value": "5m"}])

    def test_a_value_from_entry_wins_over_the_key(self):
        # The operator forwards valueFrom to the agent-api-auth container, so the
        # explicit entry is the one the watcher reads, as for a literal entry.
        deployment = self._deployment(_values(
            {"failedSchedulingMinSpan": "7m"},
            [{"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "valueFrom": _VALUE_FROM}],
        ))
        self.assertEqual(deployment["env"], [{"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "valueFrom": _VALUE_FROM}])

    def test_a_value_from_entry_for_an_unset_key_renders_unchanged(self):
        deployment = self._deployment(_values(
            {"settle": "3m"},
            [{"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "valueFrom": _VALUE_FROM}],
        ))
        self.assertEqual(deployment["env"], [
            {"name": "WATCHER_SETTLE", "value": "3m"},
            {"name": "WATCHER_FAILEDSCHEDULING_MIN_SPAN", "valueFrom": _VALUE_FROM},
        ])

    def test_the_schema_refuses_a_bad_value(self):
        for settings in (
            {"dedupWindow": "0"}, {"dedupWindow": "0s"}, {"dedupWindow": 0},
            {"scaleUpHold": "0"}, {"scaleUpHold": 0},
            {"backoffMinCount": 0}, {"failedSchedulingMinCount": 0}, {"imagePullTransientMinCount": 0},
            {"settle": "5x"}, {"settle": "2 m"}, {"settle": 30}, {"settle": True},
            {"backoffMinCount": -1}, {"backoffMinCount": 2.5}, {"backoffMinCount": "3"},
            {"autopilotScaleToZeroHold": "false"},
            {"reasons": "BackOff"},
        ):
            with self.subTest(settings=settings):
                res = self._render(_values(settings))
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn("eventWatcher", res.stderr)


if __name__ == "__main__":
    unittest.main()
