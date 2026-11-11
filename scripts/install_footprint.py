"""The install footprint figures the prerequisites page and the installer test quote.

Every figure comes from what the chart and the operator already compute, not from a
copy of their arithmetic:

- each profile's totals from ``kube-agents.quotaRequirements``, the template the
  chart's quota preflight and the installer's summary print, rendered by ``helm
  template`` with that profile's values, and with the API the chart reads as GKE
  Autopilot for an Autopilot profile;
- the per-workload figures from the chart's data files (``files/footprint.yaml``,
  which ``make chart-sync`` sums from the operator's goldens, ``flagged-pods.yaml``
  and ``transient-pods.yaml``), emitted by the same render, so nothing here parses
  YAML;
- the next-mode session quota from the ``ResourceQuota`` the operator's next-mode
  golden renders, which counts the operator's default chart pods; a profile whose
  chart runs more pods moves it by the difference the chart stamps;
- the session pod from ``a2a/gateway/spawn.go``'s constants.

Standard library only, like ``generate_docs.py``, which imports this. The one tool it
calls is ``helm``.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from generate_chart_footprint import parse_bytes, parse_cpu_millis

REPO = Path(__file__).resolve().parent.parent
CHART_DIR = REPO / "charts/kube-agents"
HELM_BINARY = "helm"
# The chart's required values, which no profile is about.
REQUIRED_SETS = (
    "platformAgent.harness.clusterName=c",
    "platformAgent.harness.location=us-east4",
    "platformAgent.harness.projectId=p",
)
# The resource kube-agents.gkeAutopilot reads; the installer passes the same one.
AUTOPILOT_API = "auto.gke.io/v1/AllowlistedWorkload"
PROBE_TEMPLATE = "templates/zz-docs-footprint-probe.yaml"
HELPERS_TEMPLATE = "templates/_helpers.tpl"
# One JSON document, so reading it needs no YAML parser: the requirements, the
# chart's pod count for the session quota, and the three data files.
PROBE = """\
{{- $probe := dict
      "requirements" (include "kube-agents.quotaRequirements" . | fromJson)
      "chartQuotaPods" (include "kube-agents.chartQuotaPods" . | int)
      "footprint" (.Files.Get "files/footprint.yaml" | fromYaml)
      "flaggedPods" (.Files.Get "files/flagged-pods.yaml" | fromYaml)
      "transientPods" (.Files.Get "files/transient-pods.yaml" | fromYaml) }}
{{ toJson (dict "apiVersion" "v1" "kind" "ConfigMap" "metadata" (dict "name" "docs-footprint-probe") "data" (dict "probe" (toJson $probe))) }}
"""
NEXT_GOLDEN = REPO / "k8s-operator/internal/testing/testdata/platform/expected/platformagent-next.yaml"
SESSION_QUOTA_NAME = "platformagent-a2a-session-quota"
A2A_MANIFESTS_GO = REPO / "k8s-operator/internal/controller/platformagent_a2a_manifests.go"
SPAWN_GO = REPO / "a2a/gateway/spawn.go"
MIB = 1024**2
GIB = 1024**3
NONE_CELL = "—"


@dataclass(frozen=True)
class Profile:
    key: str
    # How the page names it; empty for a profile only the JSON carries.
    label: str
    sets: tuple[str, ...]
    autopilot: bool = False

    @property
    def next_mode(self) -> bool:
        return "platformAgent.mode=next" in self.sets


_NEXT = ("platformAgent.mode=next",)
_HINDSIGHT_MINTER = ("hindsight.enabled=true", "githubMinter.enabled=true")
# The installer test's tfvars, as the installer maps them onto the chart
# (tests/test_installer_common.py, _FOOTPRINT_EXPECTED_SETS, which that test holds
# equal to these).
INSTALLER_SETS = (
    "platformAgent.storage.size=200Gi",
    "platformAgent.harness.memory.provider=kube_agents_memory",
    "platformAgent.harness.hermes.dashboardEnabled=false",
    "webConsole.enabled=true",
    "githubMinter.enabled=true",
    "platformAgent.mode=next",
)
PROFILES = (
    Profile("today-standard", "`today`, GKE Standard", ()),
    Profile("today-autopilot", "`today`, GKE Autopilot", (), autopilot=True),
    Profile("next-standard", "`next`, GKE Standard", _NEXT),
    Profile("next-autopilot", "`next`, GKE Autopilot", _NEXT, autopilot=True),
    Profile("next-hindsight-minter", "`next` with Hindsight and the GitHub minter, GKE Standard", _NEXT + _HINDSIGHT_MINTER),
    Profile("installer-autopilot", "", INSTALLER_SETS, autopilot=True),
    Profile("installer-standard", "", INSTALLER_SETS),
)

TOTALS_HEADER = (
    "| Profile | Pods | CPU requests | Memory requests | CPU limits | Memory limits | Ephemeral storage requests / limits | Claims | `requests.storage` | Session quota `pods` |",
    "| ------- | ---- | ------------ | --------------- | ---------- | ------------- | ----------------------------------- | ------ | ------------------ | -------------------- |",
)
WORKLOADS_HEADER = (
    "| Workload | Pods | CPU request | Memory request | CPU limit | Memory limit | Ephemeral storage request / limit | Runs |",
    "| -------- | ---- | ----------- | -------------- | --------- | ------------ | --------------------------------- | ---- |",
)
# footprint.yaml's nextMode workloads, in the order the page lists them, with what
# the Runs cell says. A workload the data file adds and this table lacks fails the
# render, so a new one cannot go unlisted.
NEXT_WORKLOADS = (
    ("a2aNats", "NATS server", "Standing, one JetStream claim each ({claim})"),
    ("a2aCallout", "Auth callout", "Standing"),
    ("a2aVerifier", "Capability verifier", "Standing"),
    ("a2aGateway", "A2A gateway", "Standing; one fewer with the operator's `A2A_GATEWAY_ELECTION=false`"),
    ("a2aConsole", "Console", "Standing"),
    ("a2aDispatcher", "Profile dispatcher", "Standing, unless `operator.profileDispatcher.enabled` is `false`, when the quota check still counts it"),
    ("a2aProvision", "Bus provisioning Job", "At install, again whenever its spec changes, and about daily"),
)
# footprint.yaml nextMode keys that are not a workload row of their own.
NEXT_NON_WORKLOADS = ("agentPod", "storage")
FLAGGED_LABELS = {
    "mcp": "MCP server",
}
FLAGGED_WHEN = {
    "true": "With the operator's `{env}=true`",
    "nonBlank": "With the operator's `{env}` set",
}
TRANSIENT_LABELS = {
    "scopePublisher": (
        "Scope publisher Job",
        "One pod at a time, every 15 minutes, where `platformAgent.scope` names a folder,"
        " organization, Shared VPC host or Metrics Scope",
    ),
}


def run_probe(profile: Profile) -> dict:
    """Render the probe for one profile and return its JSON."""
    return _probe(profile.sets, profile.autopilot)


@functools.lru_cache(maxsize=None)
def _probe(sets: tuple[str, ...], autopilot: bool) -> dict:
    if shutil.which(HELM_BINARY) is None:
        raise SystemExit(
            f"{Path(__file__).relative_to(REPO)}: the install footprint is rendered from the chart, "
            f"which needs helm; `{HELM_BINARY}` is not on PATH."
        )
    with tempfile.TemporaryDirectory() as tmp:
        # The chart as the installer's summary probe copies it
        # (scripts/installer/installer_common.sh, render_footprint_summary): its
        # metadata, values, schema and data files, with the helpers as the only
        # template beside the probe, so no other template's checks refuse a profile
        # that leaves out values only they need.
        chart = Path(tmp) / "kube-agents"
        (chart / "templates").mkdir(parents=True)
        for name in ("Chart.yaml", "values.yaml", "values.schema.json"):
            shutil.copy(CHART_DIR / name, chart / name)
        shutil.copytree(CHART_DIR / "files", chart / "files")
        shutil.copy(CHART_DIR / HELPERS_TEMPLATE, chart / HELPERS_TEMPLATE)
        (chart / PROBE_TEMPLATE).write_text(PROBE, encoding="utf-8")
        command = [HELM_BINARY, "template", "docs-footprint", str(chart), "--show-only", PROBE_TEMPLATE]
        for item in REQUIRED_SETS + sets:
            command += ["--set", item]
        if autopilot:
            command += ["--api-versions", AUTOPILOT_API]
        result = subprocess.run(command, capture_output=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise SystemExit(f"helm template failed for {list(sets)} (exit {result.returncode}):\n{result.stderr.strip()}")
    document = "\n".join(line for line in result.stdout.splitlines() if line != "---" and not line.startswith("#"))
    return json.loads(json.loads(document)["data"]["probe"])


def _go_int_constant(source: str, name: str, path: Path) -> int:
    match = re.search(rf"^\s*(?:const\s+)?{name}\s*=\s*(\d+)\s*$", source, re.M)
    if match is None:
        raise SystemExit(f"{path.relative_to(REPO)}: no integer constant {name}")
    return int(match.group(1))


def _go_string_constant(source: str, name: str, path: Path) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', source, re.M)
    if match is None:
        raise SystemExit(f"{path.relative_to(REPO)}: no string constant {name}")
    return match.group(1)


@functools.lru_cache(maxsize=1)
def golden_session_quota() -> int:
    """The pods the operator's next-mode golden puts in the session quota."""
    text = NEXT_GOLDEN.read_text(encoding="utf-8")
    for document in text.split("\n---\n"):
        if f"name: {SESSION_QUOTA_NAME}\n" in document and "kind: ResourceQuota" in document:
            match = re.search(r'^\s+pods: "(\d+)"$', document, re.M)
            if match:
                return int(match.group(1))
    raise SystemExit(f"{NEXT_GOLDEN.relative_to(REPO)}: no {SESSION_QUOTA_NAME} ResourceQuota with a pods figure")


