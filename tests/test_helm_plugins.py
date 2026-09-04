"""Unit tests for AgentPlugin templating in charts/kube-agents."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHART = _REPO_ROOT / "charts" / "kube-agents"


class HelmPluginsTemplatingTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_plugins_disabled_by_default(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertNotIn("kind: AgentPlugin", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_pubsub_platform_plugin_enabled(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("kind: AgentPlugin", proc.stdout)
        self.assertIn("name: pubsubplatform", proc.stdout)
        self.assertIn("ghcr.io/gke-labs/kube-agents/pubsub-platform:", proc.stdout)
        self.assertNotIn("name: gkestockoutinvestigator", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_stockout_investigator_requires_pubsub_platform(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "plugins.stockoutInvestigator is enabled, but plugins.pubsubPlatform is disabled",
            proc.stderr,
        )

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_stockout_investigator_with_pubsub_platform(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=my-gke-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("kind: AgentPlugin", proc.stdout)
        self.assertIn("name: pubsubplatform", proc.stdout)
        self.assertIn("name: gkestockoutinvestigator", proc.stdout)
        self.assertIn("targetProfile: \"platform\"", proc.stdout)
        self.assertIn("resource.labels.cluster_name == 'my-gke-cluster'", proc.stdout)
        self.assertIn("ghcr.io/gke-labs/kube-agents/gke-stockout-investigator:", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_mirrored_registry_rewrites_plugin_images(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
            "--set",
            "global.imageRegistry=us-docker.pkg.dev/my-proj/my-mirror",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("us-docker.pkg.dev/my-proj/my-mirror/pubsub-platform:", proc.stdout)
        self.assertIn("us-docker.pkg.dev/my-proj/my-mirror/gke-stockout-investigator:", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_plugins_inherit_platform_agent_image_tag(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "platformAgent.deployment.image.tag=custom-sha-12345",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("ghcr.io/gke-labs/kube-agents/pubsub-platform:custom-sha-12345", proc.stdout)
        self.assertIn("ghcr.io/gke-labs/kube-agents/gke-stockout-investigator:custom-sha-12345", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_stockout_investigator_renders_tuning(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("tuning:", proc.stdout)
        self.assertIn("maxInProgress: 3", proc.stdout)
        self.assertIn("maxTurns: 200", proc.stdout)
        self.assertIn("maxTurns: 150", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_plugins_inherit_custom_platform_agent_name(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "platformAgent.name=custom-platform-agent",
            "--set",
            "plugins.pubsubPlatform.enabled=true",
            "--set",
            "plugins.stockoutInvestigator.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn('agentRef: "custom-platform-agent"', proc.stdout)
        self.assertNotIn('agentRef: "platform-agent"', proc.stdout)


if __name__ == "__main__":
    unittest.main()

