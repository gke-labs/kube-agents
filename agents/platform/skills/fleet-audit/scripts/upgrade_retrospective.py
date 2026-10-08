#!/usr/bin/env python3
"""upgrade_retrospective.py — the deterministic collector behind the
`upgrade-retrospective` review (docs/designs/upgrade-failure-catalogue.md).

After a GKE upgrade nobody writes down what the upgrade did to the workloads
on the cluster, so the next cluster meets the same failure. This collector
produces the deterministic half of the retrospective:

  (A) what happened  — the `UPGRADE_MASTER` / `UPGRADE_NODES` operations in
      the window, with versions before (the ledger) and after (now);
  (B) what failed    — pods not Ready, pods Pending, nodes NotReady and the
      Warning events since the first operation, each classified against the
      catalogue by signature;
  (C) detect and mitigate next time — rendered from `MITIGATIONS`, one row
      per catalogue entry, naming the cluster's own object;
  (D) the guards     — one entry per classified failure in `guards.json`,
      which the daily readiness watch reads before the next upgrade.

A cluster is reviewed when it is *new* (absent from the ledger) or *upgraded*
(a version differs from the ledger, or an upgrade operation started since the
last run). Every other cluster is listed as unchanged. Reads that fail are
recorded under "Reads that failed" instead of failing the run: a cluster this
run could not reach is a gap the report names, not a reason to drop the rest.

Every subprocess goes through `default_run`; tests inject a fake in its place.
Nothing here writes to a cluster: `gcloud ... list`, `get-credentials` into a
private kubeconfig, and `kubectl get`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

LEDGER_VERSION = 1
GUARDS_VERSION = 1

HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "/opt/data"
# Where the ledger, the guards and the kubeconfigs live: the profile volume.
DATA_SUBDIR = "upgrade-retrospective"
LEDGER_FILENAME = "ledger.json"
GUARDS_FILENAME = "guards.json"
LATEST_REPORT_LINK = "latest.md"
# The same kubeconfig directory `collect.py` and the SOPs use, one file per
# cluster, passed per command rather than exported.
KUBECONFIG_SUBDIR = ".kubeconfigs"
KUBECONFIG_FILENAME = "kubeconfig_{project}_{cluster}_{location}.yaml"

# The first run has no ledger, so it reviews this much history.
DEFAULT_SINCE_DAYS = 14
# `--since` also takes an ISO timestamp or `<n>d`.
SINCE_DAYS_RE = re.compile(r"^(\d+)d?$")
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REPORT_DATE_FORMAT = "%Y-%m-%d"

GCLOUD_TIMEOUT_S = 120
KUBECTL_TIMEOUT_S = 120
# What `timeout(1)` exits with, so a timed-out read reads the same as one the
# shell cut off.
TIMEOUT_RC = 124
MAX_WORKERS = 4
ERROR_EXCERPT_CHARS = 300
# How much of a message a symptom keeps: enough for the signature and the
# object it names, short enough that one FailedScheduling does not fill a page.
MESSAGE_EXCERPT_CHARS = 400
# What `open(..., "w")` would create before the umask; `mkstemp` makes 0600
# and the files are read by the SOP's run and the readiness watch.
FILE_MODE = 0o666
TEMP_SUFFIX = ".tmp"

# Project discovery, mirroring `fleet_upgrade_report.py`: an explicit list in
# the environment wins, otherwise the configured project plus every project
# the credential can list.
MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
# gcloud's words for a project whose Kubernetes Engine API is off: it cannot
# hold a cluster, so its failed listing is an empty project, not a lost one.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")

UPGRADE_OPERATION_TYPES = ("UPGRADE_MASTER", "UPGRADE_NODES")
OPERATIONS_FILTER = "operationType:({types}) AND startTime>={since}"
# `targetLink` is `.../projects/<n>/(zones|locations)/<loc>/clusters/<name>[/nodePools/<pool>]`.
TARGET_LINK_RE = re.compile(r"/(?:zones|locations)/(?P<location>[^/]+)/clusters/(?P<cluster>[^/]+)(?:/nodePools/(?P<pool>[^/]+))?$")
CONTROL_PLANE_TARGET = "control plane"
# Entry 1's after-signal: a surge upgrade honours a budget for up to an hour
# per node, so an `UPGRADE_NODES` that ran longer than that per node was held.
DRAIN_HOLD_PER_NODE = timedelta(hours=1)

CLUSTER_KEY_SEPARATOR = "/"
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"
CGROUP_V2_MODE = "EFFECTIVE_CGROUP_MODE_V2"
CGROUP_V1_MODE = "EFFECTIVE_CGROUP_MODE_V1"

# Namespaces the report orders last. Not excluded: a broken kube-dns is the
# upgrade's doing as much as a broken payments-api.
SYSTEM_NAMESPACES = ("kube-system", "gmp-system")
SYSTEM_NAMESPACE_PREFIXES = ("gke-",)

# Pod phases that are not a symptom on their own.
PHASE_SUCCEEDED = "Succeeded"
PHASE_RUNNING = "Running"
PHASE_PENDING = "Pending"
CONTAINER_FAILURE_REASONS = ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError", "Error")
# Reasons a Warning event is kept for, plus any message matching
# `EVENT_MESSAGE_MARKERS` whatever its reason.
EVENT_REASONS = ("FailedScheduling", "FailedMount", "FailedAttachVolume", "BackOff", "FailedCreate", "Unhealthy", "OOMKilling")
EVENT_MESSAGE_MARKERS = ("failed calling webhook", "no matches for kind")
# Event reasons a pod's own status already carries when the pod is listed.
EVENT_REASONS_IMPLIED_BY_POD = ("FailedScheduling", "BackOff")
NODE_BAD_CONDITIONS = (("Ready", "False"), ("Ready", "Unknown"), ("NetworkUnavailable", "True"))
JOB_OWNER_KINDS = ("Job", "CronJob")
# Entry 6's best-effort markers for a Job pod that calls a removed API.
DEPRECATED_API_MARKERS = ("flowcontrol", "v1beta")

HIGH, MEDIUM = "high", "medium"
UNCLASSIFIED = "unclassified"

# Where a symptom's text comes from, for the signature table below. Each
# scope is a list of strings and a pattern matches any one of them.
SCOPE_REASON = "reason"  # the symptom's reason and each container's state reason, alone
SCOPE_SCHEDULING = "scheduling"  # a Pending pod's PodScheduled message or FailedScheduling event
SCOPE_CONTAINER = "container"  # each container's waiting/terminated message
SCOPE_ANY = "any"  # the symptom's reason and message together, and each container's

# The signature table: (catalogue entry, confidence, scope, pattern). One
# symptom can hit several entries and all are reported; the first hit per
# entry wins, so a `high` row goes before its `medium` sibling. Entries whose
# signature needs context the text does not carry (14/15 on cgroup mode and
# container count, 17 on a node-pool operation, 1 on a budget, 6 on a Job
# pod) are decided in `classify_symptom` from the constants above.
SIGNATURES = (
    (7, HIGH, SCOPE_ANY, re.compile(r"failed calling webhook")),
    (6, HIGH, SCOPE_ANY, re.compile(r"no matches for kind")),
    (19, HIGH, SCOPE_ANY, re.compile(r"PersistentVolume's node affinity")),
    (19, MEDIUM, SCOPE_REASON, re.compile(r"^FailedAttachVolume$")),
    # A FailedMount is a ConfigMap or Secret as often as a disk; only the
    # attach-shaped messages count.
    (19, MEDIUM, SCOPE_ANY, re.compile(r"^FailedMount .*(?:Unable to attach|AttachVolume|PersistentVolume|csi|timed out waiting)")),
    (12, HIGH, SCOPE_SCHEDULING, re.compile(r"didn't match (?!PersistentVolume)[^,.]*node (?:selector|affinity)")),
    (18, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient nvidia\.com/gpu")),
    (2, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient (?:cpu|memory)")),
    (20, HIGH, SCOPE_REASON, re.compile(r"^(?:ImagePullBackOff|ErrImagePull)$")),
    (18, HIGH, SCOPE_CONTAINER, re.compile(r"nvidia|CUDA|Error 803")),
)
# The four reads per cluster, in the order the report's symptoms need them.
KUBECTL_READS = (
    ("pods", ["kubectl", "get", "pods", "-A", "-o", "json"]),
    ("nodes", ["kubectl", "get", "nodes", "-o", "json"]),
    ("events", ["kubectl", "get", "events", "-A", "--field-selector", "type=Warning", "-o", "json"]),
    ("pdbs", ["kubectl", "get", "pdb", "-A", "-o", "json"]),
)
# Image references without a host are Docker Hub's.
DEFAULT_IMAGE_HOST = "docker.io"
OOM_REASON = "OOMKilled"
# How a scheduling message counts the nodes that failed each predicate; entry
# 12 is `high` only when every node failed the selector.
SCHEDULING_TOTAL_RE = re.compile(r"0/(\d+) nodes are available")
SELECTOR_MISS_COUNT_RE = re.compile(r"(\d+) node\(s\) didn't match (?!PersistentVolume)[^,.]*node (?:selector|affinity)")
WEBHOOK_NAME_RE = re.compile(r'failed calling webhook "([^"]+)"')
IMAGE_HOST_RE = re.compile(r"^([^/]+\.[^/]+|localhost(?::\d+)?)/")

# Section C's table: one row per catalogue entry, condensed from
# docs/designs/upgrade-failure-catalogue.md "The scenarios". `read_today`
# is the reader that covers the entry on `main` now.
NOTHING_SCHEDULED = "nothing scheduled; ask the assistant"
MITIGATIONS = {
    1: {
        "title": "A PodDisruptionBudget forbids the eviction",
        "before": "A budget whose disruptionsAllowed is 0 for a reason that will not clear (maxUnavailable 0, minAvailable at the replica count, or a singleton behind a budget).",
        "read_today": "the obtainability audit (`blocking-pdb`) and the readiness report",
        "mitigate_before": "Give the budget room (maxUnavailable at least 1 or minAvailable below the replica count) and a second replica, or accept the outage inside a window.",
        "mitigate_after": "Fix the budget and the stalled drain resumes; never delete the budget without replacing it.",
    },
    2: {
        "title": "No spare capacity for the displaced pods",
        "before": "A pool whose upgrade settings let a node go before its replacement exists, with requests near allocatable, the autoscaler at its ceiling or quota exhausted.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Keep the pool on maxSurge at least 1 and maxUnavailable 0, with one node of headroom and quota for the extra node.",
        "mitigate_after": "Add a node or raise the ceiling and the Pending pods schedule.",
    },
    3: {
        "title": "Every replica in one zone or on one node",
        "before": "Replicas of one Deployment on one node or in one zone, with no spread or anti-affinity.",
        "read_today": "the obtainability audit",
        "mitigate_before": "topologySpreadConstraints across zones and hosts, or anti-affinity, and a budget so the drain waits between replicas.",
        "mitigate_after": "The next rollout re-spreads the pods once the constraints are in place.",
    },
    4: {
        "title": "Data on the node is gone",
        "before": "Pods keeping state they cannot rebuild on Local SSD or emptyDir.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "State on PersistentVolumes or object storage; node-local disk only for what can be rebuilt.",
        "mitigate_after": "Restore from the source of truth.",
    },
    5: {
        "title": "Maintenance window too short, or an exclusion ends mid-roll",
        "before": "Window length against node count times drain time; an exclusion ending inside the planned change.",
        "read_today": "the readiness report and the Monday patch audit",
        "mitigate_before": "A window long enough for node count times drain time, exclusions that end outside the change, blue-green where the window is tight.",
        "mitigate_after": "Extend the window or finish the upgrade by hand so the pool stops running two versions.",
    },
    6: {
        "title": "A served API version is removed",
        "before": "Clients still calling an API version the target minor removes (the deprecation insight, audit entries labelled k8s.io/removed-release, declared apiVersions).",
        "read_today": "the GitOps scan",
        "mitigate_before": "Migrate the callers: bump client libraries and kubectl, rewrite manifests and Helm release state to the new version.",
        "mitigate_after": "The stored objects still exist; re-apply them through the new version and the clients recover.",
    },
    7: {
        "title": "A fail-closed webhook whose backend is not up",
        "before": "A webhook with failurePolicy Fail, a long timeout, a Service with no endpoints and no namespaceSelector exempting kube-system.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Exempt kube-system, a timeout of a few seconds, failurePolicy Ignore for webhooks that are not security controls, two backend replicas behind a budget.",
        "mitigate_after": "Set failurePolicy Ignore or remove the configuration to unwedge the cluster, then restore it once the backend is up.",
    },
    8: {
        "title": "A default changes in the new minor",
        "before": "The target minor's release notes read against the cluster: admission enforcement, seccomp defaults, feature gates that flip on.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Read the target minor's notes, run Pod Security Admission in warn and audit before enforce, rehearse on staging at the target version.",
        "mitigate_after": "The audit log names the rejecting rule; relabel the namespace or adjust the pod.",
    },
    9: {
        "title": "A feature is deprecated but still served",
        "before": "Deprecation warnings in API responses and audit logs (k8s.io/deprecated) for a feature with a removal date.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Plan the migration while the feature still works.",
        "mitigate_after": "None needed yet.",
    },
    10: {
        "title": "Add-on and client skew",
        "before": "Installed add-on versions against their support matrices for the target minor; a node pool further behind the control plane than the skew policy allows.",
        "read_today": "the readiness report and the Monday patch audit",
        "mitigate_before": "Upgrade add-ons to a version whose matrix includes the target before the cluster moves; keep node pools inside the skew window.",
        "mitigate_after": "Upgrade the add-on.",
    },
    11: {
        "title": "The control plane is unreachable for minutes on a zonal cluster",
        "before": "The cluster is zonal; whether its clients retry is not readable from the cluster.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "A regional cluster for anything automation depends on, and retries with backoff in the clients.",
        "mitigate_after": "Wait for the control plane; GitOps resyncs on its own.",
    },
    12: {
        "title": "A node label is removed",
        "before": "nodeSelector and affinity terms naming a label the target minor's kubelet or node image stops setting.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Replace deprecated labels in selectors with their GA names (kubernetes.io/arch, topology.kubernetes.io/zone).",
        "mitigate_after": "Patch the selector; the pods schedule.",
    },
    13: {
        "title": "The container runtime changes",
        "before": "Node agents on the CRI v1alpha2 API, images in the Docker v1 schema, DaemonSets shipping containerd 1.x configuration.",
        "read_today": "the Monday patch audit's image-type half and the compliance audit's hostpath-mount half",
        "mitigate_before": "Move agents to the CRI v1 API, rebuild v1-schema images, drop containerd 1.x overrides.",
        "mitigate_after": "The same changes under pressure; a completed pool can be downgraded in place while GKE still offers the previous version.",
    },
    14: {
        "title": "cgroup v2 under a runtime that cannot read it",
        "before": "A pool whose effectiveCgroupMode is v2 (or will be migrated at 1.33) running images with a JDK older than 8u372 or 11.0.16 or another runtime that reads cgroup v1 paths.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "A runtime that reads cgroup v2 or explicit heap flags; until 1.35 a pool can be pinned to cgroup v1 to buy time.",
        "mitigate_after": "The same, plus a temporary limit increase.",
    },
    15: {
        "title": "The OOM killer starts killing the whole container",
        "before": "A kubelet at 1.28 or later on a cgroup v2 node and containers running more than one process.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Raise the limit for multi-process containers, split workers into their own containers, or set singleProcessOOMKill in the pool's node system config.",
        "mitigate_after": "The same; the container's own logs before the upgrade show which worker used to die.",
    },
    16: {
        "title": "The network dataplane changes",
        "before": "Dataplane and DNS provider, policy count, the known issues for the target version.",
        "read_today": "the Monday audits' halves: the drift audit's datapathProvider read and the compliance audit's netpol-missing check",
        "mitigate_before": "Rehearse the target version on a staging cluster with the same dataplane; keep NetworkPolicy explicit.",
        "mitigate_after": "A completed pool can be downgraded in place while GKE still offers the previous version; the control plane cannot go back.",
    },
    17: {
        "title": "A node networking agent fails on the new image",
        "before": "The target node image's known issues; the CNI's dependence on node labels or kernel modules the image changes.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Upgrade a canary pool first and watch Service routing from inside the cluster; surge with maxUnavailable 0.",
        "mitigate_after": "Downgrade the pool to the previous version while it is offered, and fix what the CNI selected on.",
    },
    18: {
        "title": "GPU driver mismatch",
        "before": "The driver the target node image ships against the CUDA version the images need, and whether the images carry forward-compatibility libraries.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Match the driver to the images before the upgrade (GKE's driver table per version against NVIDIA's minimum-driver matrix); upgrade a canary GPU pool first.",
        "mitigate_after": "Recreate the pool with the driver version the images need.",
    },
    19: {
        "title": "In-tree volumes lose their CSI path",
        "before": "PersistentVolumes with an in-tree gcePersistentDisk spec while the PD CSI driver add-on is disabled; StorageClasses naming a retired provisioner.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Enable the PD CSI driver add-on and move StorageClasses to pd.csi.storage.gke.io.",
        "mitigate_after": "Enable the add-on; the volumes attach.",
    },
    20: {
        "title": "Images on a retired registry",
        "before": "Image references on a registry hostname that has stopped publishing (k8s.gcr.io) or that an egress allowlist no longer admits.",
        "read_today": NOTHING_SCHEDULED,
        "mitigate_before": "Mirror every image into a registry you own and keep egress allowlists in step.",
        "mitigate_after": "Retag or redirect the reference; the new nodes pull.",
    },
}
# When the cgroup mode is unknown an OOMKilled single-container pod is "14 or
# 15"; the report says so, and the guard carries the lower number.
OOM_UNDECIDED_ENTRIES = (14, 15)

REPORT_TITLE = "# Upgrade retrospective {date}"
SECTION_WHAT_HAPPENED = "### What happened"
SECTION_WHAT_FAILED = "### What failed"
SECTION_MITIGATE = "### Detect and mitigate next time"
SECTION_MITIGATION_SET_UP = "### Mitigation set up"
SECTION_UNCHANGED = "## Unchanged clusters"
SECTION_FAILED_READS = "## Reads that failed"
NONE_LINE = "_none_"


def log(msg: str) -> None:
    print(f"[upgrade_retrospective] {msg}", file=sys.stderr, flush=True)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_ts(text: str | None) -> datetime | None:
    """RFC 3339 as GKE and Kubernetes write it, with or without fractional seconds."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def hermes_home() -> Path:
    return Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME)


