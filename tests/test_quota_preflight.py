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

import contextlib
import json
import math
import os
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
_FLAGGED_PODS = _CHART / "files" / "flagged-pods.yaml"
_TRANSIENT_PODS = _CHART / "files" / "transient-pods.yaml"
_SCHEMA = _CHART / "values.schema.json"
_PREFLIGHT_TPL = _CHART / "templates" / "quota-preflight.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_A2A_MANIFESTS = _ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_a2a_manifests.go"

_HELM = shutil.which("helm")
_GO = shutil.which("go")

# The release shapes the rendered-manifest oracle checks (#5002, #4876, #4883), and the
# operator's count from the chart's workloads is held to (#5006).
_ORACLE_CASES = (
    [],
    ["hindsight.enabled=true", "githubMinter.enabled=true",
     "githubMinter.org=o", "githubMinter.repo=r"],
    ["litellm.enabled=false"],
    ["operator.replicaCount=3", "litellm.replicaCount=1"],
    ["platformAgent.cleanupHook.enabled=false"],
    ["operator.enabled=false", "hindsight.enabled=true"],
    ["webConsole.enabled=true"],
    # The replay proxy, with the values its schema requires (the probe chart drops the
    # schema, but a real render needs them), at its default replicas and at 5.
    ["litellm.replayShadow.enabled=true", "litellm.replayShadow.image.repository=r",
     "litellm.replayShadow.image.tag=t", "litellm.replayShadow.job.kind=nightly"],
    ["litellm.replayShadow.enabled=true", "litellm.replayShadow.image.repository=r",
     "litellm.replayShadow.image.tag=t", "litellm.replayShadow.job.kind=nightly",
     "litellm.replayShadow.replicaCount=5"],
    ["litellm.replicaCount=0"],
    # An operator-only release (#4883): no PlatformAgent, so the ConfigMap is the count.
    ["platformAgent.enabled=false", "hindsight.enabled=true", "githubMinter.enabled=true",
     "githubMinter.org=o", "githubMinter.repo=r"],
    # maxSurge from values (#4876): an integer above 1, a percentage that rounds up past
    # 1, an integer at a workload's single replica, and 0 (LiteLLM rolls by maxUnavailable).
    ["litellm.replicaCount=4", "litellm.rollingUpdate.maxSurge=2"],
    ["operator.replicaCount=4", "operator.rollingUpdate.maxSurge=50%"],
    ["operator.replicaCount=3", "operator.rollingUpdate.maxSurge=34%"],
    ["hindsight.enabled=true", "hindsight.api.rollingUpdate.maxSurge=3"],
    # The Hindsight API runs one replica, so only maxSurge 0 tells its list entry apart
    # from one pinned to 1 or reading the wrong key.
    ["hindsight.enabled=true", "hindsight.api.rollingUpdate.maxSurge=0",
     "hindsight.api.rollingUpdate.maxUnavailable=1"],
    ["litellm.rollingUpdate.maxSurge=0"],
    # Past the replicas: a rollout still adds at most the replicas.
    ["operator.rollingUpdate.maxSurge=100"],
    # An empty maxSurge takes the template's default, and the replay proxy reads its
    # own key, not LiteLLM's (each list entry repeats its template's fencepost call).
    ["litellm.rollingUpdate.maxSurge="],
    ["litellm.replayShadow.enabled=true", "litellm.replayShadow.image.repository=r",
     "litellm.replayShadow.image.tag=t", "litellm.replayShadow.job.kind=nightly",
     "litellm.replayShadow.rollingUpdate.maxSurge=2"],
)


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
_DEFAULT_PODS = 9
_DEFAULT_REQUESTS_CPU_MILLIS = 2756
_DEFAULT_LIMITS_CPU_MILLIS = 13200
# Memory and ephemeral storage are summed by the same helper as CPU but were asserted
# nowhere, so a generator that stopped parsing them could be regenerated and committed
# together with a green `--check` (both sides move at once) and nothing would catch it.
_DEFAULT_REQUESTS_MEMORY_BYTES = 12096 * 1024**2
_DEFAULT_LIMITS_MEMORY_BYTES = 25472 * 1024**2
# Ephemeral-storage requests: the footprint's 11Gi (the agent pod's 3Gi and the credential
# proxy's 8Gi), LiteLLM's 1280Mi per replica (its tmp emptyDir's sizeLimit plus logs) on two
# replicas, and the cleanup hook's 1088Mi.
_LITELLM_EPHEMERAL_REQUEST_BYTES = 1280 * 1024**2
_DEFAULT_REQUESTS_EPHEMERAL_BYTES = (11 * 1024 + 2 * 1280 + 1088) * 1024**2
# The same three additions, because each sets its limit equal to its request.
_DEFAULT_LIMITS_EPHEMERAL_BYTES = _DEFAULT_REQUESTS_EPHEMERAL_BYTES
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
# The operator's own request, sized to be livable as the limit an Autopilot cluster without
# bursting clamps to, and inside the band GKE Autopilot admits unchanged beside 256Mi.
_OPERATOR_REQUEST_MILLIS = 100
# The web console's request (webConsole.resources), sized to be livable as the limit an
# Autopilot cluster without bursting clamps to.
_WEB_CONSOLE_REQUEST_MILLIS = 100
_WEB_CONSOLE_REQUEST_MEMORY_BYTES = 128 * 1024**2
# Two operator-rendered claims plus two from the shell StatefulSet's volumeClaimTemplates.
_OPERATOR_PVC_COUNT = 4
_OPERATOR_STORAGE_BYTES = 102 * 1024**3
# The two claims platformAgent.storage.size sizes (the agent's data claim and the shell
# sandbox's), at the operator default each.
_DATA_CLAIMS = 2
_DATA_CLAIM_DEFAULT_BYTES = 50 * 1024**3
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
# An ephemeral-storage request set on LiteLLM in place of its 1280Mi default, so the
# totals move by the difference, per replica.
_EPHEMERAL_SET_GIB = 3
# What the shell StatefulSet leaves behind: persistentVolumeClaimRetentionPolicy is
# Retain/Retain, so these two claims survive `helm uninstall` and are in `used` on the
# next install, to be reused by name rather than created again.
_RETAINED_PVC_COUNT = 2
_RETAINED_STORAGE_GIB = 51
_MILLICORES_PER_CORE = 1000
# The credential proxy's footprint.yaml entry (the operator's defaults), and an override
# of the one key an install raises (#2324). The preflight merges the override over the
# entry per key, as the operator does, so only the overridden number moves.
_PROXY_MEMORY_LIMIT_BYTES = 2 * 1024**3
_PROXY_MEMORY_REQUEST_BYTES = 1024**3
_PROXY_CPU_LIMIT_MILLIS = 2000
_PROXY_OVERRIDE_MEMORY_LIMIT_BYTES = 3 * 1024**3
_PROXY_OVERRIDE_CPU_LIMIT_MILLIS = 4000
# A request between the 1Gi default and the 2Gi limit, so the override moves the total.
_PROXY_OVERRIDE_MEMORY_REQUEST_BYTES = 1536 * 1024**2
_PROXY_FLOAT_CPU_LIMIT = 2.5
_PROXY_FLOAT_CPU_LIMIT_MILLIS = 2500
# The operator renders no ephemeral-storage request on the proxy, so the API server sets it
# to the limit: footprint.yaml's 8Gi request is that defaulting, and it follows an override
# of the limit alone.
_PROXY_EPHEMERAL_LIMIT_BYTES = 8 * 1024**3
_PROXY_OVERRIDE_EPHEMERAL_LIMIT_BYTES = 10 * 1024**3
_PROXY_VALUE = "platformAgent.deployment.credentialProxy.resources"
# The API kube-agents.gkeAutopilot reads: the resource the operator's isGKEAutopilot probes,
# at any served version.
_AUTOPILOT_API = "auto.gke.io/v1/AllowlistedWorkload"
_AUTOPILOT_API_V1BETA1 = "auto.gke.io/v1beta1/AllowlistedWorkload"
_AUTOPILOT_API_V2 = "auto.gke.io/v2/AllowlistedWorkload"

