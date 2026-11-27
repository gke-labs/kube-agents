"""Tests for hack/kind-up.sh cluster-name plumbing."""

from pathlib import Path
import shutil
import subprocess
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
KIND_UP = REPO_ROOT / "hack" / "kind-up.sh"
CHART = REPO_ROOT / "charts" / "kube-agents"
CUSTOM_CLUSTER_NAME = "my-kind-cluster"


class KindUpClusterNameTest(unittest.TestCase):
    def test_kind_up_passes_cluster_name_to_helm_and_printed_exports(self):
        script = KIND_UP.read_text(encoding="utf-8")
        self.assertIn('--set-string "platformAgent.harness.clusterName=${CLUSTER_NAME}"', script)
        self.assertNotIn("platformAgent.harness.clusterName=kind", script)
        self.assertIn("export BENCH_TF_ROOT=./tf PROJECT_ID=kind CLUSTER_NAME=${CLUSTER_NAME}", script)
        self.assertNotIn("CLUSTER_NAME=kind", script)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_chart_renders_custom_kind_cluster_name(self):
        proc = subprocess.run(
            [
                "helm",
                "template",
                "test-release",
                str(CHART),
                "--set-string",
                "platformAgent.harness.projectId=kind",
                "--set-string",
                "platformAgent.harness.location=kind",
                "--set-string",
                f"platformAgent.harness.clusterName={CUSTOM_CLUSTER_NAME}",
                "--show-only",
                "templates/platform-agent-cr.yaml",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f'clusterName: "{CUSTOM_CLUSTER_NAME}"', proc.stdout)
        self.assertIn('location: "kind"', proc.stdout)
        self.assertIn('projectId: "kind"', proc.stdout)


if __name__ == "__main__":
    unittest.main()