def data_dir() -> Path:
    return hermes_home() / DATA_SUBDIR


def cluster_key(project: str, location: str, name: str) -> str:
    return CLUSTER_KEY_SEPARATOR.join((project, location, name))


def is_system_namespace(namespace: str) -> bool:
    return namespace in SYSTEM_NAMESPACES or namespace.startswith(SYSTEM_NAMESPACE_PREFIXES)


# --------------------------------------------------------------------------- #
# The one subprocess seam.
# --------------------------------------------------------------------------- #


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]


def _text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def default_run(argv: list[str], *, timeout: int = GCLOUD_TIMEOUT_S, env: dict | None = None) -> Run:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:  # a missing binary, a bad env
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def _excerpt(result: Run) -> str:
    return result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"


def run_json(argv: list[str], *, run: RunFn, timeout: int = GCLOUD_TIMEOUT_S, env: dict | None = None) -> tuple[object | None, str | None]:
    """Parsed JSON stdout, or the error that stands for it."""
    result = run(argv, timeout=timeout, env=env)
    if result.rc != 0:
        return None, f"{argv[0]} {argv[1]} {argv[2]} rc={result.rc}: {_excerpt(result)}"
    try:
        return json.loads(result.stdout or "null"), None
    except json.JSONDecodeError as exc:
        return None, f"{argv[0]} {argv[1]} {argv[2]} returned non-JSON: {exc}"


