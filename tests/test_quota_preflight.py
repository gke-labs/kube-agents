"""Tests for the chart's render-time ResourceQuota preflight (#749).

Run with the repository's runner: `make test-python`, or directly
`python3 -m unittest discover -s tests -p 'test_*.py'`. The `tests/` directory is inside
PYTHON_TEST_DIRS, so these run on every pull request.

The preflight is split into three templates so that the two that need no cluster can be
tested without one (see the comment above `kube-agents.quotaRequirements` in _helpers.tpl):

- `kube-agents.quotaRequirements` totals what the release needs;
- `kube-agents.quotaCheckItems` compares those totals against ResourceQuota objects
  handed to it;
- `kube-agents.quotaPreflight` does the cluster lookup and calls the two above.

The tests below render a throwaway copy of the chart with a probe template that calls the
first two directly, which is what lets them assert the arithmetic and the pass/fail
decision rather than only that the templates exist.
"""

import json
import math
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_VALUES = _CHART / "values.yaml"
_FOOTPRINT = _CHART / "files" / "footprint.yaml"
_SCHEMA = _CHART / "values.schema.json"
_PREFLIGHT_TPL = _CHART / "templates" / "quota-preflight.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"

_HELM = shutil.which("helm")

_PROBE_TEMPLATE = """
{{- if .Values.probe.emitRequirements }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-requirements
data:
  requirements: |
    {{ include "kube-agents.quotaRequirements" . }}
{{- end }}
{{- if .Values.probe.quotas }}
{{- include "kube-agents.quotaCheckItems" (dict
      "ctx" .
      "items" .Values.probe.quotas
      "required" (include "kube-agents.quotaRequirements" . | fromJson)) }}
{{- end }}
"""

# Totals for a stock install, re-derived by hand from values.yaml and footprint.yaml. These
# are the same numbers the install prerequisites table quotes, so a change that moves one
# without updating the other fails here.
_DEFAULT_PODS = 7
_DEFAULT_REQUESTS_CPU_MILLIS = 2466
_DEFAULT_LIMITS_CPU_MILLIS = 10200
# Memory and ephemeral storage are summed by the same helper as CPU but were asserted
# nowhere, so a generator that stopped parsing them could be regenerated and committed
# together with a green `--check` (both sides move at once) and nothing would catch it.
_DEFAULT_REQUESTS_MEMORY_BYTES = 8064 * 1024**2
_DEFAULT_LIMITS_MEMORY_BYTES = 22016 * 1024**2
_DEFAULT_REQUESTS_EPHEMERAL_BYTES = 5 * 1024**3
_DEFAULT_LIMITS_EPHEMERAL_BYTES = 5 * 1024**3
# Cleanup hook Job: 50m requested / 200m limit, 64Mi requested / 128Mi limit, 1 pod.
_CLEANUP_HOOK_REQUEST_MILLIS = 50
_CLEANUP_HOOK_LIMIT_MILLIS = 200
_CLEANUP_HOOK_REQUEST_MEMORY_BYTES = 64 * 1024**2
_CLEANUP_HOOK_LIMIT_MEMORY_BYTES = 128 * 1024**2
_CLEANUP_HOOK_PODS = 1
# Dashboard container: 256m requested, 1 core limit, inside the agent pod.
_DASHBOARD_REQUEST_MILLIS = 256
_DASHBOARD_LIMIT_MILLIS = 1000
# One agent pod (base + dashboard) as requested.
_AGENT_POD_REQUEST_MILLIS = 1506
# The agent pod's ephemeral-storage request, the only one among the operator-rendered
# workloads that ever sizes a surge.
_AGENT_POD_EPHEMERAL_REQUEST_BYTES = 3 * 1024**3
# The replica count at which the operator switches the gateway Deployment from Recreate to
# RollingUpdate (resolveDeploymentReplicasAndStrategy), and so the first one whose rollout
# creates a surge Pod.
_HA_REPLICAS = 2
# The largest pod that rolls with a surge Pod at defaults: LiteLLM, at a 100m CPU request.
# The agent pod is larger but rolls with Recreate at the default single replica.
_LARGEST_SURGING_REQUEST_MILLIS = 100
# The operator's own request.
_OPERATOR_REQUEST_MILLIS = 10
# Two operator-rendered claims plus two from the shell StatefulSet's volumeClaimTemplates.
_OPERATOR_PVC_COUNT = 4
_OPERATOR_STORAGE_BYTES = 22 * 1024**3
# hindsight.postgresql.storage, the volumeClaimTemplate the StatefulSet renders.
_HINDSIGHT_STORAGE_BYTES = 8 * 1024**3
# A memory quota written in decimal SI. 1G is 1,000,000,000 bytes, which is a whole number
# of no binary unit -- the case where an exact-only formatter falls back to raw bytes.
_DECIMAL_SI_HARD = "1G"
_DECIMAL_SI_HARD_BYTES = 1000000000
# LiteLLM runs two replicas at a 500m CPU limit each, requesting 100m each.
_LITELLM_REPLICAS = 2
_LITELLM_CPU_LIMIT_MILLIS = 500 * _LITELLM_REPLICAS
_LITELLM_CPU_REQUEST_MILLIS = 100 * _LITELLM_REPLICAS
# An ephemeral-storage request no chart workload sets by default, so the totals move by
# exactly this much when one does.
_EPHEMERAL_SET_GIB = 3
# What the shell StatefulSet leaves behind: persistentVolumeClaimRetentionPolicy is
# Retain/Retain, so these two claims survive `helm uninstall` and are in `used` on the
# next install, to be reused by name rather than created again.
_RETAINED_PVC_COUNT = 2
_RETAINED_STORAGE_GIB = 11
_MILLICORES_PER_CORE = 1000


def _parse_gib_or_mib(quantity: str) -> int:
    """Bytes from the Gi/Mi spellings the patch generator emits."""
    for suffix, multiplier in (("Gi", 1024**3), ("Mi", 1024**2)):
        if quantity.endswith(suffix):
            return int(quantity.removesuffix(suffix)) * multiplier
    return int(quantity)


