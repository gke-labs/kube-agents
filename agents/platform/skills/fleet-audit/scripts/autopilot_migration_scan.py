#!/usr/bin/env python3
"""autopilot_migration_scan.py — Read-only Standard-to-Autopilot workload migration readiness scanner.

Inspects live GKE Standard clusters and non-system workloads to report:
  1. Cluster-level blockers for GKE Autopilot and Autopilot-mode ComputeClasses:
     - routes-based networking (`ipAllocationPolicy.useIpAliases` is not true)
     - Extended release channel (`releaseChannel.channel == "EXTENDED"`)
     - control-plane version below `1.33.1-gke.1107000` for Autopilot-mode ComputeClasses
     - notes clusters already running GKE Autopilot (`autopilot.enabled == true`)
  2. Per-workload Autopilot admission blockers and advisories outside system namespaces:
     - Blockers:
       - `privileged` containers or initContainers (`securityContext.privileged: true`)
       - `hostNetwork: true`
       - `hostPID: true`
       - `hostIPC: true`
       - `hostPath` volumes outside `/var/log` or mounted writable (Autopilot admits
         read-only `hostPath` mounts under `/var/log`)
       - unsafe pod `securityContext.sysctls` (Autopilot admits the Kubernetes safe
         sysctl set)
     - Advisories (do not change workload status from `ready` to `blocked`):
       - user `DaemonSet` workloads
       - active standalone `Pod` objects without `ownerReferences`
  3. Per-workload Autopilot resource-request impact:
     - Target-dependent CPU floor/rounding: 50m general-purpose minimum with no 250m step
       rounding on bursting targets (Autopilot `1.30.2-gke.1394000+` on cgroupv2 and
       Standard clusters using Autopilot-mode ComputeClasses), or 250m floor and 250m step
       rounding (`max(250, ceil(cpu_millis / 250) * 250)`) on non-bursting targets
     - Target-dependent QoS burst impact (preserving `Burstable` burst on bursting targets,
       flagging `requests == limits` burst removal on non-bursting targets, and flagging
       `BestEffort` default request assignment)

Workload reads page one resource kind at a time through `collector_run.paged_dump`
(`kubectl get --raw` at `limit=500`) using a per-target kubeconfig under
`$KUBE_AGENTS_KUBECONFIG_DIR`, else `${HERMES_HOME:-/opt/data}/.kubeconfigs/`
(or an explicit `--kubeconfig`). Any failed
cluster or workload read is reported as `unknown` and sets exit code 1.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path(__file__).resolve().parent
SHARED_SCRIPT_DEPTH = 3
SHARED_SCRIPT_DIRS = (
    "/opt/defaults/scripts",
    "/opt/data/scripts",
    *(
        [str(Path(__file__).resolve().parents[SHARED_SCRIPT_DEPTH] / "scripts")]
        if len(Path(__file__).resolve().parents) > SHARED_SCRIPT_DEPTH
        else []
    ),
    str(SCRIPT_DIR),
)
for _shared_dir in SHARED_SCRIPT_DIRS:
    if _shared_dir not in sys.path:
        sys.path.append(_shared_dir)

import collector_run  # noqa: E402
import fleet_scope_args  # noqa: E402
import credential_proxy_client  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 60
PROJECTS_LIST_TIMEOUT_SECONDS = 240
ERROR_EXCERPT_CHARS = 300
JSON_INDENT = 2
EXIT_OK = 0
EXIT_ERROR = 1
KUBECONFIG_REFUSED_RC = 1

KUBECONFIG_ENV = "KUBECONFIG"
KUBECONFIG_FILE_FORMAT = "kubeconfig_{project}_{cluster}_{location}.yaml"
# `credential_proxy_client.kubeconfig_dir`, the one resolver, read once at
# import: $KUBE_AGENTS_KUBECONFIG_DIR when set, else `$HERMES_HOME/.kubeconfigs`.
DEFAULT_KUBECONFIG_DIR = credential_proxy_client.kubeconfig_dir()
OUTPUT_TMP_PREFIX = ".autopilot-migration-scan-"
OUTPUT_TMP_SUFFIX = ".tmp"

GCLOUD_BIN = "gcloud"
CLUSTERS_LIST_SUBCOMMAND = ("container", "clusters", "list")
GET_CREDENTIALS_SUBCOMMAND = ("container", "clusters", "get-credentials")
PROJECTS_LIST_ARGV = [
    GCLOUD_BIN,
    "projects",
    "list",
    "--format",
    "value(projectId)",
]
CONFIG_PROJECT_ARGV = [GCLOUD_BIN, "config", "get-value", "project"]
FORMAT_FLAG = "--format"
JSON_FORMAT = "json"
PROJECT_FLAG = "--project"
CLUSTER_FLAG = "--cluster"
LOCATION_FLAG = "--location"
KUBECONFIG_FLAG = "--kubeconfig"
KUBECONFIG_DIR_FLAG = "--kubeconfig-dir"
OUTPUT_FLAG = "--output"
NON_BURSTING_FLAG = "--non-bursting"
QUIET_FLAG = "--quiet"
UTF8_ENCODING = "utf-8"
CONTAINER_KEYS = ("containers", "initContainers", "ephemeralContainers")
SINGLE_KUBECONFIG_MULTI_CLUSTER_ERROR = (
    "--kubeconfig points to a single cluster credential file, but more than one "
    "cluster matched; pass --cluster (and --location if needed) to select a single cluster"
)

API_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
)
ZONE_TIMEOUT_MARKER = "did not respond"
AUDITABLE_CLUSTER_STATUSES = frozenset({"RUNNING", "RECONCILING"})
QUALIFIED_SEPARATOR = "/"
EXPECTED_CLUSTER_SPEC_PARTS = 2
RESTART_POLICY_FIELD = "restartPolicy"
RESTART_POLICY_ALWAYS = "Always"
OVERHEAD_FIELD = "overhead"

# Fleet-wide system namespaces (S1) skipped when evaluating user workloads.
SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "gmp-system",
        "gmp-public",
        "gke-gmp-system",
        "cnrm-system",
        "configconnector-operator-system",
        "krmapihosting-system",
        "istio-system",
        "asm-system",
        "anthos-identity-service",
        "gatekeeper-system",
        "composer-system",
    }
)
SYSTEM_NAMESPACE_PREFIXES = ("gke-", "config-management-")

# Paged collection kinds read via collector_run.paged_dump (`kubectl get --raw` at limit=500).
WORKLOAD_RESOURCES = (
    "deployments",
    "statefulsets",
    "daemonsets",
    "cronjobs",
    "jobs",
    "pods",
)

KIND_DEPLOYMENT = "Deployment"
KIND_STATEFULSET = "StatefulSet"
KIND_DAEMONSET = "DaemonSet"
KIND_CRONJOB = "CronJob"
KIND_JOB = "Job"
KIND_POD = "Pod"
TEMPLATE_WORKLOAD_KINDS = (KIND_DEPLOYMENT, KIND_STATEFULSET, KIND_DAEMONSET)

TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})
MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"

# Cluster-level statuses and rule identifiers.
CLUSTER_STATUS_READY = "ready"
CLUSTER_STATUS_BLOCKED = "blocked"
CLUSTER_STATUS_AUTOPILOT = "autopilot"
CLUSTER_STATUS_UNKNOWN = "unknown"

RULE_ROUTES_BASED_NETWORKING = "routes-based-networking"
RULE_EXTENDED_RELEASE_CHANNEL = "extended-release-channel"
RULE_CONTROL_PLANE_VERSION_FLOOR = "control-plane-below-autopilot-compute-class-floor"

RELEASE_CHANNEL_EXTENDED = "EXTENDED"
RELEASE_CHANNEL_UNSPECIFIED = "UNSPECIFIED"
AUTOPILOT_COMPUTE_CLASS_MIN_VERSION = "1.33.1-gke.1107000"
AUTOPILOT_COMPUTE_CLASS_MIN_VERSION_TUPLE = (1, 33, 1, 1107000)
AUTOPILOT_BURSTING_MIN_VERSION = "1.30.2-gke.1394000"
AUTOPILOT_BURSTING_MIN_VERSION_TUPLE = (1, 30, 2, 1394000)
CGROUP_MODE_V1 = "CGROUP_MODE_V1"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")

NOTE_ALREADY_AUTOPILOT = "cluster already runs GKE Autopilot"
NOTE_VERSION_UNKNOWN = (
    "currentMasterVersion is missing or unparsable; Autopilot-mode ComputeClass "
    f"{AUTOPILOT_COMPUTE_CLASS_MIN_VERSION} floor could not be verified"
)
DETAIL_ROUTES_BASED_NETWORKING = (
    "ipAllocationPolicy.useIpAliases is not true; GKE Autopilot and Autopilot-mode "
    "ComputeClasses require VPC-native alias IP networking"
)
DETAIL_EXTENDED_RELEASE_CHANNEL = (
    "releaseChannel.channel is EXTENDED; GKE Autopilot and Autopilot-mode "
    "ComputeClasses are not available on the Extended release channel"
)
DETAIL_VERSION_FLOOR_FORMAT = (
    "currentMasterVersion {version} is below {floor} required for "
    "Autopilot-mode ComputeClasses"
)

# Workload-level statuses, blocker rule identifiers, and advisory rule identifiers.
WORKLOAD_STATUS_READY = "ready"
WORKLOAD_STATUS_BLOCKED = "blocked"

RULE_PRIVILEGED_CONTAINER = "privileged"
RULE_HOST_NETWORK = "hostNetwork"
RULE_HOST_PID = "hostPID"
RULE_HOST_IPC = "hostIPC"
RULE_HOST_PATH = "hostPath"
RULE_SYSCTLS = "sysctls"
RULE_DAEMONSET = "DaemonSet"
RULE_STANDALONE_POD = "standalone-pod"

ALLOWED_READONLY_HOSTPATH_ROOT = "/var/log"
ALLOWED_READONLY_HOSTPATH_PREFIX = "/var/log/"
PARENT_DIR_SEGMENT = ".."
PATH_SEPARATOR = "/"

SAFE_SYSCTLS = frozenset(
    {
        "kernel.shm_rmid_forced",
        "net.ipv4.ip_local_port_range",
        "net.ipv4.ip_local_reserved_ports",
        "net.ipv4.ip_unprivileged_port_start",
        "net.ipv4.ping_group_range",
        "net.ipv4.tcp_fin_timeout",
        "net.ipv4.tcp_keepalive_intvl",
        "net.ipv4.tcp_keepalive_probes",
        "net.ipv4.tcp_keepalive_time",
        "net.ipv4.tcp_rmem",
        "net.ipv4.tcp_syncookies",
        "net.ipv4.tcp_wmem",
    }
)

# CPU and memory resource constants.
AUTOPILOT_BURSTING_MIN_CPU_MILLIS = 50
AUTOPILOT_CPU_INCREMENT_MILLIS = 250
MILLICORES_PER_CORE = 1000
BINARY_SUFFIXES: dict[str, float] = {
    "Ki": float(1 << 10),
    "Mi": float(1 << 20),
    "Gi": float(1 << 30),
    "Ti": float(1 << 40),
    "Pi": float(1 << 50),
    "Ei": float(1 << 60),
}
DECIMAL_SUFFIXES: dict[str, float] = {
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}
QUANTITY_RE = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E|)\s*$"
)

QOS_GUARANTEED = "Guaranteed"
QOS_BURSTABLE = "Burstable"
QOS_BEST_EFFORT = "BestEffort"

BURST_STATUS_GUARANTEED = "guaranteed"
BURST_STATUS_BURSTABLE = "burstable"
BURST_STATUS_BEST_EFFORT = "best-effort"

# Markdown rendering constants.
EMPTY_CELL = "-"
TABLE_SEPARATOR_CELL = "---"
ITEM_JOIN = ", "
DETAIL_JOIN = "; "
CLUSTER_TABLE_HEADERS = (
    "project",
    "location",
    "cluster",
    "mode",
    "channel",
    "control plane",
    "cluster status",
    "cluster blockers",
    "workloads (ready / blocked / total)",
    "note",
)
WORKLOAD_TABLE_HEADERS = (
    "cluster",
    "namespace",
    "kind/name",
    "status",
    "blockers",
    "advisories",
    "cpu (requested -> autopilot)",
    "qos / burst impact",
)
MODE_AUTOPILOT = "Autopilot"
MODE_STANDARD = "Standard"
UNNAMED_ITEM = "unnamed"

SCOPED_PROJECT_NOTE_FORMAT = (
    "scope narrowed to project {project!r} by `--project`: project discovery was skipped."
)

Run = collector_run.Run
Discovery = collector_run.Discovery
RunFn = Callable[..., Run]
default_run = functools.partial(collector_run.default_run, timeout=DEFAULT_TIMEOUT_SECONDS)
paged_dump = collector_run.paged_dump
failed_page_text = collector_run.failed_page_text


def is_system_namespace(namespace: str) -> bool:
    """Return True when `namespace` belongs to GKE or platform system components."""
    return namespace in SYSTEM_NAMESPACES or namespace.startswith(SYSTEM_NAMESPACE_PREFIXES)


def parse_gke_version(version: str | None) -> tuple[int, int, int, int] | None:
    """Parse `MAJOR.MINOR.PATCH[-gke.BUILD]` into a 4-tuple of ints, or None."""
    if not version or not isinstance(version, str):
        return None
    match = VERSION_RE.match(version.strip())
    if not match:
        return None
    major, minor, patch, build = match.groups()
    return (int(major), int(minor), int(patch), int(build) if build is not None else 0)


def _has_cgroup_v1(cluster: dict) -> bool:
    """Return True when `cluster` or any of its `nodePools` explicitly sets `CGROUP_MODE_V1`."""
    node_cfg = cluster.get("nodeConfig")
    if isinstance(node_cfg, dict):
        linux_cfg = node_cfg.get("linuxNodeConfig")
        if isinstance(linux_cfg, dict) and linux_cfg.get("cgroupMode") == CGROUP_MODE_V1:
            return True
    node_pools = cluster.get("nodePools")
    if isinstance(node_pools, list):
        for pool in node_pools:
            if not isinstance(pool, dict):
                continue
            pool_cfg = pool.get("config")
            if isinstance(pool_cfg, dict):
                linux_cfg = pool_cfg.get("linuxNodeConfig")
                if (
                    isinstance(linux_cfg, dict)
                    and linux_cfg.get("cgroupMode") == CGROUP_MODE_V1
                ):
                    return True
    return False


def cluster_supports_bursting(cluster: dict) -> bool:
    """Return True when `cluster` supports pod bursting (`>= 1.30.2-gke.1394000` on cgroupv2)."""
    if _has_cgroup_v1(cluster):
        return False
    master_version = str(cluster.get("currentMasterVersion") or "")
    parsed_version = parse_gke_version(master_version)
    if parsed_version is None:
        return False
    return parsed_version >= AUTOPILOT_BURSTING_MIN_VERSION_TUPLE


def evaluate_cluster(cluster: dict) -> dict:
    """Evaluate pure-function cluster-level Autopilot migration and ComputeClass rules."""
    name = str(cluster.get("name") or "")
    location = str(cluster.get("location") or cluster.get("zone") or "")
    master_version = str(cluster.get("currentMasterVersion") or "")
    release_channel_obj = cluster.get("releaseChannel")
    channel = (
        str(release_channel_obj.get("channel") or RELEASE_CHANNEL_UNSPECIFIED)
        if isinstance(release_channel_obj, dict)
        else RELEASE_CHANNEL_UNSPECIFIED
    )
    autopilot_obj = cluster.get("autopilot")
    is_autopilot = bool(isinstance(autopilot_obj, dict) and autopilot_obj.get("enabled") is True)
    supports_bursting = cluster_supports_bursting(cluster)

    if is_autopilot:
        return {
            "name": name,
            "location": location,
            "autopilot": True,
            "bursting": supports_bursting,
            "channel": channel,
            "current_master_version": master_version,
            "status": CLUSTER_STATUS_AUTOPILOT,
            "blockers": [],
            "notes": [NOTE_ALREADY_AUTOPILOT],
        }

    blockers: list[dict[str, str]] = []
    notes: list[str] = []

    ip_policy = cluster.get("ipAllocationPolicy")
    use_ip_aliases = ip_policy.get("useIpAliases") if isinstance(ip_policy, dict) else None
    if use_ip_aliases is not True:
        blockers.append(
            {
                "rule": RULE_ROUTES_BASED_NETWORKING,
                "detail": DETAIL_ROUTES_BASED_NETWORKING,
            }
        )

    if channel.upper() == RELEASE_CHANNEL_EXTENDED:
        blockers.append(
            {
                "rule": RULE_EXTENDED_RELEASE_CHANNEL,
                "detail": DETAIL_EXTENDED_RELEASE_CHANNEL,
            }
        )

    parsed_version = parse_gke_version(master_version)
    version_unknown = False
    if parsed_version is None:
        version_unknown = True
        notes.append(NOTE_VERSION_UNKNOWN)
    elif parsed_version < AUTOPILOT_COMPUTE_CLASS_MIN_VERSION_TUPLE:
        blockers.append(
            {
                "rule": RULE_CONTROL_PLANE_VERSION_FLOOR,
                "detail": DETAIL_VERSION_FLOOR_FORMAT.format(
                    version=master_version,
                    floor=AUTOPILOT_COMPUTE_CLASS_MIN_VERSION,
                ),
            }
        )

    if blockers:
        status = CLUSTER_STATUS_BLOCKED
    elif version_unknown:
        status = CLUSTER_STATUS_UNKNOWN
    else:
        status = CLUSTER_STATUS_READY

    return {
        "name": name,
        "location": location,
        "autopilot": False,
        "bursting": supports_bursting,
        "channel": channel,
        "current_master_version": master_version,
        "status": status,
        "blockers": blockers,
        "notes": notes,
    }


def parse_quantity(value: str | int | float | None) -> float | None:
    """Parse a Kubernetes quantity string or number into a finite non-negative float, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number_val: int | float | str = value
        scale = 1.0
    elif isinstance(value, str):
        match = QUANTITY_RE.match(value)
        if not match:
            return None
        number_text, suffix = match.groups()
        number_val = number_text
        if suffix == "":
            scale = 1.0
        elif suffix in BINARY_SUFFIXES:
            scale = BINARY_SUFFIXES[suffix]
        elif suffix in DECIMAL_SUFFIXES:
            scale = DECIMAL_SUFFIXES[suffix]
        else:
            return None
    else:
        return None
    try:
        scaled = float(number_val) * scale
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(scaled) or scaled < 0:
        return None
    return scaled


