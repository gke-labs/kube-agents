"""Tests that the two copies of Hindsight's NetworkPolicies agree and confine hindsight-api.

    python3 -m unittest discover -s tests -p 'test_hindsight_network_policy.py' -v

Stdlib unittest, no pytest, matching the other suites in this directory.

Hindsight's NetworkPolicy objects (`hindsight-api` and `hindsight-postgresql`)
exist in two places: `charts/kube-agents/templates/hindsight.yaml` (rendered by
the Helm chart under `hindsight.networkPolicy=true` and each component's own
`networkPolicy.enabled`) and
`k8s-operator/config/integrations/hindsight/networkpolicy.yaml` (applied by the
kustomize dev path). `make chart-check` compares only CRD, RBAC,
admission-policy, and webhook copies, so these tests hold the two Hindsight
NetworkPolicy copies in step and verify that `hindsight-api` is selected for
both Ingress and Egress with least-privilege peers.

The chart's toggles are tested here too: `hindsight.api.networkPolicy.enabled`
and `hindsight.postgresql.networkPolicy.enabled` drop one policy without the
other, `hindsight.api.networkPolicy.additionalEgress` appends CIDR-and-port
rules in the shape of the PlatformAgent CR's `spec.networkPolicy.additionalEgress`
(and the values schema bounds them as the CRD does), and the install notes
warn when the store renders without the Postgres guard.
"""

import json
import os
import pathlib
import shutil
import subprocess
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_CHART_HINDSIGHT = _CHART / "templates" / "hindsight.yaml"
_CHART_SCHEMA = _CHART / "values.schema.json"
_CHART_CRD = _CHART / "crds" / "kubeagents.x-k8s.io_platformagents.yaml"
_KUSTOMIZE_NETPOL = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "networkpolicy.yaml"

