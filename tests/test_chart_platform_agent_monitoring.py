"""The chart renders a PodMonitoring for each of the agent's pods that serves metrics.

Four today: the gateway pod, where the k8s-event-watcher serves its metrics on the
agent-api-auth sidecar; the credential-proxy pod, where the broker serves its
own on a metrics-only listener; the kube-proxy pod, where k8s-api-proxy serves its
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
_PROMETHEUS_OPERATOR_API = "monitoring.coreos.com/v1/ServiceMonitor"
_ON_PROMETHEUS = ["--api-versions", _PROMETHEUS_OPERATOR_API]
_VICTORIAMETRICS_API = "operator.victoriametrics.com/v1beta1/VMPodScrape"
_ON_VICTORIAMETRICS = ["--api-versions", _VICTORIAMETRICS_API]
_GATE = '{{- if and .Values.platformAgent.enabled (include "kube-agents.platformAgentPodMonitoring" .) }}'
_SERVICE_MONITOR_GATE = '{{- if and .Values.platformAgent.enabled (include "kube-agents.platformAgentServiceMonitor" .) }}'
_VM_POD_SCRAPE_GATE = '{{- if and .Values.platformAgent.enabled (include "kube-agents.platformAgentVMPodScrape" .) }}'

# One row per scraped pod: the PodMonitoring name suffix, the Deployment and
# container the golden declares the port on, the port's name there, and the
# keys of the pod labels the selector has to carry. The values come from the
# golden or fixture, so a label the operator moves fails this test until the
# template follows.
_SCRAPES = (
    {
        "suffix": "-gateway-monitoring",
        "sm_suffix": "-gateway-metrics",
        "deployment": "platformagent-gateway",
        "container": "agent-api-auth",
        "port_name": "event-metrics",
        "selector_keys": ("app",),
    },
    # The chat listener's own PodMonitoring: the chat_metrics plugin on the
    # platform-agent container, scraped on the leader alone.
    {
        # Selects the leader label as well: the chat listener runs on the leader
        # alone above one replica, and the golden renders one replica, where the
        # operator puts the label on the template, so the key is read from the
        # golden like the others.
        "suffix": "-gateway-chat-monitoring",
        "sm_suffix": "-gw-chat-metrics",
        "deployment": "platformagent-gateway",
        "container": "platform-agent",
        "port_name": "chat-metrics",
        "selector_keys": ("app", "kubeagents.io/is-leader"),
    },
    {
        "suffix": "-credential-proxy-monitoring",
        "sm_suffix": "-cred-metrics",
        "deployment": "platformagent-credential-proxy",
        "container": "envoy-credential-proxy",
        "port_name": "cred-metrics",
        "selector_keys": ("app", "kubeagents.x-k8s.io/component"),
    },
    # Read from the rendered A2A ingress fixture rather than the golden (see
    # the module docstring).
    {
        "suffix": "-a2a-gateway-monitoring",
        "sm_suffix": "-a2a-metrics",
        "fixture": _A2A_FIXTURE,
        "agent_name": "test-agent",
        "deployment": "test-agent-a2a-gateway",
        "container": "gateway",
        "port_name": "a2a-metrics",
        "selector_keys": ("app",),
    },
    {
        "suffix": "-kube-proxy-monitoring",
        "sm_suffix": "-kube-metrics",
        "deployment": "platformagent-kube-proxy",
        "container": "kube-proxy",
        "port_name": "kube-metrics",
        "selector_keys": ("app", "kubeagents.x-k8s.io/component"),
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


def _reads_as_non_string(value):
    """Whether YAML would read the bare value as something other than a string
    (true, 1, null), which matchLabels, a map of strings, cannot carry unquoted.
    A value YAML cannot parse at all (a Go template) is a string."""
    try:
        return not isinstance(yaml.safe_load(value), str)
    except yaml.YAMLError:
        return False


class MonitoringShapeTest(unittest.TestCase):
    """What the template and its value say, readable without helm."""

    def setUp(self):
        self.template = _TEMPLATE.read_text()

    def test_the_value_defaults_to_null_and_the_schema_admits_the_tri_state(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertIsNone(values["platformAgent"]["podMonitoring"])
        self.assertIsNone(values["platformAgent"]["serviceMonitor"])
        self.assertIsNone(values["platformAgent"]["vmPodScrape"])
        schema = json.loads((_CHART / "values.schema.json").read_text())
        props = schema["properties"]["platformAgent"]["properties"]
        self.assertEqual(props["podMonitoring"], {"type": ["boolean", "null"]})
        self.assertEqual(props["serviceMonitor"], {"type": ["boolean", "null"]})
        self.assertEqual(props["vmPodScrape"], {"type": ["boolean", "null"]})

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
                    # A value YAML would read as anything but a string (the
                    # leader label's "true") must be quoted in the template:
                    # matchLabels is map[string]string and the apiserver
                    # rejects a boolean. A plain value must not be, so the
                    # check is for the one spelling the value requires.
                    spelled = f'{key}: "{value}"' if _reads_as_non_string(value) else f"{key}: {value}"
                    self.assertIn(spelled, self.template)

    def test_the_gate_asks_the_helper_and_the_helper_asks_the_cluster(self):
        self.assertIn(_GATE, self.template)
        self.assertIn(_SERVICE_MONITOR_GATE, self.template)
        self.assertIn(_VM_POD_SCRAPE_GATE, self.template)
        helpers = _HELPERS.read_text()
        self.assertIn('{{- define "kube-agents.platformAgentPodMonitoring" -}}', helpers)
        self.assertIn(f'.Capabilities.APIVersions.Has "{_GMP_API}"', helpers)
        self.assertIn('{{- define "kube-agents.platformAgentServiceMonitor" -}}', helpers)
        self.assertIn(f'.Capabilities.APIVersions.Has "{_PROMETHEUS_OPERATOR_API}"', helpers)
        self.assertIn('{{- define "kube-agents.platformAgentVMPodScrape" -}}', helpers)
        self.assertIn(f'.Capabilities.APIVersions.Has "{_VICTORIAMETRICS_API}"', helpers)

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
        self.assertEqual(sorted(rendered), sorted({_AGENT + s["suffix"] for s in _SCRAPES}))
        # One PodMonitoring per suffix; its endpoints are the rows that share the
        # suffix, in _SCRAPES order (one each today).
        for suffix in dict.fromkeys(s["suffix"] for s in _SCRAPES):
            rows = [s for s in _SCRAPES if s["suffix"] == suffix]
            with self.subTest(scrape=suffix):
                monitoring = rendered[_AGENT + suffix]
                self.assertEqual(monitoring["spec"]["selector"], {"matchLabels": _selector(rows[0], _AGENT)})
                self.assertEqual(
                    monitoring["spec"]["endpoints"],
                    [{
                        "port": _scrape_port(row),
                        "path": "/metrics",
                        "interval": "30s",
                    } for row in rows],
                )

    def test_the_names_and_selectors_follow_the_agent_name(self):
        rendered = self._monitorings(*_ON_GKE, "--set", "platformAgent.name=custom")
        self.assertEqual(sorted(rendered), sorted({"custom" + s["suffix"] for s in _SCRAPES}))
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["suffix"]):
                self.assertEqual(
                    rendered["custom" + scrape["suffix"]]["spec"]["selector"]["matchLabels"],
                    _selector(scrape, "custom"),
                )

    def test_true_renders_them_without_asking_the_cluster(self):
        rendered = self._monitorings("--set", "platformAgent.podMonitoring=true")
        self.assertEqual(sorted(rendered), sorted({_AGENT + s["suffix"] for s in _SCRAPES}))

    def test_false_renders_nothing_even_where_the_api_is_served(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.podMonitoring=false"), {})

    def test_no_agent_renders_nothing(self):
        self.assertEqual(self._monitorings(*_ON_GKE, "--set", "platformAgent.enabled=false"), {})

    def _service_monitors(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suffixes = tuple(scrape["sm_suffix"] for scrape in _SCRAPES)
        return {
            document["metadata"]["name"]: document
            for document in yaml.safe_load_all(proc.stdout)
            if isinstance(document, dict)
            and document.get("kind") == "ServiceMonitor"
            and document["metadata"]["name"].endswith(suffixes)
        }

    def _headless_services(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suffixes = tuple(scrape["sm_suffix"] for scrape in _SCRAPES)
        return {
            document["metadata"]["name"]: document
            for document in yaml.safe_load_all(proc.stdout)
            if isinstance(document, dict)
            and document.get("kind") == "Service"
            and document["metadata"]["name"].endswith(suffixes)
        }

    def _vm_pod_scrapes(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suffixes = tuple(scrape["sm_suffix"] for scrape in _SCRAPES)
        return {
            document["metadata"]["name"]: document
            for document in yaml.safe_load_all(proc.stdout)
            if isinstance(document, dict)
            and document.get("kind") == "VMPodScrape"
            and document["metadata"]["name"].endswith(suffixes)
        }

    def test_service_monitor_default_follows_the_cluster(self):
        self.assertEqual(self._service_monitors(), {})
        self.assertEqual(self._headless_services(), {})
        rendered = self._service_monitors(*_ON_PROMETHEUS)
        services = self._headless_services(*_ON_PROMETHEUS)
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))
        self.assertEqual(sorted(services), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["sm_suffix"]):
                sm = rendered[_AGENT + scrape["sm_suffix"]]
                svc = services[_AGENT + scrape["sm_suffix"]]
                self.assertEqual(svc["spec"]["clusterIP"], "None")
                self.assertEqual(svc["spec"]["selector"], _selector(scrape, _AGENT))
                self.assertEqual(
                    svc["spec"]["ports"],
                    [{
                        "name": scrape["port_name"],
                        "port": _scrape_port(scrape),
                        "targetPort": _scrape_port(scrape),
                    }],
                )
                svc_metrics_label = f"{_AGENT}{scrape['sm_suffix']}"
                self.assertEqual(svc["metadata"]["labels"]["app"], svc_metrics_label)
                self.assertEqual(sm["spec"]["selector"], {"matchLabels": {"app": svc_metrics_label}})
                self.assertEqual(
                    sm["spec"]["endpoints"],
                    [{
                        "port": scrape["port_name"],
                        "path": "/metrics",
                        "interval": "30s",
                    }],
                )

    def test_service_monitor_true_renders_them_without_asking_the_cluster(self):
        rendered = self._service_monitors("--set", "platformAgent.serviceMonitor=true")
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))
        services = self._headless_services("--set", "platformAgent.serviceMonitor=true")
        self.assertEqual(sorted(services), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))

    def test_service_monitor_false_renders_nothing_even_where_the_api_is_served(self):
        self.assertEqual(self._service_monitors(*_ON_PROMETHEUS, "--set", "platformAgent.serviceMonitor=false"), {})
        self.assertEqual(self._headless_services(*_ON_PROMETHEUS, "--set", "platformAgent.serviceMonitor=false"), {})

    def test_service_monitor_no_agent_renders_nothing(self):
        self.assertEqual(self._service_monitors(*_ON_PROMETHEUS, "--set", "platformAgent.enabled=false"), {})
        self.assertEqual(self._headless_services(*_ON_PROMETHEUS, "--set", "platformAgent.enabled=false"), {})

    def test_vm_pod_scrape_default_follows_the_cluster(self):
        self.assertEqual(self._vm_pod_scrapes(), {})
        rendered = self._vm_pod_scrapes(*_ON_VICTORIAMETRICS)
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))
        for scrape in _SCRAPES:
            with self.subTest(scrape=scrape["sm_suffix"]):
                vmp = rendered[_AGENT + scrape["sm_suffix"]]
                self.assertEqual(vmp["spec"]["selector"], {"matchLabels": _selector(scrape, _AGENT)})
                self.assertEqual(
                    vmp["spec"]["podMetricsEndpoints"],
                    [{
                        "port": scrape["port_name"],
                        "path": "/metrics",
                        "interval": "30s",
                    }],
                )

    def test_vm_pod_scrape_true_renders_them_without_asking_the_cluster(self):
        rendered = self._vm_pod_scrapes("--set", "platformAgent.vmPodScrape=true")
        self.assertEqual(sorted(rendered), sorted(_AGENT + s["sm_suffix"] for s in _SCRAPES))

    def test_vm_pod_scrape_false_renders_nothing_even_where_the_api_is_served(self):
        self.assertEqual(self._vm_pod_scrapes(*_ON_VICTORIAMETRICS, "--set", "platformAgent.vmPodScrape=false"), {})

    def test_vm_pod_scrape_no_agent_renders_nothing(self):
        self.assertEqual(self._vm_pod_scrapes(*_ON_VICTORIAMETRICS, "--set", "platformAgent.enabled=false"), {})

    def test_service_monitor_and_vmpodscrape_labels(self):
        sm_rendered = self._service_monitors(
            *_ON_PROMETHEUS,
            "--set", "platformAgent.serviceMonitorLabels.release=kube-prometheus-stack",
            "--set", "platformAgent.serviceMonitorLabels.custom=test-label",
        )
        for sm in sm_rendered.values():
            self.assertEqual(sm["metadata"]["labels"]["release"], "kube-prometheus-stack")
            self.assertEqual(sm["metadata"]["labels"]["custom"], "test-label")

        vm_rendered = self._vm_pod_scrapes(
            *_ON_VICTORIAMETRICS,
            "--set", "platformAgent.vmPodScrapeLabels.release=victoria-metrics",
        )
        for vm in vm_rendered.values():
            self.assertEqual(vm["metadata"]["labels"]["release"], "victoria-metrics")

    def test_monitoring_scraper_namespace_renders_network_policy(self):
        proc = subprocess.run(
            [
                "helm", "template", "test-release", str(_CHART),
                *_REQUIRED, *_ON_PROMETHEUS,
                "--set", "platformAgent.monitoringScraperNamespace=monitoring",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        netpols = {
            doc["metadata"]["name"]: doc
            for doc in yaml.safe_load_all(proc.stdout)
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy" and "mon-netpol" in doc["metadata"]["name"]
        }
        self.assertIn(f"{_AGENT}-gw-mon-netpol", netpols)
        self.assertIn(f"{_AGENT}-cred-mon-netpol", netpols)
        self.assertIn(f"{_AGENT}-a2a-mon-netpol", netpols)

        gw_np = netpols[f"{_AGENT}-gw-mon-netpol"]
        self.assertEqual(gw_np["spec"]["podSelector"]["matchLabels"]["app"], f"{_AGENT}-gateway")
        self.assertEqual(len(gw_np["spec"]["ingress"]), 4)
        self.assertEqual(
            gw_np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "monitoring",
        )
        ports = [p["port"] for p in gw_np["spec"]["ingress"][0]["ports"]]
        self.assertIn(9095, ports)
        self.assertIn(9097, ports)

        # Ingress rule 1: same-namespace agent API (8642/8643)
        self.assertEqual(gw_np["spec"]["ingress"][1]["from"][0]["podSelector"], {})
        ports_api = [p["port"] for p in gw_np["spec"]["ingress"][1]["ports"]]
        self.assertIn(8642, ports_api)
        self.assertIn(8643, ports_api)

        # Ingress rule 2: operator poller on 9095/9097
        self.assertEqual(
            gw_np["spec"]["ingress"][2]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "default",
        )
        self.assertEqual(
            gw_np["spec"]["ingress"][2]["from"][0]["podSelector"]["matchLabels"]["app.kubernetes.io/name"],
            "kube-agents-operator",
        )
        ports_op = [p["port"] for p in gw_np["spec"]["ingress"][2]["ports"]]
        self.assertIn(9095, ports_op)
        self.assertIn(9097, ports_op)

        # Ingress rule 3: GMP collector on 9095/9097
        self.assertEqual(
            gw_np["spec"]["ingress"][3]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "gke-gmp-system",
        )
        ports_gmp = [p["port"] for p in gw_np["spec"]["ingress"][3]["ports"]]
        self.assertIn(9095, ports_gmp)
        self.assertIn(9097, ports_gmp)

        cp_np = netpols[f"{_AGENT}-cred-mon-netpol"]
        self.assertEqual(cp_np["spec"]["podSelector"]["matchLabels"]["app"], f"{_AGENT}-credential-proxy")
        self.assertEqual(
            cp_np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "monitoring",
        )
        self.assertEqual(cp_np["spec"]["ingress"][0]["ports"][0]["port"], 8766)

        a2a_np = netpols[f"{_AGENT}-a2a-mon-netpol"]
        self.assertEqual(a2a_np["spec"]["podSelector"]["matchLabels"]["app"], f"{_AGENT}-a2a-gateway")
        self.assertEqual(
            a2a_np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            "monitoring",
        )
        self.assertEqual(a2a_np["spec"]["ingress"][0]["ports"][0]["port"], 9096)

    def test_monitoring_scraper_namespace_unset_renders_no_network_policy(self):
        proc = subprocess.run(
            [
                "helm", "template", "test-release", str(_CHART),
                *_REQUIRED, *_ON_PROMETHEUS,
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        netpols = [
            doc["metadata"]["name"]
            for doc in yaml.safe_load_all(proc.stdout)
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy" and "mon-netpol" in doc.get("metadata", {}).get("name", "")
        ]
        self.assertEqual(netpols, [])

    def test_monitoring_scraper_namespace_renders_even_when_scrape_crds_disabled(self):
        proc = subprocess.run(
            [
                "helm", "template", "test-release", str(_CHART),
                *_REQUIRED,
                "--set", "platformAgent.monitoringScraperNamespace=monitoring",
                "--set", "platformAgent.serviceMonitor=false",
                "--set", "platformAgent.vmPodScrape=false",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        netpols = {
            doc["metadata"]["name"]: doc
            for doc in yaml.safe_load_all(proc.stdout)
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy" and "mon-netpol" in doc.get("metadata", {}).get("name", "")
        }
        self.assertEqual(len(netpols), 3)

    def test_monitoring_scraper_namespace_quoted_properly_for_yaml_non_strings(self):
        for ns in ["123", "on", "yes", "true"]:
            with self.subTest(ns=ns):
                proc = subprocess.run(
                    [
                        "helm", "template", "test-release", str(_CHART),
                        *_REQUIRED,
                        "--set-string", f"platformAgent.monitoringScraperNamespace={ns}",
                    ],
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                netpols = [
                    doc for doc in yaml.safe_load_all(proc.stdout)
                    if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy" and "mon-netpol" in doc.get("metadata", {}).get("name", "")
                ]
                self.assertEqual(len(netpols), 3)
                for np in netpols:
                    val = np["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
                    self.assertIsInstance(val, str)
                    self.assertEqual(val, ns)

    def test_gateway_netpol_renders_superset_rules_even_when_operator_network_policy_disabled(self):
        proc = subprocess.run(
            [
                "helm", "template", "test-release", str(_CHART),
                *_REQUIRED,
                "--set", "platformAgent.monitoringScraperNamespace=monitoring",
                "--set", "platformAgent.networkPolicy.enabled=false",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        netpols = {
            doc["metadata"]["name"]: doc
            for doc in yaml.safe_load_all(proc.stdout)
            if isinstance(doc, dict) and doc.get("kind") == "NetworkPolicy" and "mon-netpol" in doc.get("metadata", {}).get("name", "")
        }
        # Gateway policy renders as a superset including agent API, operator metrics, and GMP collector ingress rules
        self.assertIn(f"{_AGENT}-gw-mon-netpol", netpols)
        self.assertIn(f"{_AGENT}-cred-mon-netpol", netpols)
        self.assertIn(f"{_AGENT}-a2a-mon-netpol", netpols)
        gw_np = netpols[f"{_AGENT}-gw-mon-netpol"]
        self.assertEqual(len(gw_np["spec"]["ingress"]), 4)

    def test_monitoring_scraper_namespace_pattern_validation(self):
        # Valid DNS-1123 labels succeed
        for ns in ["monitoring", "openshift-user-workload-monitoring", "vm", "123", "on", "yes", "true"]:
            with self.subTest(ns=ns, valid=True):
                proc = subprocess.run(
                    [
                        "helm", "template", "test-release", str(_CHART),
                        *_REQUIRED,
                        "--set-string", f"platformAgent.monitoringScraperNamespace={ns}",
                    ],
                    capture_output=True, text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

        # Invalid namespace strings fail values schema validation
        for ns in ["Monitoring", "monitoring.svc", "monitoring_ns", "has space", "-starts-hyphen", "ends-hyphen-"]:
            with self.subTest(ns=ns, valid=False):
                proc = subprocess.run(
                    [
                        "helm", "template", "test-release", str(_CHART),
                        *_REQUIRED,
                        "--set-string", f"platformAgent.monitoringScraperNamespace={ns}",
                    ],
                    capture_output=True, text=True,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("/platformAgent/monitoringScraperNamespace", proc.stderr)

    def test_max_supported_agent_name_length_succeeds_under_service_monitor(self):
        # 46 chars is the operator's longest 63-character slot bound (63 - len("-credential-proxy")).
        # With shortened suffixes (<= 16 chars), no resource name or label value overflows 63 chars.
        max_name = "a" * 46
        proc = subprocess.run(
            [
                "helm", "template", "test-release", str(_CHART),
                *_REQUIRED, *_ON_PROMETHEUS,
                "--set", f"platformAgent.name={max_name}",
                "--set", "platformAgent.monitoringScraperNamespace=monitoring",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for doc in yaml.safe_load_all(proc.stdout):
            if not isinstance(doc, dict):
                continue
            name = doc.get("metadata", {}).get("name", "")
            self.assertLessEqual(
                len(name), 63,
                f"resource {doc.get('kind')}/{name} exceeds 63 characters ({len(name)})",
            )
            for k, v in doc.get("metadata", {}).get("labels", {}).items():
                self.assertLessEqual(
                    len(v), 63,
                    f"label {k}={v} on {doc.get('kind')}/{name} exceeds 63 characters ({len(v)})",
                )


if __name__ == "__main__":
    unittest.main()
