"""The smoke pipeline's Helm release routes model traffic through Vertex AI.

`hack/ci-deploy.sh` defaults `MODEL_PROVIDER` to `vertex_ai` and annotates the
chart's `kubeagents-litellm` KSA with the dedicated `kubeagents-litellm-gsa`
Workload Identity — the eval installs moved off the `GEMINI_API_KEY` path after
its fixed paid-tier-3 quota (8M input tokens/min) redded every smoke run on
2026-09-02 (#1097; diagnosis on #1184). Both halves fail silently in the diff
that breaks them: dropping the annotation `--set-string` (or the chart renaming
the key) renders an unannotated KSA, `helm --wait` still passes, and every
leased run reds at the deploy's model-call gate with nothing naming the cause.
This pins the script's flag, the provider default, and — where a real `helm`
exists — that the flag actually lands on the rendered ServiceAccount.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"

_ANNOTATION_KEY = r"litellm.vertex.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account"


class CiDeployLitellmVertexTest(unittest.TestCase):
    def test_model_provider_defaults_to_vertex_ai(self) -> None:
        text = _CI_DEPLOY.read_text()
        self.assertIn(
            'export MODEL_PROVIDER="${MODEL_PROVIDER:-vertex_ai}"',
            text,
            "hack/ci-deploy.sh no longer defaults the eval install to "
            "vertex_ai; the GEMINI_API_KEY path's fixed quota is what redded "
            "every smoke run on 2026-09-02 (#1097).",
        )

    def test_helm_release_annotates_the_litellm_ksa(self) -> None:
        text = _CI_DEPLOY.read_text()
        self.assertIn(
            f'--set-string "{_ANNOTATION_KEY}='
            "${LITELLM_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com\"",
            text,
            "hack/ci-deploy.sh must annotate the kubeagents-litellm KSA with "
            "the gateway's own GSA, or the pod has no Workload Identity and "
            "every Vertex call 403s after helm --wait has already passed.",
        )

    def test_the_litellm_gsa_is_not_the_platform_gsa(self) -> None:
        """The gateway proxies attacker-influenceable content and must hold
        aiplatform.user only — see the site's security-and-iam.md, 'The
        Vertex AI gateway is a separate identity'."""
        text = _CI_DEPLOY.read_text()
        match = re.search(r'export LITELLM_GSA_NAME="([^"]+)"', text)
        self.assertIsNotNone(match, "LITELLM_GSA_NAME is no longer exported")
        self.assertEqual(match.group(1), "kubeagents-litellm-gsa")
        self.assertNotEqual(match.group(1), "kubeagents-platform-gsa")


class HelmRendersTheAnnotatedKsaTest(unittest.TestCase):
    """The flag lands on the rendered ServiceAccount, not just in the script.

    Only a real `helm` can show that, so this skips where the binary is absent
    (a contributor's laptop) and runs in CI, which installs one.
    """

    def test_rendered_ksa_carries_the_wif_annotation(self) -> None:
        if shutil.which("helm") is None:
            self.skipTest("helm not installed")
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "t",
                str(_CHART),
                "--set-string",
                "platformAgent.harness.clusterName=c",
                "--set-string",
                "platformAgent.harness.location=us-central1",
                "--set-string",
                "platformAgent.harness.projectId=p",
                "--set-string",
                "litellm.modelProvider=vertex_ai",
                "--set-string",
                "litellm.modelDefaultName=gemini-3.1-pro-preview",
                "--set-string",
                _ANNOTATION_KEY + "=kubeagents-litellm-gsa@p.iam.gserviceaccount.com",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn(
            "iam.gke.io/gcp-service-account: kubeagents-litellm-gsa@p.iam.gserviceaccount.com",
            rendered,
            "the annotation --set-string no longer reaches the rendered "
            "kubeagents-litellm ServiceAccount",
        )
        self.assertIn("model: vertex_ai/gemini-3.1-pro-preview", rendered)
        self.assertIn("name: VERTEXAI_PROJECT", rendered)


if __name__ == "__main__":
    unittest.main()
