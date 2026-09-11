"""Tests that the Helm chart renders litellm-policy when and only when the operator does not manage it.

The NetworkPolicy `litellm-policy` has exactly one owner:
- When platformAgent.enabled and operator.enabled are both true, the operator dynamically
  manages litellm-policy (and Helm omits it).
- When either platformAgent.enabled or operator.enabled is false (and litellm.networkPolicy
  is true), Helm renders the static litellm-policy so that LiteLLM's egress is not left unrestricted.
- When litellm.networkPolicy is false, neither renders it.

Every test here drives `helm template` and asserts on the rendered objects. Asserting on the
template source text instead would fail on a semantically identical reformat and pass on a
behavioural change that keeps the string, so none of these do.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"

LITELLM_POLICY_KIND = "NetworkPolicy"
LITELLM_POLICY_NAME = "litellm-policy"
PLATFORM_AGENT_KIND = "PlatformAgent"

OPT_OUT_ANNOTATION_KEY = "kubeagents.x-k8s.io/enable-litellm-network-policy"
OPT_OUT_ANNOTATION_VALUE = "false"
COLLECTOR_NAMESPACE_ANNOTATION_KEY = "kubeagents.x-k8s.io/otlp-collector-namespace"
HELM_KEEP_ANNOTATION_KEY = "helm.sh/resource-policy"
USER_ANNOTATION_KEY = "example.com/team"
USER_ANNOTATION_VALUE = "sre"

HARNESS_PROJECT_ID = "my-proj"
HARNESS_CLUSTER_NAME = "my-cluster"
HARNESS_LOCATION = "us-central1"

VENDOR_OTLP_ENDPOINT = "https://otlp.vendor.example"
VENDOR_OTLP_ENDPOINT_NON_443 = "https://otlp.vendor.example:4318"
VENDOR_OTLP_ENDPOINT_PLAIN_HTTP = "http://otlp.vendor.example/v1/traces"
VENDOR_OTLP_ENDPOINT_IPV6_NON_443 = "http://[2001:db8::1]:4318"
VENDOR_OTLP_ENDPOINT_IPV6_443 = "https://[2001:db8::1]:443/v1/traces"
VENDOR_OTLP_ENDPOINT_QUERY_NON_443 = "https://otlp.vendor.example:4318?x=1"
VENDOR_OTLP_ENDPOINT_GRPC = "grpc://otlp.vendor.example:4317"
VENDOR_OTLP_ENDPOINT_USERINFO = "https://user:secret@otlp.vendor.example"
PRIVATE_IPV4_OTLP_ENDPOINT_443 = "https://10.100.5.7"
SINGLE_LABEL_OTLP_ENDPOINT_443 = "https://otel-collector"
PUBLIC_IPV4_OTLP_ENDPOINT_443 = "https://203.0.113.9"
INVALID_COLLECTOR_NAMESPACE = "Obs/Namespace"
# The fail message for a host the 443 rule cannot reach that the render can recognise.
PRIVATE_HOST_FAIL_FRAGMENT = "does not reach (it excepts private ranges)"
INVALID_NAMESPACE_FAIL_FRAGMENT = "is not a valid label value"
INVALID_NAMESPACE_VALUE_FAIL_FRAGMENT = "is not a valid namespace name"
IPV6_STATIC_FAIL_FRAGMENT = "reaches IPv4 destinations only"
VENDOR_OTLP_ENDPOINT_UPPERCASE_SCHEME = "HTTPS://otlp.vendor.example"
IN_CLUSTER_OTLP_ENDPOINT_NON_443 = "http://otel-collector.observability.svc.cluster.local:4318"
BARE_HOST_OTLP_ENDPOINT_NON_443 = "http://otel-collector:4318"
IP_LITERAL_OTLP_ENDPOINT_NON_443 = "http://10.100.5.7:4318"
COLLECTOR_NAMESPACE = "obs"
OTHER_COLLECTOR_NAMESPACE = "other"

# The fail message either render emits for an external endpoint the policy cannot reach.
VENDOR_PORT_FAIL_FRAGMENT = "permits egress to external hosts on port 443 only"
# The fail message for a scheme the port check cannot read a port off.
SCHEME_FAIL_FRAGMENT = "must start with http:// or https://"
# The fail message for an authority the port check cannot read a port off.
UNREADABLE_PORT_FAIL_FRAGMENT = "cannot read the port off it"

OTLP_PORTS = {4317, 4318}
MANAGED_OTEL_NAMESPACE = "gke-managed-otel"
# The fail messages the CR template emits for a platformAgent.annotations entry that
# contradicts the chart value the same key is derived from.
ANNOTATION_CONFLICT_FRAGMENT = "contradicts"

HARNESS_ARGS = [
    "--set",
    f"platformAgent.harness.projectId={HARNESS_PROJECT_ID}",
    "--set",
    f"platformAgent.harness.clusterName={HARNESS_CLUSTER_NAME}",
    "--set",
    f"platformAgent.harness.location={HARNESS_LOCATION}",
]


def _helm_template(*extra_args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "test-release", str(CHART_DIR), *extra_args],
        capture_output=True,
        text=True,
        check=check,
    )


def _annotation_set_arg(key: str, value: str) -> str:
    # Dots inside a --set key are path separators unless escaped.
    escaped_key = key.replace(".", "\\.")
    return f"platformAgent.annotations.{escaped_key}={value}"


def _litellm_policy_docs(rendered_yaml: str) -> list[dict]:
    docs = []
    for doc in yaml.safe_load_all(rendered_yaml):
        if not doc or not isinstance(doc, dict):
            continue
        if (
            doc.get("kind") == LITELLM_POLICY_KIND
            and doc.get("metadata", {}).get("name") == LITELLM_POLICY_NAME
        ):
            docs.append(doc)
    return docs


def _otlp_egress_namespaces(policy: dict) -> list[str]:
    """The namespaces the policy's 4317/4318 egress rules open, in order."""
    namespaces = []
    for rule in policy.get("spec", {}).get("egress", []):
        ports = {p.get("port") for p in rule.get("ports", [])}
        if not ports & OTLP_PORTS:
            continue
        for peer in rule.get("to", []):
            selector = (peer.get("namespaceSelector") or {}).get("matchLabels", {})
            namespaces.append(selector.get("kubernetes.io/metadata.name"))
    return namespaces