def parse_cpu_millis(value: str | int | float | None) -> int | None:
    """Parse a Kubernetes CPU quantity into integer millicores (rounded up), or None."""
    parsed = parse_quantity(value)
    if parsed is None:
        return None
    millis = parsed * MILLICORES_PER_CORE
    if not math.isfinite(millis):
        return None
    try:
        return int(math.ceil(millis))
    except OverflowError:
        return None


def parse_memory_bytes(value: str | int | float | None) -> int | None:
    """Parse a Kubernetes memory quantity into integer bytes, or None."""
    parsed = parse_quantity(value)
    if parsed is None:
        return None
    try:
        return int(math.ceil(parsed))
    except OverflowError:
        return None


def round_autopilot_cpu_millis(cpu_millis: int, *, bursting: bool = True) -> int:
    """Compute GKE Autopilot CPU request floor or step rounding.

    On bursting targets (Autopilot >= 1.30.2-gke.1394000 on cgroupv2 and Standard
    Autopilot-mode ComputeClasses), applies the 50m general-purpose minimum with no
    250m step rounding (`max(50, cpu_millis)`). On non-bursting targets, applies
    the 250m floor and 250m step rounding (`max(250, ceil(cpu_millis / 250) * 250)`).
    """
    clamped = max(0, int(cpu_millis))
    if bursting:
        return max(AUTOPILOT_BURSTING_MIN_CPU_MILLIS, clamped)
    rounded = math.ceil(clamped / AUTOPILOT_CPU_INCREMENT_MILLIS) * AUTOPILOT_CPU_INCREMENT_MILLIS
    return max(AUTOPILOT_CPU_INCREMENT_MILLIS, rounded)


