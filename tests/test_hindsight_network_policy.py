"""Tests that the two copies of Hindsight's NetworkPolicies agree and confine hindsight-api.

    python3 -m unittest discover -s tests -p 'test_hindsight_network_policy.py' -v

Stdlib unittest, no pytest, matching the other suites in this directory.

Hindsight's NetworkPolicy objects (`hindsight-api` and `hindsight-postgresql`)
exist in two places: `charts/kube-agents/templates/hindsight.yaml` (rendered by
the Helm chart when `hindsight.networkPolicy=true`) and
`k8s-operator/config/integrations/hindsight/networkpolicy.yaml` (applied by the
kustomize dev path). `make chart-check` compares only CRD, RBAC,
admission-policy, and webhook copies, so these tests hold the two Hindsight
NetworkPolicy copies in step and verify that `hindsight-api` is selected for
both Ingress and Egress with least-privilege peers.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_CHART_HINDSIGHT = _CHART / "templates" / "hindsight.yaml"
_KUSTOMIZE_NETPOL = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "networkpolicy.yaml"

_HINDSIGHT_API_POLICY = "hindsight-api"
_HINDSIGHT_POSTGRESQL_POLICY = "hindsight-postgresql"
_HELM_REQUIRED = [
    "--set",
    "platformAgent.harness.clusterName=ci-cluster",
    "--set",
    "platformAgent.harness.location=us-central1",
    "--set",
    "platformAgent.harness.projectId=ci-project",
]


def _kustomize_policies():
    docs = [d for d in yaml.safe_load_all(_KUSTOMIZE_NETPOL.read_text()) if d]
    by_name = {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
    }
    return by_name


def _chart_netpol_policies():
    """Extract the NetworkPolicy documents from the chart template without helm.

    The NetworkPolicy block inside templates/hindsight.yaml has only Helm
    control flow (`{{- if ... }}` / `{{- end }}`), `namespace: {{ .Release.Namespace }}`,
    and the label helper `{{- include "kube-agents.labels" . | nindent 4 }}`;
    stripping lines that contain `{{` leaves valid multi-document YAML whose
    `spec` can be parsed directly and compared to
    k8s-operator/config/integrations/hindsight/networkpolicy.yaml.
    """
    text = _CHART_HINDSIGHT.read_text()
    start = text.index("{{- if .Values.hindsight.networkPolicy }}")
    end = text.index("{{- if .Values.hindsight.podMonitoring }}", start)
    block = text[start:end]
    clean_lines = [line for line in block.splitlines() if "{{" not in line]
    docs = [d for d in yaml.safe_load_all("\n".join(clean_lines)) if d]
    return {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
    }


def _render_chart(*extra):
    cmd = [
        "helm",
        "template",
        "test-release",
        str(_CHART),
        *_HELM_REQUIRED,
        "--set",
        "hindsight.enabled=true",
        *extra,
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


class HindsightNetworkPolicyTest(unittest.TestCase):
    def setUp(self):
        self.kustomize = _kustomize_policies()

    def test_both_hindsight_workloads_have_a_network_policy(self):
        self.assertEqual(
            {_HINDSIGHT_API_POLICY, _HINDSIGHT_POSTGRESQL_POLICY},
            set(self.kustomize),
            "networkpolicy.yaml must carry a NetworkPolicy for both hindsight-api and hindsight-postgresql",
        )

    def test_hindsight_api_policy_confines_ingress_and_egress(self):
        api_policy = self.kustomize[_HINDSIGHT_API_POLICY]
        spec = api_policy["spec"]
        self.assertEqual(
            {"app.kubernetes.io/name": "hindsight", "app.kubernetes.io/component": "api"},
            spec["podSelector"]["matchLabels"],
        )
        self.assertEqual(["Ingress", "Egress"], spec["policyTypes"])

        # Ingress: same-namespace pods and gke-gmp-system on TCP 8888.
        self.assertEqual(
            [
                {
                    "from": [{"podSelector": {}}],
                    "ports": [{"port": 8888, "protocol": "TCP"}],
                },
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "gke-gmp-system"}
                            }
                        }
                    ],
                    "ports": [{"port": 8888, "protocol": "TCP"}],
                },
            ],
            spec["ingress"],
        )

        # Egress: DNS (53 UDP/TCP), hindsight-postgresql (5432 TCP), and
        # inference-gateway backing pods (litellm / standalone-replay on 80, 4000, 8080 TCP).
        egress = spec["egress"]
        self.assertEqual(3, len(egress))

        dns_rule, pg_rule, llm_rule = egress
        self.assertEqual(
            {
                "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}],
                "to": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                    },
                    {
                        "namespaceSelector": {
                            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                        },
                        "podSelector": {"matchLabels": {"k8s-app": "node-local-dns"}},
                    },
                    {"ipBlock": {"cidr": "169.254.20.10/32"}},
                    {"ipBlock": {"cidr": "169.254.169.254/32"}},
                    {"ipBlock": {"cidr": "10.96.0.10/32"}},
                    {
                        "ipBlock": {
                            "cidr": "0.0.0.0/0",
                            "except": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
                        }
                    },
                ],
            },
            dns_rule,
        )
        self.assertEqual(
            {
                "ports": [{"port": 5432, "protocol": "TCP"}],
                "to": [
                    {
                        "podSelector": {
                            "matchLabels": {
                                "app.kubernetes.io/name": "hindsight",
                                "app.kubernetes.io/component": "postgresql",
                            }
                        }
                    }
                ],
            },
            pg_rule,
        )
        self.assertEqual(
            {
                "ports": [
                    {"port": 80, "protocol": "TCP"},
                    {"port": 4000, "protocol": "TCP"},
                    {"port": 8080, "protocol": "TCP"},
                ],
                "to": [
                    {"podSelector": {"matchLabels": {"app": "litellm"}}},
                    {"podSelector": {"matchLabels": {"app": "standalone-replay"}}},
                ],
            },
            llm_rule,
        )

    def test_chart_template_carries_both_network_policy_specs(self):
        chart_policies = _chart_netpol_policies()
        self.assertEqual(set(self.kustomize), set(chart_policies))
        for name, policy in self.kustomize.items():
            with self.subTest(policy=name):
                self.assertEqual(policy["spec"], chart_policies[name]["spec"])
                self.assertEqual(policy["metadata"]["labels"], chart_policies[name]["metadata"]["labels"])

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_rendered_chart_matches_kustomize_and_honours_toggle(self):
        docs = _render_chart()
        rendered_netpols = {
            d["metadata"]["name"]: d
            for d in docs
            if d.get("kind") == "NetworkPolicy"
            and d["metadata"]["name"] in self.kustomize
        }
        self.assertEqual(set(self.kustomize), set(rendered_netpols))
        for name, kustomize_doc in self.kustomize.items():
            with self.subTest(policy=name):
                self.assertEqual(kustomize_doc["spec"], rendered_netpols[name]["spec"])

        disabled_docs = _render_chart("--set", "hindsight.networkPolicy=false")
        disabled_netpols = [
            d["metadata"]["name"]
            for d in disabled_docs
            if d.get("kind") == "NetworkPolicy"
            and d["metadata"]["name"] in self.kustomize
        ]
        self.assertEqual([], disabled_netpols)

        byo_gateway_docs = _render_chart("--set", "litellm.enabled=false")
        byo_gateway_netpols = {
            d["metadata"]["name"]: d
            for d in byo_gateway_docs
            if d.get("kind") == "NetworkPolicy"
            and d["metadata"]["name"] in self.kustomize
        }
        self.assertEqual(set(self.kustomize), set(byo_gateway_netpols))
        self.assertEqual(["Ingress"], byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"]["policyTypes"])
        self.assertNotIn("egress", byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"])
        self.assertEqual(
            self.kustomize[_HINDSIGHT_API_POLICY]["spec"]["ingress"],
            byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"]["ingress"],
        )


if __name__ == "__main__":
    unittest.main()
