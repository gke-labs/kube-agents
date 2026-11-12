"""operator.slackTestUsers sets the operator's A2A_SLACK_TEST_USERS.

The operator hands the A2A gateway the end-to-end test users whose app-attributed Slack posts
it reads as theirs, from its own environment only. The chart value is the first-class way to
set it, empty by default; an operator.extraEnv entry of the same name wins. The render tests
need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_slack_test_users.py' -v
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
_FLAG = "A2A_SLACK_TEST_USERS"
_OPERATOR_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_a2a_manifests.go"
_GATEWAY_GO = _REPO_ROOT / "a2a" / "gateway" / "config.go"


class SlackTestUsersValueTest(unittest.TestCase):
    """Readable without helm."""

    def test_empty_by_default(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["operator"]["slackTestUsers"], [])

    def test_the_schema_takes_member_ids(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        prop = schema["properties"]["operator"]["properties"]["slackTestUsers"]
        self.assertEqual("array", prop["type"])
        self.assertEqual("string", prop["items"]["type"])

    def test_the_name_is_the_operators_and_the_gateways(self):
        self.assertIn(f'a2aSlackTestUsersEnvVar = "{_FLAG}"', _OPERATOR_GO.read_text())
        self.assertIn(f'EnvSlackTestUsers = "{_FLAG}"', _GATEWAY_GO.read_text())


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
class SlackTestUsersRenderTest(unittest.TestCase):
    def test_unset_renders_nothing(self):
        self.assertEqual([], _flag_entries(_manager_env()))

    def test_a_list_renders_one_comma_joined_entry(self):
        env = _manager_env("--set-json", 'operator.slackTestUsers=["U01","U02"]')
        self.assertEqual([{"name": _FLAG, "value": "U01,U02"}], _flag_entries(env))

    def test_an_extra_env_entry_wins(self):
        env = _manager_env(
            "--set-json", 'operator.slackTestUsers=["U01"]',
            "--set-json", f'operator.extraEnv=[{{"name":"{_FLAG}","value":"U09"}}]',
        )
        self.assertEqual([{"name": _FLAG, "value": "U09"}], _flag_entries(env))

    def test_the_schema_refuses_a_non_id(self):
        with self.assertRaises(subprocess.CalledProcessError):
            _manager_env("--set-json", 'operator.slackTestUsers=["not an id"]')


if __name__ == "__main__":
    unittest.main()