# --------------------------------------------------------------------------- #
# Projects, clusters, operations.
# --------------------------------------------------------------------------- #


def discover_projects(*, run: RunFn) -> tuple[list[str], list[str]]:
    """The projects to enumerate when `--project` is absent, and the reads
    that failed on the way. An explicit `MONITORED_PROJECT_IDS` wins; otherwise
    the configured project plus every project the credential can list."""
    errors: list[str] = []
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    projects = set(monitored)
    for var in PROJECT_ENV_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            projects.add(value)
    if monitored:
        return sorted(projects), errors
    result = run(["gcloud", "config", "get-value", "project"])
    if result.rc == 0 and result.stdout.strip():
        projects.add(result.stdout.strip())
    result = run(["gcloud", "projects", "list", "--format", "value(projectId)"])
    if result.rc != 0:
        errors.append(f"gcloud projects list rc={result.rc}: {_excerpt(result)}; only the configured project was read")
    else:
        projects |= {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not projects:
        errors.append("project discovery named no project: no `--project`, no project variable, no configured project")
    return sorted(projects), errors


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], str | None]:
    """Every cluster in `project` as the raw `clusters list` item plus
    `project`; a project whose GKE API is off is empty rather than failed."""
    result = run(["gcloud", "container", "clusters", "list", "--project", project, "--format", "json"])
    if result.rc != 0:
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            return [], None
        return [], f"clusters list rc={result.rc}: {_excerpt(result)}"
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return [], f"clusters list returned non-JSON: {exc}"
    if not isinstance(clusters, list):
        return [], "clusters list returned JSON that is not a list"
    for cluster in clusters:
        cluster["project"] = project
        cluster["location"] = cluster.get("location") or cluster.get("zone") or ""
    return clusters, None


def list_operations(project: str, since: datetime, *, run: RunFn) -> tuple[list[dict], str | None]:
    """The upgrade operations in `project` that started at or after `since`.
    The filter is also applied here, so a gcloud that ignores it changes nothing."""
    flt = OPERATIONS_FILTER.format(types=" OR ".join(UPGRADE_OPERATION_TYPES), since=fmt_ts(since))
    parsed, error = run_json(["gcloud", "container", "operations", "list", "--project", project, "--filter", flt, "--format", "json"], run=run)
    if error:
        return [], error
    if not isinstance(parsed, list):
        return [], "operations list returned JSON that is not a list"
    ops = []
    for op in parsed:
        if op.get("operationType") not in UPGRADE_OPERATION_TYPES:
            continue
        start = parse_ts(op.get("startTime"))
        if start is None or start < since:
            continue
        ops.append(op)
    return ops, None


