"""What one run measures: container peaks, restarts, PVC use, Prometheus counters, log counts.

Container memory and CPU come from Cloud Monitoring over the run's own time
window rather than from cgroup ``memory.peak``, which counts from container start
and would carry one run's peak into the next.

Counters are read per pod, and a run reports their growth pod by pod: a pod that
did not exist when the run started (the gateway after a restart, every stateful
pod after a reset) counts from zero, so replacing a pod neither subtracts the old
pod's totals nor hides what the new one did.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.scale import kube
from kube_agents_bench.scale.records import Metric

LOG = logging.getLogger(__name__)

Sample = tuple[str, dict[str, str], float]

MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
MEMORY_METRIC = "kubernetes.io/container/memory/used_bytes"
# Non-evictable memory is the working set the kernel cannot reclaim, the part an OOM kill counts.
MEMORY_TYPE = "non-evictable"
CPU_METRIC = "kubernetes.io/container/cpu/core_usage_time"
ALIGNMENT_SECONDS = 60
QUOTA_PROJECT_HEADER = "x-goog-user-project"
HTTP_TIMEOUT_SECONDS = 60
RFC3339_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOCALHOST = "127.0.0.1"
METRICS_PATH = "/metrics"
PYTHON_FETCH = "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1]).read().decode())"
SIDECAR_RESTART_POLICY = "Always"
RUNNING_PHASE = "Running"
# The agent's pod, by the label the operator gives it; a name substring would also match the
# A2A chat gateway (platform-agent-a2a-gateway-*) on installs that run one.
GATEWAY_SELECTOR = "app=platform-agent-gateway"
PLATFORM_AGENT_RESOURCE = "platformagents"
# The CRD's default for spec.mode.
DEFAULT_MODE = "today"
# The agent's data volume: profiles, sessions, the kanban board. The other claims (bus, shell) are not it.
AGENT_DATA_CLAIM = "platform-agent-data"
AGENT_CONTAINER = "platform-agent"
PROFILES_DIR = "/opt/data/profiles"
# The scaffolder names a profile cluster-<project>-<cluster>-<location>; the project is
# known, so the cluster is what lies between it and the location (a region or zone).
PROFILE_PREFIX = "cluster-"
LOCATION_SUFFIX_RE = re.compile(r"-[a-z]+-[a-z]+[0-9]+(?:-[a-z])?$")
WATCHER_CLUSTER_UP = "k8s_event_watcher_cluster_up"
# (snapshot attribute, label selector, container, env var naming the port or None for its first port)
SCRAPE_TARGETS = (
    ("watcher", GATEWAY_SELECTOR, "agent-api-auth", "EVENT_WATCHER_METRICS_PORT"),
    ("proxy", "app=platform-agent-credential-proxy", "envoy-credential-proxy", "CREDENTIAL_PROXY_METRICS_PORT"),
    ("litellm", "app=litellm", "litellm-container", None),
)
OOM_REASON = "OOMKilled"
BYTES_PER_KIB = 1024
DF_CAPACITY_COLUMN = 1
DF_USED_COLUMN = 2
# Log lines that stand in for the two metrics the harness does not export yet.
LOG_PATTERNS = {
    # A triage whose session or whose turn could not start; TRIAGE_NOT_STARTED is the newer marker.
    "triage_lost": re.compile(
        r"Aborting troubleshooting trigger|Gateway API chat execution failed"
        r"|Failed to call gateway API chat execution|TRIAGE_NOT_STARTED"
    ),
    "run_slot_refusals": re.compile(r"Too many concurrent runs"),
    "provider_429s": re.compile(r"(?:\b429\b.*(?:RESOURCE_EXHAUSTED|rate.?limit|Too Many Requests))|RateLimitError", re.I),
}
WATCHER_COUNTERS = {
    "watcher_events_seen": "k8s_event_watcher_events_seen_total",
    "watcher_session_creates": "k8s_event_watcher_session_creates_total",
    "watcher_events_injected": "k8s_event_watcher_events_injected_total",
    "watcher_quota_suppressed": "k8s_event_watcher_events_quota_suppressed_total",
}
PROXY_REFUSALS = "kubeagents_credential_proxy_admission_refusals_total"
PROXY_WAIT = "kubeagents_credential_proxy_admission_wait_seconds"
PROXY_WAIT_QUANTILES = (("proxy_wait_p50_seconds", 0.5), ("proxy_wait_max_seconds", 1.0))
TOKEN_FAMILIES = {
    "tokens_input": re.compile(r"^litellm_input_tokens(_metric)?(_total)?$"),
    "tokens_output": re.compile(r"^litellm_output_tokens(_metric)?(_total)?$"),
    "tokens_cached": re.compile(r"^litellm_input_cached_tokens(_metric)?(_total)?$"),
}
SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)")
LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
QUANTITY_SUFFIXES = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4, "m": 0.001,
}


# --- Prometheus text ---------------------------------------------------------------------------


def parse_prometheus(text: str) -> list[Sample]:
    samples = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        labels = dict(LABEL_RE.findall(match["labels"] or ""))
        samples.append((match["name"], labels, value))
    return samples


def family_sum(samples: Iterable[Sample], name: str) -> float:
    return sum(value for sample, _, value in samples if sample == name)


def histogram_buckets(samples: Iterable[Sample], name: str) -> dict[float, float]:
    """Cumulative bucket counts by upper bound, summed over label sets."""
    buckets: dict[float, float] = {}
    for sample, labels, value in samples:
        if sample != f"{name}_bucket" or "le" not in labels:
            continue
        bound = float("inf") if labels["le"] == "+Inf" else float(labels["le"])
        buckets[bound] = buckets.get(bound, 0.0) + value
    return buckets


def bucket_delta(before: dict[float, float], after: dict[float, float]) -> dict[float, float]:
    return {bound: after[bound] - before.get(bound, 0.0) for bound in after}


def histogram_quantile(cumulative: dict[float, float], quantile: float) -> float | None:
    """The upper bound of the first bucket holding ``quantile`` of the observations."""
    if not cumulative:
        return None
    bounds = sorted(cumulative)
    total = cumulative[bounds[-1]]
    if total <= 0:
        return None
    for bound in bounds:
        if cumulative[bound] >= quantile * total:
            return bound
    return bounds[-1]


def scrape(context: str, namespace: str, pod: str, container: str, port: int) -> list[Sample]:
    """Read a container's /metrics from inside the pod.

    Neither the API server's pod proxy (NetworkPolicy drops it) nor port-forward
    (the agent's pod runs under gVisor) reaches every metrics port, so the read
    runs in the container itself, with wget or else Python.
    """
    url = f"http://{LOCALHOST}:{port}{METRICS_PATH}"
    script = f"wget -qO- {url} 2>/dev/null || python3 -c {PYTHON_FETCH!r} {url}"
    out = kube.kubectl(context, "exec", "-n", namespace, pod, "-c", container, "--", "sh", "-c", script)
    return parse_prometheus(out)


def parse_quantity(text: str) -> float:
    for suffix in sorted(QUANTITY_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * QUANTITY_SUFFIXES[suffix]
    return float(text)


# --- the host install ----------------------------------------------------------------------------


@dataclass
class Snapshot:
    """Counter values at one moment, per pod, so a run reports their growth across its window."""

    # (pod UID, container) -> restartCount
    restarts: dict[tuple[str, str], int] = field(default_factory=dict)
    # (pod UID, container) whose last termination was an OOM kill
    oom: set[tuple[str, str]] = field(default_factory=set)
    # scrape target -> pod UID -> samples
    scraped: dict[str, dict[str, list[Sample]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def samples(self, target: str) -> list[Sample]:
        return [sample for pod in self.scraped.get(target, {}).values() for sample in pod]


class Install:
    """Reads the experiment install: its pods, counters, PVC and logs."""

    def __init__(self, project: str, context: str, namespace: str, cluster: str, location: str) -> None:
        self.project = project
        self.context = context
        self.namespace = namespace
        self.cluster = cluster
        self.location = location

    def pods(self, selector: str | None = None) -> list[dict[str, Any]]:
        args = ["get", "pods", "-n", self.namespace] + (["-l", selector] if selector else [])
        return kube.kubectl_json(self.context, *args)["items"]

    def gateway_pod(self) -> dict[str, Any] | None:
        running = [pod for pod in self.pods(GATEWAY_SELECTOR) if pod["status"].get("phase") == RUNNING_PHASE]
        return running[0] if running else None

    def mode(self) -> str:
        """The PlatformAgent's mode: its spec's, or the CRD's default when the spec leaves it unset."""
        items = kube.kubectl_json(self.context, "get", PLATFORM_AGENT_RESOURCE, "-n", self.namespace)["items"]
        if not items:
            return ""
        return str(items[0].get("spec", {}).get("mode") or DEFAULT_MODE)

    def image(self) -> str:
        """The agent container's image, the build a record describes."""
        pod = self.gateway_pod()
        if not pod:
            return ""
        for container in pod["spec"]["containers"]:
            if container["name"] == AGENT_CONTAINER:
                return str(container.get("image", ""))
        return ""

    def limits(self) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for pod in self.pods():
            for container in _running_containers(pod):
                limits = container.get("resources", {}).get("limits", {})
                entry: dict[str, float] = {}
                if "memory" in limits:
                    entry["memory_bytes"] = parse_quantity(limits["memory"])
                if "cpu" in limits:
                    entry["cpu_cores"] = parse_quantity(limits["cpu"])
                if entry:
                    result[container["name"]] = entry
        return result

    def replicas(self) -> dict[str, int]:
        """Per container name, how many running pods carry it."""
        counts: dict[str, int] = {}
        for pod in self.pods():
            if pod["status"].get("phase") != RUNNING_PHASE:
                continue
            for container in _running_containers(pod):
                counts[container["name"]] = counts.get(container["name"], 0) + 1
        return counts

    def snapshot(self, targets: Iterable[str] | None = None) -> Snapshot:
        wanted = set(targets) if targets is not None else {target[0] for target in SCRAPE_TARGETS}
        snap = Snapshot()
        pods = self.pods()
        for pod in pods:
            # By UID: a StatefulSet re-creates a pod under the same name, and the new one counts from zero.
            uid = pod["metadata"]["uid"]
            for status in pod["status"].get("containerStatuses", []) + pod["status"].get("initContainerStatuses", []):
                key = (uid, status["name"])
                snap.restarts[key] = int(status.get("restartCount", 0))
                if status.get("lastState", {}).get("terminated", {}).get("reason") == OOM_REASON:
                    snap.oom.add(key)
        for attr, selector, container, env_name in SCRAPE_TARGETS:
            if attr not in wanted:
                continue
            per_pod: dict[str, list[Sample]] = {}
            matching = [pod for pod in self.pods(selector) if pod["status"].get("phase") == RUNNING_PHASE]
            if not matching:
                snap.errors.append(f"no running pod for {selector}")
            # Every replica: counters are per process, so a family's total is the sum over pods.
            for pod in matching:
                name = pod["metadata"]["name"]
                port = _metrics_port(pod, container, env_name)
                if not port:
                    snap.errors.append(f"no metrics port on {name}/{container}")
                    continue
                try:
                    per_pod[pod["metadata"]["uid"]] = scrape(self.context, self.namespace, name, container, port)
                except kube.CommandError as exc:
                    snap.errors.append(f"scrape {name}: {exc}")
            snap.scraped[attr] = per_pod
        return snap

    def profile_clusters(self) -> set[str]:
        """Cluster names that have a Cluster Agent profile on the agent's data volume."""
        pod = self.gateway_pod()
        if not pod:
            return set()
        try:
            out = kube.kubectl(self.context, "exec", "-n", self.namespace, pod["metadata"]["name"], "-c",
                               AGENT_CONTAINER, "--", "sh", "-c", f"ls {PROFILES_DIR} 2>/dev/null || true")
        except kube.CommandError:
            return set()
        prefix = f"{PROFILE_PREFIX}{self.project}-"
        names = set()
        for entry in out.split():
            if not entry.startswith(prefix):
                continue
            rest = entry[len(prefix):]
            match = LOCATION_SUFFIX_RE.search(rest)
            if match:
                names.add(rest[: match.start()])
        return names

    def watched_clusters(self) -> set[str]:
        """Cluster names the event watcher reports as up."""
        samples = self.snapshot(targets=["watcher"]).samples("watcher")
        return {labels.get("cluster", "") for name, labels, value in samples
                if name == WATCHER_CLUSTER_UP and value >= 1}

    def pvc_metrics(self) -> list[Metric]:
        """Used and total bytes of the agent's data volume, and the largest profile."""
        for pod in self.pods():
            claims = {
                v["name"] for v in pod["spec"].get("volumes", [])
                if v.get("persistentVolumeClaim", {}).get("claimName") == AGENT_DATA_CLAIM
            }
            for container in pod["spec"]["containers"]:
                for mount in container.get("volumeMounts", []):
                    if mount["name"] not in claims:
                        continue
                    path = mount["mountPath"]
                    target = ["exec", "-n", self.namespace, pod["metadata"]["name"], "-c", container["name"], "--"]
                    try:
                        df = kube.kubectl(self.context, *target, "df", "-k", path).splitlines()[-1].split()
                        metrics = [
                            Metric("pvc_capacity_bytes", float(df[DF_CAPACITY_COLUMN]) * BYTES_PER_KIB),
                            Metric("pvc_used_bytes", float(df[DF_USED_COLUMN]) * BYTES_PER_KIB),
                        ]
                        # Cluster Agent profiles only; the Platform Agent's own profile is not one.
                        du = kube.kubectl(self.context, *target, "sh", "-c",
                                          f"du -sk {path}/profiles/{PROFILE_PREFIX}* 2>/dev/null || true")
                        sizes = [float(line.split()[0]) * BYTES_PER_KIB for line in du.splitlines() if line.strip()]
                        if sizes:
                            metrics.append(Metric("profile_bytes_max", max(sizes)))
                            metrics.append(Metric("profiles", float(len(sizes))))
                        return metrics
                    except (kube.CommandError, IndexError, ValueError) as exc:
                        LOG.warning("pvc read failed on %s: %s", pod["metadata"]["name"], exc)
        return []

    def log_counts(self, since: str) -> dict[str, int]:
        """Log-line counts since ``since`` in the pods running now.

        A pod replaced during the window took its log with it, so a run that
        restarts the gateway counts the new pod's lines only.
        """
        counts = {name: 0 for name in LOG_PATTERNS}
        for pod in self.pods():
            for container in _running_containers(pod):
                try:
                    text = kube.kubectl(self.context, "logs", "-n", self.namespace, pod["metadata"]["name"],
                                        "-c", container["name"], f"--since-time={since}")
                except kube.CommandError:
                    continue
                for name, pattern in LOG_PATTERNS.items():
                    counts[name] += len(pattern.findall(text))
        return counts

    def container_peaks(self, start: float, end: float) -> dict[str, dict[str, float]]:
        """Per container, the highest memory and CPU over [start, end], from Cloud Monitoring."""
        peaks: dict[str, dict[str, float]] = {}
        base = (
            f'resource.type="k8s_container" AND resource.labels.project_id="{self.project}" '
            f'AND resource.labels.cluster_name="{self.cluster}" '
            f'AND resource.labels.namespace_name="{self.namespace}"'
        )
        queries = (
            ("memory_peak_bytes", f'metric.type="{MEMORY_METRIC}" AND metric.labels.memory_type="{MEMORY_TYPE}" AND {base}', "ALIGN_MAX"),
            ("cpu_peak_cores", f'metric.type="{CPU_METRIC}" AND {base}', "ALIGN_RATE"),
        )
        for name, query, aligner in queries:
            for series in self._time_series(query, aligner, start, end):
                container = series["resource"]["labels"].get("container_name", "")
                values = [_point_value(point) for point in series.get("points", [])]
                if container and values:
                    current = peaks.setdefault(container, {}).get(name, 0.0)
                    peaks[container][name] = max(current, max(values))
        return peaks

    def _time_series(self, query: str, aligner: str, start: float, end: float) -> list[dict[str, Any]]:
        # Application-default credentials: some gcloud user tokens are refused by the
        # Monitoring API (ACCESS_TOKEN_TYPE_UNSUPPORTED), while ADC tokens are accepted.
        token = kube.run([kube.GCLOUD, "auth", "application-default", "print-access-token"]).strip()
        params = {
            "filter": query,
            "interval.startTime": _rfc3339(start),
            "interval.endTime": _rfc3339(end),
            "aggregation.alignmentPeriod": f"{ALIGNMENT_SECONDS}s",
            "aggregation.perSeriesAligner": aligner,
        }
        base_url = MONITORING_URL.format(project=self.project)
        url = base_url + "?" + urllib.parse.urlencode(params)
        series: list[dict[str, Any]] = []
        while url:
            request = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}", QUOTA_PROJECT_HEADER: self.project}
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                page = json.loads(resp.read().decode())
            series.extend(page.get("timeSeries", []))
            next_token = page.get("nextPageToken")
            url = base_url + "?" + urllib.parse.urlencode({**params, "pageToken": next_token}) if next_token else ""
        return series