# The agent-api-auth sidecar's defaults (agentAPIAuth* in platformagent_manifests.go);
# it is one container of the agent pod, so these are already in agentPod.base and an
# override moves the pod total by the delta. Its ephemeral request is defaulted to its
# 2Gi limit, as the proxy's is.
_AA_MEMORY_REQUEST_BYTES = 384 * 1024**2
_AA_MEMORY_LIMIT_BYTES = 2 * 1024**3
_AA_CPU_LIMIT_MILLIS = 1000
_AA_CPU_REQUEST_MILLIS = 150
_AA_EPHEMERAL_LIMIT_BYTES = 2 * 1024**3
_AA_OVERRIDE_MEMORY_LIMIT_BYTES = 4 * 1024**3
_AA_OVERRIDE_CPU_LIMIT_MILLIS = 2000
_AA_OVERRIDE_CPU_REQUEST_MILLIS = 500
_AA_OVERRIDE_EPHEMERAL_LIMIT_BYTES = 10 * 1024**3
_AA_VALUE = "platformAgent.deployment.agentAPIAuth.resources"
_SIZING_VALUE = "platformAgent.deployment.sizingProfile"
# The shell container's override, and its default memory request (shellSandboxMemoryRequest in
# shell_sandbox_manifests.go, restated in footprint.yaml); its default limit is 2Gi and the
# fleet row raises the limit to 4Gi.
_SHELL_VALUE = "platformAgent.harness.experimental.shellSandbox.resources"
_SHELL_MEMORY_REQUEST_BYTES = 512 * 1024**2
# The operator's own resources block, which the preflight sums and holds to its memory floor.
_OPERATOR_VALUE = "operator.resources"
# What releases up to 0.16.0 shipped for the manager's memory limit, under the 512Mi floor.
_OPERATOR_LEGACY_MEMORY_LIMIT = "128Mi"


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

    def _requirements(self, sets: list[str] | None = None, values: dict | None = None, extra_args: list[str] | None = None) -> dict:
        res = self._render({"probe": {"emitRequirements": True}, **(values or {})}, sets, extra_args)
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

    def test_a_data_volume_size_moves_both_data_claims(self) -> None:
        """platformAgent.storage.size resizes two claims, so each GiB counts twice.

        80Gi is 30Gi over the default on each claim: 102Gi + 2 x 30Gi = 162Gi. A size
        below the default lowers the total the same way, and no claim is added.
        """
        base = self._requirements()
        self.assertEqual(base["requestsStorage"], _OPERATOR_STORAGE_BYTES)
        totals = {}
        for size, size_bytes in (("80Gi", 80 * 1024**3), ("20Gi", 20 * 1024**3),
                                 ("50Gi", _DATA_CLAIM_DEFAULT_BYTES), ("1Ti", 1024**4)):
            with self.subTest(size=size):
                req = self._requirements([f"platformAgent.storage.size={size}"])
                totals[size] = req["requestsStorage"]
                self.assertEqual(
                    req["requestsStorage"],
                    _OPERATOR_STORAGE_BYTES + _DATA_CLAIMS * (size_bytes - _DATA_CLAIM_DEFAULT_BYTES),
                )
                self.assertEqual(req["persistentVolumeClaims"], base["persistentVolumeClaims"])
        self.assertEqual(totals["80Gi"], 162 * 1024**3)

    def test_a_data_volume_size_that_does_not_fit_the_quota_fails_the_render(self) -> None:
        # The default fits a 102Gi requests.storage quota exactly; 80Gi does not.
        quota = [{
            "metadata": {"name": "storage-quota"},
            "spec": {"hard": {"requests.storage": "102Gi"}},
            "status": {"hard": {"requests.storage": "102Gi"}, "used": {"requests.storage": "0"}},
        }]
        fits = self._render({"probe": {"quotas": quota}})
        self.assertEqual(fits.returncode, 0, fits.stderr)
        refused = self._render({"probe": {"quotas": quota}}, ["platformAgent.storage.size=80Gi"])
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("requests.storage", refused.stderr)

    def test_an_oversized_data_volume_size_saturates_rather_than_wraps(self) -> None:
        """`9Ei` parses to MaxInt64, and two claims of it wrapped the total negative.

        The CR template refuses a size above 64Ti first, so this renders a copy
        without it, to reach the preflight's own arithmetic.
        """
        with self._chart_without("platform-agent-cr.yaml"):
            req = self._requirements(["platformAgent.storage.size=9Ei"])
        self.assertEqual(req["requestsStorage"], 2**63 - 1)

    def test_credential_proxy_memory_limit_override_moves_only_that_total(self) -> None:
        """The one key an install raises moves limits.memory by its difference and nothing else.

        The override is merged over the footprint entry per key, as
        resolveCredentialProxyResources does in the operator: the request, the CPU pair and
        ephemeral storage keep the footprint's numbers rather than falling back to the
        override's limit.
        """
        base = self._requirements()
        raised = self._requirements([f"{_PROXY_VALUE}.limits.memory={_PROXY_OVERRIDE_MEMORY_LIMIT_BYTES}"])
        self.assertEqual(
            raised["limitsMemory"] - base["limitsMemory"],
            _PROXY_OVERRIDE_MEMORY_LIMIT_BYTES - _PROXY_MEMORY_LIMIT_BYTES,
        )
        for key in ("pods", "requestsCpu", "limitsCpu", "requestsMemory",
                    "requestsEphemeral", "limitsEphemeral", "persistentVolumeClaims"):
            self.assertEqual(raised[key], base[key], key)

    def test_credential_proxy_request_and_cpu_overrides_are_each_counted(self) -> None:
        base = self._requirements()
        raised = self._requirements([
            f"{_PROXY_VALUE}.requests.memory={_PROXY_OVERRIDE_MEMORY_REQUEST_BYTES}",
            f"{_PROXY_VALUE}.limits.cpu={_PROXY_OVERRIDE_CPU_LIMIT_MILLIS}m",
        ])
        self.assertEqual(
            raised["requestsMemory"] - base["requestsMemory"],
            _PROXY_OVERRIDE_MEMORY_REQUEST_BYTES - _PROXY_MEMORY_REQUEST_BYTES,
        )
        self.assertEqual(
            raised["limitsCpu"] - base["limitsCpu"],
            _PROXY_OVERRIDE_CPU_LIMIT_MILLIS - _PROXY_CPU_LIMIT_MILLIS,
        )
        # A request the override does not name keeps the footprint's, not the new limit.
        self.assertEqual(raised["requestsCpu"], base["requestsCpu"])
        self.assertEqual(raised["limitsMemory"], base["limitsMemory"])

    def test_a_credential_proxy_limit_alone_below_the_default_request_lowers_it(self) -> None:
        # The operator lowers a cpu or memory request the override does not set to a
        # limit set below it (resolveCredentialProxyResources); the preflight counts that.
        base = self._requirements()
        for sets, key, delta in (
            ([f"{_PROXY_VALUE}.limits.cpu=200m"], "requestsCpu", 200 - 500),
            ([f"{_PROXY_VALUE}.limits.memory=768Mi"], "requestsMemory", (768 - 1024) * 1024**2),
            ([f"{_PROXY_VALUE}.limits.cpu=4"], "requestsCpu", 0),
        ):
            with self.subTest(sets=sets):
                self.assertEqual(self._requirements(sets)[key] - base[key], delta)

    def test_credential_proxy_on_autopilot_counts_the_autopilot_requests(self) -> None:
        # On Autopilot the operator requests 2 CPU and 2Gi (credentialProxyAutopilot*), and a
        # CPU request the override does not set stays at one CPU per GiB of memory or under.
        autopilot = ["--api-versions", _AUTOPILOT_API]
        base = self._requirements()
        on = self._requirements(extra_args=autopilot)
        self.assertEqual(on["requestsCpu"] - base["requestsCpu"], 2000 - 500)
        self.assertEqual(on["requestsMemory"] - base["requestsMemory"], (2048 - 1024) * 1024**2)
        self.assertEqual(on["limitsCpu"], base["limitsCpu"])
        lowered = self._requirements([f"{_PROXY_VALUE}.limits.memory=1536Mi"], extra_args=autopilot)
        self.assertEqual(lowered["requestsMemory"] - base["requestsMemory"], (1536 - 1024) * 1024**2)
        self.assertEqual(lowered["requestsCpu"] - base["requestsCpu"], 1500 - 500)
        # Standard does not hold the CPU request to the band.
        small = self._requirements([f"{_PROXY_VALUE}.requests.memory=256Mi"])
        self.assertEqual(small["requestsCpu"], base["requestsCpu"])
        small_on = self._requirements([f"{_PROXY_VALUE}.requests.memory=256Mi"], extra_args=autopilot)
        self.assertEqual(small_on["requestsCpu"] - base["requestsCpu"], 250 - 500)
        # A cluster serving the resource at v1beta1 only is Autopilot to the operator too.
        self.assertEqual(self._requirements(extra_args=["--api-versions", _AUTOPILOT_API_V1BETA1]), on)
        self.assertEqual(self._requirements(extra_args=["--api-versions", _AUTOPILOT_API_V2]), on)
        # Another resource in the group is not Autopilot: Standard serves auto.gke.io too.
        self.assertEqual(self._requirements(extra_args=["--api-versions", "auto.gke.io/v1/WorkloadAllowlist"]), base)
        # Just under 2Gi rounds the CPU request down to the 250m step, as the operator does.
        under = self._requirements([f"{_PROXY_VALUE}.limits.memory=2147483647"], extra_args=autopilot)
        self.assertEqual(under["requestsCpu"] - base["requestsCpu"], 1750 - 500)
        at_floor = self._requirements([f"{_PROXY_VALUE}.limits.memory=544Mi"], extra_args=autopilot)
        self.assertEqual(at_floor["requestsCpu"] - base["requestsCpu"], 500 - 500)

    def test_credential_proxy_cpu_written_as_a_float_is_counted(self) -> None:
        # A values file's `cpu: 1.5` arrives as a YAML float, not the string `--set` gives.
        base = self._requirements()
        raised = self._requirements(values={"platformAgent": {"deployment": {"credentialProxy": {
            "resources": {"limits": {"cpu": _PROXY_FLOAT_CPU_LIMIT, "memory": str(_PROXY_OVERRIDE_MEMORY_LIMIT_BYTES)}}}}}})
        self.assertEqual(
            raised["limitsCpu"] - base["limitsCpu"],
            _PROXY_FLOAT_CPU_LIMIT_MILLIS - _PROXY_CPU_LIMIT_MILLIS,
        )
        self.assertEqual(
            raised["limitsMemory"] - base["limitsMemory"],
            _PROXY_OVERRIDE_MEMORY_LIMIT_BYTES - _PROXY_MEMORY_LIMIT_BYTES,
        )

    def test_credential_proxy_quantities_in_the_crds_dotted_forms_are_counted(self) -> None:
        # `3.Gi` and `.5Ki` are in the CRD's grammar and resource.ParseQuantity reads
        # them; the preflight parses them as 3Gi and 512 cores rather than refusing.
        base = self._requirements()
        raised = self._requirements(values={"platformAgent": {"deployment": {"credentialProxy": {
            "resources": {"limits": {"memory": "3.Gi", "cpu": ".5Ki"}}}}}})
        self.assertEqual(
            raised["limitsMemory"] - base["limitsMemory"],
            _PROXY_OVERRIDE_MEMORY_LIMIT_BYTES - _PROXY_MEMORY_LIMIT_BYTES,
        )
        self.assertEqual(raised["limitsCpu"] - base["limitsCpu"], 512 * 1000 - _PROXY_CPU_LIMIT_MILLIS)

    def test_credential_proxy_ephemeral_limit_alone_moves_the_request_with_it(self) -> None:
        """An override of limits.ephemeral-storage alone is counted as a request of the same size.

        The operator renders no ephemeral-storage request, so the API server defaults it to
        the limit; counting the footprint's 8Gi against a 10Gi limit passes a quota with 8
        to 10Gi of request headroom that then refuses the pod.
        """
        base = self._requirements()
        raised = self._requirements([f"{_PROXY_VALUE}.limits.ephemeral-storage={_PROXY_OVERRIDE_EPHEMERAL_LIMIT_BYTES}"])
        moved = _PROXY_OVERRIDE_EPHEMERAL_LIMIT_BYTES - _PROXY_EPHEMERAL_LIMIT_BYTES
        self.assertEqual(raised["limitsEphemeral"] - base["limitsEphemeral"], moved)
        self.assertEqual(raised["requestsEphemeral"] - base["requestsEphemeral"], moved)
        # An explicit request still wins over the defaulting.
        explicit = self._requirements([
            f"{_PROXY_VALUE}.limits.ephemeral-storage={_PROXY_OVERRIDE_EPHEMERAL_LIMIT_BYTES}",
            f"{_PROXY_VALUE}.requests.ephemeral-storage=4Gi",
        ])
        self.assertEqual(explicit["requestsEphemeral"] - base["requestsEphemeral"],
                         4 * 1024**3 - _PROXY_EPHEMERAL_LIMIT_BYTES)

    def test_credential_proxy_null_override_key_keeps_the_default(self) -> None:
        # `memory: null` is how a values file drops a key; it means "not set", as on the CR.
        base = self._requirements()
        nulled = self._requirements([f"{_PROXY_VALUE}.limits.memory=null"])
        self.assertEqual(nulled, base)

    def test_sizing_profile_rows_are_counted(self) -> None:
        """platformAgent.deployment.sizingProfile medium and fleet are counted at their
        rows, the difference from the small footprint across the three sized containers:
        fleet raises the proxy to 4 CPU / 8Gi requests and 8 CPU / 8Gi limits, the
        sidecar's memory limit to 8Gi and the shell's to 4Gi; medium raises the proxy to
        1 CPU / 4Gi requests and a 4Gi memory limit and the sidecar's limit to 4Gi.
        Ephemeral-storage and the pod count do not move. auto is the small row the
        operator starts it at."""
        base = self._requirements()
        fleet = self._requirements([f"{_SIZING_VALUE}=fleet"])
        self.assertEqual(fleet["requestsCpu"] - base["requestsCpu"], 4000 - 500)
        self.assertEqual(fleet["requestsMemory"] - base["requestsMemory"], 7 * 1024**3)
        self.assertEqual(fleet["limitsCpu"] - base["limitsCpu"], 8000 - 2000)
        self.assertEqual(fleet["limitsMemory"] - base["limitsMemory"], (6 + 6 + 2) * 1024**3)
        medium = self._requirements([f"{_SIZING_VALUE}=medium"])
        self.assertEqual(medium["requestsCpu"] - base["requestsCpu"], 1000 - 500)
        self.assertEqual(medium["requestsMemory"] - base["requestsMemory"], 3 * 1024**3)
        self.assertEqual(medium["limitsCpu"], base["limitsCpu"])
        self.assertEqual(medium["limitsMemory"] - base["limitsMemory"], (2 + 2) * 1024**3)
        for got in (fleet, medium):
            for key in ("pods", "requestsEphemeral", "limitsEphemeral", "persistentVolumeClaims", "requestsStorage"):
                self.assertEqual(got[key], base[key], key)
        self.assertEqual(self._requirements([f"{_SIZING_VALUE}=auto"]), base)
        self.assertEqual(self._requirements([f"{_SIZING_VALUE}=small"]), base)

    def test_sizing_profile_override_merges_over_the_row(self) -> None:
        """An override key replaces the row's figure and the rest of the row stands: under
        fleet, a 3 CPU proxy request (valid on the fleet row, refused on small) counts 3
        CPU, and a sidecar memory request alone above the small limit but under the
        fleet one is counted rather than treated as refused."""
        base = self._requirements()
        proxy = self._requirements([f"{_SIZING_VALUE}=fleet", f"{_PROXY_VALUE}.requests.cpu=3"])
        self.assertEqual(proxy["requestsCpu"] - base["requestsCpu"], 3000 - 500)
        self.assertEqual(proxy["requestsMemory"] - base["requestsMemory"], 7 * 1024**3)
        sidecar = self._requirements([f"{_SIZING_VALUE}=fleet", f"{_AA_VALUE}.requests.memory=3Gi"])
        self.assertEqual(sidecar["requestsMemory"] - base["requestsMemory"], 7 * 1024**3 + 3 * 1024**3 - _AA_MEMORY_REQUEST_BYTES)
        # A sidecar override the fleet row still refuses (a request above its 8Gi limit)
        # is counted at the row, not at the small defaults.
        refused = self._requirements([f"{_SIZING_VALUE}=fleet", f"{_AA_VALUE}.requests.memory=9Gi"])
        fleet = self._requirements([f"{_SIZING_VALUE}=fleet"])
        self.assertEqual(refused, fleet)

    def test_sizing_profile_shell_override_is_refused_against_the_row(self) -> None:
        """The shell's refusal rule reads the row the profile picks, not the small
        defaults: a 3Gi memory request is refused on small (its 2Gi limit) and counted at
        the defaults, admitted on fleet (its 4Gi limit) and counted as written, and a 5Gi
        request on fleet is refused and counted at the row. Pins the footprint loop's order,
        the row first and the refusal rule after it; reversed, the 3Gi case under fleet
        would count the row alone and pass a quota the pod then cannot fit."""
        base = self._requirements()
        fleet = self._requirements([f"{_SIZING_VALUE}=fleet"])
        self.assertEqual(self._requirements([f"{_SHELL_VALUE}.requests.memory=3Gi"]), base)
        admitted = self._requirements([f"{_SIZING_VALUE}=fleet", f"{_SHELL_VALUE}.requests.memory=3Gi"])
        self.assertEqual(admitted["requestsMemory"] - base["requestsMemory"],
                         7 * 1024**3 + 3 * 1024**3 - _SHELL_MEMORY_REQUEST_BYTES)
        self.assertEqual(admitted["limitsMemory"], fleet["limitsMemory"])
        self.assertEqual(self._requirements([f"{_SIZING_VALUE}=fleet", f"{_SHELL_VALUE}.requests.memory=5Gi"]), fleet)

    def test_sizing_profile_auto_counts_a_proxy_request_alone_above_the_small_limit_at_the_row(self) -> None:
        """Under auto the CR template admits a proxy request set alone up to the fleet
        row's limit, while the preflight counts auto at the small row, where the operator
        refuses that request and renders the row: a 6Gi memory request alone (above
        small's 2Gi limit, under fleet's 8Gi) is counted at the small row rather than as a
        6Gi request against a 2Gi limit, a pod the operator never writes. The same request
        under fleet is admitted and counted as written, as is one set together with a
        limit that admits it under auto, and a limit set alone below the row's request
        lowers the request rather than reading as crossed."""
        base = self._requirements()
        self.assertEqual(self._requirements([f"{_SIZING_VALUE}=auto", f"{_PROXY_VALUE}.requests.memory=6Gi"]), base)
        self.assertEqual(self._requirements([f"{_SIZING_VALUE}=auto", f"{_PROXY_VALUE}.requests.cpu=3"]), base)
        fleet = self._requirements([f"{_SIZING_VALUE}=fleet"])
        admitted = self._requirements([f"{_SIZING_VALUE}=fleet", f"{_PROXY_VALUE}.requests.memory=6Gi"])
        self.assertEqual(admitted["requestsMemory"] - fleet["requestsMemory"], (6 - 8) * 1024**3)
        self.assertEqual(admitted["limitsMemory"], fleet["limitsMemory"])
        paired = self._requirements([f"{_SIZING_VALUE}=auto", f"{_PROXY_VALUE}.requests.memory=6Gi", f"{_PROXY_VALUE}.limits.memory=8Gi"])
        self.assertEqual(paired["requestsMemory"] - base["requestsMemory"], 5 * 1024**3)
        self.assertEqual(paired["limitsMemory"] - base["limitsMemory"], 6 * 1024**3)
        lowered = self._requirements([f"{_SIZING_VALUE}=auto", f"{_PROXY_VALUE}.limits.memory=768Mi"])
        self.assertEqual(lowered["requestsMemory"] - base["requestsMemory"], 768 * 1024**2 - 1024**3)
        self.assertEqual(lowered["limitsMemory"] - base["limitsMemory"], 768 * 1024**2 - 2 * 1024**3)

    def test_sizing_profile_on_autopilot_counts_the_rows_autopilot_requests(self) -> None:
        """On GKE Autopilot the proxy's requests are the row's Autopilot pair, not the
        small one: medium requests 2 CPU and 4Gi (the Standard medium row requests 1 CPU),
        fleet 4 CPU and 8Gi. The limits are the row's, as on Standard."""
        autopilot = ["--api-versions", _AUTOPILOT_API]
        base = self._requirements()
        small_on = self._requirements(extra_args=autopilot)
        medium = self._requirements([f"{_SIZING_VALUE}=medium"])
        medium_on = self._requirements([f"{_SIZING_VALUE}=medium"], extra_args=autopilot)
        self.assertEqual(medium_on["requestsCpu"] - base["requestsCpu"], 2000 - 500)
        self.assertEqual(medium_on["requestsCpu"] - medium["requestsCpu"], 2000 - 1000)
        self.assertEqual(medium_on["requestsMemory"] - base["requestsMemory"], 3 * 1024**3)
        self.assertEqual(medium_on["requestsMemory"] - small_on["requestsMemory"], 2 * 1024**3)
        self.assertEqual(medium_on["limitsCpu"], medium["limitsCpu"])
        self.assertEqual(medium_on["limitsMemory"], medium["limitsMemory"])
        fleet = self._requirements([f"{_SIZING_VALUE}=fleet"])
        fleet_on = self._requirements([f"{_SIZING_VALUE}=fleet"], extra_args=autopilot)
        self.assertEqual(fleet_on["requestsCpu"] - base["requestsCpu"], 4000 - 500)
        self.assertEqual(fleet_on["requestsMemory"] - base["requestsMemory"], 7 * 1024**3)
        self.assertEqual(fleet_on["limitsCpu"], fleet["limitsCpu"])
        self.assertEqual(fleet_on["limitsMemory"], fleet["limitsMemory"])
        for got in (medium_on, fleet_on):
            for key in ("pods", "requestsEphemeral", "limitsEphemeral"):
                self.assertEqual(got[key], base[key], key)

    def test_agent_api_auth_memory_limit_override_moves_only_that_total(self) -> None:
        """The field's use case (#2648): raising the sidecar's memory limit is counted,
        so a namespace ResourceQuota sized to the preflight is not passed and then
        overrun by the Recreate rollout."""
        base = self._requirements()
        raised = self._requirements([f"{_AA_VALUE}.limits.memory={_AA_OVERRIDE_MEMORY_LIMIT_BYTES}"])
        self.assertEqual(
            raised["limitsMemory"] - base["limitsMemory"],
            _AA_OVERRIDE_MEMORY_LIMIT_BYTES - _AA_MEMORY_LIMIT_BYTES,
        )
        for key in ("pods", "requestsCpu", "limitsCpu", "requestsMemory",
                    "requestsEphemeral", "limitsEphemeral", "persistentVolumeClaims"):
            self.assertEqual(raised[key], base[key], key)

    def test_agent_api_auth_request_and_cpu_overrides_are_each_counted(self) -> None:
        base = self._requirements()
        raised = self._requirements([
            f"{_AA_VALUE}.requests.memory={_AA_MEMORY_LIMIT_BYTES}",
            f"{_AA_VALUE}.limits.cpu={_AA_OVERRIDE_CPU_LIMIT_MILLIS}m",
            f"{_AA_VALUE}.requests.cpu={_AA_OVERRIDE_CPU_REQUEST_MILLIS}m",
        ])
        self.assertEqual(
            raised["requestsMemory"] - base["requestsMemory"],
            _AA_MEMORY_LIMIT_BYTES - _AA_MEMORY_REQUEST_BYTES,
        )
        self.assertEqual(
            raised["limitsCpu"] - base["limitsCpu"],
            _AA_OVERRIDE_CPU_LIMIT_MILLIS - _AA_CPU_LIMIT_MILLIS,
        )
        self.assertEqual(
            raised["requestsCpu"] - base["requestsCpu"],
            _AA_OVERRIDE_CPU_REQUEST_MILLIS - _AA_CPU_REQUEST_MILLIS,
        )
        # A requests.memory override must not leak into the limits total.
        self.assertEqual(raised["limitsMemory"], base["limitsMemory"])

    def test_agent_api_auth_ephemeral_override_moves_both_sides_with_defaulting(self) -> None:
        base = self._requirements()
        raised = self._requirements([f"{_AA_VALUE}.limits.ephemeral-storage={_AA_OVERRIDE_EPHEMERAL_LIMIT_BYTES}"])
        moved = _AA_OVERRIDE_EPHEMERAL_LIMIT_BYTES - _AA_EPHEMERAL_LIMIT_BYTES
        self.assertEqual(raised["limitsEphemeral"] - base["limitsEphemeral"], moved)
        self.assertEqual(raised["requestsEphemeral"] - base["requestsEphemeral"], moved)
        explicit = self._requirements([
            f"{_AA_VALUE}.limits.ephemeral-storage={_AA_OVERRIDE_EPHEMERAL_LIMIT_BYTES}",
            f"{_AA_VALUE}.requests.ephemeral-storage=4Gi",
        ])
        self.assertEqual(explicit["requestsEphemeral"] - base["requestsEphemeral"],
                         4 * 1024**3 - _AA_EPHEMERAL_LIMIT_BYTES)

    def test_agent_api_auth_null_override_key_keeps_the_default(self) -> None:
        base = self._requirements()
        nulled = self._requirements([f"{_AA_VALUE}.limits.memory=null"])
        self.assertEqual(nulled, base)

    def test_credential_proxy_override_the_preflight_cannot_parse_fails_naming_the_key(self) -> None:
        # Helm renders quota-preflight.yaml before the CR template, so the preflight's own
        # read of the override is the first to see it. The CR template is removed from this
        # copy, so the message can only come from kube-agents.credentialProxyFootprint.
        with self._chart_without("platform-agent-cr.yaml"):
            for value in ("-1Gi", "abc", "2GB"):
                res = self._render({"probe": {"emitRequirements": True}},
                                   [f"{_PROXY_VALUE}.limits.memory={value}"])
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(f"{_PROXY_VALUE}.limits.memory is", res.stderr)
                self.assertNotIn("cannot parse quantity", res.stderr)
            res = self._render({"probe": {"emitRequirements": True}}, [f"{_PROXY_VALUE}.limits=2Gi"])
            self.assertIn(f"{_PROXY_VALUE}.limits is 2Gi, which is not a map", res.stderr)

    @contextlib.contextmanager
    def _chart_without(self, template: str):
        """Point self.chart at a copy of the chart with one template removed, so a message
        about a value that template also reads can only come from the preflight's own read."""
        with tempfile.TemporaryDirectory() as tmp:
            chart = pathlib.Path(tmp) / "kube-agents"
            shutil.copytree(self.chart, chart)
            (chart / "templates" / template).unlink()
            original, self.chart = self.chart, chart
            try:
                yield
            finally:
                self.chart = original

    def _render_without_operator_template(self, sets: list[str]):
        """Render the probe with templates/operator-deployment.yaml removed, so a message
        about operator.resources can only come from the preflight's own read of it."""
        with self._chart_without("operator-deployment.yaml"):
            return self._render({"probe": {"emitRequirements": True}}, sets)

    def test_operator_memory_limit_the_preflight_cannot_parse_fails_naming_the_key(self) -> None:
        # Helm renders quota-preflight.yaml before operator-deployment.yaml, so the
        # preflight's read of operator.resources is the first to see it; without its own
        # call to kube-agents.operatorResourcesCheck, parseBytes refuses a bad value with
        # "please report it" and never names the key.
        res = self._render_without_operator_template([f"{_OPERATOR_VALUE}.limits.memory=lots"])
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(f'{_OPERATOR_VALUE}.limits.memory is "lots", which is not a Kubernetes quantity', res.stderr)
        self.assertNotIn("cannot parse quantity", res.stderr)

    def test_operator_memory_limit_under_the_floor_fails_in_the_preflight(self) -> None:
        # The legacy limit parses, so parseBytes would have summed it quietly; the floor
        # check in the preflight is what refuses it before the operator template could.
        res = self._render_without_operator_template(
            [f"{_OPERATOR_VALUE}.limits.memory={_OPERATOR_LEGACY_MEMORY_LIMIT}"]
        )
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn(
            f"{_OPERATOR_VALUE}.limits.memory is {_OPERATOR_LEGACY_MEMORY_LIMIT}, under the 512Mi floor",
            res.stderr,
        )

    def test_operator_disabled_leaves_a_bad_operator_memory_limit_alone(self) -> None:
        # No manager is rendered, so there is no limit to hold to the floor: a release that
        # brings its own operator (operator.enabled=false) with a legacy block in its reused
        # values renders, as the workload loop and the operator template skip it too.
        req = self._requirements(
            [
                "operator.enabled=false",
                f"{_OPERATOR_VALUE}.limits.memory={_OPERATOR_LEGACY_MEMORY_LIMIT}",
            ]
        )
        self.assertEqual(req["pods"], _DEFAULT_PODS - 1)

    def test_agent_api_auth_refused_override_counts_defaults(self) -> None:
        """An override the operator refuses is rendered at defaults, Degraded, so the
        preflight must count the defaults, not the override's delta. A zero limit, a
        limit below the default request, and a request above the default limit are the
        refusals agentAPIAuthResourcesCheck does not catch (they stay the operator's)."""
        base = self._requirements()
        for override in (
            [f"{_AA_VALUE}.limits.memory=0"],
            [f"{_AA_VALUE}.limits.memory=256Mi"],
            [f"{_AA_VALUE}.limits.cpu=0"],
            [f"{_AA_VALUE}.requests.memory=5Gi"],
            [f"{_AA_VALUE}.requests.cpu=2"],
        ):
            got = self._requirements(override)
            self.assertEqual(got, base, f"a refused override {override} moved the footprint; it must count defaults")

    def test_agent_api_auth_refused_override_at_rounding_boundary_counts_defaults(self) -> None:
        """The operator compares the exact resource.Quantity (request.Cmp(limit)); the
        preflight must decide "would the operator render this?" on the same exact figures,
        not on parseBytes/parseCpuMillis which ceil an m-suffixed quantity. An override a
        rounding step under a default crosses the pair for the operator while the rounded
        value lands back on the default, so a gate on the rounded integers counts it as
        rendered and moves the footprint under the quota the operator never asks for."""
        base = self._requirements()
        # 402653183500m = 402653183.5 bytes, half a byte under the 384Mi default request;
        # the operator refuses (limit < default request) and renders at 2Gi, Degraded.
        # 149.5m = 0.1495 cores, under the 150m default request; same refusal.
        for override in (
            [f"{_AA_VALUE}.limits.memory=402653183500m"],
            [f"{_AA_VALUE}.limits.cpu=149.5m"],
        ):
            got = self._requirements(override)
            self.assertEqual(got, base, f"a boundary refusal {override} moved the footprint; it must count defaults")

    def test_agent_api_auth_oversized_limit_does_not_wrap_the_requirement_negative(self) -> None:
        """A sidecar limit the validator admits (any byte count under 2^63) must not carry
        the replica-multiplied pod sum or the base add past int64 and wrap the requirement
        negative, which would pass every quota. The preflight saturates to the int64
        ceiling instead, which no real quota satisfies."""
        # Two replicas, 5 exabytes: 5E is under 2^63 but doubles over it at the multiply.
        two_rep = {"platformAgent": {"deployment": {
            "availability": {"replicas": 2},
            "agentAPIAuth": {"resources": {"limits": {"memory": "5E"}}},
        }}}
        got = self._requirements(values=two_rep)
        self.assertGreater(got["limitsMemory"], 0,
                           "limitsMemory wrapped negative; the replica multiply overflowed int64")
        # One replica, ~8.9GB below 2^63: the base add alone carries it past int64.
        one_rep = {"platformAgent": {"deployment": {
            "agentAPIAuth": {"resources": {"limits": {"memory": "9.22337203e18"}}},
        }}}
        got1 = self._requirements(values=one_rep)
        self.assertGreater(got1["limitsMemory"], 0,
                           "limitsMemory wrapped negative at one replica; the base add overflowed int64")

        # The proxy override flows through the same workload-loop add (one replica, no
        # multiply -- the narrower window the reviewer tied to the same clamp).
        proxy = {"platformAgent": {"deployment": {
            "credentialProxy": {"resources": {"limits": {"memory": "9.22337203e18"}}},
        }}}
        gotp = self._requirements(values=proxy)
        self.assertGreater(gotp["limitsMemory"], 0,
                           "limitsMemory wrapped negative for a proxy override; the workload-loop add overflowed int64")

    def test_agent_api_auth_override_the_preflight_cannot_parse_fails_naming_the_key(self) -> None:
        # The footprint path reads the override too; with the CR template removed, a bad
        # quantity or a non-map side can only be caught by kube-agents.agentAPIAuthResourcesCheck
        # in the preflight, naming the key rather than reaching parseBytes as garbage.
        with self._chart_without("platform-agent-cr.yaml"):
            for value in ("-1Gi", "abc", "2GB"):
                res = self._render({"probe": {"emitRequirements": True}},
                                   [f"{_AA_VALUE}.limits.memory={value}"])
                self.assertNotEqual(res.returncode, 0, res.stdout)
                self.assertIn(f"{_AA_VALUE}.limits.memory is", res.stderr)
            res = self._render({"probe": {"emitRequirements": True}}, [f"{_AA_VALUE}.limits=4Gi"])
            self.assertIn(f"{_AA_VALUE}.limits is 4Gi, which is not a map", res.stderr)

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

    def _next_mode_delta(self, agent_replicas: int = 1, gateway_standby: bool = True) -> dict:
        """What footprint.yaml's nextMode adds, summed independently of the template.

        `gateway_standby` adds the A2A gateway's second pod, which the operator runs once a
        gateway holds the election's Lease and no golden renders (the election on, its
        default), at the footprint's per-pod figures.
        """
        following = yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]
        bridge = following["agentPod"]["hermesBridge"]
        workloads = [v for k, v in following.items() if k not in ("agentPod", "storage")]
        self.assertTrue(workloads, "footprint.yaml has no nextMode workloads")
        if gateway_standby:
            gateway = following["a2aGateway"]
            workloads.append({k: (v // gateway["pods"] if isinstance(v, int) and k != "pods" else v)
                              for k, v in gateway.items() if k not in ("requests", "limits", "surges")} | {"pods": 1})
        delta = {
            "pods": sum(w["pods"] for w in workloads),
            "persistentVolumeClaims": following["storage"]["persistentVolumeClaims"],
            "requestsStorage": following["storage"]["storageBytesRequest"],
        }
        for out, key in (
            ("requestsCpu", "cpuMillisRequest"), ("limitsCpu", "cpuMillisLimit"),
            ("requestsMemory", "memoryBytesRequest"), ("limitsMemory", "memoryBytesLimit"),
            ("requestsEphemeral", "ephemeralStorageBytesRequest"),
            ("limitsEphemeral", "ephemeralStorageBytesLimit"),
        ):
            delta[out] = sum(w[key] for w in workloads) + bridge[key] * agent_replicas
        return delta

    def test_mode_next_adds_the_next_mode_footprint(self) -> None:
        """A next-mode install runs the A2A stack beside the today-mode one (#3753)."""
        base = self._requirements()
        following = self._requirements(["platformAgent.mode=next"])
        for key, value in self._next_mode_delta().items():
            with self.subTest(key=key):
                self.assertEqual(following[key] - base[key], value)
        # Not a vacuous zero: the stack is several pods and a JetStream claim.
        self.assertGreaterEqual(following["pods"] - base["pods"], 7)
        self.assertEqual(following["persistentVolumeClaims"] - base["persistentVolumeClaims"], 1)

    def test_mode_next_counts_no_gateway_standby_with_the_election_off(self) -> None:
        """A2A_GATEWAY_ELECTION=false on the operator runs one unelected gateway, so the
        preflight counts the footprint's one gateway pod and nothing more."""
        base = self._requirements()
        following = self._requirements(
            ["platformAgent.mode=next"],
            values={"operator": {"extraEnv": [{"name": "A2A_GATEWAY_ELECTION", "value": "false"}]}},
        )
        for key, value in self._next_mode_delta(gateway_standby=False).items():
            with self.subTest(key=key):
                self.assertEqual(following[key] - base[key], value)

    def test_mode_today_counts_what_an_unset_mode_does(self) -> None:
        self.assertEqual(self._requirements(["platformAgent.mode=today"]), self._requirements())

    def test_mode_next_bridge_scales_with_the_agent_replicas(self) -> None:
        """hermes-bridge is a container of the agent pod, so each agent replica carries one."""
        replicas = 2
        base = self._requirements([f"platformAgent.deployment.availability.replicas={replicas}"])
        following = self._requirements(
            ["platformAgent.mode=next", f"platformAgent.deployment.availability.replicas={replicas}"]
        )
        self.assertEqual(
            following["requestsCpu"] - base["requestsCpu"],
            self._next_mode_delta(agent_replicas=replicas)["requestsCpu"],
        )

    def test_a_quota_that_fits_today_refuses_a_next_mode_install(self) -> None:
        pods = self._requirements()["pods"]
        values = {"probe": {"quotas": [self._quota({"pods": str(pods)})]}}
        self.assertEqual(self._render(values).returncode, 0)
        res = self._render(values, ["platformAgent.mode=next"])
        self.assertNotEqual(res.returncode, 0, "a next-mode render should have failed")
        self.assertIn("pods", res.stderr)

    def test_mode_next_surge_room_fits_a_callout_with_litellm_off(self) -> None:
        """The callouts and verifiers roll surge-first, so the patch leaves room for one."""
        following = yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]
        surging = [w for w in following.values() if isinstance(w, dict) and w.get("surges")]
        self.assertGreaterEqual(len(surging), 2, "the callouts and verifiers should surge")
        widest = max(w["cpuMillisRequest"] // w["pods"] for w in surging)
        # LiteLLM and the operator also roll surge-first; shrink both below the widest.
        quiet = ["litellm.enabled=false", "operator.resources.requests.cpu=10m"]
        today = self._requirements(quiet)
        self.assertLess(today["surgeRequestsCpu"], widest, "the test needs the chart's own surges out of the way")
        req = self._requirements([*quiet, "platformAgent.mode=next"])
        self.assertGreaterEqual(req["surgeRequestsCpu"], widest)

    def _bridge_delta(self, extra_env: list[dict]) -> int:
        """The next-mode CPU request with operator.extraEnv set, less the api-bridge one."""
        base = self._requirements(["platformAgent.mode=next"])
        req = self._requirements(
            ["platformAgent.mode=next"], values={"operator": {"extraEnv": extra_env}}
        )
        return req["requestsCpu"] - base["requestsCpu"]

    def test_mode_next_counts_an_overridden_bridge_at_least_at_the_agent_containers_size(self) -> None:
        """The chart does not re-implement the operator's reader of A2A_BRIDGE_EXECUTOR and
        A2A_BRIDGE_RESOURCES; with either set, in any form, the bridge is counted at no less
        than the agent container's size, the operator's fallback under cli."""
        bridge = yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]["agentPod"]
        cli = bridge["hermesBridgeCli"]["cpuMillisRequest"] - bridge["hermesBridge"]["cpuMillisRequest"]
        self.assertGreater(cli, 0)
        zero_cpu_limit = (
            '{"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "0", "memory": "512Mi"}}'
        )
        for name, env in (
            ("cli", [{"name": "A2A_BRIDGE_EXECUTOR", "value": "cli"}]),
            ("api, set explicitly", [{"name": "A2A_BRIDGE_EXECUTOR", "value": "api"}]),
            ("by valueFrom", [{"name": "A2A_BRIDGE_EXECUTOR",
                               "valueFrom": {"configMapKeyRef": {"name": "x", "key": "executor"}}}]),
            ("a smaller override beside cli", [{"name": "A2A_BRIDGE_EXECUTOR", "value": "cli"},
                                               {"name": "A2A_BRIDGE_RESOURCES",
                                                "value": '{"requests": {"cpu": "100m"}}'}]),
            ("one the operator refuses (zero cpu limit) beside cli",
             [{"name": "A2A_BRIDGE_EXECUTOR", "value": "cli"},
              {"name": "A2A_BRIDGE_RESOURCES", "value": zero_cpu_limit}]),
            ("not JSON", [{"name": "A2A_BRIDGE_RESOURCES", "value": "big"}]),
            ("a quantity it cannot read", [{"name": "A2A_BRIDGE_RESOURCES",
                                            "value": '{"limits": {"cpu": "1x"}}'}]),
            ("a side that is not a map", [{"name": "A2A_BRIDGE_RESOURCES",
                                           "value": '{"requests": "x", "limits": {"cpu": "1"}}'}]),
        ):
            with self.subTest(name):
                self.assertEqual(self._bridge_delta(env), cli)

    def _flagged(self) -> dict:
        """files/flagged-pods.yaml by name; TestTheChartCountsEveryFlaggedPod holds it to the operator."""
        # The tests index entries by name, so a missing one fails there; the names
        # themselves are held to the operator by TestTheChartCountsEveryFlaggedPod.
        return {p["name"]: p for p in yaml.safe_load(_FLAGGED_PODS.read_text())["pods"]}

    def _flagged_delta(self, env: list) -> dict:
        base = self._requirements(["platformAgent.mode=next"])
        req = self._requirements(["platformAgent.mode=next"], values={"operator": {"extraEnv": env}})
        return {key: req[key] - base[key] for key in base}

    def _assert_counts(self, delta: dict, *pods: dict) -> None:
        for out, key in (
            ("requestsCpu", "cpuMillisRequest"), ("limitsCpu", "cpuMillisLimit"),
            ("requestsMemory", "memoryBytesRequest"), ("limitsMemory", "memoryBytesLimit"),
            ("requestsEphemeral", "ephemeralStorageBytesRequest"),
            ("limitsEphemeral", "ephemeralStorageBytesLimit"),
        ):
            self.assertEqual(delta[out], sum(p["replicas"] * p[key] for p in pods), out)
        self.assertEqual(delta["pods"], sum(p["replicas"] for p in pods))
        self.assertEqual(delta["surgeRequestsCpu"], 0, "each rolls Recreate")

    def test_mode_next_counts_the_mcp_server_when_its_flag_is_on(self) -> None:
        """The operator renders the MCP server only under A2A_MCP_DOOR=true (#3869), so no
        golden carries it and the chart adds it from files/flagged-pods.yaml."""
        mcp = self._flagged()["mcp"]
        self.assertGreater(mcp["cpuMillisLimit"], 0, "a limits.cpu quota refuses a pod with none")
        for name, env in (
            ("true", [{"name": "A2A_MCP_DOOR", "value": "true"}]),
            ("by valueFrom", [{"name": "A2A_MCP_DOOR",
                               "valueFrom": {"configMapKeyRef": {"name": "x", "key": "door"}}}]),
            ("not a string", [{"name": "A2A_MCP_DOOR", "value": True}]),
            ("last of two", [{"name": "A2A_MCP_DOOR", "value": "false"},
                             {"name": "A2A_MCP_DOOR", "value": "true"}]),
            ("a $(VAR) reference the kubelet expands", [{"name": "DOOR", "value": "true"},
                                                        {"name": "A2A_MCP_DOOR", "value": "$(DOOR)"}]),
        ):
            with self.subTest(name):
                self._assert_counts(self._flagged_delta(env), mcp)

    def test_the_mcp_server_is_not_counted_when_the_operator_reads_its_flag_as_off(self) -> None:
        for name, env in (
            ("false", [{"name": "A2A_MCP_DOOR", "value": "false"}]),
            ("TRUE, which the operator does not take", [{"name": "A2A_MCP_DOOR", "value": "TRUE"}]),
            ("no value", [{"name": "A2A_MCP_DOOR"}]),
            ("a null value", [{"name": "A2A_MCP_DOOR", "value": None}]),
            ("last of two", [{"name": "A2A_MCP_DOOR", "value": "true"},
                             {"name": "A2A_MCP_DOOR", "value": "false"}]),
        ):
            with self.subTest(name):
                self.assertEqual(set(self._flagged_delta(env).values()), {0})
        # And never under today, where the operator renders no MCP server.
        door = {"operator": {"extraEnv": [{"name": "A2A_MCP_DOOR", "value": "true"}]}}
        self.assertEqual(self._requirements(values=door), self._requirements())

    def test_mode_next_counts_the_profile_dispatcher_when_its_image_is_set(self) -> None:
        """The operator renders the dispatcher, in the release namespace, while
        A2A_DISPATCHER_IMAGE is non-blank (#4808)."""
        dispatcher = self._flagged()["dispatcher"]
        self.assertGreater(dispatcher["cpuMillisLimit"], 0, "a limits.cpu quota refuses a pod with none")
        for name, env in (
            ("an image", [{"name": "A2A_DISPATCHER_IMAGE", "value": "example.com/dispatcher:1"}]),
            ("padded", [{"name": "A2A_DISPATCHER_IMAGE", "value": " example.com/d:1 "}]),
            ("by valueFrom", [{"name": "A2A_DISPATCHER_IMAGE",
                               "valueFrom": {"configMapKeyRef": {"name": "x", "key": "image"}}}]),
            ("last of two", [{"name": "A2A_DISPATCHER_IMAGE", "value": ""},
                             {"name": "A2A_DISPATCHER_IMAGE", "value": "example.com/d:1"}]),
        ):
            with self.subTest(name):
                self._assert_counts(self._flagged_delta(env), dispatcher)

    def test_the_profile_dispatcher_is_not_counted_without_an_image(self) -> None:
        for name, env in (
            ("empty", [{"name": "A2A_DISPATCHER_IMAGE", "value": ""}]),
            ("blank, which the operator trims", [{"name": "A2A_DISPATCHER_IMAGE", "value": "   "}]),
            ("tab and newline", [{"name": "A2A_DISPATCHER_IMAGE", "value": "\t\n"}]),
            ("no value", [{"name": "A2A_DISPATCHER_IMAGE"}]),
            ("a null value", [{"name": "A2A_DISPATCHER_IMAGE", "value": None}]),
            ("last of two", [{"name": "A2A_DISPATCHER_IMAGE", "value": "example.com/d:1"},
                             {"name": "A2A_DISPATCHER_IMAGE", "value": ""}]),
        ):
            with self.subTest(name):
                self.assertEqual(set(self._flagged_delta(env).values()), {0})
        image = {"operator": {"extraEnv": [{"name": "A2A_DISPATCHER_IMAGE", "value": "example.com/d:1"}]}}
        self.assertEqual(self._requirements(values=image), self._requirements())

    def test_mode_next_counts_both_flagged_pods_together(self) -> None:
        pods = self._flagged()
        env = [{"name": "A2A_MCP_DOOR", "value": "true"},
               {"name": "A2A_DISPATCHER_IMAGE", "value": "example.com/d:1"}]
        self._assert_counts(self._flagged_delta(env), pods["mcp"], pods["dispatcher"])

    def _scope_publisher(self) -> dict:
        """files/transient-pods.yaml's entry; TestTheChartCountsTheScopePublishersJobPod holds it to the operator."""
        sp = yaml.safe_load(_TRANSIENT_PODS.read_text())["scopePublisher"]
        return {**sp, "replicas": sp["pods"]}  # the shape _assert_counts reads

    def _scope_delta(self, values: dict, extra_args: list[str] | None = None) -> dict:
        base = self._requirements(["platformAgent.mode=next"], extra_args=extra_args)
        req = self._requirements(["platformAgent.mode=next"], values=values, extra_args=extra_args)
        return {key: req[key] - base[key] for key in base}

    def test_mode_next_counts_the_scope_publishers_job_pod_for_a_scope_with_members(self) -> None:
        """The operator starts the scope publisher's Job, one pod at a time, where the gateway
        pod policy is on and the scope names something to resolve (#4816)."""
        sp = self._scope_publisher()
        self.assertEqual(sp["scopeMembers"], ["folders", "organizations", "sharedVpcHosts", "metricsScopes"])
        self.assertGreater(sp["cpuMillisLimit"], 0, "a limits.cpu quota refuses a pod with none")
        for field in sp["scopeMembers"]:
            with self.subTest(field):
                self._assert_counts(self._scope_delta({"platformAgent": {"scope": {field: ["x"]}}}), sp)
        for name, env in (
            ("true", {"value": "true"}),
            ("by valueFrom", {"valueFrom": {"configMapKeyRef": {"name": "x", "key": "p"}}}),
            ("a $(VAR) reference", {"value": "$(P)"}),
        ):
            with self.subTest(f"policy off in the chart, on by operator.extraEnv: {name}"):
                values = {"admissionPolicy": {"enabled": False},
                          "operator": {"extraEnv": [{"name": "A2A_GATEWAY_POD_POLICY", **env}]},
                          "platformAgent": {"scope": {"folders": ["x"]}}}
                self._assert_counts(self._scope_delta(values), sp)

    def test_the_scope_publishers_job_pod_is_not_counted_when_it_cannot_run(self) -> None:
        for name, values, extra in (
            ("projects only", {"platformAgent": {"scope": {"projects": ["p"]}}}, None),
            ("empty member lists", {"platformAgent": {"scope": {"folders": [], "metricsScopes": []}}}, None),
            ("policy off", {"admissionPolicy": {"enabled": False},
                            "platformAgent": {"scope": {"folders": ["x"]}}}, None),
            ("policy off, extraEnv false", {"admissionPolicy": {"enabled": False},
                                            "operator": {"extraEnv": [{"name": "A2A_GATEWAY_POD_POLICY", "value": "false"}]},
                                            "platformAgent": {"scope": {"folders": ["x"]}}}, None),
            ("Kubernetes 1.29, where the policy doesn't render",
             {"platformAgent": {"scope": {"folders": ["x"]}}}, ["--kube-version", "1.29.0"]),
        ):
            with self.subTest(name):
                self.assertEqual(set(self._scope_delta(values, extra).values()), {0})
        # And never under today, where the operator runs no publisher.
        scope = {"platformAgent": {"scope": {"folders": ["x"]}}}
        self.assertEqual(self._requirements(values=scope), self._requirements())

    @unittest.skipUnless(_HELM, "helm is not installed")
    def test_a_missing_or_unreadable_transient_pods_file_fails_a_next_render(self) -> None:
        """Like footprint.yaml: without transient-pods.yaml the publisher would count as nothing."""
        for name, damage in (
            ("missing", lambda f: f.unlink()),
            ("unreadable", lambda f: f.write_text("scopePublisher: [unclosed\n")),
            ("no scopeMembers list", lambda f: f.write_text("scopePublisher: {pods: 1}\n")),
            ("a scopeMembers entry that isn't a field name", lambda f: f.write_text(
                f.read_text().replace("[folders, organizations, sharedVpcHosts, metricsScopes]", "[folders, 42]"))),
            ("cut short after scopeMembers", lambda f: f.write_text(
                f.read_text().split("  cpuMillisRequest")[0])),
        ):
            with self.subTest(name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                chart = pathlib.Path(tmp.name) / "kube-agents"
                shutil.copytree(_CHART, chart)
                (chart / "values.schema.json").unlink(missing_ok=True)
                (chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)
                damage(chart / "files" / "transient-pods.yaml")
                with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
                    yaml.safe_dump({"probe": {"emitRequirements": True}}, fh)
                    values_path = fh.name
                self.addCleanup(lambda p=values_path: pathlib.Path(p).unlink(missing_ok=True))
                res = subprocess.run(
                    [_HELM, "template", "test-release", str(chart), "-f", values_path,
                     "--set", "platformAgent.mode=next"] + _REQUIRED_HARNESS,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res.returncode, 0, "a damaged transient-pods.yaml must fail the render")
                self.assertIn("transient-pods.yaml is missing or unreadable", res.stderr)

    def _mcp_server_delta(self, env: list) -> dict:
        base = self._requirements(["platformAgent.mode=next"])
        req = self._requirements(["platformAgent.mode=next"],
                                 values={"operator": {"mcpServer": {"enabled": True}, "extraEnv": env}})
        return {key: req[key] - base[key] for key in base}

    def test_mode_next_counts_the_mcp_server_when_operator_mcp_server_enabled_is_true(self) -> None:
        """operator.mcpServer.enabled sets A2A_MCP_DOOR=true, so the count follows it, and an
        extraEnv entry that does not turn the flag off leaves it counted."""
        mcp = self._flagged()["mcp"]
        for name, env in (
            ("the value alone", []),
            ("beside extraEnv true", [{"name": "A2A_MCP_DOOR", "value": "true"}]),
            ("beside an unrelated extraEnv entry", [{"name": "GOMEMLIMIT", "value": "100MiB"}]),
        ):
            with self.subTest(name):
                self._assert_counts(self._mcp_server_delta(env), mcp)
        self.assertEqual(self._requirements(values={"operator": {"mcpServer": {"enabled": True}}}),
                         self._requirements(), "under today the operator renders no MCP server")

    def test_an_extra_env_false_turns_off_the_mcp_server_the_value_turned_on(self) -> None:
        """extraEnv is rendered after the value and wins, in the Deployment and in the count."""
        for name, env in (
            ("false", [{"name": "A2A_MCP_DOOR", "value": "false"}]),
            ("TRUE, which the operator does not take", [{"name": "A2A_MCP_DOOR", "value": "TRUE"}]),
        ):
            with self.subTest(name):
                self.assertEqual(set(self._mcp_server_delta(env).values()), {0})

    def test_mode_next_counts_a_larger_a2a_bridge_resources_as_written(self) -> None:
        """A readable A2A_BRIDGE_RESOURCES above the agent container's size raises the count."""
        bridge = yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]["agentPod"]["hermesBridge"]
        self.assertEqual(
            self._bridge_delta(
                [{"name": "A2A_BRIDGE_RESOURCES", "value": '{"requests": {"cpu": "3500m", "memory": "1Gi"}}'}]
            ),
            3500 - bridge["cpuMillisRequest"],
        )
        # A request left out is its limit, as the API server defaults it.
        self.assertEqual(
            self._bridge_delta([{"name": "A2A_BRIDGE_RESOURCES", "value": '{"limits": {"cpu": "4"}}'}]),
            4000 - bridge["cpuMillisRequest"],
        )
        # Every spelling the Kubernetes quantity grammar admits raises it, not only the
        # common suffixes: a JSON number (a float that prints with an exponent), a leading
        # "+", an exponent and a Pi suffix, each above the agent container's 4Gi memory.
        cli_mem = yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]["agentPod"]["hermesBridgeCli"][
            "memoryBytesRequest"
        ]
        base = self._requirements(["platformAgent.mode=next"])
        for written, want in (
            ('{"requests": {"memory": 6442450944}}', 6442450944),
            ('{"requests": {"memory": "+6Gi"}}', 6 * 1024**3),
            ('{"requests": {"memory": "6e9"}}', 6 * 10**9),
            ('{"requests": {"memory": "1Pi"}}', 1024**5),
        ):
            with self.subTest(written):
                req = self._requirements(
                    ["platformAgent.mode=next"],
                    values={"operator": {"extraEnv": [{"name": "A2A_BRIDGE_RESOURCES", "value": written}]}},
                )
                self.assertGreater(want, cli_mem)
                self.assertEqual(
                    req["requestsMemory"] - base["requestsMemory"],
                    want - yaml.safe_load(_FOOTPRINT.read_text())["nextMode"]["agentPod"]["hermesBridge"]["memoryBytesRequest"],
                )

    def test_an_operator_owned_quota_is_left_to_the_operator(self) -> None:
        """The next-mode session quota a PlatformAgent owns is not a quota this check can fix:
        the operator reapplies it, so a patch would be reverted; its size is maxSessions's
        and the headroom's (#3848)."""
        owned = self._quota({"pods": "2"})
        owned["metadata"]["ownerReferences"] = [
            {"apiVersion": "kubeagents.x-k8s.io/v1alpha1", "kind": "PlatformAgent", "name": "platformagent"}
        ]
        res = self._render({"probe": {"quotas": [owned]}}, ["platformAgent.mode=next"])
        self.assertEqual(res.returncode, 0, f"an operator-owned quota should be skipped:\n{res.stderr}")
        res = self._render({"probe": {"quotas": [self._quota({"pods": "2"})]}}, ["platformAgent.mode=next"])
        self.assertNotEqual(res.returncode, 0, "the same quota, unowned, must still be checked")
    def _chart_pods_and_requirements(self, sets: list[str]) -> tuple[int, dict]:
        """The chart-pods annotation on the PlatformAgent, and the preflight's totals."""
        res = self._render({"probe": {"emitRequirements": True}}, sets)
        self.assertEqual(res.returncode, 0, f"render failed:\n{res.stderr}")
        chart_pods, required = None, None
        for doc in yaml.safe_load_all(res.stdout):
            if not doc:
                continue
            if doc.get("metadata", {}).get("name") == "probe-requirements":
                required = json.loads(doc["data"]["requirements"])
            if doc.get("kind") == "PlatformAgent":
                annotations = doc["metadata"].get("annotations") or {}
                if "kubeagents.x-k8s.io/a2a-quota-chart-pods" in annotations:
                    chart_pods = int(annotations["kubeagents.x-k8s.io/a2a-quota-chart-pods"])
        self.assertIsNotNone(chart_pods, "the PlatformAgent carries no a2a-quota-chart-pods annotation")
        self.assertIsNotNone(required, "probe ConfigMap not found")
        return chart_pods, required

    def _rendered_chart_pods(self, sets: list[str]) -> tuple[int, int, list[str]]:
        """An oracle independent of kube-agents.chartWorkloads: the standing pods the chart
        actually renders in the release namespace, and their surge pods, read from the
        manifests. Every Deployment and StatefulSet the chart renders there counts at its
        replicas; Helm hooks (the uninstall cleanup Job) are not standing. A Deployment that
        runs a pod and doesn't roll Recreate sizes the surge pods its rendered maxSurge
        allows; a StatefulSet sizes none."""
        res = self._render({"probe": {}}, sets)
        self.assertEqual(res.returncode, 0, f"render failed:\n{res.stderr}")
        chart_pods, config_map, pods, surges, names = None, None, 0, 0, []
        for doc in yaml.safe_load_all(res.stdout):
            if not doc:
                continue
            meta = doc.get("metadata", {})
            if doc.get("kind") == "PlatformAgent":
                chart_pods = int((meta.get("annotations") or {})["kubeagents.x-k8s.io/a2a-quota-chart-pods"])
            if doc.get("kind") == "ConfigMap" and meta.get("name") == "a2a-quota-chart-pods":
                config_map = int(doc["data"]["pods"])
            if "helm.sh/hook" in (meta.get("annotations") or {}):
                continue
            # A pod-running kind neither list models would pass both unseen, so it fails here.
            self.assertNotIn(doc.get("kind"), ("DaemonSet", "Job", "CronJob", "ReplicaSet", "Pod"),
                             f"the chart renders a {doc.get('kind')} that kube-agents.chartWorkloads doesn't model")
            if doc.get("kind") not in ("Deployment", "StatefulSet"):
                continue
            if meta.get("namespace") not in (None, "", "default"):  # helm template renders into "default"
                continue
            replicas = doc["spec"].get("replicas", 1)
            pods += replicas
            names.append(f"{doc['kind']}/{meta['name']}={replicas}")
            if doc["kind"] == "Deployment" and replicas > 0:
                strategy = doc["spec"].get("strategy") or {}
                if strategy.get("type") != "Recreate":
                    # The surge pods the controller can add: maxSurge as rendered (Kubernetes'
                    # 25% when the template sets none), a percentage rounded up (#4876).
                    raw = str((strategy.get("rollingUpdate") or {}).get("maxSurge", "25%")).strip()
                    n = math.ceil(float(raw[:-1]) * replicas / 100) if raw.endswith("%") else int(raw)
                    # The controller never scales the new ReplicaSet past the desired count.
                    surges += min(n, replicas)
        # The count reaches the operator one way per release (#4883): the annotation on the
        # PlatformAgent the chart renders, or, with platformAgent.enabled=false, the ConfigMap.
        if "platformAgent.enabled=false" in sets:
            self.assertIsNone(chart_pods, "a PlatformAgent rendered with platformAgent.enabled=false")
            self.assertIsNotNone(config_map, "no a2a-quota-chart-pods ConfigMap without a PlatformAgent")
            chart_pods = config_map
        else:
            self.assertIsNotNone(chart_pods, "the PlatformAgent carries no a2a-quota-chart-pods annotation")
            self.assertIsNone(config_map, "the ConfigMap rendered beside a PlatformAgent, whose annotation wins")
        return chart_pods, pods + surges, names

    def test_the_operator_is_told_the_pods_the_chart_renders_and_their_surges(self) -> None:
        """The chart-pods annotation equals the standing pods the chart renders in the release
        namespace plus their surge pods, read from the rendered manifests rather than from
        the list the annotation is computed from, so a workload the list misses fails here
        (#3848, #4893: the replay proxy was missed). With operator.enabled=false the operator
        runs elsewhere, and neither counts it."""
        for sets in _ORACLE_CASES:
            with self.subTest(sets=sets):
                chart_pods, rendered, names = self._rendered_chart_pods(["platformAgent.mode=next", *sets])
                self.assertEqual(chart_pods, rendered, names)

    def test_the_operator_counts_the_chart_workloads_as_the_chart_does(self) -> None:
        """For a PlatformAgent outside its namespace the operator counts the chart's pods from
        the chart's Deployments and StatefulSets there (a2aChartWorkloadsCount, #5006). Over
        every shape the oracle above checks, that count, run in Go on the rendered workloads,
        must equal the figure the chart stamps. Go is required in CI, where the Python shards
        set it up; the Go test must report PASS, so a skip there fails this test too."""
        if not _GO:
            if os.environ.get("CI"):
                self.fail("go is not on PATH in CI; this test needs it to run the operator's count")
            self.skipTest("go is not installed")
        with tempfile.TemporaryDirectory() as renders:
            for i, sets in enumerate(_ORACLE_CASES):
                res = self._render({"probe": {}}, ["platformAgent.mode=next", *sets])
                self.assertEqual(res.returncode, 0, f"render failed for {sets}:\n{res.stderr}")
                stamped = None
                for doc in yaml.safe_load_all(res.stdout):
                    meta = (doc or {}).get("metadata", {})
                    if doc and doc.get("kind") == "PlatformAgent":
                        stamped = (meta.get("annotations") or {})["kubeagents.x-k8s.io/a2a-quota-chart-pods"]
                    if doc and doc.get("kind") == "ConfigMap" and meta.get("name") == "a2a-quota-chart-pods":
                        stamped = doc["data"]["pods"]
                self.assertIsNotNone(stamped, f"the chart stamped no count for {sets}")
                case = pathlib.Path(renders) / f"case-{i:02d}"
                case.mkdir()
                (case / "manifest.yaml").write_text(res.stdout)
                (case / "expected").write_text(str(stamped))
            go = subprocess.run(
                [_GO, "test", "./internal/controller/", "-count=1", "-v",
                 "-run", "^TestChartWorkloadsCountMatchesTheRenderedChart$"],
                cwd=_ROOT / "k8s-operator", capture_output=True, text=True,
                env={**os.environ, "KUBE_AGENTS_CHART_RENDERS": renders},
            )
        self.assertEqual(go.returncode, 0, f"the operator's count differs from the chart's:\n{go.stdout[-4000:]}\n{go.stderr[-2000:]}")
        self.assertIn("--- PASS: TestChartWorkloadsCountMatchesTheRenderedChart", go.stdout)
        self.assertEqual(go.stdout.count("--- PASS: TestChartWorkloadsCountMatchesTheRenderedChart/case-"), len(_ORACLE_CASES))

    def test_an_operator_only_release_refuses_a_count_past_the_cap(self) -> None:
        """The ConfigMap carries the annotation's 64-pod cap check (#4883): an operator-only
        next-mode release past it fails the render rather than stamp a count the operator
        discards."""
        sets = ["platformAgent.enabled=false", "platformAgent.mode=next", "operator.replicaCount=61"]
        res = self._render({"probe": {}}, sets)
        self.assertNotEqual(res.returncode, 0, "a count past the cap rendered")
        self.assertIn("past the operator's cap of 64", res.stderr)
        ok = self._render({"probe": {}}, ["platformAgent.enabled=false", "platformAgent.mode=next", "operator.replicaCount=60"])
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_the_session_quota_counts_every_surge_pod_and_the_patch_room_stays_one(self) -> None:
        """chartSurgePods totals each rollout's surge pods (#4876), and the patch's surge room
        stays one pod of the largest surging workload, enough for a rollout to progress."""
        base = self._requirements(["platformAgent.mode=next", "litellm.replicaCount=4"])
        three = self._requirements(["platformAgent.mode=next", "litellm.replicaCount=4",
                                    "litellm.rollingUpdate.maxSurge=3"])
        self.assertEqual(three["chartSurgePods"], base["chartSurgePods"] + 2)
        for key in ("surgeRequestsCpu", "surgeRequestsMemory", "surgeLimitsCpu", "surgeLimitsMemory"):
            self.assertEqual(three[key], base[key], key)

    def test_a_max_surge_too_large_to_convert_counts_as_the_replicas(self) -> None:
        """A Deployment's replicas are an int32, so a maxSurge of 1000% or more, or of 10 or
        more digits, counts as the replicas outright, never through a conversion (#4876's
        review). Covers the values a conversion got wrong: a 20-digit integer, which the YAML
        parse makes a float past int64; 20- and 309-digit strings, which float64 reads as 0
        or wraps; and a 23-digit percentage. Leading zeros don't count as digits."""
        # maxUnavailable 1: main's both-zero guard (rollingUpdateFenceposts) reads a 309-digit
        # value as 0 and would refuse the render before the count is reached.
        sets = ["platformAgent.mode=next", "operator.replicaCount=3", "operator.rollingUpdate.maxUnavailable=1"]
        base = self._requirements(sets)
        for huge in ("99999999999999999999", "1000000000000000000000%", "1" + "0" * 309 + "%",
                     "1" + "0" * 309, "1000%", "10000000000"):
            with self.subTest(huge=huge[:12] + ("..." if len(huge) > 12 else "")):
                req = self._requirements(sets, extra_args=["--set-string", f"operator.rollingUpdate.maxSurge={huge}"])
                self.assertEqual(req["chartSurgePods"], base["chartSurgePods"] + 2)
        for huge in (99999999999999999999, 10000000000, 1.0e300):
            with self.subTest(numeric=huge):
                # Through the values file, so the value reaches the template as a number.
                req = self._requirements(sets, values={"operator": {"rollingUpdate": {"maxSurge": huge}}})
                self.assertEqual(req["chartSurgePods"], base["chartSurgePods"] + 2)
        for small, extra in (("000000000002", 1), ("000034%", 1), ("999", 2), ("9999999999", 2)):
            with self.subTest(small=small):
                req = self._requirements(sets, extra_args=["--set-string", f"operator.rollingUpdate.maxSurge={small}"])
                self.assertEqual(req["chartSurgePods"], base["chartSurgePods"] + extra)

    def test_a_replica_count_near_int64_does_not_wrap_the_stamped_count(self) -> None:
        """The chart-pods sums saturate rather than wrap (#4876's review): a replica count of
        2^62 at maxSurge 100% would carry a plain int64 add past 2^63 and stamp a negative
        count, which skips the 64-pod cap. Saturated, it stays past the cap and is refused."""
        res = self._render({"probe": {}}, ["platformAgent.mode=next", "operator.replicaCount=4611686018427387904",
                                           "operator.rollingUpdate.maxSurge=100%"])
        self.assertNotEqual(res.returncode, 0, "a count past the cap rendered")
        self.assertIn("past the operator's cap of 64", res.stderr)

    def test_a_max_surge_that_is_not_a_whole_number_or_percentage_counts_as_the_replicas(self) -> None:
        """The schema admits any string, and validate.yml requires such a value to render
        (it must not be refused), so the count bounds it from above: as many surge pods as
        the workload has replicas, the most a rollout can add."""
        base = self._requirements(["platformAgent.mode=next", "operator.replicaCount=3"])
        for bad in ("abc", "1.5", "50 %", "-1", "%"):
            with self.subTest(bad=bad):
                req = self._requirements(["platformAgent.mode=next", "operator.replicaCount=3",
                                          f"operator.rollingUpdate.maxSurge={bad}"])
                self.assertEqual(req["chartSurgePods"], base["chartSurgePods"] + 2)

    def test_the_preflight_and_the_operator_read_one_workload_list(self) -> None:
        """quotaRequirements counts the same chart workloads as chartQuotaPods, plus the
        uninstall-only cleanup hook, and the surge pods each rollout can add (#4893, #4876)."""
        op = yaml.safe_load(_FOOTPRINT.read_text())["operatorRendered"]
        operator_pods = (
            op["agentPod"]["base"]["pods"] + op["shellSandbox"]["pods"] + op["credentialProxy"]["pods"]
            + op["kubeProxy"]["pods"]
        )
        for sets in ([], ["platformAgent.cleanupHook.enabled=false"], ["litellm.replayShadow.enabled=true"]):
            with self.subTest(sets=sets):
                chart_pods, required = self._chart_pods_and_requirements(sets)
                hook = 0 if "platformAgent.cleanupHook.enabled=false" in sets else 1
                self.assertGreater(required["chartSurgePods"], 0)
                self.assertEqual(chart_pods, required["pods"] - operator_pods - hook + required["chartSurgePods"])

    def test_a_chart_count_past_the_operators_cap_fails_the_render(self) -> None:
        """The operator discards a chart-pods annotation past a2aMaxChartPods for its
        default, which would size the quota below what the namespace runs, so the chart
        refuses to stamp one: 64 renders (60 operator replicas, LiteLLM's default two,
        and a surge pod for each), 65 fails naming the cap."""
        cap = int(re.search(r"a2aMaxChartPods\s*=\s*(\d+)", _A2A_MANIFESTS.read_text()).group(1))
        self.assertEqual(cap, 64)
        ok = self._render({"probe": {}}, ["platformAgent.mode=next", f"operator.replicaCount={cap - 4}"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        refused = self._render({"probe": {}}, ["platformAgent.mode=next", f"operator.replicaCount={cap - 3}"])
        self.assertNotEqual(refused.returncode, 0, "a chart count past the cap rendered")
        self.assertIn("past the operator's cap of 64", refused.stderr)
        # The quota renders only in next mode, so a today-mode render at the same size
        # stamps its figure and passes.
        today = self._render({"probe": {}}, [f"operator.replicaCount={cap - 3}"])
        self.assertEqual(today.returncode, 0, today.stderr)

    def test_the_operators_fallback_is_the_charts_default(self) -> None:
        """An install without the chart gets a2aDefaultChartPods, which must be what a
        default chart render says."""
        chart_pods, _ = self._chart_pods_and_requirements([])
        src = _A2A_MANIFESTS.read_text()
        m = re.search(r"a2aDefaultChartPods\s*=\s*(\d+)", src)
        self.assertIsNotNone(m, "a2aDefaultChartPods not found in the operator source")
        assert m is not None
        self.assertEqual(int(m.group(1)), chart_pods)

    def test_mode_next_without_a_next_mode_footprint_fails_loudly(self) -> None:
        """A footprint without nextMode must not count the A2A stack as zero."""
        footprint_path = self.chart / "files" / "footprint.yaml"
        original = footprint_path.read_text()
        data = yaml.safe_load(original)
        del data["nextMode"]
        try:
            footprint_path.write_text(yaml.safe_dump(data))
            res = self._render({"probe": {"emitRequirements": True}}, ["platformAgent.mode=next"])
        finally:
            footprint_path.write_text(original)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no nextMode section", res.stderr)

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

    def test_web_console_adds_its_pod_and_resources_to_the_totals(self) -> None:
        """Enabling webConsole adds its one replica and resource requests."""
        base = self._requirements()
        with_console = self._requirements(["webConsole.enabled=true"])
        self.assertEqual(with_console["pods"], base["pods"] + 1)
        self.assertEqual(with_console["requestsCpu"], base["requestsCpu"] + _WEB_CONSOLE_REQUEST_MILLIS)
        self.assertEqual(with_console["requestsMemory"], base["requestsMemory"] + _WEB_CONSOLE_REQUEST_MEMORY_BYTES)

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
        """A chart-set value replaces LiteLLM's default request, per replica, in the sum."""
        base = self._requirements()
        with_ephemeral = self._requirements(
            [f"litellm.resources.requests.ephemeral-storage={_EPHEMERAL_SET_GIB}Gi"]
        )
        self.assertEqual(
            with_ephemeral["requestsEphemeral"],
            base["requestsEphemeral"]
            + (_EPHEMERAL_SET_GIB * 1024**3 - _LITELLM_EPHEMERAL_REQUEST_BYTES) * _LITELLM_REPLICAS,
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
        self.assertIn('"limits.cpu":"13700m"', res.stderr)

    def test_limits_memory_shortfall_is_enforced(self) -> None:
        """A quota deficient in limits.memory must be refused and named in the patch."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"limits.memory": "100Mi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small limits.memory quota must fail")
        self.assertIn("  - limits.memory:", res.stderr)
        self.assertIn('"limits.memory":"28544Mi"', res.stderr)

    def test_limits_ephemeral_storage_shortfall_is_enforced(self) -> None:
        """A quota deficient in limits.ephemeral-storage must be refused and named in the patch."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"limits.ephemeral-storage": "100Mi"})]}}
        )
        self.assertNotEqual(
            res.returncode, 0, "a too-small limits.ephemeral-storage quota must fail"
        )
        self.assertIn("  - limits.ephemeral-storage:", res.stderr)
        # The release's 14912Mi plus one LiteLLM surge Pod's 1280Mi.
        self.assertIn('"limits.ephemeral-storage":"16192Mi"', res.stderr)

    def test_shorthand_cpu_spelling_is_enforced(self) -> None:
        """`cpu` is the shorthand spelling for `requests.cpu` and must be checked."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"cpu": "100m"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small cpu shorthand quota must fail")
        self.assertIn("  - cpu:", res.stderr)
        # The release's 2756m plus one LiteLLM surge Pod's 100m.
        self.assertIn('"cpu":"2856m"', res.stderr)

    def test_shorthand_memory_spelling_is_enforced(self) -> None:
        """`memory` is the shorthand spelling for `requests.memory` and must be checked."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"memory": "100Mi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small memory shorthand quota must fail")
        self.assertIn("  - memory:", res.stderr)
        # The release's 12096Mi plus one LiteLLM surge Pod's 2Gi.
        self.assertIn('"memory":"14144Mi"', res.stderr)

    def test_shorthand_ephemeral_storage_spelling_is_enforced(self) -> None:
        """`ephemeral-storage` is the shorthand spelling for `requests.ephemeral-storage`."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"ephemeral-storage": "100Mi"})]}}
        )
        self.assertNotEqual(
            res.returncode, 0, "a too-small ephemeral-storage shorthand quota must fail"
        )
        self.assertIn("  - ephemeral-storage:", res.stderr)
        # The release's 14912Mi plus one LiteLLM surge Pod's 1280Mi.
        self.assertIn('"ephemeral-storage":"16192Mi"', res.stderr)

    def test_shorthand_storage_spelling_is_enforced(self) -> None:
        """`storage` is the shorthand spelling for `requests.storage`."""
        res = self._render(
            {"probe": {"quotas": [self._quota({"storage": "1Gi"})]}}
        )
        self.assertNotEqual(res.returncode, 0, "a too-small storage shorthand quota must fail")
        self.assertIn("  - storage:", res.stderr)
        self.assertIn('"storage":"102Gi"', res.stderr)

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
        self.assertIn("kubeProxy", op)
        self.assertIn("storage", op)

        # Verify agent-api-auth CPU limit cut to 1 core is reflected in footprint
        # Base: platform-agent (3 CPU limit) + fluent-bit (0.5 CPU limit) + agent-api-auth (1 CPU limit) = 4.5 CPU
        self.assertEqual(op["agentPod"]["base"]["cpuMillisLimit"], 4500)
        self.assertEqual(op["agentPod"]["dashboard"]["cpuMillisLimit"], 1000)
        self.assertEqual(op["shellSandbox"]["cpuMillisLimit"], 2000)
        self.assertEqual(op["credentialProxy"]["cpuMillisLimit"], 2000)
        self.assertEqual(op["kubeProxy"]["cpuMillisLimit"], 2000)

        # Total operator limits on default install: 4500 + 1000 + 2000 + 2000 + 2000 = 11500m (11.5 CPU);
        # the kube-proxy runs two replicas of 1000m each.
        total_op_cpu_limit = (
            op["agentPod"]["base"]["cpuMillisLimit"]
            + op["agentPod"]["dashboard"]["cpuMillisLimit"]
            + op["shellSandbox"]["cpuMillisLimit"]
            + op["credentialProxy"]["cpuMillisLimit"]
            + op["kubeProxy"]["cpuMillisLimit"]
        )
        self.assertEqual(total_op_cpu_limit, 11500)

        # Memory, in the same shape. A generator that stopped parsing memory would emit 0
        # here; `--check` alone would not catch that if the regenerated file were committed
        # in the same change, because both sides would move together.
        self.assertEqual(op["agentPod"]["base"]["memoryBytesLimit"], 10496 * 1024**2)
        self.assertEqual(op["agentPod"]["dashboard"]["memoryBytesLimit"], 2 * 1024**3)
        self.assertEqual(op["shellSandbox"]["memoryBytesLimit"], 2 * 1024**3)
        self.assertEqual(op["credentialProxy"]["memoryBytesLimit"], 2 * 1024**3)
        self.assertEqual(op["kubeProxy"]["memoryBytesLimit"], 2 * 1024**3)

        # Ephemeral storage is set on two of the four and deliberately 0 on the others;
        # asserting the zeroes is the point, since an unparsed value looks identical.
        self.assertEqual(op["agentPod"]["base"]["ephemeralStorageBytesLimit"], 3 * 1024**3)
        self.assertEqual(op["agentPod"]["dashboard"]["ephemeralStorageBytesLimit"], 0)
        self.assertEqual(op["shellSandbox"]["ephemeralStorageBytesLimit"], 0)
        self.assertEqual(
            op["credentialProxy"]["ephemeralStorageBytesLimit"], 8 * 1024**3
        )
        self.assertEqual(op["agentPod"]["base"]["ephemeralStorageBytesRequest"], 3 * 1024**3)
        self.assertEqual(op["agentPod"]["dashboard"]["ephemeralStorageBytesRequest"], 0)
        self.assertEqual(op["shellSandbox"]["ephemeralStorageBytesRequest"], 0)
        self.assertEqual(
            op["credentialProxy"]["ephemeralStorageBytesRequest"], 8 * 1024**3
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

    @unittest.skipUnless(_HELM, "helm is not installed")
    def test_a_missing_or_unreadable_flagged_pods_file_fails_a_next_render(self) -> None:
        """Like footprint.yaml: without flagged-pods.yaml the flagged pods would count as none."""
        for name, damage in (
            ("missing", lambda f: f.unlink()),
            ("unreadable", lambda f: f.write_text("pods: [unclosed\n")),
            ("no pods list", lambda f: f.write_text("pod: []\n")),
            ("an entry cut short", lambda f: f.write_text(
                f.read_text().rsplit("    memoryBytesLimit", 1)[0])),
        ):
            with self.subTest(name):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                chart = pathlib.Path(tmp.name) / "kube-agents"
                shutil.copytree(_CHART, chart)
                (chart / "values.schema.json").unlink(missing_ok=True)
                (chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)
                damage(chart / "files" / "flagged-pods.yaml")
                with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
                    yaml.safe_dump({"probe": {"emitRequirements": True}}, fh)
                    values_path = fh.name
                self.addCleanup(lambda p=values_path: pathlib.Path(p).unlink(missing_ok=True))
                res = subprocess.run(
                    [_HELM, "template", "test-release", str(chart), "-f", values_path,
                     "--set", "platformAgent.mode=next"] + _REQUIRED_HARNESS,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(res.returncode, 0, "a damaged flagged-pods.yaml must fail the render")
                self.assertIn("flagged-pods.yaml is missing or unreadable", res.stderr)

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
    """The install prerequisites page's footprint figures, and its IAM roles.

    The figures are a generated region (scripts/install_footprint.py, through
    `make docs-generate`), rendered from the same quotaRequirements template these
    tests drive, so they are held current by `make docs-check` rather than by
    patterns over the prose. What stays here is what the generator does not own.
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

    def test_the_footprint_figures_are_a_generated_region(self) -> None:
        page = self._PREREQUISITES.read_text()
        self.assertIn("<!-- BEGIN GENERATED: install-footprint -->", page)
        self.assertIn("<!-- END GENERATED: install-footprint -->", page)

    def test_the_session_quota_margin_counts_the_scope_publishers_job_pods(self) -> None:
        # The chart counts that pod from files/transient-pods.yaml (#4816), and the
        # operator's margin may not count fewer.
        manifests_src = _A2A_MANIFESTS.read_text()
        margin = re.search(r"^\s*a2aQuotaTransientMargin\s*=\s*(.+)$", manifests_src, re.MULTILINE)
        self.assertIsNotNone(margin, "a2aQuotaTransientMargin not found")
        self.assertIn("a2aScopePublisherJobPods", margin.group(1), "the margin no longer counts the scope publisher's Job pods")
        pods = re.search(r"^\s*a2aScopePublisherJobPods\s*=\s*(\d+)\s*$", manifests_src, re.MULTILINE)
        self.assertIsNotNone(pods, "a2aScopePublisherJobPods not found")
        self.assertGreaterEqual(
            int(pods.group(1)),
            yaml.safe_load(_TRANSIENT_PODS.read_text())["scopePublisher"]["pods"],
        )

    def test_the_prerequisites_page_names_the_iam_roles(self) -> None:
        """Verifies the prerequisites page names the minimum required GCP IAM roles and permissions."""
        page = self._PREREQUISITES.read_text()
        self.assertIn("`google_project_iam_member`", page)
        self.assertIn("`google_service_account_iam_member`", page)
        self.assertIn("resourcemanager.projects.setIamPolicy", page)
        self.assertIn("iam.serviceAccounts.setIamPolicy", page)
        self.assertIn("roles/resourcemanager.projectIamAdmin", page)
        self.assertIn("roles/iam.serviceAccountAdmin", page)
        self.assertIn("roles/owner", page)
        self.assertIn("roles/editor", page)



if __name__ == "__main__":
    unittest.main()