def parse_target_link(link: str) -> tuple[str, str, str | None] | None:
    m = TARGET_LINK_RE.search(link or "")
    if not m:
        return None
    return m.group("location"), m.group("cluster"), m.group("pool")


def operation_summary(op: dict) -> dict:
    start, end = parse_ts(op.get("startTime")), parse_ts(op.get("endTime"))
    target = parse_target_link(op.get("targetLink", ""))
    error = op.get("error") or {}
    error_text = error.get("message") if isinstance(error, dict) else str(error)
    return {
        "name": op.get("name"),
        "type": op.get("operationType"),
        "target": target[2] if target and target[2] else CONTROL_PLANE_TARGET,
        "start": fmt_ts(start) if start else None,
        "end": fmt_ts(end) if end else None,
        "duration_s": int((end - start).total_seconds()) if start and end else None,
        "status": op.get("status"),
        "error": error_text or op.get("statusMessage") or None,
    }


def versions_of(cluster: dict) -> dict:
    return {
        "control_plane": cluster.get("currentMasterVersion") or "",
        "node_pools": {p.get("name", ""): p.get("version", "") for p in cluster.get("nodePools") or []},
        "channel": (cluster.get("releaseChannel") or {}).get("channel") or "",
    }


def pool_cgroup_modes(cluster: dict) -> dict[str, str]:
    return {p.get("name", ""): ((p.get("config") or {}).get("effectiveCgroupMode") or "") for p in cluster.get("nodePools") or []}


# --------------------------------------------------------------------------- #
# Ledger and selection.
# --------------------------------------------------------------------------- #


def load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        log(f"WARNING: {path} unreadable ({exc}); starting from empty")
        return default
    return loaded if isinstance(loaded, dict) else default


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "updated_at": None, "clusters": {}}


def empty_guards() -> dict:
    return {"version": GUARDS_VERSION, "updated_at": None, "guards": []}


