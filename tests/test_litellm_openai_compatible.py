"""Helm render tests for `litellm.modelProvider=openai_compatible`.

The provider points the LiteLLM gateway at any server speaking the OpenAI
chat-completions API. These tests drive `helm template` and assert on the rendered
objects: the gateway config (LiteLLM's hosted_vllm handler, the server's api_base,
and an ssl_verify path only with a CA), the gateway's environment (an optional
HOSTED_VLLM_API_KEY and no SaaS key), the CA mount, every render-time refusal, the
egress rule in the static litellm-policy, and the annotation that carries the same
rule to the operator's copy. The last class proves the existing providers render
exactly what they rendered before the provider existed.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"

# A chart directory to compare the existing providers' renders against, for example
# one extracted from the branch point with
# `git archive <base> charts | tar -x -C <dir>` and given as <dir>/charts/kube-agents.
# Unset, that comparison is skipped; the inert-values comparison still runs.
BASE_CHART_DIR_ENV = "KUBE_AGENTS_BASE_CHART_DIR"

PROVIDER = "openai_compatible"
EXISTING_PROVIDERS = ("gemini", "anthropic", "openai", "vertex_ai")
LITELLM_HANDLER = "hosted_vllm"
MODEL_NAME = "meta-llama/Llama-3.1-8B-Instruct"
MODEL_ALIASES = ("model-default", "hermes-agent", MODEL_NAME)
RELEASE_NAME = "test-release"
RELEASE_NAMESPACE = "kubeagents-system"

SERVER_NAMESPACE = "models"
SERVER_TARGET_PORT = 8000
SERVICE_API_BASE = f"http://vllm.{SERVER_NAMESPACE}.svc.cluster.local:8000/v1"
SVC_NAMESPACE = "inference"
SVC_SERVICE_API_BASE = f"http://vllm.{SVC_NAMESPACE}.svc:8000/v1"
# Two labels are a hostname, not <svc>.<ns>: public endpoints such as this have no longer name.
TWO_LABEL_PUBLIC_API_BASE = "https://openrouter.ai/api/v1"
TWO_LABEL_HOST_API_BASE = f"http://vllm.{SVC_NAMESPACE}:8000/v1"
SVC_HINT = f"write it as <svc>.<ns>.svc (vllm.{SVC_NAMESPACE}.svc)"
BARE_SERVICE_API_BASE = "http://vllm:8000/v1"
PRIVATE_IP_PORT = 8443
PRIVATE_IP_API_BASE = f"https://10.20.0.5:{PRIVATE_IP_PORT}/v1"
PRIVATE_CIDRS = ["10.20.0.0/24", "fd00:1::/64"]
PRIVATE_HOSTNAME_HTTPS_API_BASE = "https://llm.corp.example/v1"
PRIVATE_HOSTNAME_HTTP_API_BASE = "http://llm.corp.example/v1"
HTTPS_DEFAULT_PORT = 443
HTTP_DEFAULT_PORT = 80
PUBLIC_API_BASE = "https://api.inference.example.com/v1"

CREDENTIALS_OBJECT = "platform-agent-secrets"
CREDENTIALS_ENTRY = "OPENAI_COMPATIBLE_API_KEY"
GATEWAY_KEY_ENV = "HOSTED_VLLM_API_KEY"
COST_MAP_ENV = "LITELLM_LOCAL_MODEL_COST_MAP"
COST_MAP_VALUE = "True"
SAAS_KEY_ENVS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
GLOBAL_TLS_ENVS = ("SSL_CERT_FILE", "SSL_VERIFY", "REQUESTS_CA_BUNDLE")

CA_OBJECT = "corp-ca"
CA_KEY = "bundle.pem"
CA_VOLUME = "openai-compatible-ca"
CA_DIR = "/etc/litellm/openai-compatible-ca"
CA_DEFAULT_MODE = 0o444

UPSTREAM_EGRESS_ANNOTATION = "kubeagents.x-k8s.io/litellm-upstream-egress"
ANNOTATION_CONFLICT_FRAGMENT = "contradicts"
NAMESPACE_LABEL = "kubernetes.io/metadata.name"
# The operator's cap on the annotation's cidrs list (maxLiteLLMUpstreamCIDRs).
MAX_CIDRS = 50

HARNESS_ARGS = [
    "--set",
    "platformAgent.harness.projectId=my-proj",
    "--set",
    "platformAgent.harness.clusterName=my-cluster",
    "--set",
    "platformAgent.harness.location=us-central1",
]
OPERATOR_OFF_ARGS = ["--set", "operator.enabled=false"]


def _helm_template(*extra_args: str, chart_dir: pathlib.Path = CHART_DIR, check: bool = True):
    return subprocess.run(
        ["helm", "template", RELEASE_NAME, str(chart_dir), "--namespace", RELEASE_NAMESPACE, *extra_args],
        capture_output=True,
        text=True,
        check=check,
    )


def _render(openai_compatible: dict | None = None, *extra_args: str, values: dict | None = None, check: bool = True):
    """Renders the chart with the provider selected and the given litellm.openaiCompatible.

    Values go through a file rather than --set: an annotation holding JSON has commas,
    which --set reads as separators.
    """
    litellm = {"modelProvider": PROVIDER, "modelDefaultName": MODEL_NAME}
    if openai_compatible is not None:
        litellm["openaiCompatible"] = openai_compatible
    merged = {"litellm": litellm}
    for key, value in (values or {}).items():
        if key == "litellm":
            litellm.update(value)
        else:
            merged[key] = value
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(merged, handle)
        values_path = handle.name
    try:
        return _helm_template("-f", values_path, *extra_args, *HARNESS_ARGS, check=check)
    finally:
        os.unlink(values_path)


def _docs(rendered: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _find(rendered: str, kind: str, name: str | None = None) -> dict | None:
    for doc in _docs(rendered):
        if doc.get("kind") == kind and (name is None or doc["metadata"]["name"] == name):
            return doc
    return None


def _model_list(rendered: str) -> list[dict]:
    configmap = _find(rendered, "ConfigMap", "litellm-config")
    return yaml.safe_load(configmap["data"]["config.yaml"])["model_list"]


def _gateway_pod_spec(rendered: str) -> dict:
    return _find(rendered, "Deployment", "litellm")["spec"]["template"]["spec"]


def _gateway_env(rendered: str) -> dict[str, dict]:
    return {entry["name"]: entry for entry in _gateway_pod_spec(rendered)["containers"][0]["env"]}


def _policy_egress(rendered: str) -> list[dict]:
    return _find(rendered, "NetworkPolicy", "litellm-policy")["spec"]["egress"]


def _cr_annotations(rendered: str) -> dict:
    return (_find(rendered, "PlatformAgent") or {}).get("metadata", {}).get("annotations") or {}


SERVICE_VALUES = {"apiBase": SERVICE_API_BASE, "targetPort": SERVER_TARGET_PORT}
CIDR_VALUES = {"apiBase": PRIVATE_IP_API_BASE, "egressCIDRs": PRIVATE_CIDRS}
PUBLIC_VALUES = {"apiBase": PUBLIC_API_BASE}


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class GatewayConfigTest(unittest.TestCase):
    def test_every_alias_routes_through_hosted_vllm_to_the_api_base(self) -> None:
        models = _model_list(_render(SERVICE_VALUES).stdout)
        self.assertEqual([entry["model_name"] for entry in models], list(MODEL_ALIASES))
        for entry in models:
            with self.subTest(entry["model_name"]):
                params = entry["litellm_params"]
                self.assertEqual(params["model"], f"{LITELLM_HANDLER}/{MODEL_NAME}")
                self.assertEqual(params["api_base"], SERVICE_API_BASE)
                # The key arrives as HOSTED_VLLM_API_KEY; no other key may be named.
                self.assertNotIn("api_key", params)
                self.assertNotIn("ssl_verify", params)

    def test_ssl_verify_names_the_mounted_bundle_only_with_a_ca(self) -> None:
        models = _model_list(_render({**SERVICE_VALUES, "caBundleSecretRef": {"name": CA_OBJECT, "key": CA_KEY}}).stdout)
        for entry in models:
            with self.subTest(entry["model_name"]):
                self.assertEqual(entry["litellm_params"]["ssl_verify"], f"{CA_DIR}/{CA_KEY}")

    def test_max_tokens_still_applies(self) -> None:
        max_tokens = 4096
        models = _model_list(_render(SERVICE_VALUES, values={"litellm": {"maxTokens": max_tokens}}).stdout)
        self.assertEqual({entry["litellm_params"]["max_tokens"] for entry in models}, {max_tokens})


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class GatewayEnvironmentTest(unittest.TestCase):
    def test_the_key_is_optional_and_read_from_the_credentials_secret(self) -> None:
        env = _gateway_env(_render(SERVICE_VALUES).stdout)
        self.assertEqual(
            env[GATEWAY_KEY_ENV]["valueFrom"]["secretKeyRef"],
            {"name": CREDENTIALS_OBJECT, "key": CREDENTIALS_ENTRY, "optional": True},
        )

    def test_the_cost_map_is_read_locally(self) -> None:
        env = _gateway_env(_render(SERVICE_VALUES).stdout)
        self.assertEqual(env[COST_MAP_ENV]["value"], COST_MAP_VALUE)

    def test_no_saas_key_and_no_process_wide_tls_setting(self) -> None:
        env = _gateway_env(_render({**SERVICE_VALUES, "caBundleSecretRef": {"name": CA_OBJECT}}).stdout)
        for name in (*SAAS_KEY_ENVS, *GLOBAL_TLS_ENVS):
            with self.subTest(name):
                self.assertNotIn(name, env)

    def test_other_providers_keep_their_keys_and_get_none_of_these(self) -> None:
        for provider in ("gemini", "openai"):
            with self.subTest(provider):
                env = _gateway_env(
                    _helm_template("--set", f"litellm.modelProvider={provider}", *HARNESS_ARGS).stdout
                )
                for name in SAAS_KEY_ENVS:
                    self.assertIn(name, env)
                self.assertNotIn(GATEWAY_KEY_ENV, env)
                self.assertNotIn(COST_MAP_ENV, env)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class CABundleMountTest(unittest.TestCase):
    def test_the_named_key_is_mounted_read_only(self) -> None:
        pod = _gateway_pod_spec(
            _render({**SERVICE_VALUES, "caBundleSecretRef": {"name": CA_OBJECT, "key": CA_KEY}}).stdout
        )
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(
            volumes[CA_VOLUME]["secret"],
            {"secretName": CA_OBJECT, "defaultMode": CA_DEFAULT_MODE, "items": [{"key": CA_KEY, "path": CA_KEY}]},
        )
        mounts = {mount["name"]: mount for mount in pod["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts[CA_VOLUME], {"name": CA_VOLUME, "mountPath": CA_DIR, "readOnly": True})

    def test_no_volume_without_a_ca(self) -> None:
        pod = _gateway_pod_spec(_render(SERVICE_VALUES).stdout)
        self.assertNotIn(CA_VOLUME, {volume["name"] for volume in pod["volumes"]})
        self.assertNotIn(CA_VOLUME, {mount["name"] for mount in pod["containers"][0]["volumeMounts"]})


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class RenderRefusalTest(unittest.TestCase):
    def _assert_fails(self, openai_compatible: dict | None, fragment: str, values: dict | None = None) -> None:
        res = _render(openai_compatible, values=values, check=False)
        self.assertNotEqual(res.returncode, 0, res.stdout[-500:])
        self.assertIn(fragment, res.stderr)

    def test_model_name_is_required(self) -> None:
        self._assert_fails(SERVICE_VALUES, "needs litellm.modelDefaultName", values={"litellm": {"modelDefaultName": ""}})

    def test_api_base_is_required(self) -> None:
        self._assert_fails(None, "needs litellm.openaiCompatible.apiBase")

    def test_api_base_must_be_http_or_https(self) -> None:
        for api_base in ("ftp://vllm.models:8000/v1", "HTTPS://llm.example.com/v1", "vllm.models:8000/v1"):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "must start with http:// or https://")

    def test_unreadable_authorities_are_refused(self) -> None:
        for api_base in (
            "https://user:pass@llm.example.com/v1",
            "https://llm.example.com:https/v1",
            "https://llm.example.com?x=1",
            "http://fd00::1:8000/v1",
            "https:///v1",
        ):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "cannot read the host and port")

    def test_port_must_be_in_range(self) -> None:
        for api_base in ("https://llm.example.com:0/v1", "https://llm.example.com:70000/v1"):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base, "egressCIDRs": PRIVATE_CIDRS}, "is outside 1-65535")

    def test_an_in_cluster_service_needs_target_port(self) -> None:
        for api_base in (SERVICE_API_BASE, SVC_SERVICE_API_BASE, BARE_SERVICE_API_BASE):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "litellm.openaiCompatible.targetPort is required")

    def test_a_service_namespace_must_be_a_namespace_name(self) -> None:
        self._assert_fails(
            {"apiBase": "http://vllm.my_models.svc:8000/v1", "targetPort": SERVER_TARGET_PORT},
            "is not a valid namespace name",
        )

    def test_a_hostname_off_443_needs_egress_cidrs(self) -> None:
        for api_base in (PRIVATE_HOSTNAME_HTTP_API_BASE, "https://llm.corp.example:8443/v1"):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "a NetworkPolicy cannot name a DNS name")

    def test_a_two_label_host_off_443_is_a_hostname_and_hints_at_the_svc_form(self) -> None:
        for openai_compatible in (
            {"apiBase": TWO_LABEL_HOST_API_BASE},
            # A targetPort does not turn it into a Service.
            {"apiBase": TWO_LABEL_HOST_API_BASE, "targetPort": SERVER_TARGET_PORT},
            # Not even on 443, where a hostname otherwise needs no rule: the
            # targetPort says a Service was meant, and nothing would reach it.
            {"apiBase": "https://vllm.inference/v1", "targetPort": SERVER_TARGET_PORT},
        ):
            with self.subTest(openai_compatible):
                self._assert_fails(openai_compatible, SVC_HINT)

    def test_only_a_two_label_host_gets_the_svc_hint(self) -> None:
        res = _render({"apiBase": PRIVATE_HOSTNAME_HTTP_API_BASE}, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("<svc>.<ns>.svc", res.stderr)

    def test_any_non_service_host_with_a_target_port_is_refused(self) -> None:
        for api_base in ("https://vllm.corp.internal/v1", "https://8.8.8.8/v1"):
            with self.subTest(api_base):
                self._assert_fails(
                    {"apiBase": api_base, "targetPort": SERVER_TARGET_PORT},
                    "targetPort is not used",
                )

    def test_a_numeric_namespace_is_quoted_in_the_static_policy(self) -> None:
        res = _helm_template(
            *HARNESS_ARGS, *OPERATOR_OFF_ARGS,
            "--set", "litellm.modelProvider=openai_compatible",
            "--set", "litellm.modelDefaultName=m",
            "--set", "litellm.openaiCompatible.apiBase=http://llm.123.svc/v1",
            "--set", f"litellm.openaiCompatible.targetPort={SERVER_TARGET_PORT}",
        )
        self.assertIn('kubernetes.io/metadata.name: "123"', res.stdout)
        names = [
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for doc in yaml.safe_load_all(res.stdout)
            if doc and doc.get("kind") == "NetworkPolicy" and doc["metadata"]["name"] == "litellm-policy"
            for rule in doc["spec"]["egress"]
            for peer in rule.get("to", [])
            if "namespaceSelector" in peer
        ]
        self.assertIn("123", names)

    def test_a_name_ending_like_a_service_but_incomplete_is_refused(self) -> None:
        for api_base in ("https://vllm.svc/v1", "https://llm.models.svc.k8s.cluster.local/v1"):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "ends like an in-cluster Service name")

    def test_the_absolute_spelling_of_a_service_gets_its_namespace_rule(self) -> None:
        res = _helm_template(
            *HARNESS_ARGS,
            "--set", "litellm.modelProvider=openai_compatible",
            "--set", "litellm.modelDefaultName=m",
            "--set", "litellm.openaiCompatible.apiBase=https://llm.models.svc.cluster.local./v1",
            "--set", f"litellm.openaiCompatible.targetPort={SERVER_TARGET_PORT}",
        )
        self.assertIn(f'{{"namespace":"models","port":{SERVER_TARGET_PORT}}}', res.stdout)

    def test_a_host_already_ending_in_svc_gets_no_svc_hint(self) -> None:
        """vllm.svc is missing its namespace; vllm.svc.svc would be no fix."""
        res = _render({"apiBase": "http://vllm.svc:8000/v1"}, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ends like an in-cluster Service name", res.stderr)
        self.assertNotIn("vllm.svc.svc", res.stderr)

    def test_a_private_or_ipv6_literal_needs_egress_cidrs_even_on_443(self) -> None:
        for api_base in (
            "https://10.20.0.5/v1",
            "https://172.16.4.4/v1",
            "https://192.168.1.1/v1",
            "https://100.64.0.9/v1",
            "https://[2001:db8::1]/v1",
            PRIVATE_IP_API_BASE,
        ):
            with self.subTest(api_base):
                self._assert_fails({"apiBase": api_base}, "names a private or IPv6 address")

    def test_egress_cidr_entries_the_operator_would_refuse_are_refused(self) -> None:
        cases = {
            "not-a-cidr": "is not in CIDR notation",
            "10.20.0.5": "is not in CIDR notation",
            "10.20.0.0/33": "is not in CIDR notation",
            "300.1.0.0/16": "is not in CIDR notation",
            "fd00::1::/64": "is not in CIDR notation",
            "fd00:1:2:3:4:5:6:7:8/64": "is not in CIDR notation",
            "10.0.0.0/8": "is broader than /12",
            "0.0.0.0/0": "is broader than /12",
            "::/0": "is broader than /48",
            "fd00::/40": "is broader than /48",
            "169.254.0.0/16": "overlaps the link-local range 169.254.0.0/16",
            "169.240.0.0/12": "overlaps the link-local range 169.254.0.0/16",
            "169.254.169.254/32": "overlaps the link-local range 169.254.0.0/16",
            "fe80::/64": "overlaps the link-local range fe80::/10",
            "FEBF:1::/64": "overlaps the link-local range fe80::/10",
            "fd20:ce::/48": "fd20:ce::254",
            "fd20:00ce:0000:0000::254/128": "fd20:ce::254",
            "::ffff:a14:0/120": "IPv4-mapped",
            "010.20.0.0/24": "is not in CIDR notation",
            "fd20:ce:0:1::/64": "fd20:ce::254",
        }
        for entry, fragment in cases.items():
            with self.subTest(entry):
                self._assert_fails({"apiBase": PRIVATE_IP_API_BASE, "egressCIDRs": [entry]}, fragment)

    def test_too_many_egress_cidrs_are_refused(self) -> None:
        cidrs = [f"10.{index}.0.0/16" for index in range(MAX_CIDRS + 1)]
        self._assert_fails({"apiBase": PRIVATE_IP_API_BASE, "egressCIDRs": cidrs}, f"the operator accepts at most {MAX_CIDRS}")

    def test_a_ca_secret_needs_a_key(self) -> None:
        self._assert_fails(
            {**SERVICE_VALUES, "caBundleSecretRef": {"name": CA_OBJECT, "key": ""}}, "caBundleSecretRef.key is empty"
        )

    def test_an_unknown_provider_names_this_one(self) -> None:
        res = _helm_template("--set", "litellm.modelProvider=vllm", *HARNESS_ARGS, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("vertex_ai, openai_compatible", res.stderr)

    def test_the_schema_refuses_a_bad_target_port_and_an_unknown_key(self) -> None:
        for openai_compatible, fragment in (
            ({**SERVICE_VALUES, "targetPort": 0}, "targetPort"),
            ({**SERVICE_VALUES, "targetPort": 70000}, "targetPort"),
            ({**SERVICE_VALUES, "apiKey": "x"}, "apiKey"),
            ({**SERVICE_VALUES, "caBundleSecretRef": {"name": CA_OBJECT, "namespace": "x"}}, "namespace"),
        ):
            with self.subTest(openai_compatible):
                self._assert_fails(openai_compatible, fragment)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class AcceptedEgressCIDRsTest(unittest.TestCase):
    def test_entries_beside_the_refused_ranges_render(self) -> None:
        for entry in ("169.253.0.0/16", "169.224.0.0/12", "10.0.0.0/12", "fd20:cf::/48", "fe7f:1::/64", " 10.20.0.0/24 "):
            with self.subTest(entry):
                res = _render({"apiBase": PRIVATE_IP_API_BASE, "egressCIDRs": [entry]}, *OPERATOR_OFF_ARGS)
                peers = _policy_egress(res.stdout)[-1]["to"]
                self.assertEqual(peers, [{"ipBlock": {"cidr": entry.strip()}}])


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class StaticPolicyRuleTest(unittest.TestCase):
    """The rule the chart's own litellm-policy carries, rendered when the operator is off."""

    def _default_egress(self) -> list[dict]:
        return _policy_egress(_helm_template(*OPERATOR_OFF_ARGS, *HARNESS_ARGS).stdout)

    def test_an_in_cluster_service_opens_its_namespace_on_the_target_port(self) -> None:
        for api_base, namespace in (
            (SERVICE_API_BASE, SERVER_NAMESPACE),
            (SVC_SERVICE_API_BASE, SVC_NAMESPACE),
            (BARE_SERVICE_API_BASE, RELEASE_NAMESPACE),
        ):
            with self.subTest(api_base):
                res = _render({"apiBase": api_base, "targetPort": SERVER_TARGET_PORT}, *OPERATOR_OFF_ARGS)
                egress = _policy_egress(res.stdout)
                self.assertEqual(egress[:-1], self._default_egress())
                self.assertEqual(
                    egress[-1],
                    {
                        "ports": [{"port": SERVER_TARGET_PORT, "protocol": "TCP"}],
                        "to": [{"namespaceSelector": {"matchLabels": {NAMESPACE_LABEL: namespace}}}],
                    },
                )

    def test_egress_cidrs_open_the_url_port(self) -> None:
        for api_base, port in (
            (PRIVATE_IP_API_BASE, PRIVATE_IP_PORT),
            (PRIVATE_HOSTNAME_HTTPS_API_BASE, HTTPS_DEFAULT_PORT),
            (PRIVATE_HOSTNAME_HTTP_API_BASE, HTTP_DEFAULT_PORT),
        ):
            with self.subTest(api_base):
                res = _render({"apiBase": api_base, "egressCIDRs": PRIVATE_CIDRS}, *OPERATOR_OFF_ARGS)
                egress = _policy_egress(res.stdout)
                self.assertEqual(egress[:-1], self._default_egress())
                self.assertEqual(
                    egress[-1],
                    {
                        "ports": [{"port": port, "protocol": "TCP"}],
                        "to": [{"ipBlock": {"cidr": cidr}} for cidr in PRIVATE_CIDRS],
                    },
                )

    def test_a_public_443_endpoint_adds_no_rule(self) -> None:
        for api_base in (PUBLIC_API_BASE, TWO_LABEL_PUBLIC_API_BASE):
            with self.subTest(api_base):
                res = _render({"apiBase": api_base}, *OPERATOR_OFF_ARGS)
                self.assertEqual(_policy_egress(res.stdout), self._default_egress())

    def test_the_operator_owned_render_has_no_static_policy(self) -> None:
        res = _render(SERVICE_VALUES)
        self.assertIsNone(_find(res.stdout, "NetworkPolicy", "litellm-policy"))


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class UpstreamEgressAnnotationTest(unittest.TestCase):
    """The same rule, carried to the operator's copy as a CR annotation."""

    def test_each_form_stamps_its_json(self) -> None:
        cases = (
            (SERVICE_VALUES, {"namespace": SERVER_NAMESPACE, "port": SERVER_TARGET_PORT}),
            (
                {"apiBase": BARE_SERVICE_API_BASE, "targetPort": SERVER_TARGET_PORT},
                {"namespace": RELEASE_NAMESPACE, "port": SERVER_TARGET_PORT},
            ),
            (
                {"apiBase": SVC_SERVICE_API_BASE, "targetPort": SERVER_TARGET_PORT},
                {"namespace": SVC_NAMESPACE, "port": SERVER_TARGET_PORT},
            ),
            (CIDR_VALUES, {"cidrs": PRIVATE_CIDRS, "port": PRIVATE_IP_PORT}),
        )
        for openai_compatible, expected in cases:
            with self.subTest(openai_compatible["apiBase"]):
                raw = _cr_annotations(_render(openai_compatible).stdout)[UPSTREAM_EGRESS_ANNOTATION]
                self.assertEqual(json.loads(raw), expected)
                # Compact, the shape the operator's strict decoder is written against.
                self.assertEqual(raw, json.dumps(expected, separators=(",", ":"), sort_keys=True))

    def test_stamped_with_the_operator_off_too(self) -> None:
        annotations = _cr_annotations(_render(SERVICE_VALUES, *OPERATOR_OFF_ARGS).stdout)
        self.assertIn(UPSTREAM_EGRESS_ANNOTATION, annotations)

    def test_absent_when_no_rule_is_needed_or_the_provider_differs(self) -> None:
        cases = {
            "public 443": _render(PUBLIC_VALUES),
            "two-label public 443": _render({"apiBase": TWO_LABEL_PUBLIC_API_BASE}),
            "gemini": _helm_template(*HARNESS_ARGS),
            "litellm disabled": _render(SERVICE_VALUES, values={"litellm": {"enabled": False}}),
        }
        for name, res in cases.items():
            with self.subTest(name):
                self.assertNotIn(UPSTREAM_EGRESS_ANNOTATION, _cr_annotations(res.stdout))

    def test_a_user_set_annotation_passes_through_for_another_provider(self) -> None:
        value = json.dumps({"namespace": SERVER_NAMESPACE, "port": SERVER_TARGET_PORT}, separators=(",", ":"))
        res = _render(
            None,
            values={
                "litellm": {"modelProvider": "gemini", "modelDefaultName": ""},
                "platformAgent": {"annotations": {UPSTREAM_EGRESS_ANNOTATION: value}},
            },
        )
        self.assertEqual(_cr_annotations(res.stdout)[UPSTREAM_EGRESS_ANNOTATION], value)

    def test_a_matching_user_set_annotation_renders(self) -> None:
        value = json.dumps({"namespace": SERVER_NAMESPACE, "port": SERVER_TARGET_PORT}, separators=(",", ":"))
        res = _render(SERVICE_VALUES, values={"platformAgent": {"annotations": {UPSTREAM_EGRESS_ANNOTATION: value}}})
        self.assertEqual(_cr_annotations(res.stdout)[UPSTREAM_EGRESS_ANNOTATION], value)

    def test_a_contradicting_user_set_annotation_fails(self) -> None:
        value = json.dumps({"namespace": "elsewhere", "port": SERVER_TARGET_PORT}, separators=(",", ":"))
        res = _render(
            SERVICE_VALUES, values={"platformAgent": {"annotations": {UPSTREAM_EGRESS_ANNOTATION: value}}}, check=False
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(ANNOTATION_CONFLICT_FRAGMENT, res.stderr)


def _existing_provider_cases() -> list[tuple[str, list[str]]]:
    """Each existing provider at its defaults, with the operator on and off."""
    cases = []
    for provider in EXISTING_PROVIDERS:
        for operator_enabled in ("true", "false"):
            args = ["--set", f"litellm.modelProvider={provider}", "--set", f"operator.enabled={operator_enabled}"]
            cases.append((f"{provider},operator={operator_enabled}", args))
    # The OTLP collector lookup shares the in-cluster Service test with this provider.
    cases.append(
        (
            "gemini,operator=false,in-cluster OTLP",
            [
                *OPERATOR_OFF_ARGS,
                "--set",
                "litellm.otel=true",
                "--set",
                "telemetry.otlpEndpoint=http://otel-collector.observability.svc.cluster.local:4318",
            ],
        )
    )
    return cases


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ExistingProvidersUnchangedTest(unittest.TestCase):
    def test_openai_compatible_values_are_inert_for_other_providers(self) -> None:
        inert_args = [
            "--set",
            f"litellm.openaiCompatible.apiBase={PRIVATE_IP_API_BASE}",
            "--set",
            f"litellm.openaiCompatible.targetPort={SERVER_TARGET_PORT}",
            "--set",
            f"litellm.openaiCompatible.egressCIDRs={{{PRIVATE_CIDRS[0]}}}",
            "--set",
            f"litellm.openaiCompatible.caBundleSecretRef.name={CA_OBJECT}",
        ]
        for name, args in _existing_provider_cases():
            with self.subTest(name):
                plain = _helm_template(*args, *HARNESS_ARGS).stdout
                with_values = _helm_template(*args, *inert_args, *HARNESS_ARGS).stdout
                self.assertEqual(plain, with_values)

    @unittest.skipUnless(os.environ.get(BASE_CHART_DIR_ENV), f"{BASE_CHART_DIR_ENV} is not set")
    def test_renders_match_the_base_chart_byte_for_byte(self) -> None:
        base_chart = pathlib.Path(os.environ[BASE_CHART_DIR_ENV])
        for name, args in _existing_provider_cases():
            with self.subTest(name):
                current = _helm_template(*args, *HARNESS_ARGS).stdout
                base = _helm_template(*args, *HARNESS_ARGS, chart_dir=base_chart).stdout
                self.assertEqual(current, base)



@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class PlainHttpNotesTest(unittest.TestCase):
    """The install notes warn about plain http to a server outside the cluster.

    NOTES.txt is rendered only by install and upgrade, which need a cluster, so
    the test renders a copy of the chart whose NOTES.txt is wrapped in a define
    and emitted through a scratch template by `helm template`.
    """

    WARNING = "is plain http to a server outside the cluster"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(CHART_DIR, chart)
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

    def _notes(self, *sets: str) -> str:
        args = ["--show-only", "templates/notes-under-test.yaml"]
        for item in ("litellm.modelProvider=openai_compatible", "litellm.modelDefaultName=m", *sets):
            args += ["--set", item]
        res = _helm_template(*HARNESS_ARGS, *args, chart_dir=self.chart)
        return yaml.safe_load(res.stdout)["data"]["notes"]

    def test_plain_http_outside_the_cluster_warns(self) -> None:
        notes = self._notes(
            "litellm.openaiCompatible.apiBase=http://10.20.0.5:8000/v1",
            "litellm.openaiCompatible.egressCIDRs={10.20.0.0/24}",
        )
        self.assertIn(self.WARNING, notes)

    def test_a_two_label_host_on_443_warns_about_the_svc_form(self) -> None:
        self.assertIn(
            "write it as <svc>.<ns>.svc (llm.models.svc)",
            self._notes("litellm.openaiCompatible.apiBase=https://llm.models/v1"),
        )
        for sets in (
            ("litellm.openaiCompatible.apiBase=https://llm.corp.example.com/v1",),
            ("litellm.openaiCompatible.apiBase=http://vllm.models.svc:8000/v1", "litellm.openaiCompatible.targetPort=8000"),
        ):
            with self.subTest(sets):
                self.assertNotIn("<svc>.<ns>.svc", self._notes(*sets))

    def test_an_in_cluster_service_or_https_does_not_warn(self) -> None:
        for sets in (
            ("litellm.openaiCompatible.apiBase=http://vllm.models.svc:8000/v1", "litellm.openaiCompatible.targetPort=8000"),
            ("litellm.openaiCompatible.apiBase=http://vllm:8000/v1", "litellm.openaiCompatible.targetPort=8000"),
            ("litellm.openaiCompatible.apiBase=https://openrouter.ai/api/v1",),
        ):
            with self.subTest(sets):
                self.assertNotIn(self.WARNING, self._notes(*sets))


if __name__ == "__main__":
    unittest.main()