@functools.lru_cache(maxsize=1)
def quota_constants() -> dict[str, int]:
    """The operator constants the session quota note quotes."""
    source = A2A_MANIFESTS_GO.read_text(encoding="utf-8")
    margin = re.search(r"^\s*a2aQuotaTransientMargin\s*=\s*(\w+(?:\s*\+\s*\w+)*)\s*$", source, re.M)
    if margin is None:
        raise SystemExit(f"{A2A_MANIFESTS_GO.relative_to(REPO)}: no a2aQuotaTransientMargin sum")
    parts = [part.strip() for part in margin.group(1).split("+")]
    return {
        "maxSessions": _go_int_constant(source, "defaultA2AMaxSessions", A2A_MANIFESTS_GO),
        "defaultChartPods": _go_int_constant(source, "a2aDefaultChartPods", A2A_MANIFESTS_GO),
        "margin": sum(int(p) if p.isdigit() else _go_int_constant(source, p, A2A_MANIFESTS_GO) for p in parts),
    }


def session_quota(profile: Profile) -> int | None:
    """The next-mode session quota's pods for a profile; None under today.

    The golden counts the operator's default chart pods, which is what a default
    chart stamps; a profile whose chart stamps more moves the quota by as many.
    """
    if not profile.next_mode:
        return None
    stamped = run_probe(profile)["chartQuotaPods"]
    return golden_session_quota() - quota_constants()["defaultChartPods"] + stamped