def write_json_atomically(path: Path, doc: object) -> None:
    """A temporary file beside `path`, fsynced, renamed over it: a reader sees
    the whole document or the previous one, never a splice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX)
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(temporary, FILE_MODE & ~umask)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(doc, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class Selection(NamedTuple):
    cluster: dict
    key: str
    status: str  # "new" | "upgraded" | "forced"
    reasons: list[str]
    window_start: datetime
    operations: list[dict]


def select_clusters(clusters: list[dict], ledger: dict, operations: list[dict], *, since: datetime, forced: set[str]) -> tuple[list[Selection], list[dict]]:
    """Which clusters this run reviews, and why; the rest as unchanged rows."""
    by_target: dict[tuple[str, str], list[dict]] = {}
    for op in operations:
        target = parse_target_link(op.get("targetLink", ""))
        if target and op.get("operationType") in UPGRADE_OPERATION_TYPES:
            by_target.setdefault((target[0], target[1]), []).append(op)
    selected, unchanged = [], []
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        entry = (ledger.get("clusters") or {}).get(key)
        if entry is not None and not entry.get("last_run"):
            # Enumerated once but never reviewed (its reads failed): still new.
            entry = None
        current = versions_of(cluster)
        reasons: list[str] = []
        window_start = since
        if entry is None:
            status = "new"
            reasons.append("first seen")
        else:
            status = "upgraded"
            last_run = parse_ts(entry.get("last_run")) or since
            window_start = min(last_run, since) if key in forced else last_run
            if entry.get("control_plane") != current["control_plane"]:
                reasons.append(f"control plane {entry.get('control_plane')} -> {current['control_plane']}")
            for pool, version in current["node_pools"].items():
                previous = (entry.get("node_pools") or {}).get(pool)
                if previous != version:
                    reasons.append(f"node pool {pool} {previous or 'absent'} -> {version}")
        ops = sorted(
            (op for op in by_target.get((cluster["location"], cluster["name"]), []) if (parse_ts(op.get("startTime")) or since) >= window_start),
            key=lambda op: op.get("startTime") or "",
        )
        if entry is not None:
            # Only an operation since the last run makes a known cluster
            # "upgraded"; a forced review widens the window without that.
            last_run = parse_ts(entry.get("last_run")) or since
            recent = [op for op in ops if (parse_ts(op.get("startTime")) or since) >= last_run]
            if recent:
                reasons.append(f"{len(recent)} upgrade operation(s) since {fmt_ts(last_run)}")
        if key in forced:
            status = status if reasons else "forced"
            reasons.append("forced by --cluster")
        if not reasons:
            unchanged.append({"cluster": key, "control_plane": current["control_plane"], "last_run": entry.get("last_run") if entry else None})
            continue
        selected.append(Selection(cluster, key, status, reasons, window_start, ops))
    unchanged.sort(key=lambda row: row["cluster"])
    return selected, unchanged


def what_happened(selection: Selection, ledger: dict) -> dict:
    cluster, current = selection.cluster, versions_of(selection.cluster)
    entry = (ledger.get("clusters") or {}).get(selection.key) or {}
    before = {"control_plane": entry.get("control_plane"), "node_pools": entry.get("node_pools") or {}} if entry else None
    return {
        "status": selection.status,
        "reasons": selection.reasons,
        "window_start": fmt_ts(selection.window_start),
        "channel": current["channel"],
        "versions_before": before,
        "versions_after": {"control_plane": current["control_plane"], "node_pools": current["node_pools"]},
        "cluster_status": cluster.get("status"),
        "operations": [operation_summary(op) for op in selection.operations],
    }


# --------------------------------------------------------------------------- #
# The cluster read.
# --------------------------------------------------------------------------- #


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return hermes_home() / KUBECONFIG_SUBDIR / KUBECONFIG_FILENAME.format(project=project, cluster=cluster, location=location)


def fetch_credentials(cluster: dict, *, run: RunFn) -> tuple[Path, str | None]:
    kc = kubeconfig_path(cluster["project"], cluster["name"], cluster["location"])
    kc.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(["gcloud", "container", "clusters", "get-credentials", cluster["name"], "--location", cluster["location"], "--project", cluster["project"]], env=env)
    if result.rc != 0:
        return kc, f"get-credentials rc={result.rc}: {_excerpt(result)}"
    return kc, None


def read_cluster(kubeconfig: Path, *, run: RunFn) -> tuple[dict[str, list], list[str]]:
    """The four lists, each `[]` when its read failed, plus the failures."""
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    out: dict[str, list] = {}
    errors: list[str] = []
    for name, argv in KUBECTL_READS:
        parsed, error = run_json(argv, run=run, timeout=KUBECTL_TIMEOUT_S, env=env)
        if error:
            errors.append(f"{name}: {error}")
            out[name] = []
            continue
        out[name] = (parsed.get("items") or []) if isinstance(parsed, dict) else []
    return out, errors


# --------------------------------------------------------------------------- #
# Symptoms: pure functions over the parsed lists.
# --------------------------------------------------------------------------- #


def _object_ref(namespace: str, kind: str, name: str) -> str:
    return f"{namespace}/{kind}/{name}" if namespace else f"{kind}/{name}"


def _condition(obj: dict, kind: str) -> dict:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond
    return {}


def _pod_text(pod: dict) -> str:
    """The text entry 6's best-effort markers are searched in: names, images,
    command, args and env values of every container."""
    parts = [pod["metadata"].get("name", "")]
    for owner in pod["metadata"].get("ownerReferences") or []:
        parts.append(owner.get("name", ""))
    for container in (pod.get("spec") or {}).get("containers") or []:
        parts.extend([container.get("name", ""), container.get("image", "")])
        parts.extend(container.get("command") or [])
        parts.extend(container.get("args") or [])
        parts.extend(str(e.get("value") or "") for e in container.get("env") or [])
    return " ".join(parts)


def _owner_kind(pod: dict) -> str:
    owners = pod["metadata"].get("ownerReferences") or []
    return owners[0].get("kind", "") if owners else ""


def pod_symptoms(pods: list[dict]) -> list[dict]:
    out = []
    for pod in pods:
        meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
        phase = status.get("phase")
        ready = _condition(pod, "Ready").get("status") == "True"
        if phase == PHASE_SUCCEEDED or (phase == PHASE_RUNNING and ready):
            continue
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        containers = spec.get("containers") or []
        images = [c.get("image", "") for c in containers]
        base = {
            "kind": "Pod",
            "namespace": namespace,
            "name": name,
            "object": _object_ref(namespace, "Pod", name),
            "system": is_system_namespace(namespace),
            "node": spec.get("nodeName"),
            "phase": phase,
            "owner_kind": _owner_kind(pod),
            "container_count": len(containers),
            "images": images,
            "labels": meta.get("labels") or {},
            "node_selector": spec.get("nodeSelector") or {},
            "spec_text": _pod_text(pod),
            "since": status.get("startTime") or meta.get("creationTimestamp"),
        }
        scheduled = _condition(pod, "PodScheduled")
        if phase == PHASE_PENDING and scheduled.get("status") == "False":
            out.append({**base, "category": "pending", "reason": scheduled.get("reason") or "Unschedulable", "message": (scheduled.get("message") or "")[:MESSAGE_EXCERPT_CHARS]})
            continue
        container_reasons = []
        for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            for key in ("state", "lastState"):
                for state_name, state in (cs.get(key) or {}).items():
                    if state_name == "running":
                        continue
                    reason = state.get("reason") or ""
                    if reason or (state_name == "terminated" and state.get("exitCode") not in (None, 0)):
                        container_reasons.append({"container": cs.get("name"), "where": key, "reason": reason or state_name, "message": (state.get("message") or "")[:MESSAGE_EXCERPT_CHARS], "exit_code": state.get("exitCode"), "finished_at": state.get("finishedAt")})
        # The catalogue's reasons lead, so the row's headline is the one a
        # signature reads rather than whichever container came first.
        container_reasons.sort(key=lambda c: c["reason"] not in CONTAINER_FAILURE_REASONS)
        reason = container_reasons[0]["reason"] if container_reasons else (status.get("reason") or ("NotReady" if phase == PHASE_RUNNING else phase or "NotReady"))
        message = container_reasons[0]["message"] if container_reasons else (status.get("message") or "")[:MESSAGE_EXCERPT_CHARS]
        out.append({**base, "category": "not-ready", "reason": reason, "message": message, "containers": container_reasons})
    return out


def node_symptoms(nodes: list[dict]) -> list[dict]:
    out = []
    for node in nodes:
        meta, conditions = node.get("metadata") or {}, {c.get("type"): c for c in (node.get("status") or {}).get("conditions") or []}
        for kind, bad in NODE_BAD_CONDITIONS:
            cond = conditions.get(kind)
            if cond and cond.get("status") == bad:
                name = meta.get("name", "")
                out.append({
                    "kind": "Node",
                    "namespace": "",
                    "name": name,
                    "object": _object_ref("", "Node", name),
                    "system": True,
                    "category": "node",
                    "node": name,
                    "pool": (meta.get("labels") or {}).get(NODEPOOL_LABEL, ""),
                    "reason": f"{kind}={bad}" if kind != "Ready" else ("NotReady" if bad == "False" else "Unknown"),
                    "message": (cond.get("message") or cond.get("reason") or "")[:MESSAGE_EXCERPT_CHARS],
                    "since": cond.get("lastTransitionTime"),
                })
    return out


def _event_time(event: dict) -> datetime | None:
    for key in ("lastTimestamp", "eventTime"):
        ts = parse_ts(event.get(key))
        if ts:
            return ts
    series = event.get("series") or {}
    return parse_ts(series.get("lastObservedTime")) or parse_ts((event.get("metadata") or {}).get("creationTimestamp"))


def event_symptoms(events: list[dict], window_start: datetime) -> list[dict]:
    """Warning events since `window_start`, one row per (object, reason)."""
    rows: dict[tuple, dict] = {}
    for event in events:
        if event.get("type") and event.get("type") != "Warning":
            continue
        message = event.get("message") or ""
        reason = event.get("reason") or ""
        if reason not in EVENT_REASONS and not any(marker in message for marker in EVENT_MESSAGE_MARKERS):
            continue
        last = _event_time(event)
        if last is None or last < window_start:
            continue
        obj = event.get("involvedObject") or {}
        namespace, kind, name = obj.get("namespace") or "", obj.get("kind") or "", obj.get("name") or ""
        key = (namespace, kind, name, reason)
        row = rows.get(key)
        if row is None:
            rows[key] = row = {
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "object": _object_ref(namespace, kind, name),
                "system": is_system_namespace(namespace) if namespace else True,
                "category": "event",
                "reason": reason,
                "message": message[:MESSAGE_EXCERPT_CHARS],
                "count": 0,
                "first_seen": fmt_ts(parse_ts(event.get("firstTimestamp")) or last),
                "last_seen": fmt_ts(last),
            }
        row["count"] += int(event.get("count") or 1)
        if fmt_ts(last) > row["last_seen"]:
            row["last_seen"], row["message"] = fmt_ts(last), message[:MESSAGE_EXCERPT_CHARS]
    return list(rows.values())


def _selector_matches(selector: dict, labels: dict) -> bool:
    match = (selector or {}).get("matchLabels") or {}
    return bool(match) and all(labels.get(k) == v for k, v in match.items())


def pdb_symptoms(pdbs: list[dict], pods: list[dict], nodes: list[dict], upgraded_pools: dict[str, dict]) -> list[dict]:
    """Entry 1's after-signal: a budget allowing no disruption whose pods sit
    on a pool an `UPGRADE_NODES` touched in the window."""
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    out = []
    for pdb in pdbs:
        meta, status = pdb.get("metadata") or {}, pdb.get("status") or {}
        if status.get("disruptionsAllowed") != 0:
            continue
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        selector = (pdb.get("spec") or {}).get("selector") or {}
        covered = [p for p in pods if (p.get("metadata") or {}).get("namespace") == namespace and _selector_matches(selector, (p.get("metadata") or {}).get("labels") or {})]
        pools = sorted({node_pool.get((p.get("spec") or {}).get("nodeName"), "") for p in covered} - {""})
        touched = [pool for pool in pools if pool in upgraded_pools]
        if not touched:
            # A budget nothing drained in the window is the readiness
            # report's before-signal, not this report's failure.
            continue
        out.append({
            "kind": "PodDisruptionBudget",
            "namespace": namespace,
            "name": name,
            "object": _object_ref(namespace, "PodDisruptionBudget", name),
            "system": is_system_namespace(namespace),
            "category": "pdb",
            "reason": "disruptionsAllowed=0",
            "message": f"spec={json.dumps({k: v for k, v in (pdb.get('spec') or {}).items() if k != 'selector'}, sort_keys=True)} pods={len(covered)} pools={pools or 'none'}",
            "pools": pools,
            "upgraded_pools": touched,
        })
    return out


def upgraded_pools_of(operations: list[dict], nodes: list[dict]) -> dict[str, dict]:
    """Node pools an `UPGRADE_NODES` touched in the window, with the longest
    operation's duration and the pool's current node count."""
    counts: dict[str, int] = {}
    for node in nodes:
        pool = ((node.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "")
        counts[pool] = counts.get(pool, 0) + 1
    pools: dict[str, dict] = {}
    for op in operations:
        if op.get("operationType") != "UPGRADE_NODES":
            continue
        target = parse_target_link(op.get("targetLink", ""))
        if not target or not target[2]:
            continue
        summary = operation_summary(op)
        row = pools.setdefault(target[2], {"nodes": counts.get(target[2], 0), "longest_s": 0, "operation": summary["name"], "start": summary["start"]})
        if (summary["duration_s"] or 0) > row["longest_s"]:
            row.update(longest_s=summary["duration_s"] or 0, operation=summary["name"], start=summary["start"])
    return pools


# --------------------------------------------------------------------------- #
# The classifier.
# --------------------------------------------------------------------------- #


class Context(NamedTuple):
    cgroup_modes: dict[str, str]  # pool -> effectiveCgroupMode
    node_pool: dict[str, str]  # node -> pool
    upgraded_pools: dict[str, dict]  # pool -> what `upgraded_pools_of` returns


def _classification(entry: int | None, confidence: str, evidence: str, detail: str = "") -> dict:
    return {
        "entry": entry,
        "title": MITIGATIONS[entry]["title"] if entry else UNCLASSIFIED,
        "confidence": confidence,
        "evidence": evidence[:MESSAGE_EXCERPT_CHARS],
        "detail": detail,
    }


def _scopes_of(symptom: dict) -> dict[str, list[str]]:
    reason, message = symptom.get("reason") or "", symptom.get("message") or ""
    containers = symptom.get("containers") or []
    scopes = {
        SCOPE_REASON: [reason] + [c["reason"] for c in containers],
        SCOPE_ANY: [f"{reason} {message}".strip()] + [f"{c['reason']} {c['message']}".strip() for c in containers],
    }
    if symptom["category"] == "pending" or (symptom["category"] == "event" and reason == "FailedScheduling"):
        scopes[SCOPE_SCHEDULING] = [message]
    if symptom["category"] == "not-ready":
        scopes[SCOPE_CONTAINER] = [c["message"] for c in containers if c["message"]]
    return scopes


def _selector_detail(symptom: dict) -> str:
    selector = symptom.get("node_selector") or {}
    return "selector " + ",".join(f"{k}={v}" for k, v in sorted(selector.items())) if selector else "node affinity"


def _image_host(image: str) -> str:
    m = IMAGE_HOST_RE.match(image)
    return m.group(1) if m else DEFAULT_IMAGE_HOST


def _image_host_detail(symptom: dict) -> str:
    hosts = sorted({_image_host(image) for image in symptom.get("images") or []})
    return "image host " + ", ".join(hosts) if hosts else ""


def classify_symptom(symptom: dict, ctx: Context) -> list[dict]:
    found: list[dict] = []
    seen: set = set()

    def add(entry, confidence, evidence, detail=""):
        if entry in seen:
            return
        seen.add(entry)
        found.append(_classification(entry, confidence, evidence, detail))

    scopes = _scopes_of(symptom)
    for entry, confidence, scope, pattern in SIGNATURES:
        m, text = None, ""
        for text in scopes.get(scope) or []:
            m = pattern.search(text)
            if m:
                break
        if not m:
            continue
        detail = ""
        if entry == 12:
            total, missed = SCHEDULING_TOTAL_RE.search(text), SELECTOR_MISS_COUNT_RE.search(text)
            if total and missed and int(missed.group(1)) < int(total.group(1)):
                confidence = MEDIUM
            detail = _selector_detail(symptom)
        elif entry == 7:
            name = WEBHOOK_NAME_RE.search(text)
            detail = f"webhook {name.group(1)}" if name else ""
        elif entry == 20:
            detail = _image_host_detail(symptom)
        # A reason-only hit is evidenced by the reason with its message.
        evidence = scopes[SCOPE_ANY][0] if scope == SCOPE_REASON else (text if scope == SCOPE_ANY else m.group(0))
        add(entry, confidence, evidence, detail)

    # OOMKilled: 14 on a cgroup v2 pool and one container, 15 when the pod
    # runs several containers, "14 or 15" when the mode is unknown.
    containers = symptom.get("containers") or []
    oom = [c for c in containers if c["reason"] == OOM_REASON]
    if oom:
        pool = ctx.node_pool.get(symptom.get("node") or "", "")
        mode = ctx.cgroup_modes.get(pool, "")
        evidence = f"container {oom[0]['container']} {OOM_REASON} exit {oom[0]['exit_code']} (pool {pool or '?'} {mode or 'cgroup mode unknown'})"
        if symptom.get("container_count", 1) > 1:
            add(15, MEDIUM, evidence, f"{symptom['container_count']} containers")
        elif mode == CGROUP_V2_MODE:
            add(14, MEDIUM, evidence, "one container on cgroup v2")
        elif mode == CGROUP_V1_MODE:
            add(None, MEDIUM, evidence, "OOMKilled on cgroup v1: neither 14 nor 15")
        else:
            add(OOM_UNDECIDED_ENTRIES[0], MEDIUM, evidence, f"{OOM_UNDECIDED_ENTRIES[0]} or {OOM_UNDECIDED_ENTRIES[1]}: cgroup mode unknown")

    # Entry 17: a node NotReady / NetworkUnavailable, high when its pool was
    # upgraded in the window.
    if symptom["category"] == "node":
        pool = symptom.get("pool") or ""
        touched = ctx.upgraded_pools.get(pool)
        add(17, HIGH if touched else MEDIUM, f"node {symptom['name']} {symptom['reason']}" + (f" after UPGRADE_NODES on {pool} at {touched['start']}" if touched else ""), f"pool {pool}")

    # Entry 1: a budget allowing no disruption on an upgraded pool, or an
    # upgrade that ran longer than the hour-per-node a drain is held.
    if symptom["category"] == "pdb":
        for pool in symptom.get("upgraded_pools") or []:
            info = ctx.upgraded_pools[pool]
            held = info["longest_s"] > info["nodes"] * DRAIN_HOLD_PER_NODE.total_seconds() and info["nodes"] > 0
            add(1, HIGH if held else MEDIUM, f"disruptionsAllowed=0 with pods on {pool}; UPGRADE_NODES {info['operation']} took {info['longest_s'] // 60} min over {info['nodes']} node(s)", f"budget {symptom['name']}")

    # Entry 6: a Job pod in Error whose spec names a removed API, best effort.
    if symptom["category"] == "not-ready" and symptom.get("owner_kind") in JOB_OWNER_KINDS:
        hits = [marker for marker in DEPRECATED_API_MARKERS if marker in (symptom.get("spec_text") or "")]
        if hits and any(c["reason"] == "Error" for c in containers):
            add(6, MEDIUM, f"{symptom['owner_kind']} pod in Error; spec mentions {', '.join(hits)}", "best effort: a name, not an API call")

    if not found:
        found.append(_classification(None, MEDIUM, f"{symptom.get('reason') or ''} {symptom.get('message') or ''}".strip()))
    return found


def collect_symptoms(cluster: dict, reads: dict[str, list], operations: list[dict], window_start: datetime) -> list[dict]:
    """Every symptom in the read, each with its classifications, user
    namespaces first."""
    nodes = reads.get("nodes") or []
    upgraded = upgraded_pools_of(operations, nodes)
    ctx = Context(
        cgroup_modes=pool_cgroup_modes(cluster),
        node_pool={(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes},
        upgraded_pools=upgraded,
    )
    pods = pod_symptoms(reads.get("pods") or [])
    pod_objects = {s["object"] for s in pods}
    # A FailedScheduling or BackOff event on a pod the pod list already
    # reports as Pending or crash-looping says the same thing twice.
    events = [e for e in event_symptoms(reads.get("events") or [], window_start) if not (e["reason"] in EVENT_REASONS_IMPLIED_BY_POD and e["object"] in pod_objects)]
    symptoms = pods + node_symptoms(nodes) + events + pdb_symptoms(reads.get("pdbs") or [], reads.get("pods") or [], nodes, upgraded)
    for symptom in symptoms:
        symptom["classifications"] = classify_symptom(symptom, ctx)
    symptoms.sort(key=lambda s: (s["system"], s["namespace"], s["kind"], s["name"], s.get("reason") or ""))
    return symptoms


# --------------------------------------------------------------------------- #
# Sections C and D.
# --------------------------------------------------------------------------- #


def mitigation_lines(symptom: dict, classification: dict) -> dict:
    entry = classification["entry"]
    row = MITIGATIONS[entry]
    subject = symptom["object"] + (f" ({classification['detail']})" if classification.get("detail") else "")
    return {
        "entry": entry,
        "title": row["title"],
        "object": symptom["object"],
        "before_signal": f"For {subject}: {row['before']}",
        "read_today": row["read_today"],
        "mitigate_before": row["mitigate_before"],
        "mitigate_after": row["mitigate_after"],
    }


def guard_id(cluster: str, entry: int, obj: str) -> str:
    return f"{cluster}#{entry}#{obj}"


def guards_for(key: str, symptoms: list[dict], seen_at: str) -> list[dict]:
    out: dict[str, dict] = {}
    for symptom in symptoms:
        for c in symptom["classifications"]:
            if c["entry"] is None:
                continue
            gid = guard_id(key, c["entry"], symptom["object"])
            if gid in out:
                continue
            out[gid] = {
                "id": gid,
                "cluster": key,
                "entry": c["entry"],
                "title": c["title"],
                "object": symptom["object"],
                "confidence": c["confidence"],
                "evidence": c["evidence"],
                "first_seen": seen_at,
                "last_seen": seen_at,
            }
    return list(out.values())


def merge_guards(existing: dict, fresh: list[dict], reviewed: set[str], seen_at: str) -> dict:
    """Keep what was there, refresh what is seen again, drop what a reviewed
    cluster no longer shows. A cluster this run did not review keeps its guards."""
    by_id = {g["id"]: g for g in existing.get("guards") or [] if isinstance(g, dict) and g.get("id")}
    fresh_ids = {g["id"] for g in fresh}
    merged = []
    for gid, guard in by_id.items():
        if guard.get("cluster") in reviewed and gid not in fresh_ids:
            continue
        merged.append(guard)
    for guard in fresh:
        old = by_id.get(guard["id"])
        if old:
            old.update(last_seen=seen_at, evidence=guard["evidence"], confidence=guard["confidence"])
        else:
            merged.append(guard)
    merged.sort(key=lambda g: (g["cluster"], g["entry"], g["object"]))
    return {"version": GUARDS_VERSION, "updated_at": seen_at, "guards": merged}


# --------------------------------------------------------------------------- #
# The review of one cluster.
# --------------------------------------------------------------------------- #


def review_cluster(selection: Selection, ledger: dict, *, run: RunFn, seen_at: str) -> dict:
    cluster = selection.cluster
    review = {
        "cluster": selection.key,
        "project": cluster["project"],
        "location": cluster["location"],
        "name": cluster["name"],
        "what_happened": what_happened(selection, ledger),
        "what_failed": [],
        "mitigations": [],
        "guards": [],
        "read_errors": [],
        "reviewed": False,
    }
    ops = selection.operations
    first_op = min((parse_ts(op.get("startTime")) for op in ops if parse_ts(op.get("startTime"))), default=None)
    window_start = first_op or selection.window_start
    review["what_happened"]["symptom_window_start"] = fmt_ts(window_start)
    kubeconfig, error = fetch_credentials(cluster, run=run)
    if error:
        review["read_errors"].append(error)
        return review
    reads, errors = read_cluster(kubeconfig, run=run)
    review["read_errors"].extend(errors)
    if len(errors) == len(KUBECTL_READS):
        return review
    review["reviewed"] = True
    symptoms = collect_symptoms(cluster, reads, ops, window_start)
    review["what_failed"] = symptoms
    review["mitigations"] = [mitigation_lines(s, c) for s in symptoms for c in s["classifications"] if c["entry"] is not None]
    review["guards"] = guards_for(selection.key, symptoms, seen_at)
    return review


# --------------------------------------------------------------------------- #
# Rendering.
# --------------------------------------------------------------------------- #


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"


def _versions_table(before: dict | None, after: dict) -> list[str]:
    lines = ["| Target | Before | After |", "| --- | --- | --- |"]
    prev_cp = (before or {}).get("control_plane") or "-"
    lines.append(f"| {CONTROL_PLANE_TARGET} | {prev_cp} | {after['control_plane']} |")
    for pool, version in sorted(after["node_pools"].items()):
        lines.append(f"| pool `{pool}` | {((before or {}).get('node_pools') or {}).get(pool) or '-'} | {version} |")
    return lines


def render_report(result: dict) -> str:
    generated = parse_ts(result["generated_at"]) or now_utc()
    lines = [REPORT_TITLE.format(date=generated.strftime(REPORT_DATE_FORMAT)), ""]
    lines.append(f"Window since {result['since']}; projects: {', '.join(result['projects']) or 'none'}; generated {result['generated_at']}.")
    lines.append("")
    for review in result["reviews"]:
        wh = review["what_happened"]
        lines += [f"## {review['cluster']}", "", SECTION_WHAT_HAPPENED, ""]
        lines.append(f"{wh['status']} ({'; '.join(wh['reasons'])}); channel {wh['channel'] or 'none'}; cluster status {wh['cluster_status']}.")
        lines.append("")
        lines += _versions_table(wh["versions_before"], wh["versions_after"])
        lines.append("")
        if wh["operations"]:
            lines += ["| Operation | Target | Start | End | Duration | Status | Error |", "| --- | --- | --- | --- | --- | --- | --- |"]
            for op in wh["operations"]:
                lines.append(f"| {op['type']} | {op['target']} | {op['start'] or '-'} | {op['end'] or '-'} | {_duration(op['duration_s'])} | {op['status']} | {op['error'] or ''} |")
        else:
            lines.append("No upgrade operation in the window.")
        lines += ["", SECTION_WHAT_FAILED, ""]
        if review["read_errors"] and not review["reviewed"]:
            lines.append(f"Not read: {'; '.join(review['read_errors'])}")
        elif not review["what_failed"]:
            lines.append(NONE_LINE)
        else:
            lines += ["| Object | Symptom | Catalogue entry | Confidence | Evidence |", "| --- | --- | --- | --- | --- |"]
            for symptom in review["what_failed"]:
                for c in symptom["classifications"]:
                    entry = f"{c['entry']}. {c['title']}" if c["entry"] else UNCLASSIFIED
                    if c.get("detail"):
                        entry += f" ({c['detail']})"
                    system = " (system)" if symptom["system"] else ""
                    lines.append(f"| `{symptom['object']}`{system} | {symptom['reason']} | {entry} | {c['confidence']} | {c['evidence'].replace('|', '/').replace(chr(10), ' ')} |")
            if review["read_errors"]:
                lines += ["", f"Partial read: {'; '.join(review['read_errors'])}"]
        lines += ["", SECTION_MITIGATE, ""]
        if not review["mitigations"]:
            lines.append(NONE_LINE)
        for m in review["mitigations"]:
            lines.append(f"- **{m['entry']}. {m['title']}** — {m['before_signal']} Read today: {m['read_today']}. Mitigate before: {m['mitigate_before']} Mitigate after: {m['mitigate_after']}")
        lines += ["", SECTION_MITIGATION_SET_UP, ""]
        if not review["guards"]:
            lines.append(NONE_LINE)
        for g in review["guards"]:
            lines.append(f"- guard `{g['object']}` entry {g['entry']} ({g['confidence']}), first seen {g['first_seen']}")
        lines.append("")
    lines += [SECTION_UNCHANGED, ""]
    if not result["unchanged"]:
        lines.append(NONE_LINE)
    for row in result["unchanged"]:
        lines.append(f"- {row['cluster']} at {row['control_plane']}, last reviewed {row['last_run'] or 'never'}")
    lines += ["", SECTION_FAILED_READS, ""]
    failed = list(result["failed_reads"]) + [f"{r['cluster']}: {e}" for r in result["reviews"] for e in r["read_errors"]]
    if not failed:
        lines.append(NONE_LINE)
    for line in failed:
        lines.append(f"- {line}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The run.
# --------------------------------------------------------------------------- #


def parse_since(text: str | None, now: datetime) -> datetime:
    if not text:
        return now - timedelta(days=DEFAULT_SINCE_DAYS)
    m = SINCE_DAYS_RE.match(text.strip())
    if m:
        return now - timedelta(days=int(m.group(1)))
    parsed = parse_ts(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"--since takes <days>, <days>d or an RFC 3339 timestamp, not {text!r}")
    return parsed


def ledger_after(ledger: dict, reviews: list[dict], clusters: list[dict], seen_at: str) -> dict:
    """Every enumerated cluster's current versions; `last_run` moves only for
    a cluster this run reviewed, so a failed read is retried next time."""
    entries = dict((ledger.get("clusters") or {}))
    reviewed = {r["cluster"] for r in reviews if r["reviewed"]}
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        current = versions_of(cluster)
        old = entries.get(key) or {}
        if key in reviewed or not old:
            entries[key] = {
                "control_plane": current["control_plane"],
                "node_pools": current["node_pools"],
                "channel": current["channel"],
                "first_seen": old.get("first_seen") or seen_at,
                "last_run": seen_at if key in reviewed else old.get("last_run"),
            }
    return {"version": LEDGER_VERSION, "updated_at": seen_at, "clusters": entries}


def collect(args: argparse.Namespace, *, run: RunFn | None = None, now: datetime | None = None) -> dict:
    # Resolved at call time, so a test that patches `default_run` is honoured.
    run = run or default_run
    now = now or now_utc()
    seen_at = fmt_ts(now)
    since = parse_since(args.since, now)
    ledger_path = Path(args.ledger) if args.ledger else data_dir() / LEDGER_FILENAME
    guards_path = Path(args.guards) if args.guards else data_dir() / GUARDS_FILENAME
    ledger = load_json(ledger_path, empty_ledger())
    guards = load_json(guards_path, empty_guards())
    forced = {c.strip() for c in args.cluster or [] if c.strip()}

    failed_reads: list[str] = []
    if args.project:
        projects = sorted({p.strip() for p in args.project if p.strip()})
    else:
        projects, failed_reads = discover_projects(run=run)
    if forced:
        projects = sorted(set(projects) | {c.split(CLUSTER_KEY_SEPARATOR)[0] for c in forced})

    clusters: list[dict] = []
    operations: list[dict] = []
    for project in projects:
        found, error = enumerate_clusters(project, run=run)
        if error:
            failed_reads.append(f"{project}: {error}")
        clusters.extend(found)
        known = [parse_ts((ledger.get("clusters") or {}).get(cluster_key(project, c["location"], c["name"]), {}).get("last_run")) for c in found]
        earliest = min([since] + [k for k in known if k])
        ops, error = list_operations(project, earliest, run=run)
        if error:
            failed_reads.append(f"{project}: {error}")
        operations.extend(ops)
    if forced:
        clusters = [c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) in forced]
        missing = forced - {cluster_key(c["project"], c["location"], c["name"]) for c in clusters}
        for key in sorted(missing):
            failed_reads.append(f"{key}: named by --cluster but not listed in its project")

    selected, unchanged = select_clusters(clusters, ledger, operations, since=since, forced=forced)
    log(f"{len(clusters)} cluster(s) in {len(projects)} project(s); reviewing {len(selected)}, {len(unchanged)} unchanged")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        reviews = list(pool.map(lambda s: review_cluster(s, ledger, run=run, seen_at=seen_at), selected))
    reviews.sort(key=lambda r: r["cluster"])

    reviewed = {r["cluster"] for r in reviews if r["reviewed"]}
    fresh_guards = [g for r in reviews for g in r["guards"]]
    new_guards = merge_guards(guards, fresh_guards, reviewed, seen_at)
    new_ledger = ledger_after(ledger, reviews, clusters, seen_at)

    result = {
        "generated_at": seen_at,
        "since": fmt_ts(since),
        "projects": projects,
        "reviews": reviews,
        "unchanged": unchanged,
        "failed_reads": failed_reads,
        "ledger_path": str(ledger_path),
        "guards_path": str(guards_path),
        "guards": new_guards["guards"],
        "dry_run": bool(args.dry_run),
    }
    if not args.dry_run:
        write_json_atomically(ledger_path, new_ledger)
        write_json_atomically(guards_path, new_guards)
        if args.output:
            write_json_atomically(Path(args.output), result)
    return result


def write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    link = path.parent / LATEST_REPORT_LINK
    with contextlib.suppress(FileNotFoundError):
        link.unlink()
    link.symlink_to(path.name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect what each new or upgraded cluster's last upgrade did, and classify what failed against the upgrade failure catalogue.")
    parser.add_argument("--project", action="append", help="GCP project to enumerate (repeatable; default: discovered the way the readiness watch discovers)")
    parser.add_argument("--cluster", action="append", help="<project>/<location>/<name>: restrict to this cluster and review it even if unchanged (repeatable)")
    parser.add_argument("--since", help=f"window for a cluster not yet in the ledger: <days>, <days>d or an RFC 3339 timestamp (default {DEFAULT_SINCE_DAYS} days)")
    parser.add_argument("--ledger", help=f"ledger path (default $HERMES_HOME/{DATA_SUBDIR}/{LEDGER_FILENAME})")
    parser.add_argument("--guards", help=f"guards path (default $HERMES_HOME/{DATA_SUBDIR}/{GUARDS_FILENAME})")
    parser.add_argument("--output", help="write the JSON result here (atomically)")
    parser.add_argument("--report", help=f"also write the Markdown report here and point {LATEST_REPORT_LINK} beside it at it")
    parser.add_argument("--dry-run", action="store_true", help="read everything, print the report, write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = collect(args)
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = render_report(result)
    sys.stdout.write(report)
    if args.report and not args.dry_run:
        write_report(Path(args.report), report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