def _running_containers(pod: dict[str, Any]) -> list[dict[str, Any]]:
    """The pod's containers plus its native sidecars (init containers that keep running)."""
    sidecars = [c for c in pod["spec"].get("initContainers", []) if c.get("restartPolicy") == SIDECAR_RESTART_POLICY]
    return pod["spec"]["containers"] + sidecars


def _metrics_port(pod: dict[str, Any], container_name: str, env_name: str | None) -> int | None:
    """The container's metrics port: from its environment when named there, else its first port."""
    for container in _running_containers(pod):
        if container["name"] != container_name:
            continue
        if env_name:
            for env in container.get("env", []):
                if env.get("name") == env_name and str(env.get("value", "")).isdigit():
                    return int(env["value"])
            return None
        ports = [p["containerPort"] for p in container.get("ports", []) if "containerPort" in p]
        return ports[0] if ports else None
    return None


def _point_value(point: dict[str, Any]) -> float:
    value = point["value"]
    return float(value.get("doubleValue", value.get("int64Value", 0)))


def _rfc3339(epoch: float) -> str:
    return time.strftime(RFC3339_FORMAT, time.gmtime(epoch))


def _growth(before: Snapshot, after: Snapshot, target: str, family: str) -> float:
    """A counter family's growth, pod by pod; a pod new since ``before`` counts from zero."""
    total = 0.0
    for pod, samples in after.scraped.get(target, {}).items():
        total += family_sum(samples, family) - family_sum(before.scraped.get(target, {}).get(pod, []), family)
    return total


