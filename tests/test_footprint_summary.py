"""The chart's footprint summary: the release's requested CPU, memory and persistent
storage with a monthly cost modeled from us-central1 list prices, printed by NOTES.txt
(`kube-agents.footprintSummary` in _helpers.tpl) and by install.sh's pre-flight summary
through the same template.

The tests render a throwaway copy of the chart whose NOTES.txt is captured into a ConfigMap
(the probe tests/test_chart_third_party_image_registry.py uses) beside a second probe that
emits `kube-agents.quotaRequirements` as JSON, so each figure in the notes is checked against
the pinned formula over the totals the quota preflight sums, at defaults and with fleet-sized
overrides.

Run: python3 -m unittest discover -s tests -p 'test_footprint_summary.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_HELM = shutil.which("helm")

_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]

# The pinned list prices (us-central1 GKE Autopilot) and the billing month, as the issue
# fixed them and _helpers.tpl declares them. A change to either side fails here.
_CPU_PER_VCPU_HOUR = 0.0445
_MEMORY_PER_GIB_HOUR = 0.0049225
_SSD_PER_GIB_MONTH = 0.10
_HOURS_PER_MONTH = 730
_CLUSTER_FEE_PER_MONTH = 73
_LABEL = "(modeled from us-central1 list prices, excluding LLM tokens)"
_GIB = 1024**3
_MILLIS_PER_CORE = 1000

# Fleet-sized overrides: a bigger data volume (two claims follow it), Hindsight on through
# the memory provider as the composition sets it, more LiteLLM replicas, a credential proxy
# with raised requests, and the minter and the web console on.
_FLEET_SETS = [
    "platformAgent.storage.size=200Gi",
    "platformAgent.harness.memory.provider=kube_agents_memory",
    "litellm.replicaCount=4",
    "platformAgent.deployment.credentialProxy.resources.requests.cpu=2",
    "platformAgent.deployment.credentialProxy.resources.requests.memory=4Gi",
    "platformAgent.deployment.credentialProxy.resources.limits.cpu=4",
    "platformAgent.deployment.credentialProxy.resources.limits.memory=8Gi",
    "githubMinter.enabled=true",
    "githubMinter.org=foo",
    "githubMinter.repo=bar",
    "platformAgent.integration.github.gitRepo=foo/bar",
    "platformAgent.integration.github.org=foo",
    "webConsole.enabled=true",
]

_REQUIREMENTS_PROBE = """apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-requirements
data:
  requirements: {{ include "kube-agents.quotaRequirements" . | toJson }}
"""


def _expected_lines(req: dict) -> dict:
    """The summary's figures from the quota totals, by the formula the template pins."""
    cores = req["requestsCpu"] / _MILLIS_PER_CORE
    memory_gib = req["requestsMemory"] / _GIB
    ssd_gib = req["requestsStorage"] / _GIB
    cpu_cost = cores * _CPU_PER_VCPU_HOUR * _HOURS_PER_MONTH
    memory_cost = memory_gib * _MEMORY_PER_GIB_HOUR * _HOURS_PER_MONTH
    ssd_cost = ssd_gib * _SSD_PER_GIB_MONTH
    total = cpu_cost + memory_cost + ssd_cost
    return {
        "cpu": f"CPU requests:        {cores:.3f} cores",
        "memory": f"Memory requests:     {memory_gib:.2f} GiB",
        "pods": f"Pods:                {req['pods']}",
        "storage": f"Persistent storage:  {ssd_gib:.0f} GiB over {req['persistentVolumeClaims']} claims",
        "cpu_cost": (
            f"CPU:      {cores:.3f} cores x ${_CPU_PER_VCPU_HOUR}/vCPU-hour"
            f" x {_HOURS_PER_MONTH} hours = ${cpu_cost:.2f}"
        ),
        "memory_cost": (
            f"Memory:   {memory_gib:.2f} GiB x ${_MEMORY_PER_GIB_HOUR}/GiB-hour"
            f" x {_HOURS_PER_MONTH} hours = ${memory_cost:.2f}"
        ),
        "ssd_cost": f"SSD:      {ssd_gib:.0f} GiB x ${_SSD_PER_GIB_MONTH:.2f}/GiB-month = ${ssd_cost:.2f}",
        "total": (
            f"Total:    ${total:.2f}/month, plus the ${_CLUSTER_FEE_PER_MONTH:.2f}/month"
            " GKE cluster fee (per cluster, noted separately)"
        ),
    }


