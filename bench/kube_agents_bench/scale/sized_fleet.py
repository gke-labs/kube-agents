"""The sized test fleet's sizes, its workloads, and the check of its designed state.

docs/designs/sized-test-fleet.md is the design. ``bench/scale/fleet-sizes.yaml``
declares size M and the factors for S and L; ``bench/tf/sized-fleet/`` builds the
clusters from the same file, with the same arithmetic as :func:`fleet` here, and
``hack/sized_fleet.py`` applies the workloads this module generates and runs the
check. The workloads are small on purpose: the test needs many objects and much
churn, not CPU.
"""

from __future__ import annotations

import calendar
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SIZES_FILE = Path(__file__).resolve().parents[2] / "scale" / "fleet-sizes.yaml"
CLUSTER_NAME_FORMAT = "sized-{size}-{index:02d}"
NAMESPACE_FORMAT = "team-{index:02d}"
WEB_NAME_FORMAT = "web-{index:04d}"
CLIENT_NAME_FORMAT = "client-{index:04d}"
BROKEN_NAME_FORMAT = "broken-{kind}-{index}"
PART_OF_LABEL = "app.kubernetes.io/part-of"
COMPONENT_LABEL = "app.kubernetes.io/component"
PART_OF = "sized-fleet"
WEB = "web"
CLIENT = "client"
BROKEN = "broken"
WEB_PORT = 8080
SERVICE_PORT = 80
MINUTES_PER_HOUR = 60
# A missed start is skipped rather than run late, so a slow cluster cannot bunch its
# CronJobs into one minute and spoil the even rate.
STARTING_DEADLINE_SECONDS = 120
# A client Pod ends by itself within cronjob_pod_seconds_max; past this margin it is cut.
ACTIVE_DEADLINE_MARGIN_SECONDS = 60
JOB_TTL_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 5
# Failing Pods alternate between the two ways a Pod fails on its own.
BROKEN_KINDS = ("image", "crash")
# The churn check passes within this share of the designed rate.
CHURN_TOLERANCE = 0.10
KUBE_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Cluster:
    name: str
    index: int
    nodes: int
    deployments: int
    cronjobs: int
    failing_pods: int


@dataclass(frozen=True)
class Fleet:
    size: str
    shape: dict[str, Any]
    clusters: tuple[Cluster, ...]

    @property
    def nodes(self) -> int:
        return sum(c.nodes for c in self.clusters)

    @property
    def deployments(self) -> int:
        return sum(c.deployments for c in self.clusters)

    @property
    def cronjobs(self) -> int:
        return sum(c.cronjobs for c in self.clusters)

    @property
    def failing_pods(self) -> int:
        return sum(c.failing_pods for c in self.clusters)

    @property
    def churn_per_minute(self) -> float:
        """Client Pods started a minute across the fleet, by design."""
        return self.cronjobs / self.shape["cronjob_period_minutes"]


def epoch(timestamp: str) -> float:
    """A Kubernetes timestamp (``2026-11-10T20:30:27Z``, always UTC) as seconds since the epoch.

    ``calendar.timegm``, not ``time.mktime``: mktime reads the local zone, and under
    daylight saving time it is off by an hour.
    """
    return float(calendar.timegm(time.strptime(timestamp, KUBE_TIME_FORMAT)))


