"""The chart renders a PodMonitoring for the gateway pod, where the k8s-event-watcher
serves its metrics on the agent-api-auth sidecar.

Three things have to agree for the scrape to work: the port the operator declares on
that sidecar, the number in this PodMonitoring, and the label the operator puts on the
gateway pod. The chart cannot read the operator, so the structural tests hold the
template to the operator's golden manifest instead. Whether it renders at all follows
the cluster by default: helm template has no cluster, so the render tests hand it the
PodMonitoring API with --api-versions where they mean a cluster that serves it. The
render tests need a helm binary, which the agent-startup job lacks.

Run: python3 -m unittest discover -s tests -p 'test_chart_platform_agent_monitoring.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-monitoring.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_GOLDEN = (
    _REPO_ROOT / "k8s-operator" / "internal" / "testing" / "testdata" / "platform" / "expected" / "platformagent.yaml"
)
_KIND_UP = _REPO_ROOT / "hack" / "kind-up.sh"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_SIDECAR = "agent-api-auth"
_PORT_NAME = "event-metrics"
_SUFFIX = "-gateway-monitoring"
_GMP_API = "monitoring.googleapis.com/v1/PodMonitoring"
# What a cluster with GKE Managed Prometheus tells helm it serves.
_ON_GKE = ["--api-versions", _GMP_API]
_GATE = '{{- if and .Values.platformAgent.enabled (include "kube-agents.platformAgentPodMonitoring" .) }}'


def _golden_sidecar_port():
    """The event-metrics containerPort the operator declares on the sidecar."""
    for document in yaml.safe_load_all(_GOLDEN.read_text()):
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        pod = document["spec"]["template"]["spec"]
        for container in pod.get("initContainers", []) + pod.get("containers", []):
            if container["name"] != _SIDECAR:
                continue
            for port in container.get("ports", []):
                if port["name"] == _PORT_NAME:
                    return port["containerPort"]
    raise AssertionError(f"no {_PORT_NAME} port on {_SIDECAR} in {_GOLDEN}")


class MonitoringShapeTest(unittest.TestCase):
    """What the template and its value say, readable without helm."""

    def setUp(self):
        self.template = _TEMPLATE.read_text()

    def test_the_value_defaults_to_null_and_the_schema_admits_the_tri_state(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIsNone(values["platformAgent"]["podMonitoring"])
        schema = json.loads((_CHART / "values.schema.json").read_text())
        self.assertEqual(
            schema["properties"]["platformAgent"]["properties"]["podMonitoring"],
            {"type": ["boolean", "null"]},
        )

    def test_the_port_is_the_one_the_operator_declares(self):
        # A number rather than the port name, because the sidecar is an init
        # container; the template says why. The number then has two homes, and
        # this is what keeps them one.
        ports = re.findall(r"^\s+- port: (\d+)$", self.template, re.MULTILINE)
        self.assertEqual(ports, [str(_golden_sidecar_port())])

    def test_the_selector_is_the_operators_gateway_label(self):
        self.assertIn("app: {{ .Values.platformAgent.name }}-gateway", self.template)

    def test_the_gate_asks_the_helper_and_the_helper_asks_the_cluster(self):
        self.assertIn(_GATE, self.template)
        helpers = _HELPERS.read_text()
        self.assertIn('{{- define "kube-agents.platformAgentPodMonitoring" -}}', helpers)
        self.assertIn(f'.Capabilities.APIVersions.Has "{_GMP_API}"', helpers)

    def test_kind_up_leaves_the_default_to_the_cluster(self):
        # kind serves no PodMonitoring API, so the null default renders nothing
        # there; pinning it false would hide a detection regression from the
        # kind job. The LiteLLM switch is a plain boolean and stays pinned.
        self.assertNotIn("platformAgent.podMonitoring", _KIND_UP.read_text())


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class MonitoringRenderTest(unittest.TestCase):
    def _monitorings(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [
            document
            for document in yaml.safe_load_all(proc.stdout)
            if isinstance(document, dict)
            and document.get("kind") == "PodMonitoring"
            and document["metadata"]["name"].endswith(_SUFFIX)
        ]

    def test_the_default_follows_the_cluster(self):
        # No PodMonitoring API, no object: the install that never needed the
        # CRD still does not. With the API served, one for the gateway.
        self.assertEqual(self._monitorings(), [])
        rendered = self._monitorings(*_ON_GKE)
        self.assertEqual(len(rendered), 1, rendered)
        monitoring = rendered[0]
        self.assertEqual(monitoring["metadata"]["name"], "platform-agent" + _SUFFIX)
        self.assertEqual(monitoring["spec"]["selector"], {"matchLabels": {"app": "platform-agent-gateway"}})
        self.assertEqual(
            monitoring["spec"]["endpoints"],
            [{"port": _golden_sidecar_port(), "path": "/metrics", "interval": "30s"}],
        )

    def test_the_name_and_selector_follow_the_agent_name(self):
        rendered = self._monitorings(*_ON_GKE, "--set", "platformAgent.name=custom")
        self.assertEqual([m["metadata"]["name"] for m in rendered], ["custom" + _SUFFIX])
        self.assertEqual(rendered[0]["spec"]["selector"]["matchLabels"], {"app": "custom-gateway"})

    def test_true_renders_it_without_asking_the_cluster(self):
        rendered = self._monitorings("--set", "platformAgent.podMonitoring=true")
        self.assertEqual([m["metadata"]["name"] for m in rendered], ["platform-agent" + _SUFFIX])

    def test_false_renders_nothing_even_where_the_api_is_served(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.podMonitoring=false"), [])

    def test_no_agent_renders_nothing(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.enabled=false"), [])


if __name__ == "__main__":
    unittest.main()