def _find_platform_agent_cr(rendered_yaml: str) -> dict | None:
    for doc in yaml.safe_load_all(rendered_yaml):
        if not doc or not isinstance(doc, dict):
            continue
        if doc.get("kind") == PLATFORM_AGENT_KIND:
            return doc
    return None


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class LiteLLMPolicyOwnershipTest(unittest.TestCase):
    def test_helm_renders_litellm_policy_combinations(self) -> None:
        # (platformAgent.enabled, operator.enabled, expected_static_policy_count)
        cases = [
            (True, True, 0),  # Operator dynamically manages litellm-policy; Helm omits it
            (False, True, 1),  # No PlatformAgent CR; Helm renders static litellm-policy
            (True, False, 1),  # No operator running; Helm renders static litellm-policy
            (False, False, 1),  # Neither running; Helm renders static litellm-policy
        ]

        for pa_enabled, op_enabled, expected_count in cases:
            subtest_name = f"platformAgent={pa_enabled},operator={op_enabled}"
            with self.subTest(subtest_name):
                args = [
                    "--set",
                    f"platformAgent.enabled={str(pa_enabled).lower()}",
                    "--set",
                    f"operator.enabled={str(op_enabled).lower()}",
                    "--set",
                    "litellm.networkPolicy=true",
                ]
                if pa_enabled:
                    args.extend(HARNESS_ARGS)

                res = _helm_template(*args)
                actual_count = len(_litellm_policy_docs(res.stdout))
                self.assertEqual(
                    actual_count,
                    expected_count,
                    f"Expected {expected_count} {LITELLM_POLICY_NAME} documents for {subtest_name}, got {actual_count}",
                )

    def test_helm_omits_litellm_policy_when_network_policy_disabled(self) -> None:
        res = _helm_template(
            "--set",
            "platformAgent.enabled=true",
            "--set",
            "operator.enabled=true",
            "--set",
            "litellm.networkPolicy=false",
            *HARNESS_ARGS,
        )
        self.assertEqual(len(_litellm_policy_docs(res.stdout)), 0)

        cr = _find_platform_agent_cr(res.stdout)
        self.assertIsNotNone(cr, "PlatformAgent CR not rendered")
        annotations = cr.get("metadata", {}).get("annotations", {})
        self.assertEqual(
            annotations.get(OPT_OUT_ANNOTATION_KEY),
            OPT_OUT_ANNOTATION_VALUE,
            f"Expected CR annotation {OPT_OUT_ANNOTATION_KEY}={OPT_OUT_ANNOTATION_VALUE}",
        )

    def test_static_policy_carries_no_helm_keep_annotation(self) -> None:
        # helm.sh/resource-policy: keep would make `helm uninstall` and
        # litellm.networkPolicy=false leave the policy behind and block a reinstall
        # under another release name; the transition it was meant for is covered by
        # the documented `kubectl annotate` on the live object.
        res = _helm_template("--set", "operator.enabled=false", *HARNESS_ARGS)
        docs = _litellm_policy_docs(res.stdout)
        self.assertEqual(len(docs), 1)
        annotations = docs[0].get("metadata", {}).get("annotations") or {}
        self.assertNotIn(HELM_KEEP_ANNOTATION_KEY, annotations)

    def test_vendor_otlp_endpoint_renders_when_operator_owns_policy(self) -> None:
        # On the default install Helm renders no litellm-policy, and the operator omits
        # the OTLP rule for an endpoint with no in-cluster namespace, so there is nothing
        # for the collector-namespace fail to protect.
        res = _helm_template(
            "--set",
            f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT}",
            "--set",
            "litellm.otel=true",
            *HARNESS_ARGS,
        )
        self.assertEqual(len(_litellm_policy_docs(res.stdout)), 0)

    def test_vendor_otlp_endpoint_off_port_443_fails_render(self) -> None:
        # Neither copy of litellm-policy lets LiteLLM reach an external host except on
        # 443, so an exporter pointed anywhere else would be blocked in silence.
        for endpoint in (
            VENDOR_OTLP_ENDPOINT_NON_443,
            VENDOR_OTLP_ENDPOINT_PLAIN_HTTP,
            VENDOR_OTLP_ENDPOINT_IPV6_NON_443,
        ):
            with self.subTest(endpoint):
                res = _helm_template(
                    "--set",
                    f"telemetry.otlpEndpoint={endpoint}",
                    "--set",
                    "litellm.otel=true",
                    *HARNESS_ARGS,
                    check=False,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(VENDOR_PORT_FAIL_FRAGMENT, res.stderr)
                self.assertNotIn("on port ,", res.stderr)

    def test_private_or_single_label_host_on_443_fails_render(self) -> None:
        # Both are in-cluster collectors in disguise: the 443 rule excepts private
        # ranges, and the remedy is the collector namespace, as it was on main.
        for render_args in ([], ["--set", "operator.enabled=false"]):
            for endpoint in (PRIVATE_IPV4_OTLP_ENDPOINT_443, SINGLE_LABEL_OTLP_ENDPOINT_443):
                with self.subTest(f"{endpoint} {render_args}"):
                    res = _helm_template(
                        "--set",
                        f"telemetry.otlpEndpoint={endpoint}",
                        "--set",
                        "litellm.otel=true",
                        *render_args,
                        *HARNESS_ARGS,
                        check=False,
                    )
                    self.assertNotEqual(res.returncode, 0)
                    self.assertIn(PRIVATE_HOST_FAIL_FRAGMENT, res.stderr)
        with self.subTest("public IPv4 literal on 443 renders"):
            _helm_template(
                "--set",
                f"telemetry.otlpEndpoint={PUBLIC_IPV4_OTLP_ENDPOINT_443}",
                "--set",
                "litellm.otel=true",
                *HARNESS_ARGS,
            )
        with self.subTest("IPv6 literal on 443 fails the static render, whose 443 rule is IPv4-only"):
            res = _helm_template(
                "--set",
                "operator.enabled=false",
                "--set",
                f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_IPV6_443}",
                "--set",
                "litellm.otel=true",
                *HARNESS_ARGS,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn(IPV6_STATIC_FAIL_FRAGMENT, res.stderr)

    def test_invalid_collector_namespace_value_fails_render(self) -> None:
        # The value route gets the validation the annotation route has: an invalid
        # namespace would stand the port check aside and then select nothing.
        for render_args in ([], ["--set", "operator.enabled=false"]):
            with self.subTest(str(render_args)):
                res = _helm_template(
                    "--set",
                    f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    f"telemetry.collectorNamespace={INVALID_COLLECTOR_NAMESPACE}",
                    *render_args,
                    *HARNESS_ARGS,
                    check=False,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(INVALID_NAMESPACE_VALUE_FAIL_FRAGMENT, res.stderr)

    def test_cr_side_opt_outs_do_not_cover_the_static_render(self) -> None:
        # With the operator absent the CR's switches remove nothing; the static policy
        # still selects LiteLLM, so the port check still has to fire.
        cases = [
            ("networkPolicy.enabled=false", ["--set", "platformAgent.networkPolicy.enabled=false"]),
            (
                "opt-out annotation",
                ["--set-string", _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, OPT_OUT_ANNOTATION_VALUE)],
            ),
        ]
        for name, args in cases:
            with self.subTest(name):
                res = _helm_template(
                    "--set",
                    "operator.enabled=false",
                    "--set",
                    f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    *args,
                    *HARNESS_ARGS,
                    check=False,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(VENDOR_PORT_FAIL_FRAGMENT, res.stderr)

    def test_annotation_values_are_read_the_way_the_operator_reads_them(self) -> None:
        with self.subTest("invalid collector namespace annotation fails"):
            res = _helm_template(
                "--set",
                f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                "--set",
                "litellm.otel=true",
                "--set",
                _annotation_set_arg(COLLECTOR_NAMESPACE_ANNOTATION_KEY, INVALID_COLLECTOR_NAMESPACE),
                *HARNESS_ARGS,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn(INVALID_NAMESPACE_FAIL_FRAGMENT, res.stderr)
        with self.subTest("upper-case opt-out stands the port check aside"):
            _helm_template(
                "--set",
                f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                "--set",
                "litellm.otel=true",
                "--set-string",
                _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, "FALSE"),
                *HARNESS_ARGS,
            )
        with self.subTest("upper-case opt-out agrees with litellm.networkPolicy=false"):
            _helm_template(
                "--set",
                "litellm.networkPolicy=false",
                "--set-string",
                _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, "FALSE"),
                *HARNESS_ARGS,
            )

    def test_collector_namespace_annotation_does_not_cover_the_static_render(self) -> None:
        # Only the operator reads the CR annotation; the static copy opens nothing for
        # it, so the port check still has to fire there.
        res = _helm_template(
            "--set",
            "operator.enabled=false",
            "--set",
            f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
            "--set",
            "litellm.otel=true",
            "--set",
            _annotation_set_arg(COLLECTOR_NAMESPACE_ANNOTATION_KEY, COLLECTOR_NAMESPACE),
            *HARNESS_ARGS,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(VENDOR_PORT_FAIL_FRAGMENT, res.stderr)

    def test_otlp_endpoint_the_port_check_cannot_read_fails_render(self) -> None:
        # A scheme the parser does not strip, or a query string, userinfo, or fragment in
        # the authority, would leave no port to read and pass as an implicit 443; the
        # operator reads the same raw value with the same parser, so the chart refuses
        # rather than diverging from it.
        cases = [
            (VENDOR_OTLP_ENDPOINT_GRPC, SCHEME_FAIL_FRAGMENT),
            (VENDOR_OTLP_ENDPOINT_UPPERCASE_SCHEME, SCHEME_FAIL_FRAGMENT),
            (VENDOR_OTLP_ENDPOINT_QUERY_NON_443, UNREADABLE_PORT_FAIL_FRAGMENT),
            (VENDOR_OTLP_ENDPOINT_USERINFO, UNREADABLE_PORT_FAIL_FRAGMENT),
        ]
        for endpoint, fragment in cases:
            with self.subTest(endpoint):
                res = _helm_template(
                    "--set",
                    f"telemetry.otlpEndpoint={endpoint}",
                    "--set",
                    "litellm.otel=true",
                    *HARNESS_ARGS,
                    check=False,
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(fragment, res.stderr)

    def test_otlp_port_check_stays_out_of_the_way(self) -> None:
        # The check is about external hosts off port 443 only. An in-cluster Service
        # host (whatever its port: the URL carries the Service port, the policy sees the
        # targetPort), an external host on 443 including an IPv6 literal, a collector
        # namespace given by value or by CR annotation (the user asserting the collector
        # is in-cluster, in either render), the exporter off, or the policy off by any of
        # its switches leave it nothing to catch.
        cases = [
            (
                "in-cluster non-443",
                ["--set", f"telemetry.otlpEndpoint={IN_CLUSTER_OTLP_ENDPOINT_NON_443}", "--set", "litellm.otel=true"],
            ),
            (
                "external IPv6 literal on 443",
                ["--set", f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_IPV6_443}", "--set", "litellm.otel=true"],
            ),
            (
                "collector namespace set, dynamic render",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={BARE_HOST_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    f"telemetry.collectorNamespace={COLLECTOR_NAMESPACE}",
                ],
            ),
            (
                "collector namespace set, static render",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={IP_LITERAL_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    f"telemetry.collectorNamespace={COLLECTOR_NAMESPACE}",
                    "--set",
                    "operator.enabled=false",
                ],
            ),
            (
                "exporter off",
                ["--set", f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}"],
            ),
            (
                "CR networkPolicy.enabled=false, dynamic render",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    "platformAgent.networkPolicy.enabled=false",
                ],
            ),
            (
                "CR opt-out annotation, dynamic render",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set-string",
                    _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, OPT_OUT_ANNOTATION_VALUE),
                ],
            ),
            (
                "collector namespace through platformAgent.annotations",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={IP_LITERAL_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    _annotation_set_arg(COLLECTOR_NAMESPACE_ANNOTATION_KEY, COLLECTOR_NAMESPACE),
                ],
            ),
            (
                "policy off",
                [
                    "--set",
                    f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT_NON_443}",
                    "--set",
                    "litellm.otel=true",
                    "--set",
                    "litellm.networkPolicy=false",
                ],
            ),
        ]
        for name, args in cases:
            with self.subTest(name):
                _helm_template(*args, *HARNESS_ARGS)

    def test_vendor_otlp_endpoint_static_render_follows_the_operator(self) -> None:
        # The static copy has no namespace to open for an external endpoint. With the
        # exporter on it emits no OTLP rule, as the operator's copy does, and the exporter
        # leaves over the port-443 rule the port check has already vouched for; with the
        # exporter off it keeps the shipping gke-managed-otel default.
        with self.subTest("exporter on"):
            res = _helm_template(
                "--set",
                "operator.enabled=false",
                "--set",
                f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT}",
                "--set",
                "litellm.otel=true",
                *HARNESS_ARGS,
            )
            docs = _litellm_policy_docs(res.stdout)
            self.assertEqual(len(docs), 1)
            self.assertEqual(_otlp_egress_namespaces(docs[0]), [])
        with self.subTest("exporter off"):
            res = _helm_template(
                "--set",
                "operator.enabled=false",
                "--set",
                f"telemetry.otlpEndpoint={VENDOR_OTLP_ENDPOINT}",
                *HARNESS_ARGS,
            )
            docs = _litellm_policy_docs(res.stdout)
            self.assertEqual(len(docs), 1)
            self.assertEqual(_otlp_egress_namespaces(docs[0]), [MANAGED_OTEL_NAMESPACE])

    def test_in_cluster_otlp_endpoint_static_render_opens_its_namespace(self) -> None:
        res = _helm_template(
            "--set",
            "operator.enabled=false",
            "--set",
            f"telemetry.otlpEndpoint={IN_CLUSTER_OTLP_ENDPOINT_NON_443}",
            "--set",
            "litellm.otel=true",
            *HARNESS_ARGS,
        )
        docs = _litellm_policy_docs(res.stdout)
        self.assertEqual(len(docs), 1)
        self.assertEqual(_otlp_egress_namespaces(docs[0]), ["observability"])

    def test_platform_agent_annotations_pass_through_beside_derived_keys(self) -> None:
        res = _helm_template(
            "--set",
            _annotation_set_arg(USER_ANNOTATION_KEY, USER_ANNOTATION_VALUE),
            "--set",
            "litellm.networkPolicy=false",
            "--set",
            f"telemetry.collectorNamespace={COLLECTOR_NAMESPACE}",
            *HARNESS_ARGS,
        )
        cr = _find_platform_agent_cr(res.stdout)
        self.assertIsNotNone(cr, "PlatformAgent CR not rendered")
        annotations = cr.get("metadata", {}).get("annotations", {})
        self.assertEqual(annotations.get(USER_ANNOTATION_KEY), USER_ANNOTATION_VALUE)
        self.assertEqual(annotations.get(OPT_OUT_ANNOTATION_KEY), OPT_OUT_ANNOTATION_VALUE)
        self.assertEqual(annotations.get(COLLECTOR_NAMESPACE_ANNOTATION_KEY), COLLECTOR_NAMESPACE)

    def test_platform_agent_annotation_matching_derived_value_renders(self) -> None:
        # --set-string, not --set: a bare `false` is a YAML boolean the template's `with`
        # skips before comparing, and a values file carries the string form this exercises.
        res = _helm_template(
            "--set-string",
            _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, OPT_OUT_ANNOTATION_VALUE),
            "--set",
            "litellm.networkPolicy=false",
            "--set",
            _annotation_set_arg(COLLECTOR_NAMESPACE_ANNOTATION_KEY, COLLECTOR_NAMESPACE),
            "--set",
            f"telemetry.collectorNamespace={COLLECTOR_NAMESPACE}",
            *HARNESS_ARGS,
        )
        cr = _find_platform_agent_cr(res.stdout)
        self.assertIsNotNone(cr, "PlatformAgent CR not rendered")
        annotations = cr.get("metadata", {}).get("annotations", {})
        self.assertEqual(annotations.get(OPT_OUT_ANNOTATION_KEY), OPT_OUT_ANNOTATION_VALUE)
        self.assertEqual(annotations.get(COLLECTOR_NAMESPACE_ANNOTATION_KEY), COLLECTOR_NAMESPACE)

    def test_platform_agent_annotation_contradicting_value_fails_render(self) -> None:
        # A user-set key the chart also derives is never overwritten in silence.
        cases = [
            (
                "enable-litellm-network-policy",
                [
                    "--set",
                    _annotation_set_arg(OPT_OUT_ANNOTATION_KEY, "true"),
                    "--set",
                    "litellm.networkPolicy=false",
                ],
            ),
            (
                "otlp-collector-namespace",
                [
                    "--set",
                    _annotation_set_arg(COLLECTOR_NAMESPACE_ANNOTATION_KEY, OTHER_COLLECTOR_NAMESPACE),
                    "--set",
                    f"telemetry.collectorNamespace={COLLECTOR_NAMESPACE}",
                ],
            ),
        ]
        for name, args in cases:
            with self.subTest(name):
                res = _helm_template(*args, *HARNESS_ARGS, check=False)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn(ANNOTATION_CONFLICT_FRAGMENT, res.stderr)


if __name__ == "__main__":
    unittest.main()
