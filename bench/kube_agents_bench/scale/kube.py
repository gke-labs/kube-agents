"""Thin wrappers over kubectl and gcloud for the measurement runner.

Every call names its context or project explicitly, so the runner never acts on
whatever the caller's current context happens to be.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger(__name__)

KUBECTL = "kubectl"
GCLOUD = "gcloud"
DEFAULT_TIMEOUT_SECONDS = 300
APPLY_TIMEOUT_SECONDS = 900
RESIZE_TIMEOUT_SECONDS = 1800
CREATE_CLUSTER_TIMEOUT_SECONDS = 1800
APPLY_CHUNK = 500
FIELD_MANAGER = "harness-scale"
ARGS_SHOWN = 6
KUBECONFIG_ENV = "KUBECONFIG"
PRIVATE_KUBECONFIG_MODE = 0o600
RETRY_ATTEMPTS = 7
RETRY_FIRST_DELAY_SECONDS = 10
RETRY_MAX_DELAY_SECONDS = 300
# Failures that pass on their own: credentials, network, an API server that is briefly away.
TRANSIENT_RE = re.compile(
    r"failed to retrieve access token|Unable to connect to the server|i/o timeout|connection refused"
    r"|connection reset|TLS handshake timeout|ServiceUnavailable|server is currently unable|getaddrinfo"
    r"|no such host|unexpected EOF|Internal error occurred|etcdserver|context deadline exceeded"
    r"|There was a problem refreshing your current auth tokens|Temporary failure in name resolution",
    re.I,
)
STDERR_TAIL_CHARS = 2000
FIXTURE_DISK_GB = 20
# Clusters counted at once; a 32-cluster level is otherwise minutes of serial kubectl calls per run.
TOTALS_PARALLELISM = 8
RELEASE_CHANNEL = "regular"
NODE_POOL_LABEL = "cloud.google.com/gke-nodepool"
LABELS_FIELD = "resourceLabels"
WORKLOAD_POOL_SUFFIX = ".svc.id.goog"
CONTEXT_FORMAT = "gke_{project}_{location}_{cluster}"
# Kinds the runner counts in every test cluster; the record's totals use these names.
COUNTED_KINDS = {
    "namespaces": "namespaces",
    "nodes": "nodes",
    "pods": "pods",
    "deployments": "deployments.apps",
    "services": "services",
    "hpas": "horizontalpodautoscalers.autoscaling",
    "configmaps": "configmaps",
    "events": "events",
    "pvcs": "persistentvolumeclaims",
}


class CommandError(RuntimeError):
    pass


def run(args: list[str], *, stdin: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run a command; retry it with backoff when it fails for a reason that passes.

    A day-long campaign meets expired tokens, a dropped network and API-server
    blips. Those are retried; any other failure, and a command that outlives its
    timeout, is raised at once: re-running a create that was about to finish, or
    a read that hangs, costs more than it saves.
    """
    shown = " ".join(args[:ARGS_SHOWN])
    delay = RETRY_FIRST_DELAY_SECONDS
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        LOG.debug("run: %s", " ".join(args))
        try:
            proc = subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise CommandError(f"{shown}... timed out after {timeout}s") from exc
        if proc.returncode == 0:
            return proc.stdout
        error = f"{shown}... exited {proc.returncode}: {proc.stderr.strip()[-STDERR_TAIL_CHARS:]}"
        if not TRANSIENT_RE.search(proc.stderr):
            raise CommandError(error)
        if attempt == RETRY_ATTEMPTS:
            raise CommandError(error)
        LOG.warning("transient failure (attempt %s of %s), retrying in %ss: %s", attempt, RETRY_ATTEMPTS, delay, error[:300])
        time.sleep(delay)
        delay = min(delay * 2, RETRY_MAX_DELAY_SECONDS)
    raise CommandError(f"{shown}... failed")


def use_private_kubeconfig(path: Path) -> None:
    """Copy the caller's kubeconfig to ``path`` and point every later kubectl and gcloud call at it.

    The default kubeconfig is shared with every other tool and session on the
    machine; a concurrent rewrite of it can drop a context mid-campaign. The
    copy is the runner's alone, and gcloud writes new test clusters into it.
    """
    flattened = run([KUBECTL, "config", "view", "--flatten", "--raw"])
    path.write_text(flattened)
    path.chmod(PRIVATE_KUBECONFIG_MODE)
    os.environ[KUBECONFIG_ENV] = str(path)


def context_name(project: str, location: str, cluster: str) -> str:
    return CONTEXT_FORMAT.format(project=project, location=location, cluster=cluster)