def counter_metrics(before: Snapshot, after: Snapshot) -> list[Metric]:
    """The counters' growth across a run's window, as metrics, pod by pod."""
    metrics: list[Metric] = []
    restarts: dict[str, int] = {}
    oomkills: dict[str, int] = {}
    for (pod, container), count in after.restarts.items():
        grown = max(0, count - before.restarts.get((pod, container), 0))
        restarts[container] = restarts.get(container, 0) + grown
        # lastState holds only the latest termination, so this counts pods whose container
        # restarted in the window and was last killed for memory, not every kill.
        if grown and (pod, container) in after.oom:
            oomkills[container] = oomkills.get(container, 0) + 1
    for container in sorted(restarts):
        metrics.append(Metric("restarts", float(restarts[container]), container))
        metrics.append(Metric("oomkills", float(oomkills.get(container, 0)), container))
    if after.scraped.get("watcher"):
        for name, family in WATCHER_COUNTERS.items():
            metrics.append(Metric(name, _growth(before, after, "watcher", family)))
    if after.scraped.get("proxy"):
        metrics.append(Metric("proxy_refusals", _growth(before, after, "proxy", PROXY_REFUSALS)))
        delta: dict[float, float] = {}
        for pod, samples in after.scraped["proxy"].items():
            grown = bucket_delta(histogram_buckets(before.scraped.get("proxy", {}).get(pod, []), PROXY_WAIT),
                                 histogram_buckets(samples, PROXY_WAIT))
            for bound, value in grown.items():
                delta[bound] = delta.get(bound, 0.0) + value
        for name, quantile in PROXY_WAIT_QUANTILES:
            value = histogram_quantile(delta, quantile)
            if value is not None and value != float("inf"):
                metrics.append(Metric(name, value))
    if after.scraped.get("litellm"):
        names = {sample for sample, _, _ in after.samples("litellm")}
        for metric, pattern in TOKEN_FAMILIES.items():
            family = next((name for name in sorted(names) if pattern.match(name)), None)
            if family:
                metrics.append(Metric(metric, _growth(before, after, "litellm", family)))
    return metrics
