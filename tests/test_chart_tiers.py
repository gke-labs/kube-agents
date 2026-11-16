"""The chart's default Tiers (#3659): read and unattended, rendered only when the
API server serves the Tier kind, so a helm upgrade that hasn't applied crds/ skips
them instead of failing on an unknown kind."""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_CHART = pathlib.Path(__file__).resolve().parents[1] / "charts" / "kube-agents"
_SETS = [
    "--set", "platformAgent.harness.clusterName=c",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=p",
]
_TIER_API = "kubeagents.x-k8s.io/v1alpha1/Tier"


def _tiers(*extra: str) -> list[dict]:
    proc = subprocess.run(["helm", "template", "t", str(_CHART), *_SETS, *extra],
                          capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(proc.stdout) if d and d.get("kind") == "Tier"]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartTiersTest(unittest.TestCase):
    def test_the_defaults_are_read_and_unattended(self):
        tiers = {t["metadata"]["name"]: t["spec"] for t in _tiers("--api-versions", _TIER_API)}
        self.assertEqual(tiers, {
            "read": {"mode": "interactive", "writeMode": "propose"},
            "unattended": {"mode": "unattended", "writeMode": "propose"},
        })

    def test_no_tier_renders_where_the_api_server_doesnt_serve_the_kind(self):
        self.assertEqual(_tiers(), [])

    def test_no_tier_renders_without_the_platform_agent(self):
        self.assertEqual(_tiers("--api-versions", _TIER_API, "--set", "platformAgent.enabled=false"), [])


if __name__ == "__main__":
    unittest.main()
