"""The dispatcher Job admission policy renders with its CRD, and its operator signal with it.

The policy names a CRD param kind, DispatchJobTemplate, shipped in the chart's crds/, which
`helm upgrade` never applies. On an upgrade whose cluster lacks that CRD the chart renders
neither the policy nor the operator's A2A_DISPATCHER_JOB_POLICY, so the dispatcher's Job mode
stays off instead of the policy refusing every Job in the profile namespace. A fresh install
(and `helm template`, which is no upgrade) renders both.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_dispatcher_job_policy.py' -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_CHART = REPO_ROOT / "charts" / "kube-agents"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


def _render(*extra: str) -> str:
    out = subprocess.run(
        ["helm", "template", "kube-agents", str(_CHART), "--namespace", "kubeagents-system",
         "--kube-version", "1.33.0", *_REQUIRED, *extra],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class DispatcherJobPolicyRenderTest(unittest.TestCase):
    def test_a_fresh_render_has_the_policy_and_the_signal(self):
        rendered = _render()
        self.assertIn("name: kube-agents-a2a-dispatcher-job", rendered)
        self.assertIn("A2A_DISPATCHER_JOB_POLICY", rendered)

    def test_an_upgrade_without_the_crd_has_neither(self):
        # `helm template` has no cluster, so the CRD lookup is empty: an upgrade onto a cluster
        # that lacks the DispatchJobTemplate CRD.
        rendered = _render("--is-upgrade")
        self.assertNotIn("name: kube-agents-a2a-dispatcher-job", rendered)
        self.assertNotIn("A2A_DISPATCHER_JOB_POLICY", rendered)

    def test_policy_off_has_neither(self):
        rendered = _render("--set", "admissionPolicy.enabled=false")
        self.assertNotIn("name: kube-agents-a2a-dispatcher-job", rendered)
        self.assertNotIn("A2A_DISPATCHER_JOB_POLICY", rendered)


if __name__ == "__main__":
    unittest.main()