def _pod_containers(pod_spec: dict, key: str) -> list[dict]:
    raw = pod_spec.get(key)
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def _container_resource_dicts(container: dict) -> tuple[dict, dict]:
    resources = container.get("resources")
    requests = resources.get("requests") if isinstance(resources, dict) else None
    limits = resources.get("limits") if isinstance(resources, dict) else None
    return (
        requests if isinstance(requests, dict) else {},
        limits if isinstance(limits, dict) else {},
    )


def evaluate_workload_resources(pod_spec: dict, *, bursting: bool = True) -> dict:
    """Evaluate Autopilot CPU floor/rounding and QoS burst impact for a pod spec."""
    app_containers = _pod_containers(pod_spec, "containers")
    init_containers = _pod_containers(pod_spec, "initContainers")
    all_containers = app_containers + init_containers

    app_requested_cpu_millis = 0
    for container in app_containers:
        req_dict, lim_dict = _container_resource_dicts(container)
        raw_cpu_req = req_dict.get("cpu")
        if raw_cpu_req is None:
            raw_cpu_req = lim_dict.get("cpu")
        cpu_req = parse_cpu_millis(raw_cpu_req)
        if cpu_req is not None:
            app_requested_cpu_millis += cpu_req

    sidecar_cpu_millis = 0
    init_peak_cpu_millis = 0
    for container in init_containers:
        req_dict, lim_dict = _container_resource_dicts(container)
        raw_cpu_req = req_dict.get("cpu")
        if raw_cpu_req is None:
            raw_cpu_req = lim_dict.get("cpu")
        cpu_req = parse_cpu_millis(raw_cpu_req) or 0
        if container.get(RESTART_POLICY_FIELD) == RESTART_POLICY_ALWAYS:
            sidecar_cpu_millis += cpu_req
        else:
            init_peak_cpu_millis = max(init_peak_cpu_millis, sidecar_cpu_millis + cpu_req)

    overhead = pod_spec.get(OVERHEAD_FIELD)
    overhead_cpu_millis = (
        parse_cpu_millis(overhead.get("cpu") if isinstance(overhead, dict) else None) or 0
    )

    requested_cpu_millis = (
        max(app_requested_cpu_millis + sidecar_cpu_millis, init_peak_cpu_millis)
        + overhead_cpu_millis
    )
    autopilot_cpu_millis = round_autopilot_cpu_millis(requested_cpu_millis, bursting=bursting)
    non_bursting_autopilot_cpu_millis = round_autopilot_cpu_millis(
        requested_cpu_millis, bursting=False
    )
    cpu_delta_millis = autopilot_cpu_millis - requested_cpu_millis
    cpu_rounded = cpu_delta_millis != 0
    cpu_rounding_summary = (
        f"{requested_cpu_millis}m -> {autopilot_cpu_millis}m (+{cpu_delta_millis}m)"
    )

    any_resource_declared = False
    all_guaranteed = bool(all_containers)
    burst_details: list[str] = []

    for container in all_containers:
        cname = str(container.get("name") or UNNAMED_ITEM)
        req_dict, lim_dict = _container_resource_dicts(container)

        raw_cpu_lim = lim_dict.get("cpu")
        raw_cpu_req = req_dict.get("cpu")
        if raw_cpu_req is None:
            raw_cpu_req = raw_cpu_lim
        raw_mem_lim = lim_dict.get("memory")
        raw_mem_req = req_dict.get("memory")
        if raw_mem_req is None:
            raw_mem_req = raw_mem_lim

        cpu_req = parse_cpu_millis(raw_cpu_req)
        cpu_lim = parse_cpu_millis(raw_cpu_lim)
        mem_req = parse_memory_bytes(raw_mem_req)
        mem_lim = parse_memory_bytes(raw_mem_lim)

        if any(v is not None and v > 0 for v in (cpu_req, cpu_lim, mem_req, mem_lim)):
            any_resource_declared = True

        container_cpu_equal = (
            cpu_req is not None and cpu_lim is not None and cpu_req > 0 and cpu_req == cpu_lim
        )
        container_mem_equal = (
            mem_req is not None and mem_lim is not None and mem_req > 0 and mem_req == mem_lim
        )

        if not (container_cpu_equal and container_mem_equal):
            all_guaranteed = False

        if cpu_req is None and cpu_lim is None:
            burst_details.append(f"{cname}: cpu requests/limits unset")
        elif cpu_lim is None:
            burst_details.append(f"{cname}: cpu limit unset (request {raw_cpu_req})")
        elif cpu_req is None or cpu_lim > cpu_req:
            burst_details.append(
                f"{cname}: cpu limit {raw_cpu_lim} > request {raw_cpu_req or '0m'}"
            )
        elif cpu_req <= 0 and cpu_lim <= 0:
            burst_details.append(f"{cname}: cpu request and limit are 0")
        elif cpu_req > cpu_lim:
            burst_details.append(
                f"{cname}: cpu request {raw_cpu_req} > limit {raw_cpu_lim}"
            )

        if mem_req is None and mem_lim is None:
            burst_details.append(f"{cname}: memory requests/limits unset")
        elif mem_lim is None:
            burst_details.append(f"{cname}: memory limit unset (request {raw_mem_req})")
        elif mem_req is None or mem_lim > mem_req:
            burst_details.append(
                f"{cname}: memory limit {raw_mem_lim} > request {raw_mem_req or '0'}"
            )
        elif mem_req <= 0 and mem_lim <= 0:
            burst_details.append(f"{cname}: memory request and limit are 0")
        elif mem_req > mem_lim:
            burst_details.append(
                f"{cname}: memory request {raw_mem_req} > limit {raw_mem_lim}"
            )

    if all_guaranteed:
        qos_class = QOS_GUARANTEED
        burst_status = BURST_STATUS_GUARANTEED
        burst_impacted = False
        if cpu_rounded:
            burst_summary = (
                f"Guaranteed (requests == limits; CPU request and limit round from "
                f"{requested_cpu_millis}m to {autopilot_cpu_millis}m)"
            )
        else:
            burst_summary = "Guaranteed (requests == limits; no burst change)"
    elif not any_resource_declared:
        qos_class = QOS_BEST_EFFORT
        burst_status = BURST_STATUS_BEST_EFFORT
        burst_impacted = True
        if bursting:
            burst_summary = (
                "BestEffort (requests and limits unset; Autopilot assigns default requests)"
            )
        else:
            burst_summary = (
                "BestEffort (requests and limits unset; non-bursting Autopilot assigns "
                "default requests and enforces requests == limits)"
            )
    else:
        qos_class = QOS_BURSTABLE
        burst_status = BURST_STATUS_BURSTABLE
        detail_text = DETAIL_JOIN.join(burst_details)
        if bursting:
            burst_impacted = False
            burst_summary = (
                f"Burstable (target supports bursting; burst above request preserved: "
                f"{detail_text})"
            )
        else:
            burst_impacted = True
            burst_summary = (
                f"Burstable (non-bursting Autopilot enforces requests == limits, removing "
                f"burst above request: {detail_text})"
            )

    return {
        "bursting": bursting,
        "requested_cpu_millis": requested_cpu_millis,
        "autopilot_cpu_millis": autopilot_cpu_millis,
        "non_bursting_autopilot_cpu_millis": non_bursting_autopilot_cpu_millis,
        "cpu_delta_millis": cpu_delta_millis,
        "cpu_rounded": cpu_rounded,
        "cpu_rounding_summary": cpu_rounding_summary,
        "qos_class": qos_class,
        "burst_status": burst_status,
        "burst_impacted": burst_impacted,
        "burst_details": burst_details,
        "burst_summary": burst_summary,
    }


