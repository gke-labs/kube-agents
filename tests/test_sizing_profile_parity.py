"""platformAgent.deployment.sizingProfile: the chart's tier rows against the operator's,
and the value's path to the CR.

The medium and fleet rows are declared twice: once as the constants in
k8s-operator/internal/controller/sizing_profile.go, which the operator renders, and
once as kube-agents.sizingTierRows in the chart, which the quota preflight sums and the
CR template merges the proxy override over. This test fails if either copy drifts: a chart
row whose figure differs from the operator's constant or that names no constant, and an
operator medium or fleet constant that no chart row carries, as
tests/test_agent_api_auth_sizing_parity.py does for the small row. It also renders
the chart with the value set, unset and misspelt, so the field's path to the CR, the
CRD-presence guard's arguments and the schema's enum each have a red when they change.
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/sizing_profile.go"
CHART = REPO_ROOT / "charts/kube-agents"
CHART_HELPERS = CHART / "templates/_helpers.tpl"
HELM = shutil.which("helm")
REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
VALUE = "platformAgent.deployment.sizingProfile"
# (tier, container, side, key, operator constant) for every figure a row changes from
# the small row. The small row itself is the defaults the other parity tests hold.
ROWS = (
    ("medium", "credentialProxy", "requests", "cpu", "credentialProxyMediumCPURequest"),
    ("medium", "credentialProxy", "requests", "memory", "credentialProxyMediumMemoryRequest"),
    ("medium", "credentialProxy", "autopilotRequests", "cpu", "credentialProxyMediumAutopilotCPURequest"),
    ("medium", "credentialProxy", "autopilotRequests", "memory", "credentialProxyMediumMemoryRequest"),
    ("medium", "credentialProxy", "limits", "cpu", "credentialProxyMediumCPULimit"),
    ("medium", "credentialProxy", "limits", "memory", "credentialProxyMediumMemoryLimit"),
    ("medium", "agentAPIAuth", "limits", "memory", "agentAPIAuthMediumMemoryLimit"),
    ("fleet", "credentialProxy", "requests", "cpu", "credentialProxyFleetCPURequest"),
    ("fleet", "credentialProxy", "requests", "memory", "credentialProxyFleetMemoryRequest"),
    ("fleet", "credentialProxy", "autopilotRequests", "cpu", "credentialProxyFleetCPURequest"),
    ("fleet", "credentialProxy", "autopilotRequests", "memory", "credentialProxyFleetMemoryRequest"),
    ("fleet", "credentialProxy", "limits", "cpu", "credentialProxyFleetCPULimit"),
    ("fleet", "credentialProxy", "limits", "memory", "credentialProxyFleetMemoryLimit"),
    ("fleet", "credentialProxy", "limits", "ephemeral-storage", "credentialProxyFleetEphemeralStorageLimit"),
    ("fleet", "agentAPIAuth", "limits", "memory", "agentAPIAuthFleetMemoryLimit"),
    ("fleet", "shellSandbox", "limits", "memory", "shellSandboxFleetMemoryLimit"),
)
# Every medium or fleet constant sizing_profile.go declares, whichever container it sizes:
# a row figure is a request or a limit, and no container prefix is assumed, so a fourth
# sized container's constants are collected too. Held equal to the constants ROWS names, so
# a figure added on the operator's side with no chart row behind it fails here rather than
# at the quota preflight that would count the smaller row.
OPERATOR_ROW_CONSTANT_RE = re.compile(r'^\s*(\w*(?:Medium|Fleet)\w*(?:Request|Limit))\s*=\s*"', re.MULTILINE)
# A constants block in the operator's shape for a container the profile does not size
# today, beside the names the collector must leave alone: the profile names (quoted, but
# neither a request nor a limit) and the thresholds (not quoted).
FOURTH_CONTAINER_CONSTANTS = '''
const (
	sizingProfileMedium = "medium"
	sizingMediumClusterThreshold int64 = 10
	dashboardMediumCPURequest = "500m"
	dashboardFleetMemoryLimit = "2Gi"
)
'''
PROBE_TEMPLATE = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-sizing-rows
data:
  rows: |
    {{ include "kube-agents.sizingTierRows" . }}
"""


def operator_constants() -> dict[str, str]:
    text = OPERATOR.read_text(encoding="utf-8")
    out = {}
    for name in {row[4] for row in ROWS}:
        found = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if found is None:
            raise AssertionError(f"{OPERATOR.relative_to(REPO_ROOT)} declares no {name}")
        out[name] = found.group(1)
    return out