def load_sizes(path: Path = SIZES_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def split(total: int, parts: int) -> list[int]:
    """``total`` divided as evenly as it goes; the first parts take one more.

    The same rule as the stack's node split (bench/tf/sized-fleet/main.tf).
    """
    if parts <= 0:
        return []
    each, remainder = divmod(total, parts)
    return [each + (1 if i < remainder else 0) for i in range(parts)]


def fleet(size: str | None = None, sizes: dict[str, Any] | None = None) -> Fleet:
    """The fleet of one size: every count of size M times the size's factor, rounded up."""
    sizes = sizes or load_sizes()
    size = size or sizes["default_size"]
    if size not in sizes["factors"]:
        raise ValueError(f"unknown size {size!r}; one of {sorted(sizes['factors'])}")
    factor = sizes["factors"][size]
    counts = {key: math.ceil(value * factor) for key, value in sizes["base"].items()}
    n = counts["clusters"]
    columns = {key: split(counts[key], n) for key in ("nodes", "deployments", "cronjobs", "failing_pods")}
    clusters = tuple(
        Cluster(
            name=CLUSTER_NAME_FORMAT.format(size=size.lower(), index=i + 1),
            index=i,
            nodes=columns["nodes"][i],
            deployments=columns["deployments"][i],
            cronjobs=columns["cronjobs"][i],
            failing_pods=columns["failing_pods"][i],
        )
        for i in range(n)
    )
    return Fleet(size=size, shape=sizes["shape"], clusters=clusters)


def _labels(component: str, name: str) -> dict[str, str]:
    return {PART_OF_LABEL: PART_OF, COMPONENT_LABEL: component, "app": name}


def _resources(shape: dict[str, Any]) -> dict[str, Any]:
    return {
        "requests": {"cpu": shape["cpu_request"], "memory": shape["memory_request"]},
        "limits": {"memory": shape["memory_limit"]},
    }


def _namespace(index: int) -> str:
    return NAMESPACE_FORMAT.format(index=index)


def web_manifests(index: int, shape: dict[str, Any]) -> list[dict[str, Any]]:
    """One HTTP server Deployment and its Service."""
    name = WEB_NAME_FORMAT.format(index=index)
    namespace = _namespace(index % shape["namespaces_per_cluster"])
    labels = _labels(WEB, name)
    serve = f"mkdir -p /www && echo ok > /www/index.html && exec httpd -f -p {WEB_PORT} -h /www"
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": shape["deployment_replicas"],
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [{
                        "name": WEB,
                        "image": shape["image"],
                        "command": ["sh", "-c", serve],
                        "ports": [{"containerPort": WEB_PORT}],
                        "readinessProbe": {"httpGet": {"path": "/", "port": WEB_PORT}},
                        "resources": _resources(shape),
                    }],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {"selector": {"app": name}, "ports": [{"port": SERVICE_PORT, "targetPort": WEB_PORT}]},
    }
    return [deployment, service]


def client_schedule(index: int, period_minutes: int) -> str:
    """Every ``period_minutes``, starting in minute ``index % period``, so the starts spread evenly.

    ``*/5`` on every CronJob would start them all in the same minute.
    """
    offset = index % period_minutes
    return f"{offset}-59/{period_minutes} * * * *"


def client_manifest(index: int, deployments: int, shape: dict[str, Any]) -> dict[str, Any]:
    """A CronJob whose Pod sends a few requests to one of its cluster's Services, then exits."""
    name = CLIENT_NAME_FORMAT.format(index=index)
    target = index % deployments
    target_namespace = _namespace(target % shape["namespaces_per_cluster"])
    url = f"http://{WEB_NAME_FORMAT.format(index=target)}.{target_namespace}.svc.cluster.local:{SERVICE_PORT}/"
    low, high = shape["cronjob_pod_seconds_min"], shape["cronjob_pod_seconds_max"]
    script = (
        f"for i in $(seq 1 {shape['client_requests']}); do "
        f"wget -q -O /dev/null -T {REQUEST_TIMEOUT_SECONDS} {url} || true; done; "
        f"sleep $(( {low} + RANDOM % {high - low + 1} ))"
    )
    labels = _labels(CLIENT, name)
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": name, "namespace": target_namespace, "labels": labels},
        "spec": {
            "schedule": client_schedule(index, shape["cronjob_period_minutes"]),
            "concurrencyPolicy": "Forbid",
            "startingDeadlineSeconds": STARTING_DEADLINE_SECONDS,
            "successfulJobsHistoryLimit": 0,
            "failedJobsHistoryLimit": 1,
            "jobTemplate": {
                "metadata": {"labels": labels},
                "spec": {
                    "backoffLimit": 0,
                    "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
                    "activeDeadlineSeconds": high + ACTIVE_DEADLINE_MARGIN_SECONDS,
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [{
                                "name": CLIENT,
                                "image": shape["image"],
                                "command": ["sh", "-c", script],
                                "resources": _resources(shape),
                            }],
                        },
                    },
                },
            },
        },
    }