def _pod_spec_for_item(item: dict) -> dict:
    kind = str(item.get("kind") or "")
    spec = item.get("spec")
    if not isinstance(spec, dict):
        return {}
    if kind == KIND_POD:
        return spec
    if kind == KIND_CRONJOB:
        job_template = spec.get("jobTemplate")
        job_spec = job_template.get("spec") if isinstance(job_template, dict) else None
        template = job_spec.get("template") if isinstance(job_spec, dict) else None
        pod_spec = template.get("spec") if isinstance(template, dict) else None
        return pod_spec if isinstance(pod_spec, dict) else {}
    template = spec.get("template")
    pod_spec = template.get("spec") if isinstance(template, dict) else None
    return pod_spec if isinstance(pod_spec, dict) else {}


def _is_var_log_path(path: str) -> bool:
    """Return True when `path` is `/var/log` or a subpath under `/var/log/` without `..` traversal."""
    cleaned = path.strip()
    if not cleaned.startswith(PATH_SEPARATOR):
        return False
    if PARENT_DIR_SEGMENT in cleaned.split(PATH_SEPARATOR):
        return False
    normalized = posixpath.normpath(cleaned)
    return (
        normalized == ALLOWED_READONLY_HOSTPATH_ROOT
        or normalized.startswith(ALLOWED_READONLY_HOSTPATH_PREFIX)
    )


