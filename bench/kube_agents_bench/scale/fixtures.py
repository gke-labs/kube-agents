"""Fixture objects the runner applies to the test clusters, and the pod churner.

Every fixture object and namespace carries ``FIXTURE_LABEL``, so a level is
applied and removed by label and re-applying one is idempotent. Pods run the pause image with a token CPU and
memory request: the request keeps the HPAs computing (an HPA over a container
with no CPU request writes a Warning event every sync, which would turn the
object factor into a Warning-event factor).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kube_agents_bench.scale import kube

LOG = logging.getLogger(__name__)

FIXTURE_LABEL = "harness-scale/fixture"
OBJECTS_NAMESPACE_FORMAT = "scale-obj-{index:04d}"
LISTING_NAMESPACE = "scale-listing"
WARNING_NAMESPACE = "scale-warning"
CHURN_NAMESPACE = "scale-churn"
PAUSE_IMAGE = "registry.k8s.io/pause:3.10"
# A tag that does not exist, so each Pod fails its pull and writes Warning events.
BROKEN_IMAGE = "registry.k8s.io/pause:harness-scale-missing"
POD_CPU_REQUEST = "1m"
POD_MEMORY_REQUEST = "8Mi"
REPLICAS_PER_DEPLOYMENT = 2
# Each Deployment brings itself, a Service and an HPA.
OBJECTS_PER_DEPLOYMENT = 3
HPA_MAX_REPLICAS = 10
HPA_CPU_UTILIZATION = 80
SERVICE_PORT = 80
CONFIGMAP_DATA_KEY = "settings"
CHURN_BATCH_SECONDS = 10
BATCH_LABEL = "harness-scale/batch"
BATCH_ID_LENGTH = 8
BATCH_ID_PREFIX = "b"
SECONDS_PER_MINUTE = 60
# The object mix per level (namespaces, Deployments, ConfigMaps); each Deployment
# brings 1 Service, 1 HPA and REPLICAS_PER_DEPLOYMENT Pods. Levels not listed
# scale the 4,000 row.
OBJECT_MIX = {100: (5, 15, 20), 4000: (100, 600, 900), 10000: (250, 1500, 2250)}
REFERENCE_LEVEL = 4000


@dataclass(frozen=True)
class Mix:
    namespaces: int
    deployments: int
    configmaps: int

    @property
    def pods(self) -> int:
        return self.deployments * REPLICAS_PER_DEPLOYMENT

    @property
    def total(self) -> int:
        # namespaces + Deployments + their Services and HPAs + Pods + ConfigMaps
        return self.namespaces + self.deployments * OBJECTS_PER_DEPLOYMENT + self.pods + self.configmaps


def object_mix(level: int) -> Mix:
    if level in OBJECT_MIX:
        return Mix(*OBJECT_MIX[level])
    namespaces, deployments, configmaps = OBJECT_MIX[REFERENCE_LEVEL]
    scale = level / REFERENCE_LEVEL
    return Mix(
        max(1, round(namespaces * scale)),
        max(1, round(deployments * scale)),
        max(1, round(configmaps * scale)),
    )


def _labels(fixture: str) -> dict[str, str]:
    return {FIXTURE_LABEL: fixture}


def _namespace(name: str, fixture: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": _labels(fixture)}}


def _pod_spec(image: str, command: list[str] | None = None) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "app",
        "image": image,
        "resources": {"requests": {"cpu": POD_CPU_REQUEST, "memory": POD_MEMORY_REQUEST}},
    }
    if command:
        container["command"] = command
    return {"containers": [container], "terminationGracePeriodSeconds": 0}


def _deployment(name: str, namespace: str, fixture: str, replicas: int, image: str = PAUSE_IMAGE,
                command: list[str] | None = None) -> dict[str, Any]:
    selector = {"app": name}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": _labels(fixture)},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": selector},
            "template": {"metadata": {"labels": {**selector, **_labels(fixture)}}, "spec": _pod_spec(image, command)},
        },
    }


def objects_manifests(level: int) -> list[dict[str, Any]]:
    """The object mix for one objects level, spread evenly across its namespaces."""
    mix = object_mix(level)
    fixture = "objects"
    names = [OBJECTS_NAMESPACE_FORMAT.format(index=index) for index in range(mix.namespaces)]
    manifests = [_namespace(name, fixture) for name in names]
    for index in range(mix.deployments):
        namespace = names[index % len(names)]
        app = f"app-{index:05d}"
        manifests.append(_deployment(app, namespace, fixture, REPLICAS_PER_DEPLOYMENT))
        manifests.append({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": app, "namespace": namespace, "labels": _labels(fixture)},
            "spec": {"selector": {"app": app}, "ports": [{"port": SERVICE_PORT}]},
        })
        manifests.append({
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": app, "namespace": namespace, "labels": _labels(fixture)},
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": app},
                "minReplicas": REPLICAS_PER_DEPLOYMENT,
                "maxReplicas": HPA_MAX_REPLICAS,
                "metrics": [{
                    "type": "Resource",
                    "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": HPA_CPU_UTILIZATION}},
                }],
            },
        })
    for index in range(mix.configmaps):
        manifests.append({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": f"config-{index:05d}", "namespace": names[index % len(names)], "labels": _labels(fixture)},
            "data": {CONFIGMAP_DATA_KEY: f"value-{index}"},
        })
    return manifests


def listing_manifests(pods: int) -> list[dict[str, Any]]:
    fixture = "listing"
    return [_namespace(LISTING_NAMESPACE, fixture), _deployment("listing", LISTING_NAMESPACE, fixture, pods)]


def warning_manifests(failing_pods: int) -> list[dict[str, Any]]:
    """One workload whose Pods fail their image pull, so each writes Warning events.

    The base point is one such Pod; the burst level is one fault on many Pods
    at once, the fan-out shape the per-Pod dedup key turns into many incidents.
    """
    fixture = "warning"
    return [
        _namespace(WARNING_NAMESPACE, fixture),
        _deployment("broken-image", WARNING_NAMESPACE, fixture, failing_pods, image=BROKEN_IMAGE),
    ]


def fixture_pods(values: dict[str, float], churn_lifetime_seconds: int, first: bool = True) -> int:
    """Pods the fixtures place on one test cluster at one point, for node planning.

    Every cluster holds the object mix and its share of the churn; the listing
    and the failing workload live on the first cluster only.
    """
    clusters = max(1, int(values.get("clusters", 1)))
    churn_live = math.ceil(values["churn_per_minute"] * churn_lifetime_seconds / SECONDS_PER_MINUTE / clusters)
    pods = object_mix(int(values["objects"])).pods + churn_live
    if first:
        pods += int(values["failing_pods"]) + int(values["listing_pods"])
    return pods


def delete_fixture(context: str, fixture: str) -> None:
    """Delete every namespace of one fixture; their objects go with them."""
    kube.kubectl(context, "delete", "namespace", "-l", f"{FIXTURE_LABEL}={fixture}",
                 "--ignore-not-found", "--wait=false")


class Churner:
    """Creates and deletes Pods at a steady rate until stopped.

    Every ``CHURN_BATCH_SECONDS`` it creates one batch of bare pause Pods per
    cluster and deletes the batches older than ``lifetime_seconds``. A rate
    that does not divide into whole Pods per batch carries the remainder to the
    next batch, so 10 per minute over two clusters is 10 per minute, not one
    per cluster per batch. ``created`` counts what it really created.
    """

    def __init__(self, contexts: list[str], per_minute: float, lifetime_seconds: int) -> None:
        self.contexts = contexts
        self.per_minute = per_minute
        self.lifetime_seconds = lifetime_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._carry = 0.0
        self.created = 0
        self.errors: list[str] = []

    def next_batch(self) -> int:
        """Pods per cluster in the next batch, carrying the fraction forward."""
        wanted = self.per_minute * CHURN_BATCH_SECONDS / SECONDS_PER_MINUTE / max(1, len(self.contexts)) + self._carry
        size = int(wanted)
        self._carry = wanted - size
        return size

    def start(self) -> None:
        for context in self.contexts:
            kube.apply(context, [_namespace(CHURN_NAMESPACE, "churn")])
        self._thread = threading.Thread(target=self._loop, name="churner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop, and delete the churned Pods still alive, so the next level starts without them."""
        self._stop.set()
        if self._thread:
            self._thread.join()
        for context in self.contexts:
            try:
                kube.kubectl(context, "delete", "pods", "-n", CHURN_NAMESPACE, "-l", f"{FIXTURE_LABEL}=churn",
                             "--wait=false", "--ignore-not-found")
            except Exception as exc:  # cleanup is best effort; the next level re-applies its own
                self.errors.append(f"churn cleanup: {exc}")

    def _batch(self, size: int) -> list[dict[str, Any]]:
        # A leading letter: an all-hex id such as 1e100000 reads as a number to kubectl's YAML parser.
        batch = BATCH_ID_PREFIX + uuid.uuid4().hex[:BATCH_ID_LENGTH]
        return [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": f"churn-{batch}-{index:04d}",
                    "namespace": CHURN_NAMESPACE,
                    "labels": {**_labels("churn"), BATCH_LABEL: batch},
                },
                "spec": _pod_spec(PAUSE_IMAGE),
            }
            for index in range(size)
        ]

    def _loop(self) -> None:
        live: list[tuple[float, str]] = []
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                size = self.next_batch()
                if size:
                    pods = self._batch(size)
                    for context in self.contexts:
                        kube.apply(context, pods)
                        self.created += len(pods)
                    live.append((started, pods[0]["metadata"]["labels"][BATCH_LABEL]))
                while live and started - live[0][0] >= self.lifetime_seconds:
                    _, old = live.pop(0)
                    for context in self.contexts:
                        kube.kubectl(context, "delete", "pods", "-n", CHURN_NAMESPACE, "-l",
                                     f"{BATCH_LABEL}={old}", "--wait=false")
            except Exception as exc:  # a failed batch is recorded and the churn goes on
                self.errors.append(f"churn: {type(exc).__name__}: {exc}")
            self._stop.wait(max(0.0, CHURN_BATCH_SECONDS - (time.monotonic() - started)))