def kubectl(context: str, *args: str, stdin: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    return run([KUBECTL, "--context", context, *args], stdin=stdin, timeout=timeout)


def kubectl_json(context: str, *args: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    return json.loads(kubectl(context, *args, "-o", "json", timeout=timeout))


def apply(context: str, manifests: Iterable[dict[str, Any]]) -> int:
    """Server-side apply in chunks; returns how many objects were applied."""
    batch: list[dict[str, Any]] = []
    applied = 0
    for manifest in manifests:
        batch.append(manifest)
        if len(batch) >= APPLY_CHUNK:
            applied += _apply_batch(context, batch)
            batch = []
    if batch:
        applied += _apply_batch(context, batch)
    return applied


def _apply_batch(context: str, batch: list[dict[str, Any]]) -> int:
    body = yaml.safe_dump_all(batch, sort_keys=False)
    kubectl(
        context,
        "apply",
        "--server-side",
        "--force-conflicts",
        f"--field-manager={FIELD_MANAGER}",
        "-f",
        "-",
        stdin=body,
        timeout=APPLY_TIMEOUT_SECONDS,
    )
    return len(batch)


def count(context: str, resource: str) -> int:
    out = kubectl(context, "get", resource, "-A", "--no-headers", "-o", "name", timeout=APPLY_TIMEOUT_SECONDS)
    return sum(1 for line in out.splitlines() if line.strip())


def totals(contexts: list[str]) -> dict[str, int]:
    """Object counts by kind, summed over the test clusters, plus the cluster count."""
    result = {name: 0 for name in COUNTED_KINDS}

    def one(context: str) -> dict[str, int]:
        return {name: count(context, resource) for name, resource in COUNTED_KINDS.items()}

    with ThreadPoolExecutor(max_workers=max(1, min(TOTALS_PARALLELISM, len(contexts)))) as pool:
        for counts in pool.map(one, contexts):
            for name, value in counts.items():
                result[name] += value
    result["clusters"] = len(contexts)
    return result


def gcloud(*args: str, project: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> str:
    return run([GCLOUD, *args, f"--project={project}", "--quiet"], timeout=timeout)


def clusters_with_prefix(project: str, zone: str, prefix: str, label: str) -> list[str]:
    """Test clusters: ``<prefix>NN`` in ``zone`` that also carry ``label``.

    The runner deletes what this returns, so both the name and the label must match.
    """
    key, _, value = label.partition("=")
    out = gcloud(
        "container", "clusters", "list", f"--zone={zone}",
        f"--filter=name~^{re.escape(prefix)}[0-9]+$ AND {LABELS_FIELD}.{key}={value}",
        "--format=value(name)", project=project,
    )
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def has_context(context: str) -> bool:
    out = run([KUBECTL, "config", "get-contexts", "-o", "name"])
    return context in out.split()


def get_credentials(project: str, zone: str, cluster: str) -> None:
    """Add a cluster's kube context without changing the caller's current one.

    gcloud makes the new context current; another tool in the same shell would
    then act on a test cluster, so the previous current context is put back.
    """
    try:
        previous = run([KUBECTL, "config", "current-context"]).strip()
    except CommandError:
        previous = ""
    run([GCLOUD, "container", "clusters", "get-credentials", cluster, f"--zone={zone}", f"--project={project}"])
    if previous:
        run([KUBECTL, "config", "use-context", previous])


def cluster_labels(project: str, location: str, cluster: str) -> dict[str, str]:
    out = gcloud("container", "clusters", "describe", cluster, f"--location={location}",
                 f"--format=json({LABELS_FIELD})", project=project)
    return dict(json.loads(out or "{}").get(LABELS_FIELD) or {})


def node_pool_size(project: str, zone: str, cluster: str, pool: str) -> int | None:
    """The pool's live node count, or ``None`` when the pool does not exist."""
    try:
        gcloud(
            "container", "node-pools", "describe", pool, f"--cluster={cluster}", f"--zone={zone}",
            "--format=value(name)", project=project,
        )
    except CommandError:
        return None
    nodes = kubectl(
        context_name(project, zone, cluster), "get", "nodes", "-l",
        f"{NODE_POOL_LABEL}={pool}", "--no-headers", "-o", "name",
    )
    return sum(1 for line in nodes.splitlines() if line.strip())


def ensure_node_pool(project: str, zone: str, cluster: str, pool: str, machine_type: str, size: int) -> None:
    """Create the fixture pool at ``size`` nodes, or resize it when it exists; never create an empty one."""
    current = node_pool_size(project, zone, cluster, pool)
    if current is None and size == 0:
        return
    if current is None:
        gcloud(
            "container", "node-pools", "create", pool, f"--cluster={cluster}", f"--zone={zone}",
            f"--machine-type={machine_type}", f"--num-nodes={size}", f"--disk-size={FIXTURE_DISK_GB}",
            project=project, timeout=RESIZE_TIMEOUT_SECONDS,
        )
        return
    if current != size:
        gcloud(
            "container", "clusters", "resize", cluster, f"--node-pool={pool}", f"--zone={zone}",
            f"--num-nodes={size}", project=project, timeout=RESIZE_TIMEOUT_SECONDS,
        )


def create_cluster(project: str, zone: str, cluster: str, machine_type: str, nodes: int, labels: str) -> None:
    gcloud(
        "container", "clusters", "create", cluster, f"--zone={zone}", f"--num-nodes={nodes}",
        f"--machine-type={machine_type}", f"--release-channel={RELEASE_CHANNEL}", f"--labels={labels}",
        f"--workload-pool={project}{WORKLOAD_POOL_SUFFIX}",
        project=project, timeout=CREATE_CLUSTER_TIMEOUT_SECONDS,
    )


def delete_cluster(project: str, zone: str, cluster: str) -> None:
    gcloud("container", "clusters", "delete", cluster, f"--zone={zone}", project=project,
           timeout=CREATE_CLUSTER_TIMEOUT_SECONDS)
