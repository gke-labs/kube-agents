"""The chart serves the operator's own metrics and renders a PodMonitoring for them.

The operator exports status.usage and status.phase of every PlatformAgent as
gauges on its metrics port (k8s-operator/internal/controller/usage_gauges.go),
so GKE Managed Prometheus reads the same numbers the poller wrote without a
second scrape of the producers. Three things have to agree for that scrape to
work: the port the manager binds (--metrics-bind-address), the containerPort
the Deployment declares, and the number in the PodMonitoring; and the
selector has to be the operator's pod labels. The render tests need a helm
binary, which the agent-startup job lacks.

Run: python3 -m unittest discover -s tests -p 'test_chart_operator_monitoring.py' -v
"""

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
_GMP_API = "monitoring.googleapis.com/v1/PodMonitoring"
_GMP_NAMESPACE = "gke-gmp-system"
_METRICS_PORT_NAME = "metrics"
_BIND_FLAG = "--metrics-bind-address="
_SECURE_FLAG = "--metrics-secure=false"


def _render(*extra, api_versions=()):
    cmd = ["helm", "template", "rel", str(_CHART), *_REQUIRED, *extra]
    for api in api_versions:
        cmd += ["--api-versions", api]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _manager(docs):
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        for c in d["spec"]["template"]["spec"]["containers"]:
            if c.get("name") == "manager":
                return d, c
    raise AssertionError("no manager container rendered")


def _operator_policies(docs):
    return [d for d in docs if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"].endswith("-operator-netpol")]


def _operator_monitorings(docs):
    return [d for d in docs if d.get("kind") == "PodMonitoring" and d["metadata"]["name"].endswith("-operator-monitoring")]


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class OperatorMonitoringRenderTest(unittest.TestCase):
    def test_the_manager_serves_metrics_on_the_declared_port_in_plain_http(self):
        docs = _render()
        _, manager = _manager(docs)
        ports = {p["name"]: p["containerPort"] for p in manager.get("ports", [])}
        self.assertIn(_METRICS_PORT_NAME, ports)
        bind = [a for a in manager["args"] if a.startswith(_BIND_FLAG)]
        self.assertEqual([f"{_BIND_FLAG}:{ports[_METRICS_PORT_NAME]}"], bind)
        self.assertIn(_SECURE_FLAG, manager["args"])

    def test_the_pod_monitoring_follows_the_port_and_the_pod_labels(self):
        docs = _render(api_versions=(_GMP_API,))
        deployment, manager = _manager(docs)
        ports = {p["name"]: p["containerPort"] for p in manager.get("ports", [])}
        monitorings = _operator_monitorings(docs)
        self.assertEqual(1, len(monitorings), [d["metadata"]["name"] for d in docs if d.get("kind") == "PodMonitoring"])
        pm = monitorings[0]
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], pm["spec"]["selector"]["matchLabels"])
        self.assertEqual([ports[_METRICS_PORT_NAME]], [e["port"] for e in pm["spec"]["endpoints"]])

    def test_the_default_follows_the_cluster(self):
        self.assertEqual([], _operator_monitorings(_render()))
        self.assertEqual(1, len(_operator_monitorings(_render(api_versions=(_GMP_API,)))))

    def test_true_renders_it_without_asking_the_cluster_and_false_never_does(self):
        self.assertEqual(1, len(_operator_monitorings(_render("--set", "operator.metrics.podMonitoring=true"))))
        self.assertEqual([], _operator_monitorings(_render("--set", "operator.metrics.podMonitoring=false", api_versions=(_GMP_API,))))

    def test_a_release_that_predates_the_key_still_renders(self):
        """helm upgrade --reuse-values renders against the old release's values, which
        have no operator.metrics key; a bare field read on nil aborts the upgrade."""
        docs = _render("--set", "operator.metrics=null", api_versions=[_GMP_API])
        _, manager = _manager(docs)
        self.assertNotIn(_METRICS_PORT_NAME, {p["name"] for p in manager.get("ports", [])})
        self.assertEqual([], _operator_monitorings(docs))
        self.assertEqual([], _operator_policies(docs))

    def test_the_listener_is_bounded_to_the_collector(self):
        """The listener is plain HTTP, so a NetworkPolicy is what bounds who reads it:
        the GMP collector's namespace on the metrics port, probes on the health port."""
        docs = _render(api_versions=[_GMP_API])
        deployment, manager = _manager(docs)
        ports = {p["name"]: p["containerPort"] for p in manager["ports"]}
        policies = _operator_policies(docs)
        self.assertEqual(1, len(policies), "one NetworkPolicy over the operator pod")
        policy = policies[0]
        self.assertEqual(deployment["spec"]["selector"]["matchLabels"], policy["spec"]["podSelector"]["matchLabels"])
        self.assertEqual(["Ingress"], policy["spec"]["policyTypes"])
        by_port = {}
        for rule in policy["spec"]["ingress"]:
            for port in rule["ports"]:
                by_port.setdefault(port["port"], []).append(rule.get("from"))
        self.assertEqual(
            [[{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": _GMP_NAMESPACE}}}]],
            by_port[ports[_METRICS_PORT_NAME]],
            "the metrics port admits the collector's namespace and nothing else",
        )
        self.assertEqual([None], by_port[ports["health"]], "probes reach the health port from anywhere")
        self.assertNotIn("webhook-server", ports, "webhooks are off by default")
        self.assertEqual(sorted(by_port), sorted([ports["health"], ports[_METRICS_PORT_NAME]]), "no rule beyond the two ports the pod has")

    def test_the_scraper_namespace_knob_also_admits_its_namespace(self):
        docs = _render("--set", "platformAgent.monitoringScraperNamespace=monitoring", api_versions=[_GMP_API])
        _, manager = _manager(docs)
        metrics_port = {p["name"]: p["containerPort"] for p in manager["ports"]}[_METRICS_PORT_NAME]
        froms = [
            peer
            for rule in _operator_policies(docs)[0]["spec"]["ingress"]
            if any(p["port"] == metrics_port for p in rule["ports"])
            for peer in rule["from"]
        ]
        self.assertIn({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}, froms)

    def test_webhooks_on_admits_the_webhook_port(self):
        docs = _render("--set", "operator.webhooks.enabled=true", api_versions=[_GMP_API])
        _, manager = _manager(docs)
        ports = {p["name"]: p["containerPort"] for p in manager["ports"]}
        open_ports = {p["port"] for rule in _operator_policies(docs)[0]["spec"]["ingress"] if "from" not in rule for p in rule["ports"]}
        self.assertEqual({ports["health"], ports["webhook-server"]}, open_ports)

    def test_metrics_off_binds_nothing_and_renders_no_scrape(self):
        docs = _render("--set", "operator.metrics.enabled=false", api_versions=(_GMP_API,))
        _, manager = _manager(docs)
        self.assertIn(f"{_BIND_FLAG}0", manager["args"])
        self.assertNotIn(_METRICS_PORT_NAME, {p["name"] for p in manager.get("ports", [])})
        self.assertEqual([], _operator_monitorings(docs))


if __name__ == "__main__":
    unittest.main()