def _volume_mount_states(pod_spec: dict) -> tuple[set[str], set[str]]:
    """Return `(mounted_volumes, writable_volumes)` across all container types in `pod_spec`."""
    mounted: set[str] = set()
    writable: set[str] = set()
    for container_key in CONTAINER_KEYS:
        for container in _pod_containers(pod_spec, container_key):
            mounts = container.get("volumeMounts")
            if not isinstance(mounts, list):
                continue
            for mount in mounts:
                if not isinstance(mount, dict):
                    continue
                mount_name = str(mount.get("name") or "")
                if not mount_name:
                    continue
                mounted.add(mount_name)
                if mount.get("readOnly") is not True:
                    writable.add(mount_name)
    return mounted, writable


def extract_workloads(items: list[dict]) -> list[dict]:
    """Filter raw cluster items to non-system workload controllers and active standalone pods."""
    selected: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            continue
        namespace = str(metadata.get("namespace") or "")
        name = str(metadata.get("name") or "")
        if not namespace or not name or is_system_namespace(namespace):
            continue

        owner_refs = metadata.get("ownerReferences")
        has_owners = isinstance(owner_refs, list) and len(owner_refs) > 0

        if kind in TEMPLATE_WORKLOAD_KINDS or kind == KIND_CRONJOB:
            selected.append(item)
        elif kind == KIND_JOB:
            if not has_owners:
                selected.append(item)
        elif kind == KIND_POD:
            if has_owners:
                continue
            annotations = metadata.get("annotations")
            if isinstance(annotations, dict) and MIRROR_POD_ANNOTATION in annotations:
                continue
            status = item.get("status")
            phase = str(status.get("phase") or "") if isinstance(status, dict) else ""
            if phase in TERMINAL_POD_PHASES:
                continue
            selected.append(item)

    selected.sort(
        key=lambda obj: (
            str((obj.get("metadata") or {}).get("namespace") or ""),
            str(obj.get("kind") or ""),
            str((obj.get("metadata") or {}).get("name") or ""),
        )
    )
    return selected


def evaluate_workload(item: dict, *, bursting: bool = True) -> dict:
    """Evaluate Autopilot admission blockers, advisories, and resource impact for one workload item."""
    kind = str(item.get("kind") or "")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    namespace = str(metadata.get("namespace") or "")
    name = str(metadata.get("name") or "")
    pod_spec = _pod_spec_for_item(item)

    blockers: list[dict[str, str]] = []
    advisories: list[dict[str, str]] = []

    privileged_containers: list[str] = []
    for container_key in CONTAINER_KEYS:
        for container in _pod_containers(pod_spec, container_key):
            sec_ctx = container.get("securityContext")
            if isinstance(sec_ctx, dict) and sec_ctx.get("privileged") is True:
                privileged_containers.append(str(container.get("name") or UNNAMED_ITEM))
    if privileged_containers:
        blockers.append(
            {
                "rule": RULE_PRIVILEGED_CONTAINER,
                "detail": (
                    f"container(s) {ITEM_JOIN.join(privileged_containers)} set "
                    "securityContext.privileged: true"
                ),
            }
        )

    if pod_spec.get("hostNetwork") is True:
        blockers.append(
            {
                "rule": RULE_HOST_NETWORK,
                "detail": "pod spec sets hostNetwork: true",
            }
        )

    if pod_spec.get("hostPID") is True:
        blockers.append(
            {
                "rule": RULE_HOST_PID,
                "detail": "pod spec sets hostPID: true",
            }
        )

    if pod_spec.get("hostIPC") is True:
        blockers.append(
            {
                "rule": RULE_HOST_IPC,
                "detail": "pod spec sets hostIPC: true",
            }
        )

    mounted_volumes, writable_volumes = _volume_mount_states(pod_spec)
    raw_volumes = pod_spec.get("volumes")
    host_path_entries: list[str] = []
    if isinstance(raw_volumes, list):
        for volume in raw_volumes:
            if not isinstance(volume, dict):
                continue
            host_path = volume.get("hostPath")
            if isinstance(host_path, dict):
                vol_name = str(volume.get("name") or UNNAMED_ITEM)
                mount_path = str(host_path.get("path") or "")
                if vol_name in mounted_volumes:
                    is_read_only = vol_name not in writable_volumes
                else:
                    is_read_only = bool(
                        volume.get("readOnly") is True or host_path.get("readOnly") is True
                    )
                if _is_var_log_path(mount_path) and is_read_only:
                    continue
                host_path_entries.append(
                    f"{vol_name} ({mount_path})" if mount_path else vol_name
                )
    if host_path_entries:
        blockers.append(
            {
                "rule": RULE_HOST_PATH,
                "detail": f"volume(s) use hostPath: {ITEM_JOIN.join(host_path_entries)}",
            }
        )

    pod_sec_ctx = pod_spec.get("securityContext")
    raw_sysctls = pod_sec_ctx.get("sysctls") if isinstance(pod_sec_ctx, dict) else None
    unsafe_sysctls: list[str] = []
    if isinstance(raw_sysctls, list):
        for entry in raw_sysctls:
            if isinstance(entry, dict):
                raw_name = str(entry.get("name") or "").strip()
                if raw_name in SAFE_SYSCTLS:
                    continue
                sysctl_name = raw_name or UNNAMED_ITEM
                sysctl_val = entry.get("value")
                unsafe_sysctls.append(
                    f"{sysctl_name}={sysctl_val}" if sysctl_val is not None else sysctl_name
                )
    if unsafe_sysctls:
        blockers.append(
            {
                "rule": RULE_SYSCTLS,
                "detail": (
                    f"pod securityContext sets unsafe sysctls: "
                    f"{ITEM_JOIN.join(unsafe_sysctls)}"
                ),
            }
        )

    if kind == KIND_DAEMONSET:
        advisories.append(
            {
                "rule": RULE_DAEMONSET,
                "detail": (
                    "user DaemonSet schedules per node; verify whether it is a node agent "
                    "unneeded on Autopilot or requires Standard node pools"
                ),
            }
        )

    if kind == KIND_POD:
        advisories.append(
            {
                "rule": RULE_STANDALONE_POD,
                "detail": (
                    "standalone Pod has no controller ownerReferences and will not be "
                    "recreated across Autopilot node lifecycle events"
                ),
            }
        )

    resources = evaluate_workload_resources(pod_spec, bursting=bursting)
    status = WORKLOAD_STATUS_BLOCKED if blockers else WORKLOAD_STATUS_READY

    return {
        "namespace": namespace,
        "kind": kind,
        "name": name,
        "status": status,
        "blockers": blockers,
        "advisories": advisories,
        "resources": resources,
    }