def broken_manifest(index: int, shape: dict[str, Any]) -> dict[str, Any]:
    """A one-replica Deployment whose Pod fails: an image that does not exist, or a command that exits 1."""
    kind = BROKEN_KINDS[index % len(BROKEN_KINDS)]
    name = BROKEN_NAME_FORMAT.format(kind=kind, index=index)
    labels = _labels(BROKEN, name)
    container: dict[str, Any] = {"name": BROKEN, "resources": _resources(shape)}
    if kind == "image":
        container["image"] = shape["missing_image"]
    else:
        container.update(image=shape["image"], command=["sh", "-c", "echo failing on purpose; exit 1"])
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": _namespace(0), "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}},
        },
    }


def cluster_manifests(cluster: Cluster, shape: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object one cluster carries: its namespaces, then the servers, the clients and the failing Pods."""
    out: list[dict[str, Any]] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": _namespace(i), "labels": {PART_OF_LABEL: PART_OF}}}
        for i in range(shape["namespaces_per_cluster"])
    ]
    for i in range(cluster.deployments):
        out += web_manifests(i, shape)
    out += [client_manifest(i, cluster.deployments, shape) for i in range(cluster.cronjobs)]
    out += [broken_manifest(i, shape) for i in range(cluster.failing_pods)]
    return out


@dataclass
class Observed:
    """What one cluster has, as the check reads it."""
    ready_nodes: int = 0
    available_web: int = 0
    cronjobs: int = 0
    failing_pods: int = 0


def check(the_fleet: Fleet, observed: dict[str, Observed], churn_per_minute: float | None) -> dict[str, Any]:
    """The fleet against its designed state: each cluster's nodes, servers, clients and failing Pods,
    and the fleet's churn rate. Returns the findings and whether the fleet is sound."""
    findings: list[str] = []
    for c in the_fleet.clusters:
        o = observed.get(c.name)
        if o is None:
            findings.append(f"{c.name}: not read")
            continue
        for what, want, got in (
            ("ready nodes", c.nodes, o.ready_nodes),
            ("available servers", c.deployments, o.available_web),
            ("client CronJobs", c.cronjobs, o.cronjobs),
        ):
            if got != want:
                findings.append(f"{c.name}: {got} {what}, designed {want}")
        if o.failing_pods < c.failing_pods:
            findings.append(f"{c.name}: {o.failing_pods} failing Pods, designed {c.failing_pods}")
    churn: dict[str, Any] = {"designed_per_minute": the_fleet.churn_per_minute}
    if churn_per_minute is not None:
        # The check and its message use the same rounded figure, so a finding never reads as a pass.
        measured = round(churn_per_minute, 1)
        churn["measured_per_minute"] = measured
        if abs(measured - the_fleet.churn_per_minute) > CHURN_TOLERANCE * the_fleet.churn_per_minute:
            findings.append(
                f"churn {measured:.1f} Pods a minute, designed {the_fleet.churn_per_minute:.1f} "
                f"within {CHURN_TOLERANCE:.0%}"
            )
    return {
        "size": the_fleet.size,
        "sound": not findings,
        "findings": findings,
        "designed": {
            "clusters": len(the_fleet.clusters),
            "nodes": the_fleet.nodes,
            "deployments": the_fleet.deployments,
            "cronjobs": the_fleet.cronjobs,
            "failing_pods": the_fleet.failing_pods,
        },
        "churn": churn,
    }
