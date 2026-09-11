"""Tests that the Helm chart renders litellm-policy when and only when the operator does not manage it.

The NetworkPolicy `litellm-policy` has exactly one owner:
- When platformAgent.enabled and operator.enabled are both true, the operator dynamically
  manages litellm-policy (and Helm omits it).
- When either platformAgent.enabled or operator.enabled is false (and litellm.networkPolicy
  is true), Helm renders the static litellm-policy so that LiteLLM's egress is not left unrestricted.
- When litellm.networkPolicy is false, neither renders it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"
LITELLM_TEMPLATE = CHART_DIR / "templates" / "litellm.yaml"
CR_TEMPLATE = CHART_DIR / "templates" / "platform-agent-cr.yaml"

EXPECTED_RENDER_GATE = (
    "{{- if and .Values.litellm.networkPolicy "
    "(not (and .Values.platformAgent.enabled .Values.operator.enabled)) }}"
)
EXPECTED_ANNOTATION_GATE = (
    '{{- $_ := set $annotations "kubeagents.x-k8s.io/enable-litellm-network-policy" "false" }}'
)

LITELLM_POLICY_KIND = "NetworkPolicy"
LITELLM_POLICY_NAME = "litellm-policy"
PLATFORM_AGENT_KIND = "PlatformAgent"

OPT_OUT_ANNOTATION_KEY = "kubeagents.x-k8s.io/enable-litellm-network-policy"
OPT_OUT_ANNOTATION_VALUE = "false"

HARNESS_PROJECT_ID = "my-proj"
HARNESS_CLUSTER_NAME = "my-cluster"
HARNESS_LOCATION = "us-central1"


def _count_litellm_policy_docs(rendered_yaml: str) -> int:
    count = 0
    for doc in yaml.safe_load_all(rendered_yaml):
        if not doc or not isinstance(doc, dict):
            continue
        if (
            doc.get("kind") == LITELLM_POLICY_KIND
            and doc.get("metadata", {}).get("name") == LITELLM_POLICY_NAME
        ):
            count += 1
    return count


def _find_platform_agent_cr(rendered_yaml: str) -> dict | None:
    for doc in yaml.safe_load_all(rendered_yaml):
        if not doc or not isinstance(doc, dict):
            continue
        if doc.get("kind") == PLATFORM_AGENT_KIND:
            return doc
    return None


class LiteLLMPolicyOwnershipTest(unittest.TestCase):
    def test_template_contains_exact_render_gate(self) -> None:
        content = LITELLM_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            EXPECTED_RENDER_GATE,
            content,
            f"litellm.yaml does not contain the expected render gate: {EXPECTED_RENDER_GATE}",
        )

    def test_cr_template_contains_opt_out_annotation_gate(self) -> None:
        content = CR_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            EXPECTED_ANNOTATION_GATE,
            content,
            f"platform-agent-cr.yaml does not contain the expected opt-out gate: {EXPECTED_ANNOTATION_GATE}",
        )

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_helm_renders_litellm_policy_combinations(self) -> None:
        # (platformAgent.enabled, operator.enabled, expected_static_policy_count)
        cases = [
            (True, True, 0),    # Operator dynamically manages litellm-policy; Helm omits it
            (False, True, 1),   # No PlatformAgent CR; Helm renders static litellm-policy
            (True, False, 1),   # No operator running; Helm renders static litellm-policy
            (False, False, 1),  # Neither running; Helm renders static litellm-policy
        ]

        for pa_enabled, op_enabled, expected_count in cases:
            subtest_name = f"platformAgent={pa_enabled},operator={op_enabled}"
            with self.subTest(subtest_name):
                args = [
                    "helm",
                    "template",
                    "test-release",
                    str(CHART_DIR),
                    "--set",
                    f"platformAgent.enabled={str(pa_enabled).lower()}",
                    "--set",
                    f"operator.enabled={str(op_enabled).lower()}",
                    "--set",
                    "litellm.networkPolicy=true",
                ]
                if pa_enabled:
                    args.extend([
                        "--set",
                        f"platformAgent.harness.projectId={HARNESS_PROJECT_ID}",
                        "--set",
                        f"platformAgent.harness.clusterName={HARNESS_CLUSTER_NAME}",
                        "--set",
                        f"platformAgent.harness.location={HARNESS_LOCATION}",
                    ])

                res = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                actual_count = _count_litellm_policy_docs(res.stdout)
                self.assertEqual(
                    actual_count,
                    expected_count,
                    f"Expected {expected_count} {LITELLM_POLICY_NAME} documents for {subtest_name}, got {actual_count}",
                )

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_helm_omits_litellm_policy_when_network_policy_disabled(self) -> None:
        args = [
            "helm",
            "template",
            "test-release",
            str(CHART_DIR),
            "--set",
            "platformAgent.enabled=true",
            "--set",
            "operator.enabled=true",
            "--set",
            "litellm.networkPolicy=false",
            "--set",
            f"platformAgent.harness.projectId={HARNESS_PROJECT_ID}",
            "--set",
            f"platformAgent.harness.clusterName={HARNESS_CLUSTER_NAME}",
            "--set",
            f"platformAgent.harness.location={HARNESS_LOCATION}",
        ]
        res = subprocess.run(args, capture_output=True, text=True, check=True)
        self.assertEqual(_count_litellm_policy_docs(res.stdout), 0)

        cr = _find_platform_agent_cr(res.stdout)
        self.assertIsNotNone(cr, "PlatformAgent CR not rendered")
        annotations = cr.get("metadata", {}).get("annotations", {})
        self.assertEqual(
            annotations.get(OPT_OUT_ANNOTATION_KEY),
            OPT_OUT_ANNOTATION_VALUE,
            f"Expected CR annotation {OPT_OUT_ANNOTATION_KEY}={OPT_OUT_ANNOTATION_VALUE}",
        )


if __name__ == "__main__":
    unittest.main()