def evaluate_cluster_workloads(
    cluster: dict,
    items: list[dict] | None,
    *,
    project: str = "",
    read_error: str | None = None,
    bursting: bool | None = None,
) -> dict:
    """Combine cluster-level evaluation with workload evaluations for one cluster."""
    cluster_eval = evaluate_cluster(cluster)
    cluster_eval["project"] = project
    target_bursting = (
        bool(cluster_eval.get("bursting", True)) if bursting is None else bool(bursting)
    )
    cluster_eval["bursting"] = target_bursting

    if cluster_eval["autopilot"]:
        cluster_eval["workloads"] = []
        cluster_eval["workload_summary"] = {
            "total": 0,
            "ready": 0,
            "blocked": 0,
            "cpu_rounded": 0,
            "burst_impacted": 0,
        }
        cluster_eval["read_error"] = None
        return cluster_eval

    if read_error is not None or items is None:
        error_text = read_error or "workload collection returned no items"
        cluster_eval["status"] = (
            CLUSTER_STATUS_BLOCKED
            if cluster_eval["blockers"]
            else CLUSTER_STATUS_UNKNOWN
        )
        cluster_eval["notes"].append(f"workloads not read: {error_text}")
        cluster_eval["workloads"] = []
        cluster_eval["workload_summary"] = {
            "total": 0,
            "ready": 0,
            "blocked": 0,
            "cpu_rounded": 0,
            "burst_impacted": 0,
        }
        cluster_eval["read_error"] = error_text
        return cluster_eval

    workloads = [
        evaluate_workload(item, bursting=target_bursting)
        for item in extract_workloads(items)
    ]
    ready_count = sum(1 for w in workloads if w["status"] == WORKLOAD_STATUS_READY)
    blocked_count = sum(1 for w in workloads if w["status"] == WORKLOAD_STATUS_BLOCKED)
    cpu_rounded_count = sum(1 for w in workloads if w["resources"]["cpu_rounded"])
    burst_impacted_count = sum(1 for w in workloads if w["resources"]["burst_impacted"])

    cluster_eval["workloads"] = workloads
    cluster_eval["workload_summary"] = {
        "total": len(workloads),
        "ready": ready_count,
        "blocked": blocked_count,
        "cpu_rounded": cpu_rounded_count,
        "burst_impacted": burst_impacted_count,
    }
    cluster_eval["read_error"] = None
    return cluster_eval


def _escape_cell(value: str) -> str:
    text = value.replace("\r", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|") if text else EMPTY_CELL


def _markdown_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    header_line = "| " + " | ".join(headers) + " |"
    separator_line = "| " + " | ".join(TABLE_SEPARATOR_CELL for _ in headers) + " |"
    lines = [header_line, separator_line]
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in row) + " |")
    return lines


def render_markdown(report: dict) -> str:
    """Render cluster and per-workload readiness summary tables as Markdown."""
    lines: list[str] = ["## Cluster Autopilot Migration Readiness", ""]
    clusters = report.get("clusters") or []
    cluster_rows: list[tuple[str, ...]] = []
    workload_rows: list[tuple[str, ...]] = []

    for cluster in clusters:
        project = str(cluster.get("project") or EMPTY_CELL)
        location = str(cluster.get("location") or EMPTY_CELL)
        name = str(cluster.get("name") or EMPTY_CELL)
        is_autopilot = bool(cluster.get("autopilot"))
        mode = MODE_AUTOPILOT if is_autopilot else MODE_STANDARD
        channel = str(cluster.get("channel") or EMPTY_CELL)
        master_version = str(cluster.get("current_master_version") or EMPTY_CELL)
        status = str(cluster.get("status") or EMPTY_CELL)
        blockers = cluster.get("blockers") or []
        blocker_rules = (
            ITEM_JOIN.join(str(b.get("rule") or "") for b in blockers)
            if blockers
            else EMPTY_CELL
        )
        summary = cluster.get("workload_summary") or {}
        if is_autopilot or cluster.get("read_error"):
            counts_cell = EMPTY_CELL
        else:
            counts_cell = (
                f"{summary.get('ready', 0)} / "
                f"{summary.get('blocked', 0)} / "
                f"{summary.get('total', 0)}"
            )
        notes = cluster.get("notes") or []
        note_cell = DETAIL_JOIN.join(str(n) for n in notes) if notes else EMPTY_CELL

        cluster_rows.append(
            (
                project,
                location,
                name,
                mode,
                channel,
                master_version,
                status,
                blocker_rules,
                counts_cell,
                note_cell,
            )
        )

        cluster_label = (
            f"{project}/{location}/{name}" if project != EMPTY_CELL else f"{location}/{name}"
        )
        for workload in cluster.get("workloads") or []:
            w_namespace = str(workload.get("namespace") or EMPTY_CELL)
            w_kind = str(workload.get("kind") or "")
            w_name = str(workload.get("name") or "")
            w_status = str(workload.get("status") or EMPTY_CELL)
            w_blockers = workload.get("blockers") or []
            w_blocker_text = (
                DETAIL_JOIN.join(
                    f"{b.get('rule')}: {b.get('detail')}" for b in w_blockers
                )
                if w_blockers
                else EMPTY_CELL
            )
            w_advisories = workload.get("advisories") or []
            w_advisory_text = (
                DETAIL_JOIN.join(
                    f"{a.get('rule')}: {a.get('detail')}" for a in w_advisories
                )
                if w_advisories
                else EMPTY_CELL
            )
            w_resources = workload.get("resources") or {}
            cpu_cell = str(w_resources.get("cpu_rounding_summary") or EMPTY_CELL)
            burst_cell = str(w_resources.get("burst_summary") or EMPTY_CELL)
            workload_rows.append(
                (
                    cluster_label,
                    w_namespace,
                    f"{w_kind}/{w_name}",
                    w_status,
                    w_blocker_text,
                    w_advisory_text,
                    cpu_cell,
                    burst_cell,
                )
            )

    if cluster_rows:
        lines.extend(_markdown_table(CLUSTER_TABLE_HEADERS, cluster_rows))
    else:
        lines.append("No clusters evaluated.")

    lines.extend(["", "## Per-Workload Autopilot Readiness & Resource Impact", ""])
    if workload_rows:
        lines.extend(_markdown_table(WORKLOAD_TABLE_HEADERS, workload_rows))
    else:
        lines.append("No non-system Standard-cluster workloads evaluated.")

    scope_note = report.get("scope_note")
    if scope_note:
        lines.extend(["", f"Note: {scope_note}"])

    errors = report.get("errors") or []
    if errors:
        lines.extend(["", "## Errors", ""])
        for err in errors:
            lines.append(f"- {err}")

    return "\n".join(lines) + "\n"


