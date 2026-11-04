"""The chart's footprint summary: the release's requested CPU, memory and persistent
storage with a monthly cost modeled from us-central1 list prices, printed by NOTES.txt
(`kube-agents.footprintSummary` in _helpers.tpl) and by install.sh's pre-flight summary
through the same template.

The tests render a throwaway copy of the chart whose NOTES.txt is captured into a ConfigMap
(tests.testing.common.chart_with_notes_probe) beside a second probe that
emits `kube-agents.quotaRequirements` as JSON, so each figure in the notes is checked against
the pinned formula over the totals the quota preflight sums, at defaults and with fleet-sized
overrides.

Run: python3 -m unittest discover -s tests -p 'test_footprint_summary.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.testing.common import NOTES_PROBE_CONFIGMAP, chart_with_notes_probe

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_HELM = shutil.which("helm")
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_CHART_README = _CHART / "README.md"

_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]

# The pinned list prices (us-central1 GKE Autopilot) and the billing month, as the issue
# fixed them and _helpers.tpl declares them. A change to either side fails here.
_CPU_PER_VCPU_HOUR = 0.0445
_MEMORY_PER_GIB_HOUR = 0.0049225
_DISK_PER_GIB_MONTH = 0.10
_HOURS_PER_MONTH = 730
_CLUSTER_FEE_PER_MONTH = 73
_LABEL = "(modeled from us-central1 list prices, excluding LLM tokens)"
_COST_HEADER = "Modeled monthly cost on GKE Autopilot " + _LABEL
# The defines that pin the prices, and what each prints.
_PRICE_DEFINES = {
    "kube-agents.footprintPriceRegion": "us-central1",
    "kube-agents.footprintPriceCpuPerVcpuHour": "0.0445",
    "kube-agents.footprintPriceMemoryPerGiBHour": "0.0049225",
    "kube-agents.footprintPriceBalancedDiskPerGiBMonth": "0.10",
    "kube-agents.footprintHoursPerMonth": "730",
    "kube-agents.footprintClusterFeePerMonth": "73",
}
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
    disk_gib = req["requestsStorage"] / _GIB
    cpu_cost = cores * _CPU_PER_VCPU_HOUR * _HOURS_PER_MONTH
    memory_cost = memory_gib * _MEMORY_PER_GIB_HOUR * _HOURS_PER_MONTH
    disk_cost = disk_gib * _DISK_PER_GIB_MONTH
    total = cpu_cost + memory_cost + disk_cost
    return {
        "cpu": f"CPU requests:        {cores:.3f} cores",
        "memory": f"Memory requests:     {memory_gib:.2f} GiB",
        "pods": f"Pods:                {req['pods']}",
        "storage": f"Persistent storage:  {disk_gib:.0f} GiB over {req['persistentVolumeClaims']} claims",
        "cpu_cost": (
            f"CPU:      {cores:.3f} cores x ${_CPU_PER_VCPU_HOUR}/vCPU-hour"
            f" x {_HOURS_PER_MONTH} hours = ${cpu_cost:.2f}"
        ),
        "memory_cost": (
            f"Memory:   {memory_gib:.2f} GiB x ${_MEMORY_PER_GIB_HOUR}/GiB-hour"
            f" x {_HOURS_PER_MONTH} hours = ${memory_cost:.2f}"
        ),
        "disk_cost": f"Disk:     {disk_gib:.0f} GiB x ${_DISK_PER_GIB_MONTH:.2f}/GiB-month = ${disk_cost:.2f}",
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
        chart = chart_with_notes_probe(_CHART, pathlib.Path(cls._tmp.name) / "kube-agents")
        (chart / "templates" / "zz-probe-requirements.yaml").write_text(_REQUIREMENTS_PROBE)
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
            if name == NOTES_PROBE_CONFIGMAP:
                notes = doc["data"]["notes"]
            elif name == "probe-requirements":
                requirements = json.loads(doc["data"]["requirements"])
        self.assertIsNotNone(notes, "NOTES probe not rendered")
        self.assertIsNotNone(requirements, "requirements probe not rendered")
        return notes, requirements

    def _assert_summary_matches(self, notes: str, req: dict) -> None:
        self.assertIn(_COST_HEADER, notes)
        for key, line in _expected_lines(req).items():
            self.assertIn(line, notes, f"{key}: expected line missing from NOTES:\n{notes}")

    def test_defaults_print_the_documented_footprint_and_its_cost(self) -> None:
        notes, req = self._render()
        self._assert_summary_matches(notes, req)
        # The stock install's figures, the ones the prerequisites page quotes, in the
        # words the notes use, so a wording change is a deliberate one here too.
        self.assertIn("CPU requests:        2.606 cores", notes)
        self.assertIn("Memory requests:     8.94 GiB", notes)
        self.assertIn("Persistent storage:  102 GiB over 4 claims", notes)
        self.assertIn("$0.0445/vCPU-hour", notes)
        self.assertIn("$0.0049225/GiB-hour", notes)
        self.assertIn("$0.10/GiB-month", notes)
        self.assertIn("$73.00/month GKE cluster fee", notes)
        self.assertNotIn("Not counted", notes)
        # The disk is the balanced class on the footprint line and on the cost line alike.
        self.assertIn("balanced persistent disk", notes)
        self.assertNotIn("SSD", notes)

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
        notes, req = self._render(["platformAgent.mode=next"])
        self._assert_summary_matches(notes, req)
        # The preflight sums the next-mode workloads (footprint.yaml's nextMode), so
        # the figures carry them, the NATS bus's JetStream claim included, and only
        # the session pods are left out.
        self.assertIn("Not counted: the session pods", notes)
        self.assertIn("40 GiB JetStream claim among them, are in the figures above", notes)
        today_notes, today = self._render(["platformAgent.mode=today"])
        self.assertNotIn("Not counted", today_notes)
        self.assertEqual(req["persistentVolumeClaims"], today["persistentVolumeClaims"] + 1)
        self.assertEqual(req["requestsStorage"], today["requestsStorage"] + 40 * _GIB)
        self.assertGreater(req["pods"], today["pods"])

    def test_summary_follows_the_quota_preflight_switch(self) -> None:
        # The summary shares the preflight's arithmetic, and the preflight's refusals name
        # quotaPreflight.enabled=false as the way round them; the notes keep that true.
        notes, _ = self._render(["quotaPreflight.enabled=false"])
        self.assertNotIn("Release footprint", notes)
        self.assertNotIn(_LABEL, notes)

    def test_the_sum_refuses_an_unsizable_input_where_the_preflight_is_inert(self) -> None:
        # `helm template` has no cluster, so the preflight's lookup finds no quota and the
        # comparison is inert; the summary still sums, so the sum's refusal of an input it
        # cannot size reaches this render, from NOTES.txt, and the switch lifts it. This is
        # the behaviour the chart README and values.yaml document. The chart itself, not
        # the probe copy, whose requirements probe would refuse first.
        cmd = [_HELM, "template", "test-release", str(_CHART), *_REQUIRED,
               "--set", "hindsight.enabled=true", "--set", "hindsight.postgresql.storage="]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, proc.stdout[:400])
        self.assertIn("NOTES.txt", proc.stderr)
        self.assertIn("quota preflight: hindsight.postgresql.storage is empty", proc.stderr)
        proc = subprocess.run(cmd + ["--set", "quotaPreflight.enabled=false"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class PinnedPricesTest(unittest.TestCase):
    """The defines hold the pinned figures, and the README names the defines rather than
    restating numbers that nothing would check."""

    def test_the_defines_hold_the_pinned_prices(self) -> None:
        helpers = _HELPERS.read_text()
        for name, value in _PRICE_DEFINES.items():
            match = re.search(r'\{\{- define "' + re.escape(name) + r'" -\}\}\n(.*)\n\{\{- end \}\}', helpers)
            self.assertIsNotNone(match, f"{name} is not defined")
            self.assertEqual(match.group(1).strip(), value, name)

    def test_the_readme_names_the_defines_not_the_figures(self) -> None:
        readme = _CHART_README.read_text()
        section = readme[readme.index("#### Footprint summary and modeled cost"):readme.index("#### Upgrade notes: operator memory floor")]
        self.assertIn("`kube-agents.footprintPrice*`", section)
        self.assertIn("`footprintHoursPerMonth`", section)
        self.assertIn("`footprintClusterFeePerMonth`", section)
        for value in ("0.0445", "0.0049225", "730-hour", "$73"):
            self.assertNotIn(value, section, f"the README restates {value}; name the define instead")
        self.assertIn("us-central1", section)
        self.assertNotIn("`helm template` as well", section)


if __name__ == "__main__":
    unittest.main()
