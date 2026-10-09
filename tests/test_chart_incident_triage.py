"""platformAgent.harness.incidentTriage reaches the CR only when an install sets it.

The CRD defaults spec.harness.incidentTriage.openPullRequest to false, and the
operator adds INCIDENT_TRIAGE_OPEN_PULL_REQUEST to the gateway only when it is
true. The chart's part is to stay out of the way: an install that never set the
value renders the CR it rendered before, with no incidentTriage block at all,
and an install that did gets the value it wrote, false included.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HARNESS = (
    "platformAgent.harness.projectId=p",
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-central1",
)
_KEY = "platformAgent.harness.incidentTriage.openPullRequest"


def _render(*sets: str) -> subprocess.CompletedProcess:
    args = ["helm", "template", "t", str(_CHART), "--show-only", _CR_TEMPLATE]
    for value in _HARNESS + sets:
        args += ["--set", value]
    return subprocess.run(args, capture_output=True, text=True)


def _harness(*sets: str) -> dict:
    result = _render(*sets)
    if result.returncode != 0:
        raise AssertionError(f"helm template failed: {result.stderr}")
    return yaml.safe_load(result.stdout)["spec"]["harness"]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartIncidentTriageTest(unittest.TestCase):
    def test_unset_renders_no_block(self):
        self.assertNotIn("incidentTriage", _harness())

    def test_true_renders_the_field(self):
        self.assertEqual(_harness(f"{_KEY}=true")["incidentTriage"], {"openPullRequest": True})

    def test_false_is_rendered_as_written(self):
        self.assertEqual(_harness(f"{_KEY}=false")["incidentTriage"], {"openPullRequest": False})

    def test_the_schema_refuses_a_string(self):
        args = ["helm", "template", "t", str(_CHART), "--show-only", _CR_TEMPLATE, "--set-string", f"{_KEY}=yes"]
        for value in _HARNESS:
            args += ["--set", value]
        result = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("openPullRequest", result.stderr)

    def test_the_schema_refuses_an_unknown_key(self):
        result = _render("platformAgent.harness.incidentTriage.autoMerge=true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("autoMerge", result.stderr)


if __name__ == "__main__":
    unittest.main()
