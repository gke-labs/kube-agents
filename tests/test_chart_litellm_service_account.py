"""Tests for the LiteLLM gateway ServiceAccount rendering across model providers.

When switching an install off vertex_ai, the chart deletes the kubeagents-litellm
ServiceAccount. If the litellm Deployment pod template omits serviceAccountName
for non-vertex providers, Helm's 3-way strategic merge patch removes that field,
but Kubernetes back-fills serviceAccountName from the deprecated serviceAccount
field on the live object (#3878). The pods then fail to start because
serviceaccounts "kubeagents-litellm" is not found.

Rendering `serviceAccountName: "default"` explicitly when not on vertex_ai ensures
Helm's patch updates serviceAccountName (and thus serviceAccount) to "default",
which exists in every namespace.
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

HARNESS_ARGS = [
    "--set-string",
    "platformAgent.harness.projectId=p",
    "--set-string",
    "platformAgent.harness.clusterName=c",
    "--set-string",
    "platformAgent.harness.location=us-central1",
]


def _render_litellm(*extra_args: str) -> str:
    return subprocess.run(
        [
            "helm",
            "template",
            "test-release",
            str(CHART_DIR),
            "--show-only",
            "templates/litellm.yaml",
            *HARNESS_ARGS,
            *extra_args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _find_docs(rendered: str, kind: str) -> list[dict]:
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict) and doc.get("kind") == kind
    ]


class LitellmTemplateStructureTest(unittest.TestCase):
    """Template-level assertions that run without requiring helm."""

    def test_litellm_template_explicitly_sets_default_service_account(self) -> None:
        text = LITELLM_TEMPLATE.read_text()
        self.assertIn(
            'serviceAccountName: "default"',
            text,
            "charts/kube-agents/templates/litellm.yaml must render "
            'serviceAccountName: "default" when provider is not vertex_ai '
            "to prevent Kubernetes from resurrecting the deleted KSA (#3878).",
        )

    def test_kustomize_base_explicitly_sets_default_service_account(self) -> None:
        base_path = REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm" / "base" / "deployment.yaml"
        doc = yaml.safe_load(base_path.read_text())
        self.assertEqual(
            doc["spec"]["template"]["spec"].get("serviceAccountName"),
            "default",
            "k8s-operator/config/integrations/litellm/base/deployment.yaml must "
            "explicitly set serviceAccountName: default to match the chart (#3878).",
        )


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class LitellmServiceAccountRenderTest(unittest.TestCase):
    def test_vertex_ai_renders_dedicated_service_account(self) -> None:
        rendered = _render_litellm(
            "--set-string",
            "litellm.modelProvider=vertex_ai",
            "--set-string",
            "litellm.modelDefaultName=gemini-2.0-flash",
        )
        deployments = _find_docs(rendered, "Deployment")
        self.assertEqual(len(deployments), 1)
        pod_spec = deployments[0]["spec"]["template"]["spec"]
        self.assertEqual(pod_spec.get("serviceAccountName"), "kubeagents-litellm")

        sa_docs = _find_docs(rendered, "ServiceAccount")
        self.assertEqual(len(sa_docs), 1)
        self.assertEqual(sa_docs[0]["metadata"]["name"], "kubeagents-litellm")

    def test_vertex_ai_custom_service_account_name(self) -> None:
        rendered = _render_litellm(
            "--set-string",
            "litellm.modelProvider=vertex_ai",
            "--set-string",
            "litellm.modelDefaultName=gemini-2.0-flash",
            "--set-string",
            "litellm.vertex.serviceAccountName=custom-vertex-sa",
        )
        deployments = _find_docs(rendered, "Deployment")
        pod_spec = deployments[0]["spec"]["template"]["spec"]
        self.assertEqual(pod_spec.get("serviceAccountName"), "custom-vertex-sa")

        sa_docs = _find_docs(rendered, "ServiceAccount")
        self.assertEqual(len(sa_docs), 1)
        self.assertEqual(sa_docs[0]["metadata"]["name"], "custom-vertex-sa")

    def test_non_vertex_providers_render_default_service_account(self) -> None:
        providers = [
            ("gemini", ["--set-string", "litellm.modelDefaultName=gemini-2.0-flash"]),
            ("anthropic", ["--set-string", "litellm.modelDefaultName=claude-3-7-sonnet"]),
            ("openai", ["--set-string", "litellm.modelDefaultName=gpt-4o"]),
            (
                "openai_compatible",
                [
                    "--set-string",
                    "litellm.modelDefaultName=meta-llama/Llama-3.1-8B-Instruct",
                    "--set-string",
                    "litellm.openaiCompatible.apiBase=https://api.openai.com/v1",
                ],
            ),
        ]
        for provider, extra_args in providers:
            with self.subTest(provider=provider):
                rendered = _render_litellm(
                    "--set-string",
                    f"litellm.modelProvider={provider}",
                    *extra_args,
                )
                deployments = _find_docs(rendered, "Deployment")
                self.assertEqual(len(deployments), 1)
                pod_spec = deployments[0]["spec"]["template"]["spec"]
                self.assertEqual(
                    pod_spec.get("serviceAccountName"),
                    "default",
                    f"provider {provider} must explicitly set serviceAccountName to 'default' (#3878)",
                )

                sa_docs = _find_docs(rendered, "ServiceAccount")
                self.assertEqual(
                    len(sa_docs),
                    0,
                    f"provider {provider} must not render the kubeagents-litellm ServiceAccount",
                )


if __name__ == "__main__":
    unittest.main()
