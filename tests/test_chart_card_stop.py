"""operator.cardStop.enabled sets the operator's A2A_CARD_STOP flag.

The operator renders A2A_CARD_STOP onto the A2A gateway as "true" only while its controller
manager carries A2A_CARD_STOP=true. The chart value is the first-class way to set it;
operator.extraEnv still works and, rendered after it, wins. The render tests need a helm
binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_card_stop.py' -v
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
_FLAG = "A2A_CARD_STOP"


class CardStopValueTest(unittest.TestCase):
    """Readable without helm."""

    def test_off_by_default(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIs(values["operator"]["cardStop"]["enabled"], False)

    def test_the_schema_takes_a_boolean_and_nothing_else(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        card_stop = schema["properties"]["operator"]["properties"]["cardStop"]
        self.assertEqual(card_stop["properties"], {"enabled": {"type": "boolean"}})
        self.assertIs(card_stop["additionalProperties"], False)


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
class CardStopRenderTest(unittest.TestCase):
    def test_default_sets_no_flag(self):
        self.assertEqual([], _flag_entries(_manager_env()))

    def test_enabled_sets_the_flag_to_the_string_true(self):
        self.assertEqual([{"name": _FLAG, "value": "true"}],
                         _flag_entries(_manager_env("--set", "operator.cardStop.enabled=true")))

    def test_enabled_beside_the_other_flags_keeps_each(self):
        env = _manager_env("--set", "operator.cardStop.enabled=true",
                           "--set", "operator.mcpServer.enabled=true",
                           "--set", "operator.profileDispatcher.enabled=false")
        self.assertEqual([{"name": _FLAG, "value": "true"}], _flag_entries(env))
        self.assertIn({"name": "A2A_MCP_DOOR", "value": "true"}, env)
        self.assertIn({"name": "A2A_PROFILE_DISPATCHER", "value": "false"}, env)

    def test_disabled_sets_no_flag(self):
        self.assertEqual([], _flag_entries(_manager_env("--set", "operator.cardStop.enabled=false")))

    def test_an_extra_env_entry_replaces_the_value_rather_than_duplicating_it(self):
        """extraEnv wins, and the value steps aside so the env carries one entry of the name."""
        env = _manager_env("--set", "operator.cardStop.enabled=true",
                           "--set", f"operator.extraEnv[0].name={_FLAG}",
                           "--set-string", "operator.extraEnv[0].value=false")
        self.assertEqual([{"name": _FLAG, "value": "false"}], _flag_entries(env))

    def test_a_release_that_predates_the_key_still_renders(self):
        """helm upgrade --reuse-values renders against old values with no operator.cardStop."""
        self.assertEqual([], _flag_entries(_manager_env("--set", "operator.cardStop=null")))


if __name__ == "__main__":
    unittest.main()