_HINDSIGHT_API_POLICY = "hindsight-api"
_HINDSIGHT_POSTGRESQL_POLICY = "hindsight-postgresql"
# The template block the kustomize copy is compared against: from the guard on
# the hindsight-api policy to the PodMonitoring guard that follows the pair.
_CHART_NETPOL_BLOCK_START = "{{- if $apiNetworkPolicy }}"
_CHART_NETPOL_BLOCK_END = "{{- if .Values.hindsight.podMonitoring }}"
_ADDITIONAL_EGRESS = "hindsight.api.networkPolicy.additionalEgress"
_NOTES_WARNING = "WARNING: hindsight-postgresql renders with no NetworkPolicy"
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
    control flow (`{{- if ... }}` / `{{- end }}` / `{{- with ... }}`),
    `namespace: {{ .Release.Namespace }}`, the label helper
    `{{- include "kube-agents.labels" . | nindent 4 }}` and the one-line
    include that appends `additionalEgress`; stripping lines that contain
    `{{` leaves valid multi-document YAML whose `spec` is the default render
    and can be compared to
    k8s-operator/config/integrations/hindsight/networkpolicy.yaml.
    """
    text = _CHART_HINDSIGHT.read_text()
    start = text.index(_CHART_NETPOL_BLOCK_START)
    end = text.index(_CHART_NETPOL_BLOCK_END, start)
    block = text[start:end]
    clean_lines = [line for line in block.splitlines() if "{{" not in line]
    docs = [d for d in yaml.safe_load_all("\n".join(clean_lines)) if d]
    return {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
    }


def _helm(*args):
    cmd = [
        "helm",
        *args,
        str(_CHART),
        *_HELM_REQUIRED,
        "--set",
        "hindsight.enabled=true",
    ]
    return subprocess.run(cmd, capture_output=True, text=True)


def _render_chart(*extra):
    proc = _helm("template", "test-release", *extra)
    proc.check_returncode()
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _hindsight_netpols(docs):
    return {
        d["metadata"]["name"]: d
        for d in docs
        if d.get("kind") == "NetworkPolicy"
        and d["metadata"]["name"] in (_HINDSIGHT_API_POLICY, _HINDSIGHT_POSTGRESQL_POLICY)
    }


def _render_notes(*extra):
    """The install notes for a values shape, or None where helm needs a cluster for them.

    `helm template` never renders NOTES.txt. `helm install --dry-run=client`
    does, without a cluster on Helm 4 (the CI version); Helm 3 still asks the
    API server for its version first, and the caller skips on that.
    """
    proc = subprocess.run(
        [
            "helm",
            "install",
            "test-release",
            str(_CHART),
            *_HELM_REQUIRED,
            "--set",
            "hindsight.enabled=true",
            "--dry-run=client",
            "--namespace",
            "kubeagents-system",
            *extra,
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "KUBECONFIG": os.devnull},
    )
    if proc.returncode != 0 and "cluster unreachable" in proc.stderr:
        return None
    proc.check_returncode()
    return proc.stdout


def _crd_additional_egress_schema():
    crd = yaml.safe_load(_CHART_CRD.read_text())
    (version,) = crd["spec"]["versions"]
    spec = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
    return spec["properties"]["networkPolicy"]["properties"]["additionalEgress"]


def _chart_additional_egress_schema():
    schema = json.loads(_CHART_SCHEMA.read_text())
    hindsight = schema["properties"]["hindsight"]["properties"]
    return hindsight["api"]["properties"]["networkPolicy"]["properties"]["additionalEgress"]


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

    def test_chart_schema_bounds_additional_egress_as_the_crd_does(self):
        """The chart's additionalEgress accepts what the CR's does, no more.

        The CIDR prefix floor, the except pattern, the protocol enum, the port
        range and the item limits are copied from the CRD; this holds the copy
        in step so a loosening on either side shows up here.
        """
        crd = _crd_additional_egress_schema()
        chart = _chart_additional_egress_schema()
        crd_rule, chart_rule = crd["items"], chart["items"]
        crd_peer = crd_rule["properties"]["to"]["items"]
        chart_peer = chart_rule["properties"]["to"]["items"]
        crd_port = crd_rule["properties"]["ports"]["items"]
        chart_port = chart_rule["properties"]["ports"]["items"]

        self.assertEqual(crd["maxItems"], chart["maxItems"])
        self.assertEqual(sorted(crd_rule["required"]), sorted(chart_rule["required"]))
        for key in ("minItems", "maxItems"):
            self.assertEqual(crd_rule["properties"]["to"][key], chart_rule["properties"]["to"][key], key)
        self.assertEqual(crd_rule["properties"]["ports"]["maxItems"], chart_rule["properties"]["ports"]["maxItems"])
        self.assertEqual(sorted(crd_peer["required"]), sorted(chart_peer["required"]))
        for key in ("pattern", "minLength", "maxLength"):
            self.assertEqual(crd_peer["properties"]["cidr"][key], chart_peer["properties"]["cidr"][key], key)
        self.assertEqual(crd_peer["properties"]["except"]["maxItems"], chart_peer["properties"]["except"]["maxItems"])
        for key in ("pattern", "maxLength"):
            self.assertEqual(
                crd_peer["properties"]["except"]["items"][key],
                chart_peer["properties"]["except"]["items"][key],
                key,
            )
        self.assertEqual(sorted(crd_port["required"]), sorted(chart_port["required"]))
        self.assertEqual(crd_port["properties"]["protocol"]["enum"], chart_port["properties"]["protocol"]["enum"])
        for key in ("minimum", "maximum"):
            self.assertEqual(crd_port["properties"]["port"][key], chart_port["properties"]["port"][key], key)
        # Closed at every level, so a selector peer or a misspelt key is refused
        # rather than rendered as nothing.
        self.assertIs(False, chart_rule["additionalProperties"])
        self.assertIs(False, chart_peer["additionalProperties"])
        self.assertIs(False, chart_port["additionalProperties"])

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_rendered_chart_matches_kustomize_and_honours_toggle(self):
        rendered_netpols = _hindsight_netpols(_render_chart())
        self.assertEqual(set(self.kustomize), set(rendered_netpols))
        for name, kustomize_doc in self.kustomize.items():
            with self.subTest(policy=name):
                self.assertEqual(kustomize_doc["spec"], rendered_netpols[name]["spec"])

        disabled_netpols = _hindsight_netpols(_render_chart("--set", "hindsight.networkPolicy=false"))
        self.assertEqual({}, disabled_netpols)

        byo_gateway_netpols = _hindsight_netpols(_render_chart("--set", "litellm.enabled=false"))
        self.assertEqual(set(self.kustomize), set(byo_gateway_netpols))
        self.assertEqual(["Ingress"], byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"]["policyTypes"])
        self.assertNotIn("egress", byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"])
        self.assertEqual(
            self.kustomize[_HINDSIGHT_API_POLICY]["spec"]["ingress"],
            byo_gateway_netpols[_HINDSIGHT_API_POLICY]["spec"]["ingress"],
        )

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_each_policy_has_its_own_toggle_under_the_umbrella(self):
        """Dropping hindsight-api's policy keeps hindsight-postgresql's, and the reverse.

        The Postgres policy is the only guard on a database with no password,
        so the api-side escape for a bring-your-own gateway must not take it
        along. The umbrella still wins: false renders neither, whatever the
        component toggles say, as it did before they existed.
        """
        for extra in (
            ("--set", "hindsight.api.networkPolicy.enabled=false"),
            ("--set", "hindsight.api.networkPolicy.enabled=false", "--set", "litellm.enabled=false"),
        ):
            with self.subTest(extra=extra):
                api_off = _hindsight_netpols(_render_chart(*extra))
                self.assertEqual({_HINDSIGHT_POSTGRESQL_POLICY}, set(api_off))
                self.assertEqual(
                    self.kustomize[_HINDSIGHT_POSTGRESQL_POLICY]["spec"],
                    api_off[_HINDSIGHT_POSTGRESQL_POLICY]["spec"],
                )

        pg_off = _hindsight_netpols(_render_chart("--set", "hindsight.postgresql.networkPolicy.enabled=false"))
        self.assertEqual({_HINDSIGHT_API_POLICY}, set(pg_off))
        self.assertEqual(self.kustomize[_HINDSIGHT_API_POLICY]["spec"], pg_off[_HINDSIGHT_API_POLICY]["spec"])

        umbrella_off = _hindsight_netpols(
            _render_chart(
                "--set",
                "hindsight.networkPolicy=false",
                "--set",
                "hindsight.api.networkPolicy.enabled=true",
                "--set",
                "hindsight.postgresql.networkPolicy.enabled=true",
            )
        )
        self.assertEqual({}, umbrella_off)

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_additional_egress_is_appended_in_the_operators_shape(self):
        """additionalEgress rules land after the gateway rule, as ipBlock peers.

        Under litellm.enabled=false a non-empty list also turns the egress
        half on, so the custom gateway is named rather than the whole policy
        dropped: DNS, Postgres and the in-cluster gateway rule render as they
        do by default, then the caller's rules. An except is carried through,
        a rule without ports opens every port to its peers (the NetworkPolicy
        API's reading), and a null list is the empty list.
        """
        rule_sets = [
            "--set",
            f"{_ADDITIONAL_EGRESS}[0].to[0].cidr=10.200.0.0/16",
            "--set",
            f"{_ADDITIONAL_EGRESS}[0].to[0].except[0]=10.200.1.0/24",
            "--set",
            f"{_ADDITIONAL_EGRESS}[0].to[1].cidr=192.168.5.7",
            "--set",
            f"{_ADDITIONAL_EGRESS}[0].ports[0].protocol=TCP",
            "--set",
            f"{_ADDITIONAL_EGRESS}[0].ports[0].port=8443",
            "--set",
            f"{_ADDITIONAL_EGRESS}[1].to[0].cidr=fd00::/48",
        ]
        expected_tail = [
            {
                "ports": [{"port": 8443, "protocol": "TCP"}],
                "to": [
                    {"ipBlock": {"cidr": "10.200.0.0/16", "except": ["10.200.1.0/24"]}},
                    {"ipBlock": {"cidr": "192.168.5.7"}},
                ],
            },
            {"to": [{"ipBlock": {"cidr": "fd00::/48"}}]},
        ]
        default_egress = self.kustomize[_HINDSIGHT_API_POLICY]["spec"]["egress"]

        for litellm in ("true", "false"):
            with self.subTest(litellm_enabled=litellm):
                netpols = _hindsight_netpols(_render_chart("--set", f"litellm.enabled={litellm}", *rule_sets))
                self.assertEqual(set(self.kustomize), set(netpols))
                api = netpols[_HINDSIGHT_API_POLICY]["spec"]
                self.assertEqual(["Ingress", "Egress"], api["policyTypes"])
                self.assertEqual(self.kustomize[_HINDSIGHT_API_POLICY]["spec"]["ingress"], api["ingress"])
                self.assertEqual(default_egress + expected_tail, api["egress"])
                self.assertEqual(
                    self.kustomize[_HINDSIGHT_POSTGRESQL_POLICY]["spec"],
                    netpols[_HINDSIGHT_POSTGRESQL_POLICY]["spec"],
                )

        null_list = _hindsight_netpols(_render_chart("--set", f"{_ADDITIONAL_EGRESS}=null"))
        self.assertEqual(self.kustomize[_HINDSIGHT_API_POLICY]["spec"], null_list[_HINDSIGHT_API_POLICY]["spec"])

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_the_schema_refuses_an_additional_egress_rule_the_crd_would(self):
        """Helm refuses the shapes the operator refuses, naming the key.

        Helm 3 prints the path dotted and Helm 4 as a JSON pointer; both carry
        the leaf, which is what is asserted.
        """
        refusals = {
            "a whole-internet peer": (
                [f"{_ADDITIONAL_EGRESS}[0].to[0].cidr=0.0.0.0/0"],
                "cidr",
            ),
            "ports with no peer": (
                [f"{_ADDITIONAL_EGRESS}[0].ports[0].protocol=TCP", f"{_ADDITIONAL_EGRESS}[0].ports[0].port=443"],
                "to",
            ),
            "a selector peer": (
                [f"{_ADDITIONAL_EGRESS}[0].to[0].podSelector.matchLabels.app=gateway"],
                "cidr",
            ),
            "a lower-case protocol": (
                [
                    f"{_ADDITIONAL_EGRESS}[0].to[0].cidr=10.0.0.0/24",
                    f"{_ADDITIONAL_EGRESS}[0].ports[0].protocol=tcp",
                    f"{_ADDITIONAL_EGRESS}[0].ports[0].port=443",
                ],
                "protocol",
            ),
            "a misspelt toggle": (["hindsight.api.networkPolicy.enable=false"], "enable"),
            "a misspelt postgres toggle": (["hindsight.postgresql.networkPolicy.enable=false"], "enable"),
        }
        for what, (sets, leaf) in refusals.items():
            with self.subTest(what=what):
                args = []
                for item in sets:
                    args += ["--set", item]
                proc = _helm("template", "test-release", *args)
                self.assertNotEqual(0, proc.returncode, f"{what} rendered: {proc.stdout[:200]}")
                self.assertIn("schema", proc.stderr)
                self.assertIn(leaf, proc.stderr)

    @unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
    def test_the_install_notes_warn_when_postgres_renders_unguarded(self):
        """The one loud signal for a passwordless Postgres with no policy.

        Both routes to that state warn and name the value that caused it; the
        default and the api-only drop do not.
        """
        default_notes = _render_notes()
        if default_notes is None:
            self.skipTest("this helm needs a cluster to render NOTES.txt (Helm 3); Helm 4 does not")
        self.assertNotIn(_NOTES_WARNING, default_notes)

        api_off_notes = _render_notes("--set", "hindsight.api.networkPolicy.enabled=false")
        self.assertNotIn(_NOTES_WARNING, api_off_notes)

        pg_off_notes = _render_notes("--set", "hindsight.postgresql.networkPolicy.enabled=false")
        self.assertIn(_NOTES_WARNING, pg_off_notes)
        self.assertIn("hindsight.postgresql.networkPolicy.enabled=false", pg_off_notes)

        umbrella_off_notes = _render_notes("--set", "hindsight.networkPolicy=false")
        self.assertIn(_NOTES_WARNING, umbrella_off_notes)
        self.assertIn("(hindsight.networkPolicy=false)", umbrella_off_notes)


if __name__ == "__main__":
    unittest.main()