@unittest.skipIf(_HELM is None, "helm is required for chart template rendering tests")
class FootprintSummaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(_CHART, chart)
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
        (templates / "zz-probe-requirements.yaml").write_text(_REQUIREMENTS_PROBE)
        cls.chart = chart

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _render(self, sets=()) -> tuple[str, dict]:
        cmd = [_HELM, "template", "test-release", str(self.chart), *_REQUIRED]
        for item in sets:
            cmd += ["--set", item]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"render failed:\n{proc.stderr}")
        notes = requirements = None
        for doc in yaml.safe_load_all(proc.stdout):
            name = (doc or {}).get("metadata", {}).get("name")
            if name == "notes-under-test":
                notes = doc["data"]["notes"]
            elif name == "probe-requirements":
                requirements = json.loads(doc["data"]["requirements"])
        self.assertIsNotNone(notes, "NOTES probe not rendered")
        self.assertIsNotNone(requirements, "requirements probe not rendered")
        return notes, requirements

    def _assert_summary_matches(self, notes: str, req: dict) -> None:
        self.assertIn(_LABEL, notes)
        for key, line in _expected_lines(req).items():
            self.assertIn(line, notes, f"{key}: expected line missing from NOTES:\n{notes}")

    def test_defaults_print_the_documented_footprint_and_its_cost(self) -> None:
        notes, req = self._render()
        self._assert_summary_matches(notes, req)
        # The stock install's figures, the ones the prerequisites page quotes, in the
        # words the notes use, so a wording change is a deliberate one here too.
        self.assertIn("CPU requests:        2.506 cores", notes)
        self.assertIn("Memory requests:     8.69 GiB", notes)
        self.assertIn("Persistent storage:  102 GiB over 4 claims", notes)
        self.assertIn("$0.0445/vCPU-hour", notes)
        self.assertIn("$0.0049225/GiB-hour", notes)
        self.assertIn("$0.10/GiB-month", notes)
        self.assertIn("$73.00/month GKE cluster fee", notes)
        self.assertNotIn("Not counted: the spec.mode next workloads", notes)

    def test_fleet_sized_overrides_move_every_figure(self) -> None:
        _, defaults = self._render()
        notes, req = self._render(_FLEET_SETS)
        self._assert_summary_matches(notes, req)
        for key in ("requestsCpu", "requestsMemory", "requestsStorage", "pods", "persistentVolumeClaims"):
            self.assertGreater(req[key], defaults[key], key)
        # 200Gi on two data claims (150Gi over the default each) plus Hindsight's 8Gi claim.
        self.assertIn("Persistent storage:  410 GiB over 5 claims", notes)

    def test_storage_size_alone_moves_the_ssd_line_and_the_total(self) -> None:
        default_notes, default_req = self._render()
        notes, req = self._render(["platformAgent.storage.size=80Gi"])
        self.assertEqual(req["requestsCpu"], default_req["requestsCpu"])
        self.assertEqual(req["requestsStorage"], default_req["requestsStorage"] + 2 * 30 * _GIB)
        self.assertIn("Persistent storage:  162 GiB over 4 claims", notes)
        self.assertIn(_expected_lines(req)["total"], notes)
        self.assertNotEqual(_expected_lines(req)["total"], _expected_lines(default_req)["total"])
        self.assertIn(_expected_lines(default_req)["total"], default_notes)

    def test_next_mode_names_what_the_summary_leaves_out(self) -> None:
        notes, _ = self._render(["platformAgent.mode=next"])
        self.assertIn("Not counted: the spec.mode next workloads", notes)

    def test_summary_follows_the_quota_preflight_switch(self) -> None:
        # The summary shares the preflight's arithmetic, and the preflight's refusals name
        # quotaPreflight.enabled=false as the way round them; the notes keep that true.
        notes, _ = self._render(["quotaPreflight.enabled=false"])
        self.assertNotIn("Release footprint", notes)
        self.assertNotIn(_LABEL, notes)


if __name__ == "__main__":
    unittest.main()