def _format_cpu(millis: int) -> str:
    """Format millicores matching kube-agents.formatCpu."""
    if millis and millis % _MILLICORES_PER_CORE == 0:
        return str(millis // _MILLICORES_PER_CORE)
    return f"{millis}m"

_REQUIRED_HARNESS = [
    "--set",
    "platformAgent.harness.clusterName=c",
    "--set",
    "platformAgent.harness.location=us-east4",
    "--set",
    "platformAgent.harness.projectId=p",
]


@unittest.skipUnless(_HELM, "helm is not installed")
class PreflightDecisionTest(unittest.TestCase):
    """Drives the preflight's arithmetic and its pass/fail decision without a cluster."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(_CHART, cls.chart)
        # The probe adds a value the shipped schema does not declare, and the schema is
        # closed. Dropping it from the throwaway copy keeps the probe out of the real chart.
        (cls.chart / "values.schema.json").unlink(missing_ok=True)
        (cls.chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _render(
        self,
        values: dict,
        sets: list[str] | None = None,
        extra_args: list[str] | None = None,
    ):
        """Render the probe chart. Returns the CompletedProcess without asserting on it."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump(values, fh)
            values_path = fh.name
        args = [_HELM, "template", "test-release", str(self.chart), "-f", values_path]
        args += _REQUIRED_HARNESS
        for item in sets or []:
            args += ["--set", item]
        args += extra_args or []
        try:
            return subprocess.run(args, capture_output=True, text=True)
        finally:
            pathlib.Path(values_path).unlink(missing_ok=True)

    def _requirements(self, sets: list[str] | None = None) -> dict:
        res = self._render({"probe": {"emitRequirements": True}}, sets)
        self.assertEqual(res.returncode, 0, f"render failed:\n{res.stderr}")
        for doc in yaml.safe_load_all(res.stdout):
            if doc and doc.get("metadata", {}).get("name") == "probe-requirements":
                return json.loads(doc["data"]["requirements"])
        self.fail(f"probe ConfigMap not found in output:\n{res.stdout}")

    def test_default_totals_match_the_documented_footprint(self) -> None:
        req = self._requirements()
        self.assertEqual(req["pods"], _DEFAULT_PODS)
        self.assertEqual(req["requestsCpu"], _DEFAULT_REQUESTS_CPU_MILLIS)
        self.assertEqual(req["limitsCpu"], _DEFAULT_LIMITS_CPU_MILLIS)
        self.assertEqual(req["requestsMemory"], _DEFAULT_REQUESTS_MEMORY_BYTES)
        self.assertEqual(req["limitsMemory"], _DEFAULT_LIMITS_MEMORY_BYTES)
        self.assertEqual(req["requestsEphemeral"], _DEFAULT_REQUESTS_EPHEMERAL_BYTES)
        self.assertEqual(req["limitsEphemeral"], _DEFAULT_LIMITS_EPHEMERAL_BYTES)

    def test_disabling_the_dashboard_drops_it_from_the_total(self) -> None:
        """The flag is harness.hermes.dashboardEnabled, one level deeper than harness.

        Read at harness.dashboardEnabled the lookup matches nothing, the branch never runs,
        and a dashboard-off install is charged for a dashboard it will not schedule.
        """
        base = self._requirements()
        off = self._requirements(["platformAgent.harness.hermes.dashboardEnabled=false"])
        self.assertEqual(
            base["requestsCpu"] - off["requestsCpu"], _DASHBOARD_REQUEST_MILLIS
        )
        self.assertEqual(base["limitsCpu"] - off["limitsCpu"], _DASHBOARD_LIMIT_MILLIS)
        # The dashboard shares the agent's pod, so switching it off frees no pod.
        self.assertEqual(base["pods"], off["pods"])

    def test_disabling_cleanup_hook_drops_it_from_the_total(self) -> None:
        """Disabling the pre-delete cleanup hook drops its pod and resources."""
        base = self._requirements()
        off = self._requirements(["platformAgent.cleanupHook.enabled=false"])
        self.assertEqual(base["pods"] - off["pods"], _CLEANUP_HOOK_PODS)
        self.assertEqual(
            base["requestsCpu"] - off["requestsCpu"], _CLEANUP_HOOK_REQUEST_MILLIS
        )
        self.assertEqual(
            base["limitsCpu"] - off["limitsCpu"], _CLEANUP_HOOK_LIMIT_MILLIS
        )
        self.assertEqual(
            base["requestsMemory"] - off["requestsMemory"],
            _CLEANUP_HOOK_REQUEST_MEMORY_BYTES,
        )
        self.assertEqual(
            base["limitsMemory"] - off["limitsMemory"], _CLEANUP_HOOK_LIMIT_MEMORY_BYTES
        )

    def test_agent_replicas_multiply_the_agent_pod(self) -> None:
        """An HA install must not pass a check sized for one replica."""
        replicas = 3
        base = self._requirements()
        ha = self._requirements(
            [f"platformAgent.deployment.availability.replicas={replicas}"]
        )
        self.assertEqual(ha["pods"] - base["pods"], replicas - 1)
        self.assertEqual(
            ha["requestsCpu"] - base["requestsCpu"],
            _AGENT_POD_REQUEST_MILLIS * (replicas - 1),
        )

    def test_operator_replicas_are_counted(self) -> None:
        replicas = 3
        base = self._requirements()
        scaled = self._requirements([f"operator.replicaCount={replicas}"])
        self.assertEqual(scaled["pods"] - base["pods"], replicas - 1)
        self.assertEqual(
            scaled["requestsCpu"] - base["requestsCpu"],
            _OPERATOR_REQUEST_MILLIS * (replicas - 1),
        )

    def test_claims_are_counted_and_do_not_scale_with_replicas(self) -> None:
        base = self._requirements()
        self.assertEqual(base["persistentVolumeClaims"], _OPERATOR_PVC_COUNT)
        self.assertEqual(base["requestsStorage"], _OPERATOR_STORAGE_BYTES)
        ha = self._requirements(["platformAgent.deployment.availability.replicas=3"])
        self.assertEqual(ha["persistentVolumeClaims"], _OPERATOR_PVC_COUNT)

    def _quota(self, hard: dict, used: dict | None = None, **extra) -> dict:
        spec = {"hard": hard}
        spec.update(extra)
        return {
            "metadata": {"name": "test-quota"},
            "spec": spec,
            "status": {"used": used or {}},
        }

    def test_a_quota_that_cannot_fit_the_release_fails_the_render(self) -> None:
        res = self._render({"probe": {"quotas": [self._quota({"pods": "2"})]}})
        self.assertNotEqual(res.returncode, 0, "render should have failed")
        self.assertIn("test-quota", res.stderr)
        self.assertIn("pods", res.stderr)
        self.assertIn("kubectl patch resourcequota", res.stderr)

    def test_a_quota_with_room_passes(self) -> None:
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {"pods": "50", "requests.cpu": "100", "limits.cpu": "200"}
                        )
                    ]
                }
            }
        )
        self.assertEqual(res.returncode, 0, f"render should have passed:\n{res.stderr}")

    def test_generic_operator_rendered_workload_is_counted(self) -> None:
        """A workload added to operatorRendered is counted without editing the template."""
        base_req = self._requirements()

        footprint_path = self.chart / "files" / "footprint.yaml"
        original_footprint = footprint_path.read_text()
        footprint_data = yaml.safe_load(original_footprint)
        footprint_data["operatorRendered"]["extraWorkload"] = {
            "pods": 1,
            "cpuMillisRequest": 200,
            "cpuMillisLimit": 400,
            "memoryBytesRequest": 128 * 1024**2,
            "memoryBytesLimit": 256 * 1024**2,
            "ephemeralStorageBytesRequest": 0,
            "ephemeralStorageBytesLimit": 0,
        }
        try:
            footprint_path.write_text(yaml.safe_dump(footprint_data))
            new_req = self._requirements()
            self.assertEqual(new_req["pods"], base_req["pods"] + 1)
            self.assertEqual(new_req["requestsCpu"], base_req["requestsCpu"] + 200)
            self.assertEqual(new_req["limitsCpu"], base_req["limitsCpu"] + 400)
            self.assertEqual(
                new_req["requestsMemory"], base_req["requestsMemory"] + 128 * 1024**2
            )
            self.assertEqual(
                new_req["limitsMemory"], base_req["limitsMemory"] + 256 * 1024**2
            )
        finally:
            footprint_path.write_text(original_footprint)

    def test_a_scoped_quota_is_skipped(self) -> None:
        """A scoped quota covers a subset of pods this template cannot identify."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota({"pods": "1"}, scopes=["BestEffort"]),
                        self._quota(
                            {"pods": "1"},
                            scopeSelector={"matchExpressions": []},
                        ),
                    ]
                }
            }
        )
        self.assertEqual(res.returncode, 0, f"scoped quotas must be skipped:\n{res.stderr}")

    def test_the_patch_leaves_room_for_a_surge_pod(self) -> None:
        """used + required exactly fits at rest and then stalls the first rollout."""
        res = self._render({"probe": {"quotas": [self._quota({"pods": "2"})]}})
        self.assertNotEqual(res.returncode, 0)
        # Required is the default pod count; the patch must ask for more than that.
        self.assertIn(f'"pods":"{_DEFAULT_PODS + 1}"', res.stderr)

    def test_large_binary_units_parse(self) -> None:
        """`1Pi` must not read as 0 and refuse the release against a huge quota."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota({"limits.memory": "1Pi", "requests.storage": "1Pi"})
                    ]
                }
            }
        )
        self.assertEqual(res.returncode, 0, f"1Pi should be ample:\n{res.stderr}")

    def test_an_unparseable_quantity_fails_loudly(self) -> None:
        """Falling through to 0 would refuse the release and misdescribe the cluster."""
        res = self._render({"probe": {"quotas": [self._quota({"limits.memory": "12xyz"})]}})
        self.assertNotEqual(res.returncode, 0, "an unreadable quantity must not be guessed")
        self.assertIn("cannot parse", res.stderr)

    def test_unmodelled_quota_keys_are_ignored(self) -> None:
        res = self._render(
            {"probe": {"quotas": [self._quota({"services": "1", "secrets": "1"})]}}
        )
        self.assertEqual(
            res.returncode, 0, f"unmodelled keys must be skipped:\n{res.stderr}"
        )

    # A quota big enough for the release but nearly spent. `hard` alone clears the first
    # branch, so only the `hard - used` one can fail these renders -- which is what makes
    # them the tests for it.
    _CROWDED_HARD = {"pods": "50"}
    _CROWDED_USED = {"pods": str(50 - _DEFAULT_PODS + 1)}

    def test_used_leaves_too_little_headroom_for_a_fresh_install(self) -> None:
        res = self._render(
            {
                "probe": {
                    "quotas": [self._quota(self._CROWDED_HARD, used=self._CROWDED_USED)]
                }
            }
        )
        self.assertNotEqual(res.returncode, 0, "a nearly-full quota must refuse an install")
        self.assertIn("available headroom", res.stderr)

    def test_the_same_quota_passes_on_upgrade(self) -> None:
        """The release's own pods are in `used`, so subtracting them again refuses every upgrade."""
        res = self._render(
            {
                "probe": {
                    "quotas": [self._quota(self._CROWDED_HARD, used=self._CROWDED_USED)]
                }
            },
            extra_args=["--is-upgrade"],
        )
        self.assertEqual(res.returncode, 0, f"upgrade must not be refused:\n{res.stderr}")

    def test_the_patch_adds_to_what_is_already_used(self) -> None:
        """A patch that forgets `used` is short by exactly `used` and fixes nothing."""
        used = 40
        res = self._render(
            {
                "probe": {
                    "quotas": [self._quota({"pods": "2"}, used={"pods": str(used)})]
                }
            }
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f'"pods":"{used + _DEFAULT_PODS + 1}"', res.stderr)

    def test_a_canonical_count_quantity_parses(self) -> None:
        """The API server writes 1000 back as `1k`; read as 0 it refuses an ample quota."""
        res = self._render({"probe": {"quotas": [self._quota({"pods": "1k"})]}})
        self.assertEqual(res.returncode, 0, f"1k pods should be ample:\n{res.stderr}")

    def test_a_canonical_cpu_quantity_parses(self) -> None:
        """CPU is DecimalSI, so `requests.cpu: 1000` reads back from the API server as `1k`."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"requests.cpu": "1k", "limits.cpu": "2k"})]}}
        )
        self.assertEqual(res.returncode, 0, f"1k/2k CPU should be ample:\n{res.stderr}")

    def test_a_quota_too_large_for_int64_does_not_wrap_negative(self) -> None:
        """`1E` CPU is 10^21 millicores and `8Ei` is 2^63 bytes, both past math.MaxInt64.

        Converted with a bare `int64` they wrapped to -9223372036854775808, so `hard` read
        as negative, fell short of every requirement, and the render was refused with a
        patch asking for a negative quota — against quotas that cannot constrain anything.
        """
        for key, quantity in (
            ("requests.cpu", "1E"),
            ("requests.cpu", "10P"),
            ("requests.memory", "8Ei"),
            ("requests.memory", "10E"),
            ("pods", "1E"),
        ):
            with self.subTest(key=key, quantity=quantity):
                res = self._render({"probe": {"quotas": [self._quota({key: quantity})]}})
                self.assertEqual(
                    res.returncode,
                    0,
                    f"{quantity} is larger than the release needs:\n{res.stderr}",
                )

    def test_an_explicit_zero_request_is_not_charged_the_limit(self) -> None:
        """`requests.cpu: 0` is valid input — every `resources` block is an open object.

        Chained through Sprig's `default`, which calls 0 empty, it read as absent and the
        limit was charged instead: LiteLLM's two Pods were summed at their 500m limit
        rather than the nothing they ask for.
        """
        base = self._requirements()
        zeroed = self._requirements(["litellm.resources.requests.cpu=0"])
        self.assertEqual(
            base["requestsCpu"] - zeroed["requestsCpu"],
            _LITELLM_CPU_REQUEST_MILLIS,
            "an explicit zero request must drop LiteLLM's requests, not raise them",
        )
        self.assertEqual(
            zeroed["limitsCpu"], base["limitsCpu"], "limits must be untouched by it"
        )

    def test_fractional_memory_in_used_milli_bytes_parses(self) -> None:
        """A neighbour pod with `memory: 1.2Gi` makes `status.used` read `1288490188800m`."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {"requests.memory": "20Gi"},
                            used={"requests.memory": "1288490188800m"},
                        )
                    ]
                }
            }
        )
        self.assertEqual(
            res.returncode,
            0,
            f"fractional memory in used (milli-bytes) must parse:\n{res.stderr}",
        )

    def test_sub_millicore_cpu_in_used_micro_and_nanocores_parses(self) -> None:
        """A neighbour pod with `cpu: 1.5m` makes `status.used` read `1500u`, and 1m + 100u reads `1000100u`."""
        for used_qty in ("1500u", "1000100u", "1500000n", "0.5m", "1.5m"):
            with self.subTest(used=used_qty):
                res = self._render(
                    {
                        "probe": {
                            "quotas": [
                                self._quota(
                                    {"requests.cpu": "10"},
                                    used={"requests.cpu": used_qty},
                                )
                            ]
                        }
                    }
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"sub-millicore CPU {used_qty} in used must parse:\n{res.stderr}",
                )

    def test_a_decimal_si_quota_is_still_diagnosed_in_units(self) -> None:
        """A decimal-SI quota divides into no whole Mi, and used to print as raw bytes.

        Every figure in the message became an eleven-digit byte count, including the
        remediation patch -- unreadable, in the one output whose whole job is to be read.
        """
        res = self._render(
            {"probe": {"quotas": [self._quota({"requests.memory": _DECIMAL_SI_HARD})]}}
        )
        self.assertNotEqual(res.returncode, 0)
        # The quota's own spelling, so it matches `kubectl describe resourcequota`.
        self.assertIn(f"hard {_DECIMAL_SI_HARD}", res.stderr)
        self.assertNotIn(str(_DECIMAL_SI_HARD_BYTES), res.stderr)

        patch = re.search(r'"requests\.memory":"([^"]+)"', res.stderr)
        self.assertIsNotNone(patch, f"no memory patch value in:\n{res.stderr}")
        quantity = patch.group(1)
        self.assertTrue(
            quantity.endswith("Mi") or quantity.endswith("Gi"),
            f"unreadable patch quantity {quantity!r}",
        )
        mult = 1024**3 if quantity.endswith("Gi") else 1024**2
        number = int(quantity[:-2])
        # Rounding a patch down prints one that is short of what the release needs, so the
        # operator runs it and the install fails anyway. Up is the only safe direction.
        self.assertGreaterEqual(
            number * mult,
            _DEFAULT_REQUESTS_MEMORY_BYTES,
        )

    def test_hindsight_adds_its_claim_to_the_totals(self) -> None:
        """Sized from a key the chart does not have, the claim counts as zero and stalls."""
        base = self._requirements()
        with_hindsight = self._requirements(["hindsight.enabled=true"])
        self.assertEqual(
            with_hindsight["persistentVolumeClaims"],
            base["persistentVolumeClaims"] + 1,
        )
        self.assertEqual(
            with_hindsight["requestsStorage"],
            base["requestsStorage"] + _HINDSIGHT_STORAGE_BYTES,
        )

    def test_a_workload_scaled_to_zero_is_not_charged(self) -> None:
        """`replicas | default 1` read a falsy 0 as absent and charged a full replica.

        AvailabilitySpec.Replicas is Minimum=0, so 0 is a value a user can set, and an
        upgrade was refused over a pod that would not exist.
        """
        base = self._requirements()
        no_agent = self._requirements(["platformAgent.deployment.availability.replicas=0"])
        self.assertEqual(no_agent["pods"], base["pods"] - 1)
        self.assertEqual(
            no_agent["requestsCpu"], base["requestsCpu"] - _AGENT_POD_REQUEST_MILLIS
        )

        no_operator = self._requirements(["operator.replicaCount=0"])
        self.assertEqual(no_operator["pods"], base["pods"] - 1)
        self.assertEqual(
            no_operator["requestsCpu"], base["requestsCpu"] - _OPERATOR_REQUEST_MILLIS
        )

    def test_a_pruned_resources_map_does_not_break_the_render(self) -> None:
        """`--set litellm.resources.limits=null` is valid input the schema accepts.

        Reaching two levels in aborted the render with `nil pointer evaluating
        interface {}.cpu` -- and only in a namespace that has a ResourceQuota, which is
        the one population this check exists for. A dropped limit counts as zero.
        """
        base = self._requirements()
        pruned = self._requirements(["litellm.resources.limits=null"])
        self.assertEqual(pruned["limitsCpu"], base["limitsCpu"] - _LITELLM_CPU_LIMIT_MILLIS)
        self.assertEqual(pruned["pods"], base["pods"])

    def test_chart_workload_requests_default_to_limits_when_omitted(self) -> None:
        """When a workload sets limits but omits requests, requests defaults to limits."""
        base = self._requirements()
        # litellm default: requests.cpu=100m, limits.cpu=500m across 2 replicas.
        # Omitting requests defaults requests.cpu to limits.cpu (500m per replica, +800m total).
        without_requests = self._requirements(["litellm.resources.requests=null"])
        diff_millis = (500 - 100) * _LITELLM_REPLICAS
        self.assertEqual(without_requests["requestsCpu"], base["requestsCpu"] + diff_millis)

    def test_chart_workload_ephemeral_storage_is_counted(self) -> None:
        """Only the footprint contributed ephemeral storage, so a chart-set value was free."""
        base = self._requirements()
        with_ephemeral = self._requirements(
            [f"litellm.resources.requests.ephemeral-storage={_EPHEMERAL_SET_GIB}Gi"]
        )
        self.assertEqual(
            with_ephemeral["requestsEphemeral"],
            base["requestsEphemeral"]
            + _EPHEMERAL_SET_GIB * 1024**3 * _LITELLM_REPLICAS,
        )

    def test_an_empty_hindsight_storage_key_fails_loudly(self) -> None:
        """`storage: null` passes the closed schema and used to count as zero silently."""
        res = self._render(
            {"probe": {"emitRequirements": True}},
            ["hindsight.enabled=true", "hindsight.postgresql.storage=null"],
        )
        self.assertNotEqual(res.returncode, 0, "an unsized claim must not count as zero")
        self.assertIn("hindsight.postgresql.storage is empty", res.stderr)

    def test_retained_claims_do_not_refuse_a_reinstall(self) -> None:
        """The shell StatefulSet retains its claims, so they are in `used` on reinstall.

        They are reused by name rather than created again. Charging them twice refused a
        reinstall into a namespace sized exactly for the release, and the patch it printed
        asked for 50% more claims than the release will ever hold.
        """
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {
                                "persistentvolumeclaims": str(_OPERATOR_PVC_COUNT),
                                "requests.storage": f"{_OPERATOR_STORAGE_BYTES // 1024**3}Gi",
                            },
                            used={
                                "persistentvolumeclaims": str(_RETAINED_PVC_COUNT),
                                "requests.storage": f"{_RETAINED_STORAGE_GIB}Gi",
                            },
                        )
                    ]
                }
            }
        )
        self.assertEqual(
            res.returncode, 0, f"retained claims must not refuse a reinstall:\n{res.stderr}"
        )

    def test_a_quota_too_small_for_the_claims_is_still_refused(self) -> None:
        """Exempting claims from the headroom check must not disable the `hard` check."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota({"persistentvolumeclaims": str(_OPERATOR_PVC_COUNT - 1)})
                    ]
                }
            }
        )
        self.assertNotEqual(res.returncode, 0, "a quota that cannot hold the claims must fail")
        self.assertIn("persistentvolumeclaims", res.stderr)

    def test_the_upgrade_patch_does_not_double_count_used(self) -> None:
        """On upgrade `used` already holds the release's own pods.

        Adding the two asked for the release twice: a release needing 6 pods with 4
        running was told to patch to 11 where 7 does.
        """
        running = _DEFAULT_PODS - 2
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota({"pods": str(running)}, used={"pods": str(running)})
                    ]
                }
            },
            extra_args=["--is-upgrade"],
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f'"pods":"{_DEFAULT_PODS + 1}"', res.stderr)
        self.assertNotIn(f'"pods":"{running + _DEFAULT_PODS + 1}"', res.stderr)

    def test_every_deficient_quota_is_reported_at_once(self) -> None:
        """`fail` inside the loop reported one quota per render, so two meant two rounds."""
        first = self._quota({"pods": "2"})
        first["metadata"] = {"name": "first-quota"}
        second = self._quota({"requests.cpu": "100m"})
        second["metadata"] = {"name": "second-quota"}
        res = self._render({"probe": {"quotas": [first, second]}})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("first-quota", res.stderr)
        self.assertIn("second-quota", res.stderr)

    def test_the_patch_leaves_ephemeral_room_for_a_surge_pod(self) -> None:
        """A surge Pod brings its ephemeral storage with it, same as its CPU and memory.

        Asserted at two replicas because the agent pod is the only workload in the release
        that requests ephemeral storage, and it rolls with a surge Pod only there: at the
        default single replica the operator renders `strategy: Recreate`.
        """
        sets = [f"platformAgent.deployment.availability.replicas={_HA_REPLICAS}"]
        required = self._requirements(sets)
        res = self._render(
            {"probe": {"quotas": [self._quota({"requests.ephemeral-storage": "1Mi"})]}},
            sets,
        )
        self.assertNotEqual(res.returncode, 0)
        patch = re.search(r'"requests\.ephemeral-storage":"([^"]+)"', res.stderr)
        self.assertIsNotNone(patch, f"no ephemeral patch value in:\n{res.stderr}")
        self.assertEqual(
            _parse_gib_or_mib(patch.group(1)),
            required["requestsEphemeral"] + _AGENT_POD_EPHEMERAL_REQUEST_BYTES,
            "the patch must leave room for the surge Pod it promises",
        )

    def test_ephemeral_storage_remediation_notes_limitrange_requirement(self) -> None:
        """A quota deficient in ephemeral storage must note the LimitRange declaration requirement."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"requests.ephemeral-storage": "100Mi"})]}}
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("LimitRange", res.stderr)
        self.assertIn("must specify requests.ephemeral-storage", res.stderr)

    def test_ephemeral_storage_remediation_notes_limitrange_when_ephemeral_not_deficient(
        self,
    ) -> None:
        """A quota constraining ephemeral storage must note LimitRange when another resource trips a shortfall."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {
                                "requests.cpu": "100m",
                                "requests.ephemeral-storage": "100Gi",
                            }
                        )
                    ]
                }
            }
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("LimitRange", res.stderr)
        self.assertIn("must specify requests.ephemeral-storage", res.stderr)

    def test_the_default_patch_does_not_size_a_surge_the_gateway_never_creates(self) -> None:
        """At one replica the operator rolls the gateway with Recreate, which never surges.

        Sizing the patch for an agent pod there asked a default install for an agent pod's
        worth of CPU more than the release can ever consume — it is the largest workload in
        the release, so it won the surge maximum on every install that tripped a shortfall.
        """
        required = self._requirements()
        res = self._render({"probe": {"quotas": [self._quota({"requests.cpu": "100m"})]}})
        self.assertNotEqual(res.returncode, 0)
        expected_patch_cpu = _format_cpu(
            required["requestsCpu"] + _LARGEST_SURGING_REQUEST_MILLIS
        )
        self.assertIn(
            f'"requests.cpu":"{expected_patch_cpu}"',
            res.stderr,
            "the patch must be sized for the largest pod that actually surges",
        )

    def test_an_ha_patch_does_size_the_agent_pod_surge(self) -> None:
        """Above one replica the gateway rolls with RollingUpdate, so its Pod does surge."""
        sets = [f"platformAgent.deployment.availability.replicas={_HA_REPLICAS}"]
        required = self._requirements(sets)
        res = self._render(
            {"probe": {"quotas": [self._quota({"requests.cpu": "100m"})]}}, sets
        )
        self.assertNotEqual(res.returncode, 0)
        expected_patch_cpu = _format_cpu(
            required["requestsCpu"] + _AGENT_POD_REQUEST_MILLIS
        )
        self.assertIn(
            f'"requests.cpu":"{expected_patch_cpu}"',
            res.stderr,
            "an HA gateway's rollout needs room for an agent pod",
        )

    def test_count_pods_spelling_is_enforced(self) -> None:
        """`count/pods` is valid Kubernetes syntax alongside `pods` and must be checked."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"count/pods": str(_DEFAULT_PODS - 1)})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small count/pods quota must fail")
        self.assertIn("count/pods", res.stderr)
        self.assertIn(f'"count/pods":"{_DEFAULT_PODS + 1}"', res.stderr)

    def test_count_persistentvolumeclaims_spelling_is_enforced(self) -> None:
        """`count/persistentvolumeclaims` is valid Kubernetes syntax alongside `persistentvolumeclaims`."""
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {"count/persistentvolumeclaims": str(_OPERATOR_PVC_COUNT - 1)}
                        )
                    ]
                }
            }
        )
        self.assertNotEqual(
            res.returncode,
            0,
            "a too-small count/persistentvolumeclaims quota must fail",
        )
        self.assertIn("count/persistentvolumeclaims", res.stderr)
        self.assertIn(
            f'"count/persistentvolumeclaims":"{_OPERATOR_PVC_COUNT}"', res.stderr
        )

    def test_limits_cpu_shortfall_is_enforced(self) -> None:
        """A quota deficient in limits.cpu must be refused and named in the patch."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"limits.cpu": "100m"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small limits.cpu quota must fail")
        self.assertIn("  - limits.cpu:", res.stderr)
        self.assertIn('"limits.cpu":"10700m"', res.stderr)

    def test_limits_memory_shortfall_is_enforced(self) -> None:
        """A quota deficient in limits.memory must be refused and named in the patch."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"limits.memory": "100Mi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small limits.memory quota must fail")
        self.assertIn("  - limits.memory:", res.stderr)
        self.assertIn('"limits.memory":"25088Mi"', res.stderr)

    def test_limits_ephemeral_storage_shortfall_is_enforced(self) -> None:
        """A quota deficient in limits.ephemeral-storage must be refused and named in the patch."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"limits.ephemeral-storage": "100Mi"})]}}
        )
        self.assertNotEqual(
            res.returncode, 0, "a too-small limits.ephemeral-storage quota must fail"
        )
        self.assertIn("  - limits.ephemeral-storage:", res.stderr)
        self.assertIn('"limits.ephemeral-storage":"5Gi"', res.stderr)

    def test_shorthand_cpu_spelling_is_enforced(self) -> None:
        """`cpu` is the shorthand spelling for `requests.cpu` and must be checked."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"cpu": "100m"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small cpu shorthand quota must fail")
        self.assertIn("  - cpu:", res.stderr)
        self.assertIn('"cpu":"2566m"', res.stderr)

    def test_shorthand_memory_spelling_is_enforced(self) -> None:
        """`memory` is the shorthand spelling for `requests.memory` and must be checked."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"memory": "100Mi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small memory shorthand quota must fail")
        self.assertIn("  - memory:", res.stderr)
        self.assertIn('"memory":"10112Mi"', res.stderr)

    def test_shorthand_ephemeral_storage_spelling_is_enforced(self) -> None:
        """`ephemeral-storage` is the shorthand spelling for `requests.ephemeral-storage`."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"ephemeral-storage": "100Mi"})]}}
        )
        self.assertNotEqual(
            res.returncode, 0, "a too-small ephemeral-storage shorthand quota must fail"
        )
        self.assertIn("  - ephemeral-storage:", res.stderr)
        self.assertIn('"ephemeral-storage":"5Gi"', res.stderr)

    def test_shorthand_storage_spelling_is_enforced(self) -> None:
        """`storage` is the shorthand spelling for `requests.storage`."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"storage": "1Gi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small storage shorthand quota must fail")
        self.assertIn("  - storage:", res.stderr)
        self.assertIn('"storage":"22Gi"', res.stderr)

    def test_the_install_patch_for_claims_does_not_double_count_retained_used(self) -> None:
        """Retained claims sitting in `used` on install must not double the suggested patch."""
        req_gib = _OPERATOR_STORAGE_BYTES // 1024**3
        res = self._render(
            {
                "probe": {
                    "quotas": [
                        self._quota(
                            {"requests.storage": f"{req_gib - 2}Gi"},
                            used={"requests.storage": f"{req_gib}Gi"},
                        )
                    ]
                }
            }
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f'"requests.storage":"{req_gib}Gi"', res.stderr)
        self.assertNotIn(f'"requests.storage":"{req_gib * 2}Gi"', res.stderr)



class QuotaPreflightTest(unittest.TestCase):
    def test_footprint_file_structure(self) -> None:
        self.assertTrue(_FOOTPRINT.is_file(), f"missing {_FOOTPRINT}")
        data = yaml.safe_load(_FOOTPRINT.read_text())
        self.assertIn("operatorRendered", data)
        op = data["operatorRendered"]
        self.assertIn("agentPod", op)
        self.assertIn("base", op["agentPod"])
        self.assertIn("dashboard", op["agentPod"])
        self.assertIn("shellSandbox", op)
        self.assertIn("credentialProxy", op)
        self.assertIn("storage", op)

        # Verify agent-api-auth CPU limit cut to 1 core is reflected in footprint
        # Base: platform-agent (3 CPU limit) + fluent-bit (0.5 CPU limit) + agent-api-auth (1 CPU limit) = 4.5 CPU
        self.assertEqual(op["agentPod"]["base"]["cpuMillisLimit"], 4500)
        self.assertEqual(op["agentPod"]["dashboard"]["cpuMillisLimit"], 1000)
        self.assertEqual(op["shellSandbox"]["cpuMillisLimit"], 2000)
        self.assertEqual(op["credentialProxy"]["cpuMillisLimit"], 1000)

        # Total operator limits on default install: 4500 + 1000 + 2000 + 1000 = 8500m (8.5 CPU)
        total_op_cpu_limit = (
            op["agentPod"]["base"]["cpuMillisLimit"]
            + op["agentPod"]["dashboard"]["cpuMillisLimit"]
            + op["shellSandbox"]["cpuMillisLimit"]
            + op["credentialProxy"]["cpuMillisLimit"]
        )
        self.assertEqual(total_op_cpu_limit, 8500)

        # Memory, in the same shape. A generator that stopped parsing memory would emit 0
        # here; `--check` alone would not catch that if the regenerated file were committed
        # in the same change, because both sides would move together.
        self.assertEqual(op["agentPod"]["base"]["memoryBytesLimit"], 10496 * 1024**2)
        self.assertEqual(op["agentPod"]["dashboard"]["memoryBytesLimit"], 2 * 1024**3)
        self.assertEqual(op["shellSandbox"]["memoryBytesLimit"], 2 * 1024**3)
        self.assertEqual(op["credentialProxy"]["memoryBytesLimit"], 1 * 1024**3)

        # Ephemeral storage is set on two of the four and deliberately 0 on the others;
        # asserting the zeroes is the point, since an unparsed value looks identical.
        self.assertEqual(op["agentPod"]["base"]["ephemeralStorageBytesLimit"], 3 * 1024**3)
        self.assertEqual(op["agentPod"]["dashboard"]["ephemeralStorageBytesLimit"], 0)
        self.assertEqual(op["shellSandbox"]["ephemeralStorageBytesLimit"], 0)
        self.assertEqual(
            op["credentialProxy"]["ephemeralStorageBytesLimit"], 2 * 1024**3
        )
        self.assertEqual(op["agentPod"]["base"]["ephemeralStorageBytesRequest"], 3 * 1024**3)
        self.assertEqual(op["agentPod"]["dashboard"]["ephemeralStorageBytesRequest"], 0)
        self.assertEqual(op["shellSandbox"]["ephemeralStorageBytesRequest"], 0)
        self.assertEqual(
            op["credentialProxy"]["ephemeralStorageBytesRequest"], 2 * 1024**3
        )

        self.assertEqual(op["storage"]["persistentVolumeClaims"], _OPERATOR_PVC_COUNT)
        self.assertEqual(op["storage"]["storageBytesRequest"], _OPERATOR_STORAGE_BYTES)

    def test_values_yaml_quota_preflight_enabled(self) -> None:
        values = yaml.safe_load(_VALUES.read_text())
        self.assertIn("quotaPreflight", values)
        self.assertTrue(values["quotaPreflight"].get("enabled"))

    def test_quota_preflight_is_declared_in_the_values_schema(self) -> None:
        """The schema is closed, so an undeclared key fails every helm command."""
        schema = json.loads(_SCHEMA.read_text())
        self.assertIn("quotaPreflight", schema["properties"])
        self.assertIn("enabled", schema["properties"]["quotaPreflight"]["properties"])

    def test_templates_exist(self) -> None:
        self.assertTrue(_PREFLIGHT_TPL.is_file(), f"missing {_PREFLIGHT_TPL}")
        tpl_content = _PREFLIGHT_TPL.read_text()
        self.assertIn("kube-agents.quotaPreflight", tpl_content)

        helpers_content = _HELPERS.read_text()
        for name in (
            "kube-agents.quotaPreflight",
            "kube-agents.quotaRequirements",
            "kube-agents.quotaCheckItems",
            "kube-agents.parseCpuMillis",
            "kube-agents.parseBytes",
            "kube-agents.formatCpu",
            "kube-agents.formatBytes",
        ):
            self.assertIn(f'define "{name}"', helpers_content)

    @unittest.skipUnless(_HELM, "helm is not installed")
    def test_helm_template_inert_without_cluster(self) -> None:
        res = subprocess.run(
            [
                _HELM,
                "template",
                "test-release",
                str(_CHART),
                "--set",
                "quotaPreflight.enabled=true",
                *_REQUIRED_HARNESS,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"helm template failed:\n{res.stderr}")

    @unittest.skipUnless(_HELM, "helm is not installed")
    def test_missing_footprint_fails_render(self) -> None:
        """A chart missing footprint.yaml must fail preflight rather than zeroing pods."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chart = pathlib.Path(tmp.name) / "kube-agents"
        shutil.copytree(_CHART, chart)
        (chart / "values.schema.json").unlink(missing_ok=True)
        (chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)
        (chart / "files" / "footprint.yaml").unlink()

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump({"probe": {"emitRequirements": True}}, fh)
            values_path = fh.name
        self.addCleanup(lambda: pathlib.Path(values_path).unlink(missing_ok=True))
        res = subprocess.run(
            [_HELM, "template", "test-release", str(chart), "-f", values_path]
            + _REQUIRED_HARNESS,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0, "missing footprint must fail render")
        self.assertIn("footprint.yaml is missing or unreadable", res.stderr)

    def test_the_bypass_flag_gates_the_whole_check(self) -> None:
        """`quotaPreflight.enabled=false` is the only remedy offered to a deployer without
        `list resourcequotas`, and nothing exercised it.

        The refusal itself needs a cluster, so what is asserted here is that the flag gates
        the template body rather than some inner branch of it: with the flag off the render
        succeeds, and the helper reaches the lookup only inside that guard.
        """
        if _HELM:
            res = subprocess.run(
                [
                    _HELM,
                    "template",
                    "test-release",
                    str(_CHART),
                    "--set",
                    "quotaPreflight.enabled=false",
                    *_REQUIRED_HARNESS,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"the bypass must render:\n{res.stderr}")

        helpers = _HELPERS.read_text()
        marker = '{{- define "kube-agents.quotaPreflight" -}}'
        start = helpers.find(marker)
        self.assertNotEqual(start, -1, "kube-agents.quotaPreflight must be defined")
        template_text = helpers[start + len(marker) :]

        # Extract control tags (if/range/with and end) in order
        tags: list[tuple[int, str]] = []
        for m in re.finditer(
            r"\{\{-?\s*(if\b[^\}]*|range\b[^\}]*|with\b[^\}]*|end)\s*-?\}\}",
            template_text,
        ):
            tags.append((m.start(), m.group(1).strip()))

        self.assertTrue(
            len(tags) >= 2,
            "quotaPreflight must contain opening guard and closing end tags",
        )
        self.assertTrue(
            tags[0][1].startswith("if .Values.quotaPreflight.enabled"),
            "quotaPreflight must begin with the enabled guard",
        )

        # Track nesting depth to locate where the enabled guard closes.
        depth = 0
        guard_closed_at = -1
        for i, (offset, tag) in enumerate(tags):
            if tag.startswith(("if", "range", "with")):
                depth += 1
            elif tag == "end":
                depth -= 1
                if depth == 0 and guard_closed_at == -1:
                    guard_closed_at = i
                    break

        self.assertNotEqual(
            guard_closed_at,
            -1,
            "the enabled guard must have a matching closing end",
        )
        # The matching end for the enabled guard must be the penultimate tag,
        # immediately preceding the define's own closing end.
        self.assertEqual(
            guard_closed_at,
            len(tags) - 2,
            "the enabled guard must enclose the entire template body before the define closes",
        )

        lookup_offset = template_text.find("lookup")
        self.assertNotEqual(
            lookup_offset,
            -1,
            "lookup must be present in quotaPreflight",
        )
        guard_start_offset = tags[0][0]
        guard_end_offset = tags[guard_closed_at][0]
        self.assertTrue(
            guard_start_offset < lookup_offset < guard_end_offset,
            "the lookup must sit inside the enabled guard block",
        )
        first_end_offset = next(offset for offset, tag in tags if tag == "end")
        self.assertTrue(
            lookup_offset < first_end_offset,
            "the lookup must precede any inner closing end in the enabled guard",
        )


class DocumentedFootprintTest(unittest.TestCase):
    """The install prerequisites page quotes figures derived from values and footprint.

    Nothing regenerated them, and the row that preceded this one was stale within a
    release. These recompute each figure and look for it on the page, so a change that
    moves a total fails here rather than in a reader's namespace.
    """

    _PREREQUISITES = (
        _ROOT
        / "docs"
        / "site"
        / "src"
        / "content"
        / "docs"
        / "install"
        / "prerequisites.md"
    )

    @unittest.skipUnless(_HELM, "helm is not installed")
    def test_the_prerequisites_page_quotes_the_current_totals(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        chart = pathlib.Path(tmp.name) / "kube-agents"
        shutil.copytree(_CHART, chart)
        (chart / "values.schema.json").unlink(missing_ok=True)
        (chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            yaml.safe_dump({"probe": {"emitRequirements": True}}, fh)
            values_path = fh.name
        self.addCleanup(lambda: pathlib.Path(values_path).unlink(missing_ok=True))
        res = subprocess.run(
            [_HELM, "template", "test-release", str(chart), "-f", values_path]
            + _REQUIRED_HARNESS,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"render failed:\n{res.stderr}")
        required = None
        for doc in yaml.safe_load_all(res.stdout):
            if doc and doc.get("metadata", {}).get("name") == "probe-requirements":
                required = json.loads(doc["data"]["requirements"])
        self.assertIsNotNone(required, "probe ConfigMap not found")

        # Collapsed, so a figure that spans a line break still matches: the guard is
        # about the numbers, not about where the prose happens to wrap.
        page = " ".join(self._PREREQUISITES.read_text().split())
        gib = 1024**3
        # Check that the quota summary sentence quotes the exact current totals without stale figures.
        m_sentence = re.search(
            r"(\d+)\s+pods,\s+(\d+)\s+persistent volume claims totalling\s+(\d+)\s+GiB of `requests\.storage`,\s+and\s+(\d+)\s+GiB of ephemeral-storage requests against\s+(\d+)\s+GiB of limits\.",
            page,
        )
        self.assertIsNotNone(
            m_sentence, "quota summary sentence not found on prerequisites page"
        )
        self.assertEqual(int(m_sentence.group(1)), required["pods"], "stale pod count")
        self.assertEqual(
            int(m_sentence.group(2)),
            required["persistentVolumeClaims"],
            "stale claim count",
        )
        self.assertEqual(
            int(m_sentence.group(3)),
            required["requestsStorage"] // gib,
            "stale claim storage",
        )
        self.assertEqual(
            int(m_sentence.group(4)),
            required["requestsEphemeral"] // gib,
            "stale ephemeral-storage requests",
        )
        self.assertEqual(
            int(m_sentence.group(5)),
            required["limitsEphemeral"] // gib,
            "stale ephemeral-storage limits",
        )

        # Check schedulable capacity table row. CPU is quoted to one decimal, like
        # memory: the total sits between whole cores, and rounding it to one would
        # understate the request by a fifth.
        m_capacity = re.search(
            r"requests about\s+([\d\.]+)\s+vCPU and\s+([\d\.]+)\s+GiB across\s+(\d+)\s+pods",
            page,
        )
        self.assertIsNotNone(
            m_capacity, "schedulable capacity row not found on prerequisites page"
        )
        self.assertEqual(
            float(m_capacity.group(1)),
            math.floor((required["requestsCpu"] / 1000) * 10 + 0.5) / 10,
        )
        self.assertEqual(
            float(m_capacity.group(2)),
            math.floor((required["requestsMemory"] / gib) * 10 + 0.5) / 10,
        )
        self.assertEqual(int(m_capacity.group(3)), required["pods"])

        # Check limits table row.
        m_limits = re.search(
            r"plus\s+~?([\d\.]+)\s+CPU and ~([\d\.]+)\s+GiB in limits\.",
            page,
        )
        self.assertIsNotNone(
            m_limits, "limits row not found on prerequisites page"
        )
        self.assertEqual(
            float(m_limits.group(1)),
            math.floor((required["limitsCpu"] / 1000) * 10 + 0.5) / 10,
        )
        self.assertEqual(
            float(m_limits.group(2)),
            math.floor((required["limitsMemory"] / gib) * 10 + 0.5) / 10,
        )



if __name__ == "__main__":
    unittest.main()
