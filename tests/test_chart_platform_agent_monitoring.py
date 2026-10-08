"""The chart renders a PodMonitoring for each of the agent's pods that serves metrics.

Three today: the gateway pod, where the k8s-event-watcher serves its metrics on the
agent-api-auth sidecar; the credential-proxy pod, where the broker serves its
own on a metrics-only listener; and the A2A gateway pod a `spec.mode: next`
install runs, which serves its own on a metrics-only listener too. For each,
three things have to agree for the scrape to work: the port the operator
declares on the container, the number in the PodMonitoring, and the labels the
operator puts on the pod. The chart cannot read the operator, so the structural
tests hold the template to the operator's golden manifest instead. No golden
renders mode next, so the A2A gateway's row is held to the rendered A2A
ingress fixture (k8s-operator/internal/controller/testdata/a2a-gateway-ingress.yaml).
Whether they render at all follows the cluster by
default: helm template has no cluster, so the render tests hand it the
PodMonitoring API with --api-versions where they mean a cluster that serves it.
The render tests need a helm binary, which the agent-startup job lacks.

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
_A2A_FIXTURE = (
    _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "testdata" / "a2a-gateway-ingress.yaml"
)
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_AGENT = "platform-agent"
# The CR name the golden renders; the operator's pod labels carry it.
_GOLDEN_AGENT = "platformagent"
_GMP_API = "monitoring.googleapis.com/v1/PodMonitoring"
# What a cluster with GKE Managed Prometheus tells helm it serves.
_ON_GKE = ["--api-versions", _GMP_API]
_GATE = '{{- if and .Values.platformAgent.enabled (include "kube-agents.platformAgentPodMonitoring" .) }}'

# One row per scraped pod: the PodMonitoring name suffix, the Deployment and
# container the golden declares the port on, the port's name there, and the
# keys of the pod labels the selector has to carry. The values come from the
# golden or fixture, so a label the operator moves fails this test until the
# template follows.
_SCRAPES = (
    {
        "suffix": "-gateway-monitoring",
        "deployment": "platformagent-gateway",
        "container": "agent-api-auth",
        "port_name": "event-metrics",
        "selector_keys": ("app",),
    },
    {
        "suffix": "-credential-proxy-monitoring",
        "deployment": "platformagent-credential-proxy",
        "container": "envoy-credential-proxy",
        "port_name": "cred-metrics",
        "selector_keys": ("app", "kubeagents.x-k8s.io/component"),
    },
    # Read from the rendered A2A ingress fixture rather than the golden (see
    # the module docstring).
    {
        "suffix": "-a2a-gateway-monitoring",
        "fixture": _A2A_FIXTURE,
        "agent_name": "test-agent",
        "deployment": "test-agent-a2a-gateway",
        "container": "gateway",
        "port_name": "a2a-metrics",
        "selector_keys": ("app",),
    },
)


def _scrape_port(scrape):
    """The port a row's PodMonitoring has to scrape, from the golden or the A2A fixture."""
    fixture = scrape.get("fixture", _GOLDEN)
    return _manifest_port(fixture, scrape["deployment"], scrape["container"], scrape["port_name"])


def _manifest_port(fixture, deployment, container, port_name):
    """The named containerPort the operator declares, read from the golden or fixture render."""
    for document in yaml.safe_load_all(fixture.read_text()):
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        if document["metadata"]["name"] != deployment:
            continue
        pod = document["spec"]["template"]["spec"]
        for candidate in pod.get("initContainers", []) + pod.get("containers", []):
            if candidate["name"] != container:
                continue
            for port in candidate.get("ports", []):
                if port["name"] == port_name:
                    return port["containerPort"]
    raise AssertionError(f"no {port_name} port on {deployment}/{container} in {fixture}")


def _manifest_pod_labels(fixture, deployment):
    """The pod-template labels the operator puts on the named Deployment, from the golden or fixture."""
    for document in yaml.safe_load_all(fixture.read_text()):
        if isinstance(document, dict) and document.get("kind") == "Deployment" and document["metadata"]["name"] == deployment:
            return document["spec"]["template"]["metadata"]["labels"]
    raise AssertionError(f"no Deployment {deployment} in {fixture}")


def _selector(scrape, name):
    """The selector the PodMonitoring has to carry for agent `name`: the manifest's
    values for the keys the row names, with the manifest's agent name replaced."""
    fixture = scrape.get("fixture", _GOLDEN)
    agent_name = scrape.get("agent_name", _GOLDEN_AGENT)
    labels = _manifest_pod_labels(fixture, scrape["deployment"])
    return {key: labels[key].replace(agent_name, name) for key in scrape["selector_keys"]}


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

    def test_the_ports_are_the_ones_the_operator_declares(self):
        # Numbers rather than port names, because the gateway's listener is on
        # an init container; the template says why. Each number then has two
        # homes, and this is what keeps them one. The template lists the
        # scrapes in _SCRAPES order.
        ports = re.findall(r"^\s+- port: (\d+)$", self.template, re.MULTILINE)
        self.assertEqual(
            ports,
            [str(_scrape_port(s)) for s in _SCRAPES],
        )

    def test_the_selectors_are_the_operators_pod_labels(self):
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["suffix"]):
                for key, value in _selector(scrape, "{{ .Values.platformAgent.name }}").items():
                    self.assertIn(f"{key}: {value}", self.template)

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
        suffixes = tuple(scrape["suffix"] for scrape in _SCRAPES)
        return {
            document["metadata"]["name"]: document
            for document in yaml.safe_load_all(proc.stdout)
            if isinstance(document, dict)
            and document.get("kind") == "PodMonitoring"
            and document["metadata"]["name"].endswith(suffixes)
        }

    def test_the_default_follows_the_cluster(self):
        # No PodMonitoring API, no objects: the install that never needed the
        # CRD still does not. With the API served, one per scraped pod.
        self.assertEqual(self._monitorings(), {})
        rendered = self._monitorings(*_ON_GKE)
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["suffix"] for s in _SCRAPES))
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["suffix"]):
                monitoring = rendered[_AGENT + scrape["suffix"]]
                self.assertEqual(monitoring["spec"]["selector"], {"matchLabels": _selector(scrape, _AGENT)})
                self.assertEqual(
                    monitoring["spec"]["endpoints"],
                    [{
                        "port": _scrape_port(scrape),
                        "path": "/metrics",
                        "interval": "30s",
                    }],
                )

    def test_the_names_and_selectors_follow_the_agent_name(self):
        rendered = self._monitorings(*_ON_GKE, "--set", "platformAgent.name=custom")
        self.assertEqual(sorted(rendered), sorted("custom" + s["suffix"] for s in _SCRAPES))
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["suffix"]):
                self.assertEqual(
                    rendered["custom" + scrape["suffix"]]["spec"]["selector"]["matchLabels"],
                    _selector(scrape, "custom"),
                )

    def test_true_renders_them_without_asking_the_cluster(self):
        rendered = self._monitorings("--set", "platformAgent.podMonitoring=true")
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["suffix"] for s in _SCRAPES))

    def test_false_renders_nothing_even_where_the_api_is_served(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.podMonitoring=false"), {})

    def test_no_agent_renders_nothing(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.enabled=false"), {})


if __name__ == "__main__":
    unittest.main()