def discover_projects(
    project_override: str | None,
    *,
    scope: fleet_scope_args.DeclaredScope,
    run: RunFn = default_run,
) -> Discovery:
    """Resolve the project list from `--project`, `fleet_scope_args`, or `gcloud`."""
    if project_override is not None:
        cleaned = project_override.strip()
        if not cleaned:
            return Discovery(
                [], fleet_scope_args.EMPTY_PROJECT_OVERRIDE_ERROR, None
            )
        override_error = scope.override_error(cleaned)
        if override_error:
            return Discovery([], override_error, None)
        return Discovery(
            [cleaned],
            None,
            SCOPED_PROJECT_NOTE_FORMAT.format(project=cleaned),
        )

    if scope.declared:
        if not scope.projects:
            return Discovery([], scope.empty_error(), None)
        return Discovery(list(scope.projects), None, scope.note())

    config_res = run(CONFIG_PROJECT_ARGV)
    base_project = config_res.stdout.strip() if config_res.rc == 0 else ""
    projects = [base_project] if base_project else []

    try:
        list_res = run(PROJECTS_LIST_ARGV, timeout=PROJECTS_LIST_TIMEOUT_SECONDS)
    except TypeError:
        list_res = run(PROJECTS_LIST_ARGV)
    if list_res.rc != 0:
        stderr = list_res.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        if not projects:
            return Discovery(
                [],
                f"project discovery failed: `gcloud config get-value project` named no project "
                f"and `gcloud projects list` failed (rc={list_res.rc}): {stderr}",
            )
        return Discovery(
            projects,
            None,
            f"`gcloud projects list` failed (rc={list_res.rc}: {stderr}); fell back to {base_project!r}.",
        )

    listed = [
        line.strip()
        for line in (list_res.stdout or "").splitlines()
        if line.strip()
    ]
    for candidate in listed:
        if candidate not in projects:
            projects.append(candidate)

    if base_project and base_project not in listed:
        partial_note = (
            f"`gcloud projects list` rc=0 did not name the active project {base_project!r}, "
            f"so it is filtered rather than complete: it returned {len(listed)} project(s)."
        )
        return Discovery(projects, None, partial_note)

    if not projects:
        return Discovery(
            [],
            "project discovery named no project: `gcloud config get-value project` is unset "
            "and `gcloud projects list` returned nothing",
        )
    return Discovery(projects, None)


