"""operator.delegation.roundsMax is held to 1.

How many delegate requests one AgentProfile task may make (the A2A gateway's
delegation.rounds rule). Only 1 is supported until a profile task's later rounds ship, so
the values schema and the render both refuse any other value, and the operator renders
A2A_DELEGATION_ROUNDS_MAX=1 onto the gateway itself. The render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_delegation_rounds.py' -v
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
_FLAG = "A2A_DELEGATION_ROUNDS_MAX"
_OPERATOR_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_a2a_manifests.go"
_GATEWAY_GO = _REPO_ROOT / "a2a" / "gateway" / "config.go"


class DelegationRoundsValueTest(unittest.TestCase):
    """Readable without helm."""

    def test_one_by_default(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["operator"]["delegation"]["roundsMax"], 1)

    def test_the_schema_takes_only_one(self):
        schema = json.loads((_CHART / "values.schema.json").read_text())
        prop = schema["properties"]["operator"]["properties"]["delegation"]["properties"]["roundsMax"]
        self.assertEqual("integer", prop["type"])
        self.assertEqual((1, 1), (prop["minimum"], prop["maximum"]))

    def test_the_name_is_the_operators_and_the_gateways(self):
        self.assertIn(f'a2aDelegationRoundsMaxEnvVar = "{_FLAG}"', _OPERATOR_GO.read_text())
        self.assertIn(f'delegationRoundsEnv = "{_FLAG}"', _GATEWAY_GO.read_text())


def _render(*extra):
    cmd = ["helm", "template", "rel", str(_CHART), "-s", _OPERATOR_TEMPLATE, *_REQUIRED, *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class DelegationRoundsRenderTest(unittest.TestCase):
    def test_one_renders_and_sets_nothing_on_the_manager(self):
        out = _render("--set", "operator.delegation.roundsMax=1")
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertNotIn(_FLAG, out.stdout)

    def test_above_one_is_refused_by_the_schema(self):
        out = _render("--set", "operator.delegation.roundsMax=3")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("roundsMax", out.stderr)

    def test_above_one_is_refused_by_the_render_past_the_schema(self):
        out = _render("--set", "operator.delegation.roundsMax=3", "--skip-schema-validation")
        self.assertNotEqual(0, out.returncode)
        self.assertIn("delegation rounds above 1 are not supported yet", out.stderr)


if __name__ == "__main__":
    unittest.main()
