"""operator.profileDispatcher.enabled is the off-switch for the AgentProfile dispatcher.

The operator renders the dispatcher (<agent>-a2a-dispatcher) beside every next-mode
PlatformAgent unless its controller manager runs with A2A_PROFILE_DISPATCHER=false. The chart
value is the supported way to set that; on (the default) renders no entry at all, and
operator.extraEnv still works and wins. The quota preflight counts the dispatcher from
footprint.yaml, so an install that switches it off is over-counted by its one pod. The render
tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_profile_dispatcher.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_OPERATOR_TEMPLATE = "templates/operator-deployment.yaml"
_FLAG = "A2A_PROFILE_DISPATCHER"


class ProfileDispatcherValueTest(unittest.TestCase):
    """Readable without helm."""

    def test_on_by_default(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIs(values["operator"]["profileDispatcher"]["enabled"], True)

    def test_the_schema_takes_a_boolean_and_nothing_else(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        d = schema["properties"]["operator"]["properties"]["profileDispatcher"]
        self.assertEqual(d["properties"], {"enabled": {"type": "boolean"}})
        self.assertIs(d["additionalProperties"], False)

    def test_the_flagged_pods_file_no_longer_lists_it(self):
        """On by default, it's in footprint.yaml; listed here too it would be counted twice."""
        flagged = yaml.safe_load((_CHART / "files" / "flagged-pods.yaml").read_text())
        self.assertNotIn("dispatcher", [p["name"] for p in flagged["pods"]])


def _manager_env(*extra):
    cmd = ["helm", "template", "rel", str(_CHART), "-s", _OPERATOR_TEMPLATE, *_REQUIRED, *extra]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    for doc in yaml.safe_load_all(out):
        if doc and doc.get("kind") == "Deployment":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c.get("name") == "manager":
                    return c.get("env", [])
    raise AssertionError("no manager container rendered")


def _flag_entries(env):
    return [e for e in env if e.get("name") == _FLAG]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ProfileDispatcherRenderTest(unittest.TestCase):
    def test_default_sets_no_flag(self):
        """On is the operator's own default, so the chart says nothing."""
        self.assertEqual([], _flag_entries(_manager_env()))

    def test_disabled_sets_the_flag_to_the_string_false(self):
        self.assertEqual([{"name": _FLAG, "value": "false"}],
                         _flag_entries(_manager_env("--set", "operator.profileDispatcher.enabled=false")))

    def test_an_extra_env_entry_replaces_the_value_rather_than_duplicating_it(self):
        env = _manager_env("--set", "operator.profileDispatcher.enabled=false",
                           "--set", f"operator.extraEnv[0].name={_FLAG}",
                           "--set-string", "operator.extraEnv[0].value=true")
        self.assertEqual([{"name": _FLAG, "value": "true"}], _flag_entries(env))

    def test_a_release_that_predates_the_key_gets_the_dispatcher(self):
        """helm upgrade --reuse-values renders against old values with no operator.profileDispatcher:
        no entry, so the operator's default, on, applies. That is the flip."""
        self.assertEqual([], _flag_entries(_manager_env("--set", "operator.profileDispatcher=null")))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(shutil.which("helm") is None, "helm is required for chart rendering tests")
class TseWarningInNotesTest(unittest.TestCase):
    """NOTES.txt warns when tse is enabled on a next install with the dispatcher
    on: tse has no persona image yet, so its warm worker crash-loops (#3842)."""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory()
        chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(_CHART, chart)
        templates = chart / "templates"
        notes = (templates / "NOTES.txt").read_text()
        (templates / "NOTES.txt").unlink()
        (templates / "_notes_under_test.tpl").write_text(
            '{{- define "notes-under-test" -}}\n' + notes + "\n{{- end -}}\n"
        )
        (templates / "notes-under-test.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes-under-test\n"
            'data:\n  notes: {{ include "notes-under-test" . | toJson }}\n'
        )
        cls.chart = chart

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def notes(self, *args: str) -> str:
        cmd = [shutil.which("helm"), "template", "t", str(self.chart),
               "--show-only", "templates/notes-under-test.yaml", *_REQUIRED, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return yaml.safe_load(proc.stdout)["data"]["notes"]

    def test_tse_on_next_with_the_dispatcher_warns(self):
        out = self.notes("--set", "platformAgent.mode=next", "--set", "profiles.tse.enabled=true")
        self.assertIn("profiles.tse.enabled is true on a next install", out)
        self.assertIn("operator.profileDispatcher.enabled=false", out)

    def test_no_warning_without_the_combination(self):
        for args in (
            ["--set", "platformAgent.mode=next"],
            ["--set", "platformAgent.mode=today", "--set", "profiles.tse.enabled=true"],
            ["--set", "platformAgent.mode=next", "--set", "profiles.tse.enabled=true",
             "--set", "operator.profileDispatcher.enabled=false"],
        ):
            with self.subTest(args=args):
                self.assertNotIn("profiles.tse.enabled is true", self.notes(*args))