def cpu(millis: int) -> str:
    """A CPU quantity as Kubernetes writes it: whole cores, else millicores."""
    return str(millis // 1000) if millis % 1000 == 0 else f"{millis}m"


def memory(size: int) -> str:
    """A byte quantity in the largest binary unit that holds it whole."""
    for unit, scale in (("Gi", GIB), ("Mi", MIB), ("Ki", 1024)):
        if size and size % scale == 0:
            return f"{size // scale}{unit}"
    return str(size)


def profiles_model() -> dict:
    """Every profile's values and totals, for tests/data/footprint_profiles.json."""
    out = {}
    for profile in PROFILES:
        out[profile.key] = {
            "label": profile.label,
            "sets": list(profile.sets),
            "autopilot": profile.autopilot,
            "requirements": run_probe(profile)["requirements"],
            "sessionQuotaPods": session_quota(profile),
        }
    return {"profiles": out}


def profiles_json() -> str:
    return json.dumps(profiles_model(), indent=2, sort_keys=False) + "\n"


def _totals_row(profile: Profile) -> str:
    r = run_probe(profile)["requirements"]
    quota = session_quota(profile)
    return (
        f"| {profile.label} | {r['pods']} | {cpu(r['requestsCpu'])} | {memory(r['requestsMemory'])}"
        f" | {cpu(r['limitsCpu'])} | {memory(r['limitsMemory'])}"
        f" | {memory(r['requestsEphemeral'])} / {memory(r['limitsEphemeral'])}"
        f" | {r['persistentVolumeClaims']} | {memory(r['requestsStorage'])}"
        f" | {NONE_CELL if quota is None else quota} |"
    )


def _workload_row(label: str, pods: str, entry: dict, runs: str) -> str:
    return (
        f"| {label} | {pods} | {cpu(entry['cpuMillisRequest'])} | {memory(entry['memoryBytesRequest'])}"
        f" | {cpu(entry['cpuMillisLimit'])} | {memory(entry['memoryBytesLimit'])}"
        f" | {_ephemeral(entry)} | {runs} |"
    )


def _ephemeral(entry: dict) -> str:
    request = entry.get("ephemeralStorageBytesRequest", 0)
    limit = entry.get("ephemeralStorageBytesLimit", 0)
    return NONE_CELL if not request and not limit else f"{memory(request)} / {memory(limit)}"


def _per_pod(entry: dict) -> dict:
    """footprint.yaml sums each workload over its pods; the page quotes one pod."""
    pods = entry["pods"]
    figures = {k: v for k, v in entry.items() if type(v) is int and k != "pods"}
    uneven = sorted(k for k, v in figures.items() if v % pods)
    if uneven:
        raise SystemExit(f"footprint.yaml sums {uneven} unevenly over {pods} pods; one pod's figure is not a whole number")
    return {k: v // pods for k, v in figures.items()}


def _session_pod() -> dict:
    source = SPAWN_GO.read_text(encoding="utf-8")

    def quantity(name: str) -> str:
        return _go_string_constant(source, name, SPAWN_GO)

    # The chart footprint generator's parsers, which read every quantity form the
    # chart's template does, so the two footprint scripts cannot disagree.
    ephemeral = parse_bytes(quantity("workerEphemeralStorageRequest"))
    return {
        "cpuMillisRequest": parse_cpu_millis(quantity("workerCPURequest")),
        "memoryBytesRequest": parse_bytes(quantity("workerMemoryRequest")),
        "cpuMillisLimit": parse_cpu_millis(quantity("workerCPULimit")),
        "memoryBytesLimit": parse_bytes(quantity("workerMemoryLimit")),
        # spawn.go limits ephemeral storage at its request.
        "ephemeralStorageBytesRequest": ephemeral,
        "ephemeralStorageBytesLimit": ephemeral,
    }


def workloads_rows() -> list[str]:
    """One row per next-mode workload, one pod's requests and limits."""
    default_next = next(p for p in PROFILES if p.key == "next-standard")
    probe = run_probe(default_next)
    following = probe["footprint"]["nextMode"]
    listed = {key for key, _, _ in NEXT_WORKLOADS} | set(NEXT_NON_WORKLOADS)
    unlisted = sorted(set(following) - listed)
    if unlisted:
        raise SystemExit(
            f"charts/kube-agents/files/footprint.yaml nextMode has {unlisted}, which the footprint "
            f"table does not list; add each to NEXT_WORKLOADS in {Path(__file__).relative_to(REPO)}."
        )
    storage = following["storage"]
    claim = memory(storage["storageBytesRequest"] // storage["persistentVolumeClaims"])
    rows = []
    for key, label, runs in NEXT_WORKLOADS:
        entry = following[key]
        pods = entry["pods"]
        if key == "a2aGateway":
            # The standby the election runs, which no golden renders; a golden
            # that starts rendering it would be counted twice.
            if pods != 1:
                raise SystemExit(
                    f"footprint.yaml nextMode.a2aGateway has {pods} pods; the footprint table adds the "
                    "election's standby to a golden that renders one gateway. Drop that addition in "
                    f"{Path(__file__).relative_to(REPO)} if the golden now renders the standby."
                )
            pods += 1
        rows.append(_workload_row(label, str(pods), _per_pod(entry), runs.format(claim=claim)))
    bridge = following["agentPod"]["hermesBridge"]
    cli = following["agentPod"]["hermesBridgeCli"]
    rows.append(
        _workload_row(
            "`hermes-bridge` container",
            "in the agent pod",
            bridge,
            "Under the default `api` executor; `A2A_BRIDGE_EXECUTOR=cli` raises it to"
            f" {cpu(cli['cpuMillisRequest'])} CPU and {memory(cli['memoryBytesRequest'])} memory"
            f" (limits {cpu(cli['cpuMillisLimit'])} and {memory(cli['memoryBytesLimit'])}),"
            " and `A2A_BRIDGE_RESOURCES` overrides either",
        )
    )
    for pod in probe["flaggedPods"]["pods"]:
        if pod["name"] not in FLAGGED_LABELS or pod["enabledWhen"] not in FLAGGED_WHEN:
            raise SystemExit(
                f"charts/kube-agents/files/flagged-pods.yaml has {pod['name']} ({pod['enabledWhen']}), which the "
                f"footprint table cannot name; add it to FLAGGED_LABELS / FLAGGED_WHEN in {Path(__file__).relative_to(REPO)}."
            )
        when = FLAGGED_WHEN[pod["enabledWhen"]].format(env=pod["env"])
        rows.append(_workload_row(FLAGGED_LABELS[pod["name"]], str(pod["replicas"]), pod, when))
    for key, entry in probe["transientPods"].items():
        if key not in TRANSIENT_LABELS:
            raise SystemExit(
                f"charts/kube-agents/files/transient-pods.yaml has {key}, which the footprint table cannot "
                f"name; add it to TRANSIENT_LABELS in {Path(__file__).relative_to(REPO)}."
            )
        label, runs = TRANSIENT_LABELS[key]
        rows.append(_workload_row(label, str(entry["pods"]), entry, runs))
    rows.append(
        _workload_row(
            "`a2a-worker` session pod",
            "up to `maxSessions`",
            _session_pod(),
            "One per `delegate:` from chat and per conversation moved to `/session`",
        )
    )
    return rows


def render_tables() -> str:
    """The page's footprint block: totals by profile, the session quota, the workloads."""
    constants = quota_constants()
    lines = [
        "Totals by install profile, as the chart's quota preflight sums them (the operator's"
        " pods included, the session pods not):",
        "",
        *TOTALS_HEADER,
        *(_totals_row(p) for p in PROFILES if p.label),
        "",
        f"The session quota is `spec.harness.tuning.maxSessions` ({constants['maxSessions']} by default)"
        " plus the pods the operator runs for the agent, the chart's pods in the namespace with"
        f" their rollout surges ({constants['defaultChartPods']} when the operator cannot read the"
        f" chart's count), and a margin of {constants['margin']} for the operator's rollout surges,"
        " its Jobs and the gateway's count-then-create overshoot.",
        "",
        "Under `spec.mode: next`, per pod:",
        "",
        *WORKLOADS_HEADER,
        *workloads_rows(),
    ]
    return "\n".join(lines)