def _run_helm(chart: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([HELM, "template", "t", str(chart), *REQUIRED, *args], capture_output=True, text=True)


def _platform_agent(rendered: str) -> dict:
    for doc in yaml.safe_load_all(rendered):
        if doc and doc.get("kind") == "PlatformAgent":
            return doc
    raise AssertionError("no PlatformAgent in the render")


@unittest.skipUnless(HELM, "helm is not installed")
class SizingTierRowsParity(unittest.TestCase):
    """The chart's rows, rendered through the helper, equal the operator's constants."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(CHART, cls.chart)
        (cls.chart / "templates" / "zz-probe.yaml").write_text(PROBE_TEMPLATE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_chart_rows_match_the_operator_constants(self) -> None:
        res = _run_helm(self.chart)
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = None
        for doc in yaml.safe_load_all(res.stdout):
            if doc and doc.get("metadata", {}).get("name") == "probe-sizing-rows":
                rows = json.loads(doc["data"]["rows"])
        self.assertIsNotNone(rows, "probe ConfigMap not rendered")
        op = operator_constants()
        seen = set()
        for tier, container, side, key, name in ROWS:
            got = rows.get(tier, {}).get(container, {}).get(side, {}).get(key)
            self.assertEqual(got, op[name], f"{tier}.{container}.{side}.{key}: chart {got!r}, operator {name} = {op[name]!r}")
            seen.add((tier, container, side, key))
        # And nothing in the chart's rows that the table above does not name.
        for tier, containers in rows.items():
            for container, sides in containers.items():
                for side, keys in sides.items():
                    for key in keys:
                        self.assertIn((tier, container, side, key), seen, f"chart row {tier}.{container}.{side}.{key} has no operator constant in ROWS")


class OperatorRowConstantsHaveAChartRow(unittest.TestCase):
    """Every medium or fleet constant the operator declares is named by ROWS, the other
    direction of the parity above; this one needs no helm."""

    def test_every_operator_row_constant_is_in_rows(self) -> None:
        declared = set(OPERATOR_ROW_CONSTANT_RE.findall(OPERATOR.read_text(encoding="utf-8")))
        self.assertTrue(declared, f"{OPERATOR.relative_to(REPO_ROOT)} declares no medium or fleet constant")
        self.assertEqual(declared, {row[4] for row in ROWS})

    def test_the_collector_reads_a_container_it_has_not_seen(self) -> None:
        """The collector keys on the request-or-limit suffix, not on the containers sized
        today, so a fourth container's constants reach the assertion above and the profile
        names and thresholds do not."""
        self.assertEqual(
            set(OPERATOR_ROW_CONSTANT_RE.findall(FOURTH_CONTAINER_CONSTANTS)),
            {"dashboardMediumCPURequest", "dashboardFleetMemoryLimit"},
        )


@unittest.skipUnless(HELM, "helm is not installed")
class SizingProfileReachesTheCR(unittest.TestCase):
    def test_a_set_profile_is_forwarded(self) -> None:
        for profile in ("small", "medium", "fleet", "auto"):
            res = _run_helm(CHART, "--set", f"{VALUE}={profile}")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(_platform_agent(res.stdout)["spec"]["deployment"].get("sizingProfile"), profile)

    def test_an_unset_profile_writes_no_field(self) -> None:
        res = _run_helm(CHART)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("sizingProfile", _platform_agent(res.stdout)["spec"]["deployment"])
        self.assertNotIn("sizingProfile", res.stdout)

    def test_the_schema_refuses_a_misspelt_profile(self) -> None:
        res = _run_helm(CHART, "--set", f"{VALUE}=large")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("sizingProfile", res.stderr)

    def test_a_proxy_override_valid_on_the_chosen_row_renders(self) -> None:
        """A 3 CPU request crosses the small row's 2 CPU limit and sits under fleet's 8;
        the CR template merges the override over the profile's row, as the operator
        admits it, so the fleet render passes and the small render fails."""
        over = ["--set", "platformAgent.deployment.credentialProxy.resources.requests.cpu=3"]
        fleet = _run_helm(CHART, "--set", f"{VALUE}=fleet", *over)
        self.assertEqual(fleet.returncode, 0, fleet.stderr)
        small = _run_helm(CHART, *over)
        self.assertNotEqual(small.returncode, 0)
        self.assertIn("exceeds the sizing profile row's limits.cpu", small.stderr)


if __name__ == "__main__":
    unittest.main()