def list_project_clusters(
    project: str, *, run: RunFn = default_run
) -> tuple[list[dict] | None, str | None]:
    """List GKE clusters in `project` via `gcloud container clusters list --format json`."""
    argv = [
        GCLOUD_BIN,
        *CLUSTERS_LIST_SUBCOMMAND,
        PROJECT_FLAG,
        project,
        FORMAT_FLAG,
        JSON_FORMAT,
    ]
    res = run(argv)
    if res.rc != 0:
        stderr = (res.stderr or "").strip()
        excerpt = stderr[:ERROR_EXCERPT_CHARS] or "no stderr"
        if any(marker in stderr for marker in API_DISABLED_MARKERS):
            from collect import refusal_owner  # noqa: PLC0415

            is_owner, why = refusal_owner(project, stderr, run=run)
            if is_owner:
                return [], None
            return (
                None,
                f"`gcloud container clusters list --project {project}` failed "
                f"(rc={res.rc}; {why}): {excerpt}",
            )
        return None, f"`gcloud container clusters list --project {project}` failed (rc={res.rc}): {excerpt}"
    raw = (res.stdout or "").strip()
    if not raw:
        return (
            None,
            f"`gcloud container clusters list --project {project}` exited 0 with empty stdout",
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"`gcloud container clusters list --project {project}` returned invalid JSON: {exc}"
    if not isinstance(parsed, list):
        return None, f"`gcloud container clusters list --project {project}` returned {type(parsed).__name__}, not a list"
    clusters = [c for c in parsed if isinstance(c, dict)]
    incomplete = [
        line.strip()
        for line in (res.stderr or "").splitlines()
        if ZONE_TIMEOUT_MARKER in line
    ]
    if incomplete:
        detail = " ".join(incomplete)[:ERROR_EXCERPT_CHARS]
        return (
            clusters,
            f"`gcloud container clusters list --project {project}` rc=0 but incomplete: {detail}",
        )
    return clusters, None


def kubeconfig_path(kubeconfig_dir: Path, project: str, cluster: str, location: str) -> Path:
    """Return the per-target kubeconfig path for `(project, cluster, location)`."""
    return kubeconfig_dir / KUBECONFIG_FILE_FORMAT.format(
        project=project,
        cluster=cluster,
        location=location,
    )


def fetch_cluster_credentials(
    project: str,
    cluster: str,
    location: str,
    *,
    kubeconfig_dir: Path = DEFAULT_KUBECONFIG_DIR,
    run: RunFn = default_run,
) -> tuple[Path, str | None]:
    """Fetch per-cluster credentials into an isolated kubeconfig under `kubeconfig_dir`."""
    kc = kubeconfig_path(kubeconfig_dir, project, cluster, location)
    from collect import prepare_kubeconfig  # noqa: PLC0415

    note = prepare_kubeconfig(kc)
    if note:
        return kc, note

    argv = [
        GCLOUD_BIN,
        *GET_CREDENTIALS_SUBCOMMAND,
        cluster,
        LOCATION_FLAG,
        location,
        PROJECT_FLAG,
        project,
        QUIET_FLAG,
    ]
    res = run(argv, env={**os.environ, KUBECONFIG_ENV: str(kc)})
    if res.rc != 0:
        excerpt = (res.stderr or res.stdout or "").strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        return kc, f"`gcloud container clusters get-credentials {cluster}` failed (rc={res.rc}): {excerpt}"
    return kc, None


def collect_cluster_workloads(
    project: str,
    cluster_record: dict,
    *,
    kubeconfig: Path | None = None,
    kubeconfig_dir: Path = DEFAULT_KUBECONFIG_DIR,
    bursting: bool | None = None,
    run: RunFn = default_run,
) -> dict:
    """Evaluate a cluster record and, for Standard clusters, page and evaluate its workloads."""
    cluster_name = str(cluster_record.get("name") or "")
    location = str(cluster_record.get("location") or cluster_record.get("zone") or "")
    autopilot_obj = cluster_record.get("autopilot")
    is_autopilot = bool(isinstance(autopilot_obj, dict) and autopilot_obj.get("enabled") is True)
    if is_autopilot:
        return evaluate_cluster_workloads(
            cluster_record, [], project=project, bursting=bursting
        )

    cluster_status = str(cluster_record.get("status") or "")
    if cluster_status and cluster_status not in AUDITABLE_CLUSTER_STATUSES:
        return evaluate_cluster_workloads(
            cluster_record,
            None,
            project=project,
            read_error=(
                f"cluster status is {cluster_status}, which is neither RUNNING nor RECONCILING"
            ),
            bursting=bursting,
        )

    if kubeconfig is not None:
        kc_path = kubeconfig
        if not kc_path.is_file():
            return evaluate_cluster_workloads(
                cluster_record,
                None,
                project=project,
                read_error=f"kubeconfig {kc_path} does not exist",
                bursting=bursting,
            )
    else:
        kc_path, cred_error = fetch_cluster_credentials(
            project,
            cluster_name,
            location,
            kubeconfig_dir=kubeconfig_dir,
            run=run,
        )
        if cred_error:
            return evaluate_cluster_workloads(
                cluster_record,
                None,
                project=project,
                read_error=cred_error,
                bursting=bursting,
            )

    kube_env = {**os.environ, KUBECONFIG_ENV: str(kc_path)}
    dump, dump_run = paged_dump(WORKLOAD_RESOURCES, run=run, env=kube_env)
    if dump is None:
        stderr = (dump_run.stderr or dump_run.stdout or "").strip()[:ERROR_EXCERPT_CHARS]
        cmd_text = failed_page_text(dump_run.argv) if dump_run.argv else "kubectl get --raw"
        return evaluate_cluster_workloads(
            cluster_record,
            None,
            project=project,
            read_error=f"`{cmd_text}` failed (rc={dump_run.rc}): {stderr}",
            bursting=bursting,
        )

    items = [i for i in (dump.get("items") or []) if isinstance(i, dict)]
    return evaluate_cluster_workloads(
        cluster_record, items, project=project, bursting=bursting
    )


def _matches_cluster_filter(
    cluster_record: dict,
    cluster_filter: str | None,
    location_filter: str | None,
) -> bool:
    name = str(cluster_record.get("name") or "")
    location = str(cluster_record.get("location") or cluster_record.get("zone") or "")
    if location_filter and location != location_filter:
        return False
    if not cluster_filter:
        return True
    if QUALIFIED_SEPARATOR in cluster_filter:
        parts = cluster_filter.split(QUALIFIED_SEPARATOR)
        if len(parts) == EXPECTED_CLUSTER_SPEC_PARTS:
            return location == parts[0] and name == parts[1]
    return name == cluster_filter


def scan_fleet(
    *,
    project_override: str | None = None,
    cluster_filter: str | None = None,
    location_filter: str | None = None,
    kubeconfig: Path | None = None,
    kubeconfig_dir: Path = DEFAULT_KUBECONFIG_DIR,
    bursting: bool | None = None,
    scope: fleet_scope_args.DeclaredScope | None = None,
    run: RunFn = default_run,
) -> dict:
    """Discover projects and clusters, evaluate cluster and workload rules, and build the report."""
    active_scope = scope if scope is not None else fleet_scope_args.DeclaredScope()
    discovery = discover_projects(project_override, scope=active_scope, run=run)

    errors: list[str] = []
    if discovery.error:
        errors.append(discovery.error)
    elif discovery.partial and project_override is None:
        errors.append(discovery.partial)

    evaluated_clusters: list[dict] = []
    matched_records: list[tuple[str, dict]] = []

    for project in discovery.projects:
        clusters, list_err = list_project_clusters(project, run=run)
        if list_err:
            errors.append(list_err)
        if clusters is None:
            continue
        kept_clusters = active_scope.drop_excluded(project, clusters)
        for cluster_record in kept_clusters:
            if not _matches_cluster_filter(cluster_record, cluster_filter, location_filter):
                continue
            matched_records.append((project, cluster_record))

    if kubeconfig is not None and len(matched_records) > 1:
        errors.append(SINGLE_KUBECONFIG_MULTI_CLUSTER_ERROR)
        matched_records = []

    for project, cluster_record in matched_records:
        name = str(cluster_record.get("name") or "")
        location = str(cluster_record.get("location") or cluster_record.get("zone") or "")
        evaluated = collect_cluster_workloads(
            project,
            cluster_record,
            kubeconfig=kubeconfig,
            kubeconfig_dir=kubeconfig_dir,
            bursting=bursting,
            run=run,
        )
        if evaluated.get("read_error"):
            errors.append(
                f"{project}/{location}/{name}: {evaluated['read_error']}"
            )
        elif evaluated.get("status") == CLUSTER_STATUS_UNKNOWN:
            unknown_notes = DETAIL_JOIN.join(
                str(n) for n in (evaluated.get("notes") or [CLUSTER_STATUS_UNKNOWN])
            )
            errors.append(f"{project}/{location}/{name}: {unknown_notes}")
        evaluated_clusters.append(evaluated)

    if (cluster_filter or location_filter) and not matched_records and not errors:
        target_desc = cluster_filter or location_filter or ""
        errors.append(f"no cluster matching {target_desc!r} was found in the scanned project scope")

    evaluated_clusters.sort(
        key=lambda c: (
            str(c.get("project") or ""),
            str(c.get("location") or ""),
            str(c.get("name") or ""),
        )
    )

    return {
        "projects": discovery.projects,
        "scope_note": discovery.partial,
        "clusters": evaluated_clusters,
        "errors": errors,
    }


def write_atomic_json(path: Path, payload: dict) -> None:
    """Write `payload` to `path` atomically via a temporary sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=OUTPUT_TMP_PREFIX,
        suffix=OUTPUT_TMP_SUFFIX,
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=UTF8_ENCODING) as handle:
            json.dump(payload, handle, indent=JSON_INDENT, sort_keys=True, allow_nan=False)
            handle.write("\n")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for `autopilot_migration_scan.py`."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only GKE Standard-to-Autopilot and Autopilot-mode ComputeClass "
            "workload migration readiness scanner."
        ),
    )
    parser.add_argument(
        PROJECT_FLAG,
        default=None,
        help="GCP project ID to scan (overrides project discovery within declared scope).",
    )
    parser.add_argument(
        CLUSTER_FLAG,
        default=None,
        help="Optional cluster filter (`<location>/<name>` or `<name>`).",
    )
    parser.add_argument(
        LOCATION_FLAG,
        default=None,
        help="Optional cluster location (region or zone) when `--cluster` is a bare name.",
    )
    parser.add_argument(
        KUBECONFIG_FLAG,
        default=None,
        help="Optional path to an existing kubeconfig for the target cluster (skips get-credentials).",
    )
    parser.add_argument(
        KUBECONFIG_DIR_FLAG,
        default=str(DEFAULT_KUBECONFIG_DIR),
        help="Directory for per-target kubeconfigs written by `gcloud container clusters get-credentials`.",
    )
    parser.add_argument(
        OUTPUT_FLAG,
        default=None,
        help="Optional file path to write the JSON report to.",
    )
    parser.add_argument(
        NON_BURSTING_FLAG,
        action="store_true",
        default=False,
        help=(
            "Evaluate CPU rounding and QoS burst impact for a non-bursting Autopilot target "
            "(250m floor/steps and requests == limits)."
        ),
    )
    fleet_scope_args.add_scope_arguments(parser)
    return parser


def main(argv: list[str] | None = None, *, run: RunFn = default_run) -> int:
    """Entry point for CLI execution."""
    parser = build_parser()
    args = parser.parse_args(argv)

    scope = fleet_scope_args.DeclaredScope()
    scope.set(
        args.scope_projects,
        args.scope_unread,
        args.scope_exclude_clusters,
    )

    kc_override = Path(args.kubeconfig) if args.kubeconfig else None
    kc_dir = Path(args.kubeconfig_dir) if args.kubeconfig_dir else DEFAULT_KUBECONFIG_DIR
    bursting_override: bool | None = False if args.non_bursting else None

    report = scan_fleet(
        project_override=args.project,
        cluster_filter=args.cluster,
        location_filter=args.location,
        kubeconfig=kc_override,
        kubeconfig_dir=kc_dir,
        bursting=bursting_override,
        scope=scope,
        run=run,
    )

    if args.output:
        write_atomic_json(Path(args.output), report)

    sys.stdout.write(render_markdown(report))
    return EXIT_ERROR if report.get("errors") else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
