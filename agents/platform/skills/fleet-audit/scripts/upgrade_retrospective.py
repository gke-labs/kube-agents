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
      which the daily readiness watch will read before the next upgrade.

Symptoms, (C) rows and guards are keyed by a pod's top owner (Deployment,
CronJob, StatefulSet, DaemonSet; a bare pod stays a Pod), so one finding
covers every replica and a guard goes away when the owner's replacement pods
are healthy. A budget stays keyed as a budget.

A cluster is reviewed when it is *new* (absent from the ledger) or *upgraded*
(a version differs from the ledger, or an upgrade operation started since the
last run). Every other cluster is listed as unchanged.

The Markdown report has three sections, Errors, Warnings and Info. An entry
under the first two is one incident: one owner object (or budget) on one
cluster, carrying the four parts above inline. Error is a high-confidence
classified symptom on a user workload, an operation GKE reported failed, or a
node NotReady after its pool was upgraded; Warning is a medium-confidence or
unclassified symptom, anything in a system namespace, and a guard still live
from an earlier run on a cluster this run did not review; Info is each clean
review's (A) summary, the unchanged clusters, and the reads that failed. A
read that fails is listed rather than failing the run: a cluster this run
could not reach is a gap the report names, not a reason to drop the rest.
Each reviewed cluster also gets an Info block: the next upgrade (the
channel's current default as the target, the maintenance window and when it
next opens, exclusions with scope and end), the catalogue shapes present
(static before-signals: a budget allowing no disruption, a selector on a
deprecated node label, an image on a retired registry, an in-tree PD volume,
a user DaemonSet on hostNetwork or the containerd socket, state on a
node-local volume, a CUDA pin on a GPU workload, a fail-closed webhook with
no backend, a pre-cgroup-v2 runtime image on an exposed pool; GKE's own
agents are counted on one line),
and the baseline recorded. Shapes are risks, never incidents; each writes a
`risk` guard beside the symptoms' `failure` guards. The JSON carries the same
grouping under `sections` beside the per-cluster `reviews`.

Projects come from `--project`, else the active gcloud project plus
`gcloud projects list`, exactly as `collect.py` discovers the fleet.

Only a run invoked with `--full` is *full* (`--full` with `--cluster` is a
usage error): it records the fleet's project set in the ledger and may
change it (a project absent from its `--project` set is pruned with its
clusters and guards, a joined project is added), prunes departed clusters,
refreshes every fleet cluster's stored symptom set (pods, nodes and owners,
one list each), writes `reports/<finish-UTC>.md` and moves
`upgrade-retro-report.md` to it. A roster project whose listing failed is
that project's gap, not the run's: its entries and guards stay unchanged,
its known clusters are listed under "Reads that failed", and the run stays
full. Every other run is *scoped*, whatever its `--project` set: it reviews
its targets, never adds a project to the fleet (a cluster outside it is
reported "outside the fleet; not recorded" and gets no ledger entry or
guard), prunes nothing, reports no stale guard outside its scope, writes
`reports/<finish-UTC>-scoped.md` and leaves the link alone; `--since` is a
hand run that widens the window and advances no last-run time. The newest fourteen reports of each kind are kept. Each
symptom carries an onset read from the object (a Pending pod's scheduling
transition or start; for a pod that is not Ready the `Ready` condition's
transition to False, else its start, never its latest crash; an event's
first observation; a node condition's transition); one whose onset predates the window's first operation, or
that the previous full run recorded, is a Warning that says so, never an
Error; with no readable onset the stored set decides, and a first run grades
by onset alone and says so. Confidence is high only when the signature names
the entry's own mechanism and an operation in the window reached the object
(its pool; the control plane for 6 and 7); entry 14 also needs a runtime
image below the floor, entry 20 the same image still running on an
untouched pool. A crash record beside no ledger blocks every run until
`--reset-ledger` archives it; a run starts from empty only when neither
exists.

The store (`ledger.json`, `guards.json`, `reports/`) lives on the shell
sandbox's data volume under /opt/data/upgrade-retrospective, readable by the
agent's tools in any session and surviving restarts. One run at a time: an
exclusive lock on `.lock` there is held for the whole run, and a second run
exits 0 saying so with nothing written (`--dry-run` takes no lock). A run
writes its report, then the JSON, then the guards, then the ledger last, each
atomically, so a crash leaves at most a report with no ledger advance. A
ledger or guards file that exists but cannot be read, or carries another
version, is moved aside as `<name>.unreadable-<ts>` and the run stops: that
file is a crash record, not a re-baseline, and a run proceeds as a first run
only when no ledger file existed at all. A cluster with an upgrade operation
still in flight is not reviewed; Info lists it as upgrading now, nothing is
read from it and no guard is written, and only operations that ended inside
the window count. An unchanged cluster
that still holds a live guard is re-read cheaply every run -- only the reads
its guards need -- so a guard clears when an operator's fix lands between
upgrades, without waiting for the next one. The weekly run therefore
re-reads every cluster that holds a guard, and with risk guards present
that is most of the fleet; the SOP sizes the schedule for that. A failure
guard whose only source was an event is not re-checkable (a re-check reads
no events) and waits for the cluster's next full review.

Every subprocess goes through `default_run`; tests inject a fake in its place.
Nothing here writes to a cluster: `gcloud ... list`, `get-credentials` into a
private kubeconfig, and `kubectl get`.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
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
# The store: ledger, guards and reports. The collector runs in the agent's
# shell sandbox, whose /opt/data is the sandbox's own volume rather than the
# gateway's profile volume, so the path is absolute there and not derived
# from HERMES_HOME; `UPGRADE_RETROSPECTIVE_HOME` overrides it.
STORE_HOME_ENV = "UPGRADE_RETROSPECTIVE_HOME"
DEFAULT_STORE_DIR = "/opt/data/upgrade-retrospective"
DATA_SUBDIR = "upgrade-retrospective"
LEDGER_FILENAME = "ledger.json"
GUARDS_FILENAME = "guards.json"
REPORTS_SUBDIR = "reports"
# One run at a time per store: an exclusive, non-blocking lock on this file.
LOCK_FILENAME = ".lock"
REPORT_IS_LINK_TEXT = "--report must not be named {name}: that is the link a full run points at the newest report"
LOCK_HELD_TEXT = "another retrospective run holds {path} since {since}; nothing written"
STATE_UNREADABLE_DRY_RUN_TEXT = "{path} {why}; a dry run moves nothing and stops here. Nothing written."
STATE_NOT_MOVED_TEXT = "{path} {why} and could not be moved aside ({error}); it is unchanged in place. Fix or move it by hand, then rerun. Nothing written."
STATE_UNREADABLE_TEXT = "{path} {why}; moved to {aside}. A set-aside ledger is a crash record, not a re-baseline: restore it or remove it on purpose, then rerun. Nothing written."
EXIT_USAGE = 2
# Reports are named by the run's finish time in UTC; a scoped run (one that
# did not cover the whole fleet) is marked so it never stands for the fleet.
REPORT_TS_FORMAT = "%Y%m%dT%H%M%SZ"
REPORT_FILENAME = "{ts}{scoped}.md"
REPORT_JSON_FILENAME = "{ts}{scoped}.json"
SCOPED_SUFFIX = "-scoped"
REPORT_NAME_RE = re.compile(r"^(\d{8}T\d{6}Z)(-scoped)?\.(md|json)$")
# How many reports of each kind the store keeps.
REPORTS_KEPT = 14
LATEST_REPORT_LINK = "upgrade-retro-report.md"
# The report file and its link are swapped in through these.
REPORT_TEMP_SUFFIX = ".tmp"
LINK_TEMP_SUFFIX = ".tmp-link"
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

# Project discovery, as `collect.py`'s `discover_fleet`: `--project`, else
# the active gcloud project plus every project `gcloud projects list`
# returns. No environment variable: this script reads the same fleet as its
# three siblings in this directory.
# gcloud's words for a project whose Kubernetes Engine API is off: it cannot
# hold a cluster, so its failed listing is an empty project, not a lost one.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")

OP_UPGRADE_MASTER, OP_UPGRADE_NODES = "UPGRADE_MASTER", "UPGRADE_NODES"
UPGRADE_OPERATION_TYPES = (OP_UPGRADE_MASTER, OP_UPGRADE_NODES)
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
REASON_ERROR = "Error"
REASON_COMPLETED = "Completed"
REASON_UNSCHEDULABLE = "Unschedulable"
REASON_FAILED_SCHEDULING = "FailedScheduling"
CONTAINER_FAILURE_REASONS = ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError", REASON_ERROR)
# Symptom categories, selection statuses, and the catalogue entries the
# classifier and the shape detectors decide in code.
CATEGORY_PENDING, CATEGORY_NOT_READY, CATEGORY_NODE, CATEGORY_EVENT, CATEGORY_PDB = "pending", "not-ready", "node", "event", "pdb"
STATUS_NEW, STATUS_UPGRADED, STATUS_FORCED = "new", "upgraded", "forced"
# A symptom against the ledger's baseline from the cluster's last review.
SINCE_FIRST_SEEN, SINCE_NEW, SINCE_BEFORE = "first seen", "new since the last review", "present before"
PREDATES_UPGRADE_TEXT = "predates the upgrade: its onset is before this window's first operation, or the previous full run already recorded it; graded Warning"
FIRST_RUN_GRADING_TEXT = "first run: graded by onset only, no previous symptom set."
# The fleet: the project set a full run records in the ledger. A scoped run
# reviews a cluster outside it but records nothing for it.
LEDGER_PROJECTS_KEY = "projects"
OUTSIDE_FLEET_TEXT = "outside the fleet; not recorded"
FULL_WITH_CLUSTER_TEXT = "--full names a fleet-wide run and cannot be combined with --cluster"
NOT_FULL_TEXT = "no --full: a scoped run"
LISTING_FAILED_TEXT = "{cluster}: its project's listing failed ({error}); ledger entry and guards kept unchanged"
# A crash record beside no ledger blocks every run until it is archived.
CRASH_RECORD_GLOB = LEDGER_FILENAME + ".unreadable-*"
ARCHIVE_SUBDIR = "archive"
CRASH_RECORD_TEXT = "{path} sits beside no ledger: a crash record, not a re-baseline. Restore it as the ledger or run --reset-ledger to archive it; until then nothing starts from empty. Nothing written."
RESET_LEDGER_TEXT = "archived {count} crash record(s) under {archive}; the next run starts fresh."
RESET_LEDGER_NOTHING_TEXT = "no crash record to archive."
# The full-run refresh of every fleet cluster's symptom set: pods for the
# symptoms, nodes for the pools, owners so the keys match the review's.
REFRESH_READS = ("pods", "nodes", "owners")
BASELINE_SEPARATOR = "|"
ENTRY_BUDGET, ENTRY_CAPACITY, ENTRY_NODE_LOCAL_STATE, ENTRY_REMOVED_API, ENTRY_WEBHOOK = 1, 2, 4, 6, 7
ENTRY_NODE_LABEL, ENTRY_RUNTIME, ENTRY_CGROUP_V2, ENTRY_OOM_GROUP, ENTRY_NODE_AGENT = 12, 13, 14, 15, 17
ENTRY_GPU, ENTRY_IN_TREE_VOLUME, ENTRY_REGISTRY = 18, 19, 20
# Entries whose signature is text an upgrade did not necessarily cause: high
# only when a node-pool operation in the window touched the pod's pool.
POOL_GATED_ENTRIES = (ENTRY_CAPACITY, ENTRY_GPU, ENTRY_REGISTRY)
GATE_CLOSED_TEXT = "no operation in the window touched this object's pool"
# Entries whose mechanism is the control plane's; their gate is an
# UPGRADE_MASTER in the window rather than a node-pool operation.
CONTROL_PLANE_ENTRIES = (ENTRY_REMOVED_API, ENTRY_WEBHOOK)
# Reasons a Warning event is kept for, plus any message matching
# `EVENT_MESSAGE_MARKERS` whatever its reason.
EVENT_REASONS = (REASON_FAILED_SCHEDULING, "FailedMount", "FailedAttachVolume", "BackOff", "FailedCreate", "Unhealthy", "OOMKilling")
EVENT_MESSAGE_MARKERS = ("failed calling webhook", "no matches for kind")
# Event reasons a pod's own status already carries when the pod is listed.
EVENT_REASONS_IMPLIED_BY_POD = (REASON_FAILED_SCHEDULING, "BackOff")
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
    (ENTRY_WEBHOOK, HIGH, SCOPE_ANY, re.compile(r"failed calling webhook")),
    (ENTRY_REMOVED_API, HIGH, SCOPE_ANY, re.compile(r"no matches for kind")),
    (ENTRY_IN_TREE_VOLUME, HIGH, SCOPE_ANY, re.compile(r"PersistentVolume's node affinity")),
    (ENTRY_IN_TREE_VOLUME, MEDIUM, SCOPE_REASON, re.compile(r"^FailedAttachVolume$")),
    # A FailedMount is a ConfigMap or Secret as often as a disk, and the
    # kubelet's mount-timeout sentence is the same for both; only a message
    # naming an attach or a persistent volume counts.
    (ENTRY_IN_TREE_VOLUME, MEDIUM, SCOPE_ANY, re.compile(r"^FailedMount .*(?:AttachVolume|PersistentVolume|\bpvc-|\bpv-)")),
    (ENTRY_NODE_LABEL, HIGH, SCOPE_SCHEDULING, re.compile(r"didn't match (?!PersistentVolume)[^,.]*node (?:selector|affinity)")),
    (ENTRY_GPU, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient nvidia\.com/gpu")),
    (ENTRY_CAPACITY, HIGH, SCOPE_SCHEDULING, re.compile(r"Insufficient (?:cpu|memory)")),
    (ENTRY_REGISTRY, HIGH, SCOPE_REASON, re.compile(r"^(?:ImagePullBackOff|ErrImagePull)$")),
    # Driver and CUDA error text only: an image name containing `nvidia`
    # in a pull back-off is entry 20, not a driver mismatch.
    (ENTRY_GPU, HIGH, SCOPE_CONTAINER, re.compile(r"Error 803|CUDA driver version|unsupported display driver|NVML|nvidia-container-cli|could not select device driver")),
)
# The reads per cluster. `owners` is the intermediates a pod's
# ownerReferences stop at: a ReplicaSet names its Deployment and a Job its
# CronJob only in their own metadata.
KUBECTL_READS = (
    ("pods", ["kubectl", "get", "pods", "-A", "-o", "json"]),
    ("nodes", ["kubectl", "get", "nodes", "-o", "json"]),
    ("events", ["kubectl", "get", "events", "-A", "--field-selector", "type=Warning", "-o", "json"]),
    ("pdbs", ["kubectl", "get", "pdb", "-A", "-o", "json"]),
    ("owners", ["kubectl", "get", "replicasets,jobs", "-A", "-o", "json"]),
    # The four below feed the shape detectors only; a failure there loses
    # the risks table, not the review.
    ("workloads", ["kubectl", "get", "deploy,ds,sts,cronjobs", "-A", "-o", "json"]),
    ("storage", ["kubectl", "get", "pv,storageclasses", "-o", "json"]),
    ("webhooks", ["kubectl", "get", "validatingwebhookconfigurations,mutatingwebhookconfigurations", "-o", "json"]),
    ("endpointslices", ["kubectl", "get", "endpointslices", "-A", "-o", "json"]),
)
SHAPE_READS = ("workloads", "storage", "webhooks", "endpointslices")
# A review needs these to have answered; the shape reads are optional.
CORE_READS = ("pods", "nodes", "events", "pdbs", "owners")

# Shapes: the catalogue's before-signals, read statically. Each is a risk,
# never an incident. The labels a kubelet stopped setting (entry 12), the
# registries that stopped publishing (20), the in-tree PD provisioner (19),
# the containerd socket a node agent couples itself to (13), and the names
# that say a node-local volume holds state (4).
DEPRECATED_NODE_LABEL_PREFIXES = ("beta.kubernetes.io/", "failure-domain.beta.kubernetes.io/")
RETIRED_IMAGE_HOSTS = ("k8s.gcr.io/", "gcr.io/google-containers/", "gcr.io/google_containers/", "gcr.io/kubernetes-helm/")
IN_TREE_PD_PROVISIONER = "kubernetes.io/gce-pd"
IN_TREE_PD_VOLUME_KEY = "gcePersistentDisk"
PD_CSI_ADDON_KEY = "gcePersistentDiskCsiDriverConfig"
CSI_PD_PROVISIONER = "pd.csi.storage.gke.io"
CONTAINERD_SOCKET_PATH_PREFIXES = ("/run/containerd", "/var/run/containerd")
# A cache is the acceptable use the catalogue names, so it is not here; the
# words are anchored so `nginx-cache` and `wal-e-bin` do not match on a syllable.
STATEFUL_VOLUME_NAME_RE = re.compile(r"(?<![a-z])(?:data|state|db|queue|store|journal|wal|persist)(?![a-z])", re.I)
LOCAL_SSD_HOSTPATH_PREFIXES = ("/mnt/disks", "/mnt/stateful_partition")
GPU_RESOURCE = "nvidia.com/gpu"
# A CUDA pin in an image tag (`nvidia/cuda:12.2.0-base`) or an env value.
CUDA_IMAGE_PIN_RE = re.compile(r"cuda[:/_-]?(\d+\.\d+)", re.I)
CUDA_ENV_NAME_MARKER = "CUDA"
VERSION_IN_TEXT_RE = re.compile(r"\d+\.\d+")
WEBHOOK_FAIL_CLOSED = "Fail"
# Kubernetes' default admission timeout; a webhook at or above it with no
# namespaceSelector is the catalogue's full shape, below it a lesser one.
WEBHOOK_DEFAULT_TIMEOUT_S = 10
WEBHOOK_LONG_TIMEOUT_S = 10
# A webhook stops a drain only if its rules reach pod creation: core group,
# `pods` (or a wildcard), operation CREATE (or a wildcard); no rules match nothing.
WEBHOOK_POD_GROUPS = ("", "*")
WEBHOOK_POD_RESOURCES = ("pods", "*", "*/*", "pods/*")
WEBHOOK_POD_OPERATIONS = ("CREATE", "*")
WEBHOOK_CONFIGURATION_KINDS = ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration")
ENDPOINTSLICE_SERVICE_LABEL = "kubernetes.io/service-name"
SINGLE_REPLICA = 1
# Namespaces whose DaemonSets are GKE's own agents: they move with the node
# image, so 13 and 17 there are counted on one line, not reported or guarded.
# `is_system_namespace` covers kube-system, gmp-system and every `gke-*`
# (gke-gmp-system, gke-managed-*); Config Sync's are the one more family.
MANAGED_AGENT_NAMESPACE_PREFIXES = ("config-management-",)
MANAGED_AGENT_ENTRIES = (ENTRY_RUNTIME, ENTRY_NODE_AGENT)
MANAGED_AGENTS_LINE = "{count} GKE-managed agents use host networking or the runtime socket; upgraded with the node image."
# Entry 14: runtimes that read their memory limit from cgroup v1 paths, by
# image repository and the first tag that reads cgroup v2, from the cgroup
# v2 page: JDK 8u372 and 11.0.16; .NET 3.1 (so 2.x and 3.0 are affected).
JAVA_IMAGE_REPOS = ("eclipse-temurin", "openjdk", "adoptopenjdk")
JAVA_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?:u(\d+)|\.(\d+)\.(\d+))")
JDK8_CGROUP_V2_UPDATE = 372
JDK11_CGROUP_V2_PATCH = (0, 16)
JDK_FIRST_MAJOR_WITH_CGROUP_V2 = 15
DOTNET_IMAGE_REPO_MARKERS = ("mcr.microsoft.com/dotnet/", "microsoft/dotnet")
DOTNET_TAG_RE = re.compile(r"^(\d+)\.(\d+)")
DOTNET_CGROUP_V2_VERSION = (3, 1)
# The minor at which GKE migrates a cgroup v1 pool to v2.
CGROUP_V2_MIGRATION_MINOR = 33
# Kinds whose spec carries a pod template.
TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob")
# What the risks table says it looked for when it found nothing.
# What the risks table says it looked for when it found nothing, each with
# the reads it needs; a check whose read failed is listed as not performed.
SPEC_CHECK_READS = ("pods", "owners", "workloads")
SHAPE_CHECKS = (
    ("budgets allowing no disruption and single-replica workloads behind a budget (1)", ("pdbs", "workloads")),
    ("selectors on beta.kubernetes.io / failure-domain.beta.kubernetes.io labels (12)", SPEC_CHECK_READS),
    ("images on a retired registry host (20)", SPEC_CHECK_READS),
    ("in-tree gcePersistentDisk volumes and kubernetes.io/gce-pd StorageClasses (19)", ("storage",)),
    ("DaemonSets on hostNetwork (17) or mounting the containerd socket (13)", SPEC_CHECK_READS),
    ("emptyDir or local-SSD volumes whose name suggests state (4)", SPEC_CHECK_READS),
    ("GPU workloads pinning a CUDA version (18)", SPEC_CHECK_READS),
    ("fail-closed webhooks whose Service has no ready endpoint (7)", ("webhooks", "endpointslices")),
    ("pre-cgroup-v2 runtime images on a cgroup v2 pool or a v1 pool GKE will migrate at 1.33 (14; pinned tags only, floating tags such as 8-jre are not matched)", SPEC_CHECK_READS + ("nodes",)),
    ("not checked statically: a multi-process container (15) is not visible from the spec", ()),
)
NOT_CHECKED_TEXT = "; not checked, their reads failed: "
# Guard kinds: a `failure` broke the last upgrade, a `risk` is a shape
# present before the next one.
GUARD_KIND_FAILURE, GUARD_KIND_RISK = "failure", "risk"
# Which reads can show a guard's finding again. A guard is dropped only when
# every one of them answered on the review that did not see it; a slow
# `kubectl get pods` must not erase a cluster's memory.
FAILURE_GUARD_READS = ("pods", "nodes", "events", "pdbs", "owners")
# A failure guard whose only source was an event cannot be re-observed by a
# re-check, which reads no events; it is kept until the next full review.
GUARD_SOURCE_EVENT = CATEGORY_EVENT
RECHECK_NOT_RECHECKABLE_TEXT = "{count} event-only guard(s) not re-checkable; cleared by the next review of the cluster"
STALE_PARTIAL_TEXT = "cluster read partially this run ({failed}); the guard below could not be re-observed and was kept."
STALE_EVENT_ONLY_TEXT = "cluster re-checked this run; the guard below came from an event alone and a re-check reads no events, so it is kept until the cluster's next full review."
STALE_RECHECK_FAILED_TEXT = "cluster re-checked this run but the read failed ({errors}); the guard below was kept."
SPEC_SHAPE_READS = ("pods", "owners", "workloads")
SHAPE_READS_BY_ENTRY = {
    ENTRY_BUDGET: ("pdbs", "workloads"),
    ENTRY_NODE_LABEL: SPEC_SHAPE_READS,
    ENTRY_REGISTRY: SPEC_SHAPE_READS,
    ENTRY_NODE_LOCAL_STATE: SPEC_SHAPE_READS,
    ENTRY_GPU: SPEC_SHAPE_READS,
    # The owner's pools come from the node list; without it every pool counts.
    ENTRY_CGROUP_V2: SPEC_SHAPE_READS + ("nodes",),
    ENTRY_RUNTIME: SPEC_SHAPE_READS,
    ENTRY_NODE_AGENT: SPEC_SHAPE_READS,
    ENTRY_IN_TREE_VOLUME: ("storage",),
    ENTRY_WEBHOOK: ("webhooks", "endpointslices"),
}
# A pod a controller named: ReplicaSet, Job and DaemonSet pods end in a
# five-character hash, StatefulSet pods in an ordinal. Used to charge an
# event on a pod that no longer exists to its owner.
POD_HASH_SUFFIX_RE = re.compile(r"^[a-z0-9]{5}$")
POD_ORDINAL_SUFFIX_RE = re.compile(r"^\d+$")
PREFIX_RESOLVED_KINDS = ("ReplicaSet", "Job", "DaemonSet", "StatefulSet")
# An unreadable or foreign-version state file is moved here, never overwritten.
UNREADABLE_SUFFIX = ".unreadable-{ts}"
# A re-check asks whether a finding is still present, not when it began, so
# it bounds nothing by time.
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# A re-check reads only what a cluster's live guards need: a failure guard
# its pods and owners (nodes for the pool, budgets for an entry-1 guard), a
# risk guard the shape's feeding reads.
RECHECK_FAILURE_READS = ("pods", "owners", "nodes")
RECHECK_BUDGET_READS = ("pdbs",)
# Tenant text reaches the Markdown only through `_cell`: control characters
# go, and the three characters that could open a cell, a code span or a line.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
CELL_ESCAPES = (("|", "/"), ("`", "'"))

# The next upgrade, from the cluster record and `get-server-config`.
NO_WINDOW_TEXT = "no maintenance window: an upgrade may start at any hour"
DEFAULT_EXCLUSION_SCOPE = "NO_UPGRADES"
RRULE_FREQ_DAILY, RRULE_FREQ_WEEKLY = "DAILY", "WEEKLY"
RRULE_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# How far ahead the next window opening is searched: a weekly rule repeats
# inside this many days.
NEXT_WINDOW_SEARCH_DAYS = 8
DAILY_WINDOW_TIME_FORMAT = "%H:%M"
# GKE's default daily window length when the record carries none.
DEFAULT_DAILY_WINDOW = timedelta(hours=4)
SECONDS_PER_HOUR = 3600
ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-gke\.(\d+))?$")
# Symptoms, (C) rows and guards are keyed by a pod's top owner so a finding
# survives the pods being replaced: Pod -> ReplicaSet -> Deployment, Pod ->
# Job -> CronJob, Pod -> StatefulSet/DaemonSet; a bare pod stays a Pod. The
# kinds here are the ones with an owner of their own worth one more hop.
OWNER_INTERMEDIATE_KINDS = ("ReplicaSet", "Job")
OWNER_MAX_HOPS = 3
# How a pod-backed symptom's evidence names the pods behind it.
POD_EVIDENCE_FORMAT = "{count} of {total} pods: {evidence}; e.g. {example}"
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
# `read_today` is the first sentence of the catalogue's "Read today" line,
# verbatim; the test parses the catalogue to hold the two together.
MITIGATIONS = {
    1: {
        "title": "A PodDisruptionBudget forbids the eviction",
        "before": "A budget whose disruptionsAllowed is 0 for a reason that will not clear (maxUnavailable 0, minAvailable at the replica count, or a singleton behind a budget).",
        "read_today": "the readiness mode of `fleet-upgrade-verification` grades it `blocked`, and the obtainability audit reports it as `blocking-pdb`",
        "mitigate_before": "Give the budget room (maxUnavailable at least 1 or minAvailable below the replica count) and a second replica, or accept the outage inside a window.",
        "mitigate_after": "Fix the budget and the stalled drain resumes; never delete the budget without replacing it.",
    },
    2: {
        "title": "No spare capacity for the displaced pods",
        "before": "A pool whose upgrade settings let a node go before its replacement exists, with requests near allocatable, the autoscaler at its ceiling or quota exhausted.",
        "read_today": "the stockout-prevention audit flags regional GPU, TPU and CPU quota near exhaustion and pools near `autoscaling.maxNodeCount`; `maxSurge` and headroom against allocatable are unread",
        "mitigate_before": "Keep the pool on maxSurge at least 1 and maxUnavailable 0, with one node of headroom and quota for the extra node.",
        "mitigate_after": "Add a node or raise the ceiling and the Pending pods schedule.",
    },
    3: {
        "title": "Every replica in one zone or on one node",
        "before": "Replicas of one Deployment on one node or in one zone, with no spread or anti-affinity.",
        "read_today": "the obtainability audit's spread and pinning checks",
        "mitigate_before": "topologySpreadConstraints across zones and hosts, or anti-affinity, and a budget so the drain waits between replicas.",
        "mitigate_after": "The next rollout re-spreads the pods once the constraints are in place.",
    },
    4: {
        "title": "Data on the node is gone",
        "before": "Pods keeping state they cannot rebuild on Local SSD or emptyDir.",
        "read_today": "nothing as an upgrade risk; the fleet waste audit reads `emptyDir` and `hostPath` volumes only as scale-down blockers",
        "mitigate_before": "State on PersistentVolumes or object storage; node-local disk only for what can be rebuilt.",
        "mitigate_after": "Restore from the source of truth.",
    },
    5: {
        "title": "Maintenance window too short, or an exclusion ends mid-roll",
        "before": "Window length against node count times drain time; an exclusion ending inside the planned change.",
        "read_today": "the readiness mode grades the covering exclusion; the security-patch orchestrator reads the window",
        "mitigate_before": "A window long enough for node count times drain time, exclusions that end outside the change, blue-green where the window is tight.",
        "mitigate_after": "Extend the window or finish the upgrade by hand so the pool stops running two versions.",
    },
    6: {
        "title": "A served API version is removed",
        "before": "Clients still calling an API version the target minor removes (the deprecation insight, audit entries labelled k8s.io/removed-release, declared apiVersions).",
        "read_today": "the deprecation scan in `fleet-upgrade-verification` reads the `apiVersion`s a linked GitOps repository declares in raw YAML and JSON, skipping Helm templates",
        "mitigate_before": "Migrate the callers: bump client libraries and kubectl, rewrite manifests and Helm release state to the new version.",
        "mitigate_after": "The stored objects still exist; re-apply them through the new version and the clients recover.",
    },
    7: {
        "title": "A fail-closed webhook whose backend is not up",
        "before": "A webhook with failurePolicy Fail, a long timeout, a Service with no endpoints and no namespaceSelector exempting kube-system.",
        "read_today": "nothing before an upgrade; the upgrade skill's stuck-upgrade steps check whether webhooks are rejecting pod creation on new nodes, after the fact",
        "mitigate_before": "Exempt kube-system, a timeout of a few seconds, failurePolicy Ignore for webhooks that are not security controls, two backend replicas behind a budget.",
        "mitigate_after": "Set failurePolicy Ignore or remove the configuration to unwedge the cluster, then restore it once the backend is up.",
    },
    8: {
        "title": "A default changes in the new minor",
        "before": "The target minor's release notes read against the cluster: admission enforcement, seccomp defaults, feature gates that flip on.",
        "read_today": "nothing reads a namespace's admission pin; the upgrade plan hands the compatibility check to the operator after at most a quick search",
        "mitigate_before": "Read the target minor's notes, run Pod Security Admission in warn and audit before enforce, rehearse on staging at the target version.",
        "mitigate_after": "The audit log names the rejecting rule; relabel the namespace or adjust the pod.",
    },
    9: {
        "title": "A feature is deprecated but still served",
        "before": "Deprecation warnings in API responses and audit logs (k8s.io/deprecated) for a feature with a removal date.",
        "read_today": "nothing scheduled; the audit log stamps every deprecated call (`k8s.io/deprecated`), and the agent can read it on request",
        "mitigate_before": "Plan the migration while the feature still works.",
        "mitigate_after": "None needed yet.",
    },
    10: {
        "title": "Add-on and client skew",
        "before": "Installed add-on versions against their support matrices for the target minor; a node pool further behind the control plane than the skew policy allows.",
        "read_today": "the readiness mode grades node-pool skew, and the security-patch orchestrator's `pool-skew` check flags a pool too far behind its control plane every Monday; add-on and client skew are unread",
        "mitigate_before": "Upgrade add-ons to a version whose matrix includes the target before the cluster moves; keep node pools inside the skew window.",
        "mitigate_after": "Upgrade the add-on.",
    },
    11: {
        "title": "The control plane is unreachable for minutes on a zonal cluster",
        "before": "The cluster is zonal; whether its clients retry is not readable from the cluster.",
        "read_today": "nothing as an upgrade risk; the cluster inventory audit records each control plane's location, and the upgrade skill recommends regional control planes when asked",
        "mitigate_before": "A regional cluster for anything automation depends on, and retries with backoff in the clients.",
        "mitigate_after": "Wait for the control plane; GitOps resyncs on its own.",
    },
    12: {
        "title": "A node label is removed",
        "before": "nodeSelector and affinity terms naming a label the target minor's kubelet or node image stops setting.",
        "read_today": "nothing",
        "mitigate_before": "Replace deprecated labels in selectors with their GA names (kubernetes.io/arch, topology.kubernetes.io/zone).",
        "mitigate_after": "Patch the selector; the pods schedule.",
    },
    13: {
        "title": "The container runtime changes",
        "before": "Node agents on the CRI v1alpha2 API, images in the Docker v1 schema, DaemonSets shipping containerd 1.x configuration.",
        "read_today": "the security-patch orchestrator flags a pool whose `config.imageType` the location no longer offers or that names a pre-containerd variant, and the compliance audit's `hostpath-mount` check flags a pod mounting the containerd socket as a security finding; CRI clients, image schemas and containerd configuration are unread as an upgrade risk",
        "mitigate_before": "Move agents to the CRI v1 API, rebuild v1-schema images, drop containerd 1.x overrides.",
        "mitigate_after": "The same changes under pressure; a completed pool can be downgraded in place while GKE still offers the previous version.",
    },
    14: {
        "title": "cgroup v2 under a runtime that cannot read it",
        "before": "A pool whose effectiveCgroupMode is v2 (or will be migrated at 1.33) running images with a JDK older than 8u372 or 11.0.16 or another runtime that reads cgroup v1 paths.",
        "read_today": "nothing",
        "mitigate_before": "A runtime that reads cgroup v2 or explicit heap flags; until 1.35 a pool can be pinned to cgroup v1 to buy time.",
        "mitigate_after": "The same, plus a temporary limit increase.",
    },
    15: {
        "title": "The OOM killer starts killing the whole container",
        "before": "A kubelet at 1.28 or later on a cgroup v2 node and containers running more than one process.",
        "read_today": "nothing",
        "mitigate_before": "Raise the limit for multi-process containers, split workers into their own containers, or set singleProcessOOMKill in the pool's node system config.",
        "mitigate_after": "The same; the container's own logs before the upgrade show which worker used to die.",
    },
    16: {
        "title": "The network dataplane changes",
        "before": "Dataplane and DNS provider, policy count, the known issues for the target version.",
        "read_today": "the fleet-consistency drift audit reads each cluster's `datapathProvider` and its network-policy settings across the cohort, so a member whose dataplane differs from its peers is reported, and the compliance audit's `netpol-missing` check flags a namespace with no protecting policy without asking whether the cluster enforces any; how a policy behaves, and the DNS provider, are unread",
        "mitigate_before": "Rehearse the target version on a staging cluster with the same dataplane; keep NetworkPolicy explicit.",
        "mitigate_after": "A completed pool can be downgraded in place while GKE still offers the previous version; the control plane cannot go back.",
    },
    17: {
        "title": "A node networking agent fails on the new image",
        "before": "The target node image's known issues; the CNI's dependence on node labels or kernel modules the image changes.",
        "read_today": "nothing as an upgrade risk; the compliance audit flags an agent's host networking and host-path mount as security findings",
        "mitigate_before": "Upgrade a canary pool first and watch Service routing from inside the cluster; surge with maxUnavailable 0.",
        "mitigate_after": "Downgrade the pool to the previous version while it is offered, and fix what the CNI selected on.",
    },
    18: {
        "title": "GPU driver mismatch",
        "before": "The driver the target node image ships against the CUDA version the images need, and whether the images carry forward-compatibility libraries.",
        "read_today": "nothing reads a workload's CUDA pin; the upgrade plan hands the driver and CUDA compatibility check to the operator after at most a quick search",
        "mitigate_before": "Match the driver to the images before the upgrade (GKE's driver table per version against NVIDIA's minimum-driver matrix); upgrade a canary GPU pool first.",
        "mitigate_after": "Recreate the pool with the driver version the images need.",
    },
    19: {
        "title": "In-tree volumes lose their CSI path",
        "before": "PersistentVolumes with an in-tree gcePersistentDisk spec while the PD CSI driver add-on is disabled; StorageClasses naming a retired provisioner.",
        "read_today": "nothing as an upgrade risk; the fleet waste audit reads disks for cost (orphaned volumes, unattached disks), not for how they are attached",
        "mitigate_before": "Enable the PD CSI driver add-on and move StorageClasses to pd.csi.storage.gke.io.",
        "mitigate_after": "Enable the add-on; the volumes attach.",
    },
    20: {
        "title": "Images on a retired registry",
        "before": "Image references on a registry hostname that has stopped publishing (k8s.gcr.io) or that an egress allowlist no longer admits.",
        "read_today": "nothing as an upgrade risk; the compliance audit reads image references for floating tags, not for retired hosts",
        "mitigate_before": "Mirror every image into a registry you own and keep egress allowlists in step.",
        "mitigate_after": "Retag or redirect the reference; the new nodes pull.",
    },
}
# When the cgroup mode is unknown an OOMKilled single-container pod is "14 or
# 15"; the report says so, and the guard carries the lower number.
OOM_UNDECIDED_ENTRIES = (ENTRY_CGROUP_V2, ENTRY_OOM_GROUP)

REPORT_TITLE = "# Upgrade retrospective {date}"
# The report's three sections. An incident under Errors or Warnings is one
# owner object (or budget) on one cluster, with the four parts inline.
SECTION_ERRORS = "## Errors"
SECTION_WARNINGS = "## Warnings"
SECTION_INFO = "## Info"
PART_WHAT_HAPPENED = "**What happened.**"
PART_WHAT_FAILED = "**What failed.**"
PART_MITIGATE = "**Detect and mitigate next time.**"
PART_MITIGATION_SET_UP = "**Mitigation set up.**"
PART_NEXT_UPGRADE = "**Next upgrade.**"
PART_RISKS = "**Risks present.**"
PART_BASELINE = "**Baseline recorded.**"
NO_SHAPE_TEXT = "no catalogue shape found; checked: "
# What an incident says when it has no catalogue entry to cite. `_none_` is
# reserved for an empty Errors or Warnings section.
UNCLASSIFIED_MITIGATION_TEXT = "no catalogue entry matched this symptom; it is reported for a reader to classify, and the entries' rows above do not apply."
UNCLASSIFIED_GUARD_TEXT = "no guard: an unclassified symptom writes none until it has an entry."
OPERATION_GUARD_TEXT = "no guard: an operation carries none; its cluster's risks are in the Info block."
INFO_UNCHANGED = "Unchanged clusters:"
INFO_REMOVED = "Removed clusters:"
INFO_RECHECKED = "Re-checked for live guards and refreshed symptom sets:"
PARTIAL_READ_TEXT = "Partially read ({failed}): the guards were kept and the cluster is re-read next run."
INFO_FAILED_READS = "Reads that failed:"
NONE_LINE = "_none_"
SEVERITY_ERROR, SEVERITY_WARNING = "error", "warning"
# Incident kinds: a symptom on an object, an operation GKE reported failed,
# a guard from an earlier run on a cluster this run did not review.
INCIDENT_SYMPTOM, INCIDENT_OPERATION, INCIDENT_STALE_GUARD = "symptom", "operation", "stale-guard"
# GKE operation statuses: only a DONE operation with an end time counts for
# a review; one still PENDING, RUNNING or ABORTING holds its cluster back.
OPERATION_TERMINAL_STATUS = "DONE"
OPERATION_IN_FLIGHT_STATUSES = ("PENDING", "RUNNING", "ABORTING")
INFO_UPGRADING = "Upgrading now:"
INFO_SCOPED = "Scoped run: {reason}. Nothing outside the scope was pruned, no stale guard outside it is reported, and the fleet link was not moved."
UPGRADING_LINE = "{cluster}: upgrading now ({operation} {target} since {start}); reviewed on the next run"
OPERATION_OBJECT_PREFIX = "operation/"
NO_OPERATION_LINE = "no upgrade operation in the window"
NODE_AFTER_POOL_UPGRADE_ENTRY = ENTRY_NODE_AGENT


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
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp (`--since 2026-10-01T00:00:00`) is UTC, which is
    # what the pod runs in; reading it in a laptop's zone would move the window.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def hermes_home() -> Path:
    return Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME)


def data_dir() -> Path:
    return Path(os.environ.get(STORE_HOME_ENV) or DEFAULT_STORE_DIR)


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
    that failed on the way: the active gcloud project plus every project
    `gcloud projects list` returns, as `collect.py` discovers."""
    errors: list[str] = []
    projects: set[str] = set()
    result = run(["gcloud", "config", "get-value", "project"])
    if result.rc == 0 and result.stdout.strip():
        projects.add(result.stdout.strip())
    result = run(["gcloud", "projects", "list", "--format", "value(projectId)"])
    if result.rc != 0:
        errors.append(f"gcloud projects list rc={result.rc}: {_excerpt(result)}; only the configured project was read")
    else:
        projects |= {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not projects:
        errors.append("project discovery named no project: no `--project` and no active gcloud project")
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


class StateUnreadable(Exception):
    """A ledger or guards file exists but cannot be used. The run stops: a
    set-aside ledger is a crash record, not a re-baseline, and a run proceeds
    as a first run only when no ledger file existed at all."""


def _set_aside(path: Path, why: str, now: datetime, move: bool = True) -> None:
    """Move a state file this run cannot use out of the way, so nothing
    overwrites it, and stop. A dry run moves nothing: it only reports."""
    if not move:
        raise StateUnreadable(STATE_UNREADABLE_DRY_RUN_TEXT.format(path=path, why=why))
    aside = path.with_name(path.name + UNREADABLE_SUFFIX.format(ts=now.strftime("%Y%m%dT%H%M%SZ")))
    try:
        os.replace(path, aside)
    except OSError as exc:
        raise StateUnreadable(STATE_NOT_MOVED_TEXT.format(path=path, why=why, error=exc)) from exc
    raise StateUnreadable(STATE_UNREADABLE_TEXT.format(path=path, why=why, aside=aside))


def load_json(path: Path, default: dict, *, version: int | None = None, now: datetime | None = None, move_aside: bool = True) -> dict:
    now = now or now_utc()
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError and a non-UTF-8 byte
        _set_aside(path, f"is unreadable ({exc})", now, move_aside)
    if not isinstance(loaded, dict):
        _set_aside(path, "is not a JSON object", now, move_aside)
    if version is not None and loaded.get("version") != version:
        _set_aside(path, f"has version {loaded.get('version')!r}, this collector writes {version}", now, move_aside)
    return loaded


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "updated_at": None, LEDGER_PROJECTS_KEY: [], "clusters": {}}


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


def _op_end(op: dict) -> datetime | None:
    return parse_ts(op.get("endTime"))


def _op_in_flight(op: dict) -> bool:
    return (op.get("status") or "") in OPERATION_IN_FLIGHT_STATUSES or ((op.get("status") or "") != OPERATION_TERMINAL_STATUS and _op_end(op) is None)


class Selection(NamedTuple):
    cluster: dict
    key: str
    status: str  # STATUS_NEW | STATUS_UPGRADED | STATUS_FORCED
    reasons: list[str]
    window_start: datetime
    operations: list[dict]


def select_clusters(clusters: list[dict], ledger: dict, operations: list[dict], *, since: datetime, forced: set[str], widen: bool = False) -> tuple[list[Selection], list[dict], list[dict]]:
    """Which clusters this run reviews, and why; the rest as unchanged rows;
    and the clusters an operation still in flight holds back until the next
    run. Only an operation that ended, inside the window, counts."""
    by_target: dict[tuple[str, str, str], list[dict]] = {}
    for op in operations:
        target = parse_target_link(op.get("targetLink", ""))
        if target and op.get("operationType") in UPGRADE_OPERATION_TYPES:
            by_target.setdefault((op.get("project") or "", target[0], target[1]), []).append(op)
    selected, unchanged, upgrading = [], [], []
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        cluster_ops = by_target.get((cluster["project"], cluster["location"], cluster["name"]), [])
        in_flight = [op for op in cluster_ops if _op_in_flight(op)]
        if in_flight:
            op = operation_summary(sorted(in_flight, key=lambda o: o.get("startTime") or "")[0])
            upgrading.append({"cluster": key, "operation": op})
            continue
        entry = (ledger.get("clusters") or {}).get(key)
        if entry is not None and not entry.get("last_run"):
            # Enumerated once but never reviewed (its reads failed): still new.
            entry = None
        current = versions_of(cluster)
        reasons: list[str] = []
        window_start = since
        if entry is None:
            status = STATUS_NEW
            reasons.append("first seen")
        else:
            status = STATUS_UPGRADED
            last_run = parse_ts(entry.get("last_run")) or since
            window_start = min(last_run, since) if (key in forced or widen) else last_run
            if entry.get("control_plane") != current["control_plane"]:
                reasons.append(f"control plane {entry.get('control_plane')} -> {current['control_plane']}")
            for pool, version in current["node_pools"].items():
                previous = (entry.get("node_pools") or {}).get(pool)
                # A pool created since the last run is new, not upgraded.
                if previous is not None and previous != version:
                    reasons.append(f"node pool {pool} {previous} -> {version}")
            partial = parse_ts(entry.get("partial_read"))
            if partial and partial >= last_run:
                reasons.append(f"previous review at {entry['partial_read']} read the cluster partially")
        ops = sorted(
            (op for op in cluster_ops if (op.get("status") or "") == OPERATION_TERMINAL_STATUS and _op_end(op) and _op_end(op) >= window_start),
            key=lambda op: op.get("startTime") or "",
        )
        if entry is not None:
            # Only an operation that ended since the last run makes a known
            # cluster "upgraded"; a forced review widens the window without that.
            last_run = parse_ts(entry.get("last_run")) or since
            recent = [op for op in ops if _op_end(op) >= last_run]
            if recent:
                reasons.append(f"{len(recent)} upgrade operation(s) since {fmt_ts(last_run)}")
        if key in forced:
            status = status if reasons else STATUS_FORCED
            reasons.append("forced by --cluster")
        if not reasons:
            unchanged.append({"cluster": key, "control_plane": current["control_plane"], "last_run": entry.get("last_run") if entry else None})
            continue
        selected.append(Selection(cluster, key, status, reasons, window_start, ops))
    unchanged.sort(key=lambda row: row["cluster"])
    upgrading.sort(key=lambda row: row["cluster"])
    return selected, unchanged, upgrading


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


def _api_marker_hits(pod: dict) -> list[str]:
    """Which of entry 6's best-effort markers the pod names, in its names,
    images, command, args and env values. Only the marker names leave this
    function: the text itself (an inline env value can be a credential) is
    never stored or written."""
    text = _pod_text(pod)
    return [marker for marker in DEPRECATED_API_MARKERS if marker in text]


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


def _controller_of(obj: dict) -> tuple[str, str] | None:
    for owner in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if owner.get("kind") and owner.get("name"):
            return owner["kind"], owner["name"]
    return None


class Resolver:
    """Maps any pod, ReplicaSet or Job to its top owner, from the pod list
    and the `owners` read. An intermediate the read did not return is itself
    the top: the hop is unknown rather than absent."""

    def __init__(self, pods: list[dict], owners: list[dict], workloads: list[dict] | None = None):
        self.parent: dict[tuple[str, str, str], tuple[str, str]] = {}
        # Controllers by namespace, longest name first, for a pod that is
        # gone and known only by the name an event carries.
        self.controllers: dict[str, list[tuple[str, str]]] = {}
        for pod in pods:
            controller = _controller_of(pod)
            meta = pod.get("metadata") or {}
            if controller:
                self.parent[(meta.get("namespace", ""), "Pod", meta.get("name", ""))] = controller
        for obj in list(owners) + list(workloads or []):
            meta = obj.get("metadata") or {}
            kind, namespace, name = obj.get("kind") or "", meta.get("namespace", ""), meta.get("name", "")
            controller = _controller_of(obj)
            if controller and kind in OWNER_INTERMEDIATE_KINDS:
                self.parent[(namespace, kind, name)] = controller
            if kind in PREFIX_RESOLVED_KINDS and name:
                self.controllers.setdefault(namespace, []).append((kind, name))
        for names in self.controllers.values():
            names.sort(key=lambda kn: -len(kn[1]))
        # Which pods each top owner has, for "n of m pods".
        self.pods_of: dict[str, list[str]] = {}
        for pod in pods:
            meta = pod.get("metadata") or {}
            kind, name = self.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
            self.pods_of.setdefault(_object_ref(meta.get("namespace", ""), kind, name), []).append(meta.get("name", ""))

    def _controller_by_prefix(self, namespace: str, pod_name: str) -> tuple[str, str] | None:
        for kind, name in self.controllers.get(namespace) or []:
            if not pod_name.startswith(name + "-"):
                continue
            suffix = pod_name[len(name) + 1:]
            if (kind == "StatefulSet" and POD_ORDINAL_SUFFIX_RE.match(suffix)) or (kind != "StatefulSet" and POD_HASH_SUFFIX_RE.match(suffix)):
                return kind, name
        return None

    def resolve(self, namespace: str, kind: str, name: str) -> tuple[str, str]:
        if kind == "Pod" and (namespace, kind, name) not in self.parent:
            by_prefix = self._controller_by_prefix(namespace, name)
            if by_prefix:
                kind, name = by_prefix
        for _ in range(OWNER_MAX_HOPS):
            parent = self.parent.get((namespace, kind, name))
            if parent is None:
                break
            kind, name = parent
        return kind, name

    def pod_total(self, owner_object: str) -> int:
        return len(self.pods_of.get(owner_object) or [])


def _pod_detail(pod: dict) -> dict:
    meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    containers = spec.get("containers") or []
    return {
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "container_count": len(containers),
        "images": [c.get("image", "") for c in containers],
        "labels": meta.get("labels") or {},
        "node_selector": spec.get("nodeSelector") or {},
        "api_markers": _api_marker_hits(pod),
        "started": status.get("startTime") or meta.get("creationTimestamp"),
    }


def _pod_last_activity(pod: dict) -> datetime | None:
    """When the pod last did something a symptom can date: the latest
    container termination, else the pod's start, else its creation."""
    status = pod.get("status") or {}
    stamps = []
    for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        for key in ("state", "lastState"):
            terminated = (cs.get(key) or {}).get("terminated") or {}
            if terminated.get("finishedAt"):
                stamps.append(parse_ts(terminated["finishedAt"]))
    # A pod that lost readiness without restarting is dated by that transition.
    ready = _condition(pod, "Ready")
    if ready.get("status") != "True" and ready.get("lastTransitionTime"):
        stamps.append(parse_ts(ready["lastTransitionTime"]))
    stamps = [t for t in stamps if t]
    if stamps:
        return max(stamps)
    return parse_ts(status.get("startTime")) or parse_ts((pod.get("metadata") or {}).get("creationTimestamp"))


def _pod_onset(pod: dict, activity: datetime | None) -> datetime | None:
    """When the symptom began, read from the object: a Pending pod's
    scheduling transition or start; for a pod that is not Ready, the Ready
    condition's transition to False, else the pod's start. The last
    termination is the latest crash, never the onset (`activity` dates the
    window bound, not the symptom)."""
    status = pod.get("status") or {}
    meta = pod.get("metadata") or {}
    if status.get("phase") == PHASE_PENDING:
        scheduled = _condition(pod, "PodScheduled")
        return parse_ts(scheduled.get("lastTransitionTime")) or parse_ts(status.get("startTime")) or parse_ts(meta.get("creationTimestamp"))
    ready = _condition(pod, "Ready")
    if ready.get("status") == "False" and parse_ts(ready.get("lastTransitionTime")):
        return parse_ts(ready["lastTransitionTime"])
    return parse_ts(status.get("startTime")) or parse_ts(meta.get("creationTimestamp"))


def pod_symptoms(pods: list[dict], resolver: Resolver, window_start: datetime | None = None) -> list[dict]:
    """One row per (top owner, category, reason), carrying the pods behind
    it; the row's detail (node, containers, images) is the example pod's.
    A pod whose last activity predates `window_start` is not the upgrade's."""
    rows: dict[tuple, dict] = {}
    for pod in pods:
        meta, status = pod.get("metadata") or {}, pod.get("status") or {}
        phase = status.get("phase")
        ready = _condition(pod, "Ready").get("status") == "True"
        if phase == PHASE_SUCCEEDED or (phase == PHASE_RUNNING and ready):
            continue
        # A Pending pod is a present condition; a pod whose last container
        # exit or start predates the window is an older story.
        activity = _pod_last_activity(pod)
        if window_start and activity and activity < window_start and phase != PHASE_PENDING:
            continue
        namespace, pod_name = meta.get("namespace", ""), meta.get("name", "")
        kind, name = resolver.resolve(namespace, "Pod", pod_name)
        obj = _object_ref(namespace, kind, name)
        onset = _pod_onset(pod, activity)
        base = {
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "object": obj,
            "system": is_system_namespace(namespace),
            "owner_kind": kind,
            "pods": [pod_name],
            "pod_count": 1,
            "pod_total": resolver.pod_total(obj),
            "example_pod": pod_name,
            "onset": fmt_ts(onset) if onset else None,
            **_pod_detail(pod),
        }
        scheduled = _condition(pod, "PodScheduled")
        if phase == PHASE_PENDING and scheduled.get("status") == "False":
            _merge_pod_row(rows, {**base, "category": CATEGORY_PENDING, "reason": scheduled.get("reason") or REASON_UNSCHEDULABLE, "message": (scheduled.get("message") or "")[:MESSAGE_EXCERPT_CHARS]})
            continue
        container_reasons = []
        for cs in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
            for key in ("state", "lastState"):
                for state_name, state in (cs.get(key) or {}).items():
                    if state_name == "running":
                        continue
                    reason = state.get("reason") or ""
                    # A clean exit (`Completed`, exit 0) is not a failure: an init
                    # container that finished must not headline a not-Ready pod.
                    if (reason and reason != REASON_COMPLETED) or (state_name == "terminated" and state.get("exitCode") not in (None, 0)):
                        container_reasons.append({"container": cs.get("name"), "where": key, "reason": reason or state_name, "message": (state.get("message") or "")[:MESSAGE_EXCERPT_CHARS], "exit_code": state.get("exitCode"), "finished_at": state.get("finishedAt")})
        # The catalogue's reasons lead, so the row's headline is the one a
        # signature reads rather than whichever container came first.
        container_reasons.sort(key=lambda c: c["reason"] not in CONTAINER_FAILURE_REASONS)
        reason = container_reasons[0]["reason"] if container_reasons else (status.get("reason") or ("NotReady" if phase == PHASE_RUNNING else phase or "NotReady"))
        message = container_reasons[0]["message"] if container_reasons else (status.get("message") or "")[:MESSAGE_EXCERPT_CHARS]
        _merge_pod_row(rows, {**base, "category": CATEGORY_NOT_READY, "reason": reason, "message": message, "containers": container_reasons})
    return list(rows.values())


def _merge_pod_row(rows: dict[tuple, dict], row: dict) -> None:
    key = (row["object"], row["category"], row["reason"])
    existing = rows.get(key)
    if existing is None:
        rows[key] = row
        return
    existing["pods"].append(row["example_pod"])
    existing["pods"].sort()
    existing["pod_count"] = len(existing["pods"])
    existing["example_pod"] = existing["pods"][0]
    # The row's onset is the earliest of its pods'.
    onsets = [o for o in (existing.get("onset"), row.get("onset")) if o]
    existing["onset"] = min(onsets) if onsets else None


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
                    "category": CATEGORY_NODE,
                    "node": name,
                    "pool": (meta.get("labels") or {}).get(NODEPOOL_LABEL, ""),
                    "reason": f"{kind}={bad}" if kind != "Ready" else ("NotReady" if bad == "False" else "Unknown"),
                    "message": (cond.get("message") or cond.get("reason") or "")[:MESSAGE_EXCERPT_CHARS],
                    "onset": fmt_ts(parse_ts(cond.get("lastTransitionTime"))) if parse_ts(cond.get("lastTransitionTime")) else None,
                })
    return out


def _event_time(event: dict) -> datetime | None:
    """When the event last fired. A series-shaped event (events.k8s.io read
    back through core/v1) carries its first observation in `eventTime` and
    its last in `series.lastObservedTime`, so the series comes first."""
    series = event.get("series") or {}
    for value in (series.get("lastObservedTime"), event.get("lastTimestamp"), event.get("eventTime"), (event.get("metadata") or {}).get("creationTimestamp")):
        ts = parse_ts(value)
        if ts:
            return ts
    return None


def _event_count(event: dict) -> int:
    series = event.get("series") or {}
    return int(series.get("count") or event.get("count") or 1)


def event_symptoms(events: list[dict], window_start: datetime, resolver: Resolver) -> list[dict]:
    """Warning events since `window_start`, one row per (top owner, reason);
    a pod's or ReplicaSet's event is charged to the owner that outlives it."""
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
        namespace, involved_kind, involved_name = obj.get("namespace") or "", obj.get("kind") or "", obj.get("name") or ""
        kind, name = resolver.resolve(namespace, involved_kind, involved_name)
        key = (namespace, kind, name, reason)
        row = rows.get(key)
        if row is None:
            rows[key] = row = {
                "kind": kind,
                "namespace": namespace,
                "name": name,
                "object": _object_ref(namespace, kind, name),
                "system": is_system_namespace(namespace) if namespace else True,
                "category": CATEGORY_EVENT,
                "reason": reason,
                "message": message[:MESSAGE_EXCERPT_CHARS],
                "count": 0,
                "first_seen": fmt_ts(parse_ts(event.get("firstTimestamp")) or parse_ts(event.get("eventTime")) or last),
                "last_seen": fmt_ts(last),
            }
            rows[key]["onset"] = rows[key]["first_seen"]
            if involved_kind == "Pod" and (kind, name) != (involved_kind, involved_name):
                row.update(pods=[], pod_count=0, pod_total=resolver.pod_total(row["object"]), example_pod=involved_name)
        if "pods" in row and involved_name not in row["pods"]:
            row["pods"].append(involved_name)
            row["pods"].sort()
            row["pod_count"], row["example_pod"] = len(row["pods"]), row["pods"][0]
        row["count"] += _event_count(event)
        if fmt_ts(last) > row["last_seen"]:
            row["last_seen"], row["message"] = fmt_ts(last), message[:MESSAGE_EXCERPT_CHARS]
    return list(rows.values())


def _selector_matches(selector: dict, labels: dict) -> bool:
    """A label selector evaluated in full: `matchLabels` and `matchExpressions`
    (In, NotIn, Exists, DoesNotExist). An empty selector matches nothing here:
    a budget with no selector covers no pod this collector can name."""
    match = (selector or {}).get("matchLabels") or {}
    expressions = (selector or {}).get("matchExpressions") or []
    if not match and not expressions:
        return False
    if not all(labels.get(k) == v for k, v in match.items()):
        return False
    for expr in expressions:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


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
            "category": CATEGORY_PDB,
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
        if op.get("operationType") != OP_UPGRADE_NODES:
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
    master_upgraded: bool = False  # an UPGRADE_MASTER ended in the window
    images_on_untouched_pools: frozenset = frozenset()  # images Running on a pool no operation touched


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
    if symptom["category"] == CATEGORY_PENDING or (symptom["category"] == CATEGORY_EVENT and reason == REASON_FAILED_SCHEDULING):
        scopes[SCOPE_SCHEDULING] = [message]
    if symptom["category"] == CATEGORY_NOT_READY:
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


def _gate_open(symptom: dict, entry: int, ctx: Context) -> bool:
    """Whether an operation in the window reached the object: its pool for a
    node-side mechanism, the control plane for 6 and 7. A pod without a node
    (Pending) counts any upgraded pool."""
    if entry in CONTROL_PLANE_ENTRIES:
        return ctx.master_upgraded or bool(ctx.upgraded_pools)
    pool = symptom.get("pool") or ctx.node_pool.get(symptom.get("node") or "", "")
    return pool in ctx.upgraded_pools if pool else bool(ctx.upgraded_pools)


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
        if entry == ENTRY_NODE_LABEL:
            total, missed = SCHEDULING_TOTAL_RE.search(text), SELECTOR_MISS_COUNT_RE.search(text)
            if total and missed and int(missed.group(1)) < int(total.group(1)):
                confidence = MEDIUM
            detail = _selector_detail(symptom)
        elif entry == ENTRY_WEBHOOK:
            name = WEBHOOK_NAME_RE.search(text)
            detail = f"webhook {name.group(1)}" if name else ""
        elif entry == ENTRY_REGISTRY:
            detail = _image_host_detail(symptom)
        # High only when the signature names the entry's own mechanism and the
        # object's pool had an operation in the window (for a control-plane
        # mechanism, an UPGRADE_MASTER); otherwise medium.
        if confidence == HIGH and not _gate_open(symptom, entry, ctx):
            confidence = MEDIUM
            detail = (detail + "; " if detail else "") + GATE_CLOSED_TEXT
        if entry == ENTRY_REGISTRY and confidence == HIGH:
            # The mechanism is a rebuilt node that cannot pull what the old
            # ones had cached: high needs the same image still running on a
            # pool no operation touched.
            elsewhere = [img for img in symptom.get("images") or [] if img in ctx.images_on_untouched_pools]
            if elsewhere:
                detail = (detail + "; " if detail else "") + f"same image running on an untouched pool ({elsewhere[0]})"
            else:
                confidence = MEDIUM
                detail = (detail + "; " if detail else "") + "no pod with this image running on an untouched pool"
        # A reason-only hit is evidenced by the reason with its message.
        evidence = scopes[SCOPE_ANY][0] if scope == SCOPE_REASON else (text if scope == SCOPE_ANY else m.group(0))
        add(entry, confidence, evidence, detail)

    # OOMKilled: both 14 and 15 need cgroup v2, and a multi-process
    # container is not visible from a spec, so on v2 the verdict is "14 or
    # 15" with the container count as detail; on v1 it is neither.
    containers = symptom.get("containers") or []
    oom = [c for c in containers if c["reason"] == OOM_REASON]
    if oom:
        pool = ctx.node_pool.get(symptom.get("node") or "", "")
        mode = ctx.cgroup_modes.get(pool, "")
        evidence = f"container {oom[0]['container']} {OOM_REASON} exit {oom[0]['exit_code']} (pool {pool or '?'} {mode or 'cgroup mode unknown'})"
        undecided = f"{OOM_UNDECIDED_ENTRIES[0]} or {OOM_UNDECIDED_ENTRIES[1]}"
        count = f"{symptom.get('container_count', 1)} container(s)"
        runtime = next((f"{img} ({cgroup_v1_runtime(img)})" for img in symptom.get("images") or [] if cgroup_v1_runtime(img)), None)
        if mode == CGROUP_V2_MODE and runtime and _gate_open(symptom, ENTRY_CGROUP_V2, ctx):
            add(ENTRY_CGROUP_V2, HIGH, f"{evidence}; runtime image {runtime}", f"cgroup v1 runtime on cgroup v2; {count}")
        elif mode == CGROUP_V2_MODE:
            add(OOM_UNDECIDED_ENTRIES[0], MEDIUM, evidence, f"{undecided} on cgroup v2; {count}")
        elif mode == CGROUP_V1_MODE:
            add(None, MEDIUM, evidence, f"OOMKilled on cgroup v1: neither {undecided}")
        else:
            add(OOM_UNDECIDED_ENTRIES[0], MEDIUM, evidence, f"{undecided}: cgroup mode unknown; {count}")

    # Entry 17: a node NotReady / NetworkUnavailable, high when its pool was
    # upgraded in the window.
    if symptom["category"] == CATEGORY_NODE:
        pool = symptom.get("pool") or ""
        touched = ctx.upgraded_pools.get(pool)
        transition = f" since {symptom['onset']}" if symptom.get("onset") else ""
        add(ENTRY_NODE_AGENT, HIGH if touched else MEDIUM, f"node {symptom['name']} {symptom['reason']}{transition}" + (f" after UPGRADE_NODES on {pool} at {touched['start']}" if touched else ""), f"pool {pool}")

    # Entry 1: a budget allowing no disruption on an upgraded pool, or an
    # upgrade that ran longer than the hour-per-node a drain is held.
    if symptom["category"] == CATEGORY_PDB and symptom.get("upgraded_pools"):
        # Every touched pool counts: the budget allowed nothing on a pool that
        # was drained, the mechanism itself; the pool whose drain visibly
        # stalled, if any, is named as detail.
        touched = {pool: ctx.upgraded_pools[pool] for pool in symptom["upgraded_pools"]}
        held_pools = [pool for pool, info in touched.items() if info["nodes"] > 0 and info["longest_s"] > info["nodes"] * DRAIN_HOLD_PER_NODE.total_seconds()]
        named = held_pools[0] if held_pools else next(iter(touched))
        info, held = touched[named], bool(held_pools)
        if True:
            pool = named
            add(ENTRY_BUDGET, HIGH, f"disruptionsAllowed=0 with pods on {', '.join(touched)}; UPGRADE_NODES {info['operation']} on {pool} took {info['longest_s'] // 60} min over {info['nodes']} node(s)", f"budget {symptom['name']}" + (f"; drain held past an hour per node on {', '.join(held_pools)}" if held else ""))

    # Entry 6: a Job pod in Error whose spec names a removed API, best effort.
    if symptom["category"] == CATEGORY_NOT_READY and symptom.get("owner_kind") in JOB_OWNER_KINDS:
        hits = symptom.get("api_markers") or []
        if hits and any(c["reason"] == REASON_ERROR for c in containers):
            add(ENTRY_REMOVED_API, MEDIUM, f"{symptom['owner_kind']} pod in Error; spec mentions {', '.join(hits)}", "best effort: a name, not an API call")

    if not found:
        found.append(_classification(None, MEDIUM, f"{symptom.get('reason') or ''} {symptom.get('message') or ''}".strip()))
    return found


def symptom_key(symptom: dict) -> str:
    """The baseline's name for a symptom: owner, category and reason, no
    tenant text."""
    return BASELINE_SEPARATOR.join((symptom["object"], symptom["category"], symptom.get("reason") or ""))


def mark_since(symptoms: list[dict], baseline: list[str] | None, first_operation: datetime | None = None) -> None:
    """Against the stored symptom set and each symptom's onset. `baseline`
    None means no previous full run: everything is first seen and graded by
    onset alone. A symptom predates the upgrade when its onset is before the
    window's first operation, or the stored set already has it; with no
    readable onset the stored set decides."""
    previous = set(baseline or [])
    for symptom in symptoms:
        recorded = baseline is not None and symptom_key(symptom) in previous
        symptom["since"] = SINCE_FIRST_SEEN if baseline is None else (SINCE_BEFORE if recorded else SINCE_NEW)
        onset = parse_ts(symptom.get("onset"))
        before_operation = bool(first_operation and onset and onset < first_operation)
        symptom["predates_upgrade"] = recorded or before_operation


def collect_symptoms(cluster: dict, reads: dict[str, list], operations: list[dict], window_start: datetime) -> list[dict]:
    """Every symptom in the read, each with its classifications, user
    namespaces first."""
    nodes = reads.get("nodes") or []
    upgraded = upgraded_pools_of(operations, nodes)
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    untouched_images = set()
    for pod in reads.get("pods") or []:
        pool = node_pool.get((pod.get("spec") or {}).get("nodeName"), "")
        if pool and pool not in upgraded and (pod.get("status") or {}).get("phase") == PHASE_RUNNING and _condition(pod, "Ready").get("status") == "True":
            untouched_images.update(c.get("image") or "" for c in (pod.get("spec") or {}).get("containers") or [])
    ctx = Context(
        cgroup_modes=pool_cgroup_modes(cluster),
        node_pool=node_pool,
        upgraded_pools=upgraded,
        master_upgraded=any(op.get("operationType") == OP_UPGRADE_MASTER for op in operations),
        images_on_untouched_pools=frozenset(untouched_images),
    )
    resolver = Resolver(reads.get("pods") or [], reads.get("owners") or [], reads.get("workloads") or [])
    pods = pod_symptoms(reads.get("pods") or [], resolver, window_start)
    pod_objects = {s["object"] for s in pods}
    # A FailedScheduling or BackOff event on a workload the pod list already
    # reports as Pending or crash-looping says the same thing twice.
    events = [e for e in event_symptoms(reads.get("events") or [], window_start, resolver) if not (e["reason"] in EVENT_REASONS_IMPLIED_BY_POD and e["object"] in pod_objects)]
    symptoms = pods + node_symptoms(nodes) + events + pdb_symptoms(reads.get("pdbs") or [], reads.get("pods") or [], nodes, upgraded)
    for symptom in symptoms:
        symptom["classifications"] = classify_symptom(symptom, ctx)
        if symptom.get("pod_count"):
            for c in symptom["classifications"]:
                c["evidence"] = POD_EVIDENCE_FORMAT.format(count=symptom["pod_count"], total=max(symptom["pod_total"], symptom["pod_count"]), evidence=c["evidence"], example=symptom["example_pod"])[:MESSAGE_EXCERPT_CHARS]
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


def guard_id(cluster: str, entry: int, obj: str, kind: str = GUARD_KIND_FAILURE) -> str:
    return f"{cluster}#{kind}#{entry}#{obj}"


def _guard(key: str, kind: str, entry: int, title: str, obj: str, confidence: str, evidence: str, seen_at: str, reads: tuple[str, ...]) -> dict:
    return {
        "id": guard_id(key, entry, obj, kind),
        "kind": kind,
        "reads": list(reads),
        "cluster": key,
        "entry": entry,
        "title": title,
        "object": obj,
        "confidence": confidence,
        "evidence": evidence,
        "first_seen": seen_at,
        "last_seen": seen_at,
    }


def guards_for(key: str, symptoms: list[dict], seen_at: str) -> list[dict]:
    out: dict[str, dict] = {}
    for symptom in symptoms:
        for c in symptom["classifications"]:
            if c["entry"] is None:
                continue
            guard = _guard(key, GUARD_KIND_FAILURE, c["entry"], c["title"], symptom["object"], c["confidence"], c["evidence"], seen_at, FAILURE_GUARD_READS)
            guard["source"] = symptom["category"]
            existing = out.get(guard["id"])
            if existing is None:
                out[guard["id"]] = guard
            elif existing["source"] == GUARD_SOURCE_EVENT and symptom["category"] != GUARD_SOURCE_EVENT:
                # A pod or node showed the same finding: the guard is not event-only.
                existing["source"] = symptom["category"]
    return list(out.values())


def risk_guards_for(key: str, shapes: list[dict], seen_at: str) -> list[dict]:
    out: dict[str, dict] = {}
    for shape in shapes:
        guard = _guard(key, GUARD_KIND_RISK, shape["entry"], shape["title"], shape["object"], shape["confidence"], shape["evidence"], seen_at, SHAPE_READS_BY_ENTRY.get(shape["entry"], SPEC_SHAPE_READS))
        out.setdefault(guard["id"], guard)
    return list(out.values())


def merge_guards(existing: dict, fresh: list[dict], reviewed: set[str], seen_at: str, answered: dict[str, set[str]] | None = None, removed: set[str] | None = None) -> dict:
    """Keep what was there, refresh what is seen again, drop what a reviewed
    cluster no longer shows -- but only when every read that could show it
    answered (`answered` is per cluster; absent means all did). A cluster
    this run did not review keeps its guards; a cluster its project no longer
    lists (`removed`) loses them."""
    by_id = {g["id"]: {"kind": GUARD_KIND_FAILURE, **g} for g in existing.get("guards") or [] if isinstance(g, dict) and g.get("id")}
    fresh_ids = {g["id"] for g in fresh}
    merged = []
    for gid, guard in by_id.items():
        cluster = guard.get("cluster")
        if cluster in (removed or set()):
            continue
        if cluster in reviewed and gid not in fresh_ids:
            needed = set(guard.get("reads") or FAILURE_GUARD_READS)
            if answered is None or needed <= answered.get(cluster, set()):
                continue
        merged.append(guard)
    for guard in fresh:
        old = by_id.get(guard["id"])
        if old:
            old.update(last_seen=seen_at, evidence=guard["evidence"], confidence=guard["confidence"])
            if guard.get("source"):
                old["source"] = guard["source"]
        else:
            merged.append(guard)
    merged.sort(key=lambda g: (g["cluster"], g["entry"], g["object"]))
    return {"version": GUARDS_VERSION, "updated_at": seen_at, "guards": merged}


# --------------------------------------------------------------------------- #
# Shapes: the catalogue's before-signals read statically. Risks, not
# incidents; each is keyed by owner like a symptom and writes a `risk` guard.
# --------------------------------------------------------------------------- #


def _shape(obj: dict, entry: int, confidence: str, evidence: str, detail: str = "") -> dict:
    return {
        "reads": list(SHAPE_READS_BY_ENTRY.get(entry, SPEC_SHAPE_READS)),
        "kind": obj["kind"],
        "namespace": obj["namespace"],
        "name": obj["name"],
        "object": obj["object"],
        "system": obj["system"],
        "entry": entry,
        "title": MITIGATIONS[entry]["title"],
        "confidence": confidence,
        "evidence": evidence[:MESSAGE_EXCERPT_CHARS],
        "detail": detail,
    }


def _object_of(namespace: str, kind: str, name: str) -> dict:
    return {"kind": kind, "namespace": namespace, "name": name, "object": _object_ref(namespace, kind, name), "system": is_system_namespace(namespace) if namespace else True}


def _pod_template_spec(workload: dict) -> dict | None:
    spec = workload.get("spec") or {}
    if workload.get("kind") == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    return (spec.get("template") or {}).get("spec")


def _template_labels(workload: dict) -> dict:
    spec = workload.get("spec") or {}
    return ((spec.get("template") or {}).get("metadata") or {}).get("labels") or {}


def pod_specs_by_owner(workloads: list[dict], pods: list[dict], resolver: Resolver) -> dict[str, tuple[dict, dict, dict]]:
    """Every workload's pod template, plus the spec of any pod whose owner
    the workload list does not cover (bare pods, Jobs), keyed by owner."""
    out: dict[str, tuple[dict, dict, dict]] = {}
    for workload in workloads:
        if workload.get("kind") not in TEMPLATE_KINDS:
            continue
        meta = workload.get("metadata") or {}
        obj = _object_of(meta.get("namespace", ""), workload["kind"], meta.get("name", ""))
        spec = _pod_template_spec(workload)
        if spec is not None:
            out[obj["object"]] = (obj, spec, workload)
    for pod in pods:
        meta = pod.get("metadata") or {}
        kind, name = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
        obj = _object_of(meta.get("namespace", ""), kind, name)
        if obj["object"] not in out:
            out[obj["object"]] = (obj, pod.get("spec") or {}, pod)
    return out


def _selector_label_keys(spec: dict) -> list[str]:
    keys = list((spec.get("nodeSelector") or {}).keys())
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    terms = list((node_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") or {}).get("nodeSelectorTerms") or [])
    terms += [p.get("preference") or {} for p in node_affinity.get("preferredDuringSchedulingIgnoredDuringExecution") or []]
    for term in terms:
        for expr in (term.get("matchExpressions") or []) + (term.get("matchFields") or []):
            if expr.get("key"):
                keys.append(expr["key"])
    return keys


def _containers(spec: dict) -> list[dict]:
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


def shapes_in_spec(obj: dict, spec: dict) -> list[dict]:
    """Entries 12, 20, 4, 18 from one pod spec, and 13/17 for a DaemonSet."""
    out = []
    deprecated = [k for k in _selector_label_keys(spec) if k.startswith(DEPRECATED_NODE_LABEL_PREFIXES)]
    if deprecated:
        selector = spec.get("nodeSelector") or {}
        evidence = ", ".join(f"{k}={selector[k]}" if k in selector else f"affinity on {k}" for k in sorted(set(deprecated)))
        out.append(_shape(obj, ENTRY_NODE_LABEL, HIGH, f"selector {evidence}", f"selector {sorted(set(deprecated))[0]}"))
    for container in _containers(spec):
        image = container.get("image") or ""
        if image.startswith(RETIRED_IMAGE_HOSTS):
            out.append(_shape(obj, ENTRY_REGISTRY, HIGH, f"image {image}", f"image host {_image_host(image)}"))
            break
    for volume in spec.get("volumes") or []:
        name = volume.get("name") or ""
        host_path = (volume.get("hostPath") or {}).get("path") or ""
        if "emptyDir" in volume and STATEFUL_VOLUME_NAME_RE.search(name):
            out.append(_shape(obj, ENTRY_NODE_LOCAL_STATE, MEDIUM, f"emptyDir volume `{name}`", f"volume {name}"))
        elif host_path.startswith(LOCAL_SSD_HOSTPATH_PREFIXES):
            out.append(_shape(obj, ENTRY_NODE_LOCAL_STATE, MEDIUM, f"hostPath volume `{name}` at {host_path}", f"volume {name}"))
    gpu = any(GPU_RESOURCE in ((c.get("resources") or {}).get("limits") or {}) or GPU_RESOURCE in ((c.get("resources") or {}).get("requests") or {}) for c in _containers(spec))
    if gpu:
        pins = []
        for container in _containers(spec):
            m = CUDA_IMAGE_PIN_RE.search(container.get("image") or "")
            if m:
                pins.append(f"image {container.get('image')}")
            for env in container.get("env") or []:
                if CUDA_ENV_NAME_MARKER in (env.get("name") or "").upper() and VERSION_IN_TEXT_RE.search(str(env.get("value") or "")):
                    pins.append(f"env {env.get('name')}={env.get('value')}")
        if pins:
            out.append(_shape(obj, ENTRY_GPU, MEDIUM, f"{GPU_RESOURCE} requested with {'; '.join(pins)}", "CUDA pin"))
    if obj["kind"] == "DaemonSet":
        if spec.get("hostNetwork"):
            out.append(_shape(obj, ENTRY_NODE_AGENT, MEDIUM, "DaemonSet on hostNetwork", "hostNetwork"))
        for volume in spec.get("volumes") or []:
            host_path = (volume.get("hostPath") or {}).get("path") or ""
            if host_path.startswith(CONTAINERD_SOCKET_PATH_PREFIXES):
                out.append(_shape(obj, ENTRY_RUNTIME, MEDIUM, f"DaemonSet mounts {host_path} (volume `{volume.get('name')}`)", f"hostPath {host_path}"))
                break
    return out


def budget_shapes(pdbs: list[dict], workloads: list[dict]) -> list[dict]:
    """Entry 1: a budget allowing no disruption, and a one-replica
    Deployment or StatefulSet a budget selects."""
    out = []
    for pdb in pdbs:
        meta, status = pdb.get("metadata") or {}, pdb.get("status") or {}
        namespace, name = meta.get("namespace", ""), meta.get("name", "")
        obj = _object_of(namespace, "PodDisruptionBudget", name)
        spec = {k: v for k, v in (pdb.get("spec") or {}).items() if k != "selector"}
        if status.get("disruptionsAllowed") == 0:
            out.append(_shape(obj, ENTRY_BUDGET, HIGH, f"disruptionsAllowed=0, spec {json.dumps(spec, sort_keys=True)}", f"budget {name}"))
        selector = (pdb.get("spec") or {}).get("selector") or {}
        for workload in workloads:
            wmeta = workload.get("metadata") or {}
            if workload.get("kind") not in ("Deployment", "StatefulSet") or wmeta.get("namespace") != namespace:
                continue
            if (workload.get("spec") or {}).get("replicas") == SINGLE_REPLICA and _selector_matches(selector, _template_labels(workload)):
                wobj = _object_of(namespace, workload["kind"], wmeta.get("name", ""))
                out.append(_shape(wobj, ENTRY_BUDGET, HIGH, f"one replica behind budget {name} ({json.dumps(spec, sort_keys=True)})", f"budget {name}"))
    return out


def storage_shapes(storage: list[dict], cluster: dict) -> list[dict]:
    """Entry 19: in-tree PD volumes and StorageClasses; high when the PD CSI
    add-on is off, medium when migration carries them and the class should
    still move."""
    addon = ((cluster.get("addonsConfig") or {}).get(PD_CSI_ADDON_KEY) or {}).get("enabled")
    confidence = MEDIUM if addon else HIGH
    addon_text = "PD CSI driver add-on enabled" if addon else "PD CSI driver add-on disabled"
    out = []
    for item in storage:
        meta = item.get("metadata") or {}
        obj = _object_of("", item.get("kind") or "", meta.get("name", ""))
        if item.get("kind") == "PersistentVolume" and IN_TREE_PD_VOLUME_KEY in (item.get("spec") or {}):
            disk = ((item.get("spec") or {}).get(IN_TREE_PD_VOLUME_KEY) or {}).get("pdName", "")
            out.append(_shape(obj, ENTRY_IN_TREE_VOLUME, confidence, f"in-tree {IN_TREE_PD_VOLUME_KEY} {disk}; {addon_text}", f"volume {meta.get('name', '')}"))
        elif item.get("kind") == "StorageClass" and item.get("provisioner") == IN_TREE_PD_PROVISIONER and not addon:
            # With the add-on on, CSI migration serves `gce-pd` and GKE's own
            # `standard` class would be a permanent row on every cluster.
            out.append(_shape(obj, ENTRY_IN_TREE_VOLUME, HIGH, f"provisioner {IN_TREE_PD_PROVISIONER}; {addon_text}; move to {CSI_PD_PROVISIONER}", f"class {meta.get('name', '')}"))
    return out


def _ready_services(endpointslices: list[dict]) -> set[tuple[str, str]]:
    ready = set()
    for es in endpointslices:
        meta = es.get("metadata") or {}
        service = (meta.get("labels") or {}).get(ENDPOINTSLICE_SERVICE_LABEL)
        if not service:
            continue
        for endpoint in es.get("endpoints") or []:
            if (endpoint.get("conditions") or {}).get("ready") is not False:
                ready.add((meta.get("namespace", ""), service))
                break
    return ready


def _rules_reach_pod_creation(hook: dict) -> bool:
    for rule in hook.get("rules") or []:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        operations = rule.get("operations") or []
        if any(g in WEBHOOK_POD_GROUPS for g in groups) and any(r in WEBHOOK_POD_RESOURCES for r in resources) and any(o in WEBHOOK_POD_OPERATIONS for o in operations):
            return True
    return False


def webhook_shapes(webhooks: list[dict], endpointslices: list[dict] | None) -> list[dict]:
    """Entry 7: a fail-closed webhook whose Service has no ready endpoint.
    Without the endpoint read the check cannot run and reports nothing."""
    if endpointslices is None:
        return []
    ready = _ready_services(endpointslices)
    out = []
    for config in webhooks:
        if config.get("kind") not in WEBHOOK_CONFIGURATION_KINDS:
            continue
        meta = config.get("metadata") or {}
        obj = _object_of("", config["kind"], meta.get("name", ""))
        for hook in config.get("webhooks") or []:
            service = ((hook.get("clientConfig") or {}).get("service")) or {}
            if hook.get("failurePolicy") != WEBHOOK_FAIL_CLOSED or not service.get("name") or not _rules_reach_pod_creation(hook):
                continue
            if (service.get("namespace", ""), service["name"]) not in ready:
                # The catalogue's full shape also has a long timeout and no
                # namespaceSelector exempting kube-system; short of that it
                # is a lesser risk.
                timeout = hook.get("timeoutSeconds") or WEBHOOK_DEFAULT_TIMEOUT_S
                full = timeout >= WEBHOOK_LONG_TIMEOUT_S and not hook.get("namespaceSelector")
                out.append(_shape(obj, ENTRY_WEBHOOK, HIGH if full else MEDIUM, f"webhook {hook.get('name')} failurePolicy {WEBHOOK_FAIL_CLOSED}, timeout {timeout}s{'' if hook.get('namespaceSelector') else ', no namespaceSelector'}; Service {service.get('namespace', '')}/{service['name']} has no ready endpoint", f"webhook {hook.get('name')}"))
                break
    return out


def _image_repository_and_tag(image: str) -> tuple[str, str]:
    """`docker.io/library/eclipse-temurin:8u302-b08-jre` -> (`eclipse-temurin`, `8u302-b08-jre`);
    the repository is the last path component, the tag what follows the colon."""
    name, _, digest_free = image.partition("@")
    path, sep, tag = name.rpartition(":")
    if not sep or "/" in tag:
        path, tag = name, ""
    return path.rsplit("/", 1)[-1], tag


def cgroup_v1_runtime(image: str) -> str | None:
    """Why `image` is a runtime that misreads a cgroup v2 limit, or None."""
    repository, tag = _image_repository_and_tag(image)
    if repository in JAVA_IMAGE_REPOS:
        m = JAVA_TAG_RE.match(tag)
        if not m:
            return None
        major, update, minor, patch = (int(x) if x is not None else None for x in m.groups())
        if major == 8 and update is not None and update < JDK8_CGROUP_V2_UPDATE:
            return f"JDK 8u{update} < 8u{JDK8_CGROUP_V2_UPDATE}"
        if major == 11 and minor is not None and (minor, patch) < JDK11_CGROUP_V2_PATCH:
            return f"JDK 11.{minor}.{patch} < 11.{JDK11_CGROUP_V2_PATCH[0]}.{JDK11_CGROUP_V2_PATCH[1]}"
        if major < 8 or (8 < major < 11) or (11 < major < JDK_FIRST_MAJOR_WITH_CGROUP_V2):
            return f"JDK {major} predates cgroup v2 support"
        return None
    if any(marker in image for marker in DOTNET_IMAGE_REPO_MARKERS):
        m = DOTNET_TAG_RE.match(tag)
        if m and (int(m.group(1)), int(m.group(2))) < DOTNET_CGROUP_V2_VERSION:
            return f".NET {m.group(1)}.{m.group(2)} < {DOTNET_CGROUP_V2_VERSION[0]}.{DOTNET_CGROUP_V2_VERSION[1]}"
    return None


def _pool_exposure(pool: str, mode: str, version: str) -> str | None:
    """How the pool exposes a cgroup v1 runtime: on v2 now, or v1 but below
    the minor GKE migrates at."""
    if mode == CGROUP_V2_MODE:
        return f"pool {pool} on cgroup v2"
    parsed = parse_version(version)
    if mode == CGROUP_V1_MODE and parsed and parsed[1] < CGROUP_V2_MIGRATION_MINOR:
        return f"pool {pool} on cgroup v1 at {version}, migrated to v2 at 1.{CGROUP_V2_MIGRATION_MINOR}"
    return None


def runtime_shapes(obj: dict, spec: dict, pools: list[str], cluster: dict) -> list[dict]:
    """Entry 14 for a pod spec: a cgroup v1 runtime image on an exposed pool.
    `pools` are the pools the owner's pods run on; empty means any pool."""
    modes, versions = pool_cgroup_modes(cluster), versions_of(cluster)["node_pools"]
    candidates = pools or sorted(modes)
    exposures = [e for e in (_pool_exposure(p, modes.get(p, ""), versions.get(p, "")) for p in candidates) if e]
    if not exposures:
        return []
    for container in _containers(spec):
        reason = cgroup_v1_runtime(container.get("image") or "")
        if reason:
            return [_shape(obj, ENTRY_CGROUP_V2, MEDIUM, f"image {container.get('image')} ({reason}) on {'; '.join(exposures)}", "runtime image")]
    return []


def _is_managed_agent_namespace(namespace: str) -> bool:
    return is_system_namespace(namespace) or namespace.startswith(MANAGED_AGENT_NAMESPACE_PREFIXES)


def collect_risks(cluster: dict, reads: dict[str, list]) -> tuple[list[dict], int]:
    """The shapes present, and how many GKE-managed agents carry 13/17
    (counted, not reported: they move with the node image)."""
    pods = reads.get("pods") or []
    nodes = reads.get("nodes") or []
    workloads = reads.get("workloads") or []
    resolver = Resolver(pods, reads.get("owners") or [], workloads)
    node_pool = {(n.get("metadata") or {}).get("name"): ((n.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "") for n in nodes}
    pools_of: dict[str, set[str]] = {}
    for pod in pods:
        meta = pod.get("metadata") or {}
        kind, name = resolver.resolve(meta.get("namespace", ""), "Pod", meta.get("name", ""))
        pool = node_pool.get((pod.get("spec") or {}).get("nodeName"), "")
        if pool:
            pools_of.setdefault(_object_ref(meta.get("namespace", ""), kind, name), set()).add(pool)
    shapes: list[dict] = []
    for obj, spec, _ in pod_specs_by_owner(workloads, pods, resolver).values():
        shapes += shapes_in_spec(obj, spec)
        shapes += runtime_shapes(obj, spec, sorted(pools_of.get(obj["object"], ())), cluster)
    shapes += budget_shapes(reads.get("pdbs") or [], workloads)
    shapes += storage_shapes(reads.get("storage") or [], cluster)
    shapes += webhook_shapes(reads.get("webhooks") or [], reads.get("endpointslices") if "endpointslices" in reads else None)
    managed_agents: set[str] = set()
    kept = []
    for shape in shapes:
        if shape["entry"] in MANAGED_AGENT_ENTRIES and _is_managed_agent_namespace(shape["namespace"]):
            managed_agents.add(shape["object"])
            continue
        kept.append(shape)
    unique: dict[tuple, dict] = {}
    for shape in kept:
        unique.setdefault((shape["object"], shape["entry"]), shape)
    return sorted(unique.values(), key=lambda s: (s["system"], s["namespace"], s["kind"], s["name"], s["entry"])), len(managed_agents)


def collect_shapes(cluster: dict, reads: dict[str, list]) -> list[dict]:
    return collect_risks(cluster, reads)[0]


# --------------------------------------------------------------------------- #
# The next upgrade: target, window, exclusions.
# --------------------------------------------------------------------------- #


def fetch_server_config(project: str, location: str, *, run: RunFn) -> tuple[dict | None, str | None]:
    parsed, error = run_json(["gcloud", "container", "get-server-config", "--location", location, "--project", project, "--format", "json"], run=run)
    if error:
        return None, error
    return (parsed if isinstance(parsed, dict) else None), (None if isinstance(parsed, dict) else "get-server-config returned JSON that is not an object")


def _parse_iso_duration(text: str) -> timedelta | None:
    m = ISO_DURATION_RE.match(text or "")
    if not m:
        return None
    hours, minutes, seconds = (int(x or 0) for x in m.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _next_opening(window: dict, now: datetime) -> tuple[str, datetime | None]:
    """A one-line description of the maintenance window and when it next
    opens; `None` when the rule is one this does not parse."""
    daily = window.get("dailyMaintenanceWindow")
    if daily:
        start = datetime.strptime(daily.get("startTime") or "00:00", DAILY_WINDOW_TIME_FORMAT).time()
        length = _parse_iso_duration(daily.get("duration") or "") or DEFAULT_DAILY_WINDOW
        candidate = datetime.combine(now.date(), start, tzinfo=timezone.utc)
        if candidate <= now:
            candidate += timedelta(days=1)
        return f"daily at {daily.get('startTime')} UTC for {int(length.total_seconds() // SECONDS_PER_HOUR)}h", candidate
    recurring = window.get("recurringWindow")
    if recurring:
        rule = {k: v for k, v in (part.split("=", 1) for part in (recurring.get("recurrence") or "").split(";") if "=" in part)}
        start = parse_ts((recurring.get("window") or {}).get("startTime"))
        end = parse_ts((recurring.get("window") or {}).get("endTime"))
        if start is None:
            return f"recurring {recurring.get('recurrence')}", None
        length = (end - start) if end else timedelta(0)
        days = [RRULE_WEEKDAYS[d] for d in (rule.get("BYDAY") or "").split(",") if d in RRULE_WEEKDAYS]
        freq = rule.get("FREQ")
        if freq == RRULE_FREQ_WEEKLY and not days:
            # RFC 5545: a WEEKLY rule without BYDAY repeats on DTSTART's weekday.
            days = [start.weekday()]
        text = f"{freq or 'recurring'}{' on ' + rule['BYDAY'] if rule.get('BYDAY') else ''} at {start.strftime(DAILY_WINDOW_TIME_FORMAT)} UTC for {int(length.total_seconds() // SECONDS_PER_HOUR)}h"
        if freq not in (RRULE_FREQ_DAILY, RRULE_FREQ_WEEKLY):
            return text, None
        for offset in range(NEXT_WINDOW_SEARCH_DAYS):
            candidate = datetime.combine((now + timedelta(days=offset)).date(), start.timetz())
            if candidate >= now and (freq == RRULE_FREQ_DAILY or not days or candidate.weekday() in days):
                return text, candidate
        return text, None
    return NO_WINDOW_TEXT, None


def next_upgrade(cluster: dict, server_config: dict | None, now: datetime) -> dict:
    current = cluster.get("currentMasterVersion") or ""
    channel = (cluster.get("releaseChannel") or {}).get("channel") or ""
    target = None
    if server_config:
        if channel:
            info = next((c for c in server_config.get("channels") or [] if c.get("channel") == channel), None)
            target = (info or {}).get("defaultVersion")
        else:
            target = server_config.get("defaultClusterVersion")
    cur_t, tgt_t = parse_version(current), parse_version(target or "")
    below = (cur_t < tgt_t) if cur_t and tgt_t else None
    policy = ((cluster.get("maintenancePolicy") or {}).get("window") or {})
    window_text, opens = _next_opening(policy, now)
    exclusions = []
    for name, exclusion in sorted((policy.get("maintenanceExclusions") or {}).items()):
        start, end = parse_ts(exclusion.get("startTime")), parse_ts(exclusion.get("endTime"))
        exclusions.append({
            "name": name,
            "scope": ((exclusion.get("maintenanceExclusionOptions") or {}).get("scope")) or DEFAULT_EXCLUSION_SCOPE,
            "start": fmt_ts(start) if start else None,
            "end": fmt_ts(end) if end else None,
            "active": bool(start and end and start <= now <= end),
        })
    return {
        "channel": channel,
        "current": current,
        "target": target,
        "below_target": below,
        "window": window_text,
        "next_opens": fmt_ts(opens) if opens else None,
        "exclusions": exclusions,
    }


def parse_version(v: str) -> tuple[int, int, int, int] | None:
    m = VERSION_RE.match(v or "")
    if not m:
        return None
    major, minor, patch, build = m.groups()
    return (int(major), int(minor), int(patch), int(build or 0))


# --------------------------------------------------------------------------- #
# The review of one cluster.
# --------------------------------------------------------------------------- #


def recheck_cluster(key: str, cluster: dict, cluster_guards: list[dict], *, run: RunFn, refresh: bool = False) -> dict:
    """Re-read an unchanged cluster for the guards it holds: the symptom or
    shape behind each is looked for again with only the reads it needs. A
    guard whose finding is gone, and whose reads all answered, is cleared."""
    needed: set[str] = set(REFRESH_READS) if refresh else set()
    for guard in cluster_guards:
        if guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_RISK:
            needed |= set(guard.get("reads") or SPEC_SHAPE_READS)
        else:
            needed |= set(RECHECK_FAILURE_READS)
            if guard.get("entry") == ENTRY_BUDGET:
                needed |= set(RECHECK_BUDGET_READS)
    result = {"cluster": key, "guards": len(cluster_guards), "cleared": [], "refreshed": [], "not_recheckable": [], "errors": [], "symptom_baseline": None}
    kubeconfig, error = fetch_credentials(cluster, run=run)
    if error:
        result["errors"].append(error)
        return result
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    reads: dict[str, list] = {}
    for name, argv in KUBECTL_READS:
        if name not in needed:
            continue
        parsed, read_error = run_json(argv, run=run, timeout=KUBECTL_TIMEOUT_S, env=env)
        if read_error:
            result["errors"].append(f"{name}: {read_error}")
            continue
        reads[name] = (parsed.get("items") or []) if isinstance(parsed, dict) else []
    answered = set(reads)
    # A budget still allowing no disruption keeps its entry-1 failure guard:
    # the drain it held cannot be re-observed without the operation.
    budgets_at_zero = {_object_ref((b.get("metadata") or {}).get("namespace", ""), "PodDisruptionBudget", (b.get("metadata") or {}).get("name", "")) for b in reads.get("pdbs") or [] if (b.get("status") or {}).get("disruptionsAllowed") == 0}
    symptoms = collect_symptoms(cluster, reads, [], EPOCH)
    if refresh and set(REFRESH_READS) <= answered:
        result["symptom_baseline"] = sorted({symptom_key(sym) for sym in symptoms})
    fresh = {g["id"] for g in guards_for(key, symptoms, "")}
    fresh |= {g["id"] for g in risk_guards_for(key, collect_risks(cluster, reads)[0], "")}
    for guard in cluster_guards:
        kind = guard.get("kind", GUARD_KIND_FAILURE)
        if kind == GUARD_KIND_FAILURE and guard.get("source") == GUARD_SOURCE_EVENT:
            result["not_recheckable"].append(guard["id"])
            continue
        reads_needed = set(guard.get("reads") or SPEC_SHAPE_READS) if kind == GUARD_KIND_RISK else set(RECHECK_FAILURE_READS) | (set(RECHECK_BUDGET_READS) if guard.get("entry") == ENTRY_BUDGET else set())
        present = guard["id"] in fresh or (kind == GUARD_KIND_FAILURE and guard.get("entry") == ENTRY_BUDGET and guard.get("object") in budgets_at_zero)
        if present:
            result["refreshed"].append(guard["id"])
        elif reads_needed <= answered:
            result["cleared"].append(guard["id"])
    return result


def _safe_recheck(key: str, cluster: dict, cluster_guards: list[dict], *, run: RunFn, refresh: bool = False) -> dict:
    try:
        return recheck_cluster(key, cluster, cluster_guards, run=run, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 -- the boundary is the point
        log(f"{key}: re-check failed: {exc!r}")
        return {"cluster": key, "guards": len(cluster_guards), "cleared": [], "refreshed": [], "not_recheckable": [], "errors": [f"re-check failed: {exc!r}"[:ERROR_EXCERPT_CHARS]], "symptom_baseline": None}


def _safe_review(selection: Selection, ledger: dict, **kwargs) -> dict:
    """`review_cluster` with the exception boundary the docstring promises:
    an object shape this collector did not expect is a failed read of that
    cluster, not the end of the run."""
    try:
        return review_cluster(selection, ledger, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- the boundary is the point
        log(f"{selection.key}: review failed: {exc!r}")
        return {
            "cluster": selection.key,
            "project": selection.cluster["project"],
            "location": selection.cluster["location"],
            "name": selection.cluster["name"],
            "what_happened": what_happened(selection, ledger),
            "what_failed": [],
            "mitigations": [],
            "shapes": [],
            "managed_agents": 0,
            "next_upgrade": next_upgrade(selection.cluster, kwargs.get("server_config"), kwargs.get("now") or now_utc()),
            "baseline": None,
            "symptom_baseline": None,
            "guards": [],
            "read_errors": [f"review failed: {exc!r}"[:ERROR_EXCERPT_CHARS]],
            "reviewed": False,
            "partial": list(CORE_READS),
            "answered": [],
        }


def review_cluster(selection: Selection, ledger: dict, *, run: RunFn, seen_at: str, server_config: dict | None = None, now: datetime | None = None) -> dict:
    cluster = selection.cluster
    review = {
        "cluster": selection.key,
        "project": cluster["project"],
        "location": cluster["location"],
        "name": cluster["name"],
        "what_happened": what_happened(selection, ledger),
        "what_failed": [],
        "mitigations": [],
        "shapes": [],
        "managed_agents": 0,
        "next_upgrade": next_upgrade(cluster, server_config, now or now_utc()),
        "baseline": None,
        "symptom_baseline": None,
        "guards": [],
        "read_errors": [],
        "reviewed": False,
        "partial": [],
        "answered": [],
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
    failed = {e.split(":")[0] for e in errors}
    review["answered"] = [name for name, _ in KUBECTL_READS if name not in failed]
    review["partial"] = [name for name in CORE_READS if name in failed]
    if len(review["partial"]) == len(CORE_READS):
        # Nothing was evaluated, so nothing counts as answered: no guard of
        # this cluster may be dropped on this run's say-so.
        review["answered"] = []
        return review
    # A partial read still reports what it saw; it is `reviewed` -- the
    # ledger moves and absent guards drop -- only when every core read answered.
    review["reviewed"] = not review["partial"]
    symptoms = collect_symptoms(cluster, {k: v for k, v in reads.items() if k not in failed}, ops, window_start)
    entry = (ledger.get("clusters") or {}).get(selection.key) or {}
    mark_since(symptoms, entry.get("symptoms") if entry.get("last_run") else None, first_op)
    review["what_failed"] = symptoms
    review["symptom_baseline"] = sorted({symptom_key(sym) for sym in symptoms})
    review["mitigations"] = [mitigation_lines(s, c) for s in symptoms for c in s["classifications"] if c["entry"] is not None]
    shapes, managed_agents = collect_risks(cluster, {k: v for k, v in reads.items() if not any(e.startswith(k + ":") for e in errors)})
    review["shapes"] = shapes
    review["managed_agents"] = managed_agents
    review["guards"] = guards_for(selection.key, symptoms, seen_at) + risk_guards_for(selection.key, shapes, seen_at)
    review["baseline"] = {
        "versions": versions_of(cluster),
        "pods": len(reads.get("pods") or []),
        "budgets": len(reads.get("pdbs") or []),
        "shapes": len(shapes),
        "symptom_count": len(symptoms),
        "first_seen": sum(1 for sym in symptoms if sym.get("since") == SINCE_FIRST_SEEN),
        "first_run": not entry.get("last_run"),
        "guards": [g["id"] for g in review["guards"]],
        "shape_reads_failed": [e for e in errors if e.split(":")[0] in SHAPE_READS],
    }
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
        lines.append(f"| pool `{_cell(pool)}` | {_cell(((before or {}).get('node_pools') or {}).get(pool) or '-')} | {_cell(version)} |")
    return lines


def _operation_failed(op: dict) -> bool:
    return bool(op.get("error")) or (op.get("status") or "") != OPERATION_TERMINAL_STATUS


def _symptom_severity(symptom: dict) -> str:
    """Error for a high-confidence entry on a user workload, or a node that
    went NotReady after its pool was upgraded; Warning for everything else
    (medium, system namespace, unclassified)."""
    for c in symptom["classifications"]:
        if c["entry"] == NODE_AFTER_POOL_UPGRADE_ENTRY and c["confidence"] == HIGH and symptom["category"] == CATEGORY_NODE:
            return SEVERITY_ERROR
        if c["entry"] is not None and c["confidence"] == HIGH and not symptom["system"]:
            return SEVERITY_ERROR
    return SEVERITY_WARNING


def _entries_label(classifications: list[dict]) -> str:
    numbers = sorted({c["entry"] for c in classifications if c["entry"] is not None})
    label = ", ".join(str(n) for n in numbers)
    if any(c["entry"] is None for c in classifications):
        label = f"{label}, {UNCLASSIFIED}" if label else UNCLASSIFIED
    return label


def _incident_key(incident: dict) -> tuple[str, str]:
    return incident["cluster"], incident["object"]


def triage(reviews: list[dict], unchanged: list[dict], failed_reads: list[str], guards: list[dict], seen_at: str, removed: list[str] | None = None, rechecks: list[dict] | None = None, upgrading: list[dict] | None = None, in_scope: set[str] | None = None, scope_reason: str = "", unlisted: set[str] | None = None) -> dict:
    """Group the reviews into the report's three sections. An incident is one
    object on one cluster: its symptoms, their (C) rows and the guards it
    produced, plus the cluster's (A) summary."""
    errors: list[dict] = []
    warnings: list[dict] = []
    clean: list[dict] = []
    for review in reviews:
        incidents: dict[str, dict] = {}
        upgraded = review["what_happened"]["status"] == STATUS_UPGRADED
        for symptom in review["what_failed"]:
            incident = incidents.get(symptom["object"])
            if incident is None:
                incidents[symptom["object"]] = incident = {
                    "kind": INCIDENT_SYMPTOM,
                    "severity": SEVERITY_WARNING,
                    "cluster": review["cluster"],
                    "object": symptom["object"],
                    "system": symptom["system"],
                    "entries": "",
                    "predates_upgrade": False,
                    "what_happened": review["what_happened"],
                    "symptoms": [],
                    "mitigations": [m for m in review["mitigations"] if m["object"] == symptom["object"]],
                    "guards": [g for g in review["guards"] if g["object"] == symptom["object"]],
                }
            incident["symptoms"].append(symptom)
            # A symptom that predates the window's operations, or that the
            # previous full run recorded, is reported but never as the
            # upgrade's Error.
            predates = bool(symptom.get("predates_upgrade")) and (upgraded or bool(review["what_happened"]["operations"]))
            if predates:
                incident["predates_upgrade"] = True
            if _symptom_severity(symptom) == SEVERITY_ERROR and not predates:
                incident["severity"] = SEVERITY_ERROR
        for incident in incidents.values():
            incident["entries"] = _entries_label([c for s in incident["symptoms"] for c in s["classifications"]])
        for op in review["what_happened"]["operations"]:
            if _operation_failed(op):
                incidents[OPERATION_OBJECT_PREFIX + (op["name"] or "")] = {
                    "kind": INCIDENT_OPERATION,
                    "severity": SEVERITY_ERROR,
                    "cluster": review["cluster"],
                    "object": f"{OPERATION_OBJECT_PREFIX}{op['type']} {op['target']}",
                    "system": False,
                    "entries": "operation " + (op["status"] or "?"),
                    "what_happened": review["what_happened"],
                    "operation": op,
                    "symptoms": [],
                    "mitigations": [],
                    "guards": [],
                }
        if review["reviewed"] or review.get("partial") and review["answered"]:
            # Every reviewed cluster gets an Info block: its next upgrade,
            # the risks present and the baseline recorded. A cluster none of
            # whose reads answered is not clean; Info names it under the failed reads.
            clean.append({
                "cluster": review["cluster"],
                "what_happened": review["what_happened"],
                "incidents": len(incidents),
                "partial": review.get("partial") or [],
                "outside_fleet": bool(review.get("outside_fleet")),
                "next_upgrade": review["next_upgrade"],
                "shapes": review["shapes"],
                "managed_agents": review["managed_agents"],
                "baseline": review["baseline"],
            })
        for incident in sorted(incidents.values(), key=lambda i: i["object"]):
            (errors if incident["severity"] == SEVERITY_ERROR else warnings).append(incident)
    partial_reads = {r["cluster"]: r["partial"] for r in reviews if r.get("partial") and not r["reviewed"]}
    not_recheckable = {gid for r in rechecks or [] for gid in r.get("not_recheckable") or []}
    recheck_errors = {r["cluster"]: r["errors"] for r in rechecks or [] if r.get("errors")}
    for guard in guards:
        # A risk is never an incident, so only a failure guard that outlived
        # its last review is a Warning; stale risk guards stay in the file,
        # and a scoped run says nothing about clusters outside its scope.
        if in_scope is not None and guard.get("cluster") not in in_scope:
            continue
        if guard.get("cluster") in (unlisted or set()):
            # Its project's listing failed this run: the gap is under "Reads
            # that failed", not a stale Warning per guard.
            continue
        if guard.get("last_seen") != seen_at and guard.get("kind", GUARD_KIND_FAILURE) == GUARD_KIND_FAILURE:
            warnings.append({
                "partial": partial_reads.get(guard["cluster"]) or [],
                "event_only": guard["id"] in not_recheckable,
                "recheck_errors": recheck_errors.get(guard["cluster"]) or [],
                "kind": INCIDENT_STALE_GUARD,
                "severity": SEVERITY_WARNING,
                "cluster": guard["cluster"],
                "object": guard["object"],
                "system": False,
                "entries": str(guard["entry"]),
                "what_happened": None,
                "symptoms": [],
                "mitigations": [],
                "guards": [guard],
            })
    errors.sort(key=_incident_key)
    warnings.sort(key=_incident_key)
    return {
        "errors": errors,
        "warnings": warnings,
        "info": {"clean": clean, "unchanged": unchanged, "failed_reads": failed_reads, "removed": list(removed or []), "rechecked": list(rechecks or []), "upgrading": list(upgrading or []), "scoped": scope_reason},
    }


def _operations_lines(wh: dict) -> list[str]:
    if not wh["operations"]:
        return [NO_OPERATION_LINE + "."]
    lines = ["", "| Operation | Target | Start | End | Duration | Status | Error |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for op in wh["operations"]:
        lines.append(f"| {_cell(op['type'])} | {_cell(op['target'])} | {op['start'] or '-'} | {op['end'] or '-'} | {_duration(op['duration_s'])} | {_cell(op['status'])} | {_cell(op['error'] or '')} |")
    return lines


def _what_happened_lines(wh: dict) -> list[str]:
    lines = [f"{PART_WHAT_HAPPENED} {wh['status']} ({_cell('; '.join(wh['reasons']))}); channel {_cell(wh['channel'] or 'none')}; cluster status {_cell(wh['cluster_status'])}.", ""]
    lines += _versions_table(wh["versions_before"], wh["versions_after"])
    lines += _operations_lines(wh)
    return lines


def _cell(text: object) -> str:
    """Tenant text made safe for a Markdown cell, heading or code span."""
    out = CONTROL_CHARS_RE.sub(" ", str(text if text is not None else ""))
    for raw, safe in CELL_ESCAPES:
        out = out.replace(raw, safe)
    return out


def _incident_lines(incident: dict) -> list[str]:
    lines = [f"### {incident['entries']} — {_cell(incident['cluster'])} — `{_cell(incident['object'])}`" + (" (system)" if incident["system"] else ""), ""]
    if incident["kind"] == INCIDENT_STALE_GUARD:
        guard = incident["guards"][0]
        if incident.get("partial"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_PARTIAL_TEXT.format(failed=', '.join(incident['partial']))}")
        elif incident.get("event_only"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_EVENT_ONLY_TEXT}")
        elif incident.get("recheck_errors"):
            lines.append(f"{PART_WHAT_HAPPENED} {STALE_RECHECK_FAILED_TEXT.format(errors=_cell('; '.join(incident['recheck_errors'])))}")
        else:
            lines.append(f"{PART_WHAT_HAPPENED} cluster not reviewed this run; the guard below is from an earlier run.")
        lines.append(f"{PART_WHAT_FAILED} entry {guard['entry']}. {guard['title']} ({guard['confidence']}) last seen {guard['last_seen']}: {_cell(guard['evidence'])}")
        lines.append(f"{PART_MITIGATE} the entry's row applies until the cluster is reviewed again.")
        lines.append(f"{PART_MITIGATION_SET_UP} guard `{_cell(guard['object'])}` entry {guard['entry']}, first seen {guard['first_seen']}, still live.")
        return lines + [""]
    lines += _what_happened_lines(incident["what_happened"])
    lines.append("")
    if incident["kind"] == INCIDENT_OPERATION:
        op = incident["operation"]
        lines.append(f"{PART_WHAT_FAILED} {_cell(op['type'])} on {_cell(op['target'])} ended {_cell(op['status'])}: {_cell(op['error'] or 'no error text')}")
        lines.append(f"{PART_MITIGATE} GKE's error text names the cause; the catalogue's entry 2 (capacity) and 5 (window) are the usual ones for an operation that did not complete.")
        lines.append(f"{PART_MITIGATION_SET_UP} {OPERATION_GUARD_TEXT}")
        return lines + [""]
    lines += [PART_WHAT_FAILED, "", "| Symptom | Since | Catalogue entry | Confidence | Evidence |", "| --- | --- | --- | --- | --- |"]
    for symptom in incident["symptoms"]:
        for c in symptom["classifications"]:
            entry = f"{c['entry']}. {c['title']}" if c["entry"] else UNCLASSIFIED
            if c.get("detail"):
                entry += f" ({_cell(c['detail'])})"
            lines.append(f"| {_cell(symptom['reason'])} | {symptom.get('since') or '-'} | {entry} | {c['confidence']} | {_cell(c['evidence'])} |")
    if incident.get("predates_upgrade"):
        lines += ["", PREDATES_UPGRADE_TEXT + "."]
    lines += ["", PART_MITIGATE]
    if not incident["mitigations"]:
        lines.append(UNCLASSIFIED_MITIGATION_TEXT)
    for m in incident["mitigations"]:
        lines.append(f"- **{m['entry']}. {m['title']}** — {_cell(m['before_signal'])} Read today: {m['read_today']}. Mitigate before: {m['mitigate_before']} Mitigate after: {m['mitigate_after']}")
    lines += ["", PART_MITIGATION_SET_UP]
    if not incident["guards"]:
        lines.append(UNCLASSIFIED_GUARD_TEXT)
    for g in incident["guards"]:
        lines.append(f"- guard `{_cell(g['id'])}` {g['kind']} entry {g['entry']} ({g['confidence']}), first seen {g['first_seen']}")
    return lines + [""]


def _next_upgrade_lines(nu: dict) -> list[str]:
    standing = "unknown" if nu["below_target"] is None else ("behind the target" if nu["below_target"] else "at or ahead of the target")
    exclusions = "; ".join(f"{_cell(e['name'])} ({_cell(e['scope'])}) until {e['end'] or '?'}{' [active]' if e['active'] else ''}" for e in nu["exclusions"]) or "none"
    return [
        f"{PART_NEXT_UPGRADE} channel {_cell(nu['channel'] or 'none')}; target {_cell(nu['target'] or 'unknown')}; cluster at {_cell(nu['current'])}, {standing}. "
        f"Window: {nu['window']}; next opens {nu['next_opens'] or 'unknown'}. Exclusions: {exclusions}."
    ]


def _risks_lines(shapes: list[dict], managed_agents: int, failed_reads: list[str] | None = None) -> list[str]:
    agents = [MANAGED_AGENTS_LINE.format(count=managed_agents)] if managed_agents else []
    if not shapes:
        failed = {e.split(":")[0] for e in failed_reads or []}
        checked = [text for text, reads in SHAPE_CHECKS if not (set(reads) & failed)]
        skipped = [text for text, reads in SHAPE_CHECKS if set(reads) & failed]
        line = f"{PART_RISKS} {NO_SHAPE_TEXT}{'; '.join(checked)}"
        if skipped:
            line += NOT_CHECKED_TEXT + "; ".join(skipped)
        return [line + "."] + agents
    lines = [PART_RISKS, "", "| Object | Catalogue entry | Confidence | Evidence | Mitigate before |", "| --- | --- | --- | --- | --- |"]
    for shape in shapes:
        system = " (system)" if shape["system"] else ""
        lines.append(f"| `{_cell(shape['object'])}`{system} | {shape['entry']}. {shape['title']} | {shape['confidence']} | {_cell(shape['evidence'])} | {_cell(MITIGATIONS[shape['entry']]['mitigate_before'])} |")
    return lines + ([""] + agents if agents else [])


def _baseline_lines(baseline: dict | None) -> list[str]:
    if not baseline:
        return [f"{PART_BASELINE} {NONE_LINE}"]
    versions = baseline["versions"]
    pools = ", ".join(f"{_cell(p)} {_cell(v)}" for p, v in sorted(versions["node_pools"].items()))
    failed = f" Shape reads that failed: {'; '.join(baseline['shape_reads_failed'])}." if baseline["shape_reads_failed"] else ""
    symptoms = f" Symptom baseline recorded: {baseline.get('symptom_count', 0)} symptom(s), {baseline.get('first_seen', 0)} first seen."
    if baseline.get("first_run"):
        symptoms += " " + FIRST_RUN_GRADING_TEXT
    lines = [f"{PART_BASELINE} control plane {versions['control_plane']}; pools {pools or 'none'}; {baseline['pods']} pods, {baseline['budgets']} budgets, {baseline['shapes']} shapes.{failed}{symptoms} Guards written: {len(baseline['guards'])}."]
    for gid in baseline["guards"]:
        lines.append(f"- `{_cell(gid)}`")
    return lines


def _cluster_block_lines(row: dict) -> list[str]:
    standing = "clean" if not row["incidents"] else f"{row['incidents']} incident(s) above"
    if row.get("outside_fleet"):
        standing += f"; {OUTSIDE_FLEET_TEXT}"
    lines = [f"### {_cell(row['cluster'])} — {standing}", ""]
    lines += _what_happened_lines(row["what_happened"])
    lines.append("")
    if row.get("partial"):
        lines += [PARTIAL_READ_TEXT.format(failed=", ".join(row["partial"])), ""]
    lines += _next_upgrade_lines(row["next_upgrade"])
    lines.append("")
    lines += _risks_lines(row["shapes"], row.get("managed_agents") or 0, list(row.get("partial") or []) + list((row.get("baseline") or {}).get("shape_reads_failed") or []))
    lines.append("")
    lines += _baseline_lines(row["baseline"])
    return lines + [""]


def render_report(result: dict) -> str:
    generated = parse_ts(result["generated_at"]) or now_utc()
    sections = result["sections"]
    lines = [REPORT_TITLE.format(date=generated.strftime(REPORT_DATE_FORMAT)), ""]
    lines.append(f"Window since {result['since']}; projects: {', '.join(result['projects']) or 'none'}; generated {result['generated_at']}.")
    lines.append("")
    for heading, incidents in ((SECTION_ERRORS, sections["errors"]), (SECTION_WARNINGS, sections["warnings"])):
        lines += [heading, ""]
        if not incidents:
            lines += [NONE_LINE, ""]
        for incident in incidents:
            lines += _incident_lines(incident)
    info = sections["info"]
    lines += [SECTION_INFO, ""]
    if info.get("scoped"):
        lines += [INFO_SCOPED.format(reason=_cell(info["scoped"])), ""]
    if not (info["clean"] or info["unchanged"] or info["failed_reads"] or info.get("removed") or info.get("rechecked") or info.get("upgrading")):
        lines += [NONE_LINE, ""]
    # Severity sections keep `_none_`; Info is never empty after a review.
    for row in info["clean"]:
        lines += _cluster_block_lines(row)
    if info["unchanged"]:
        lines += [INFO_UNCHANGED, ""]
        for row in info["unchanged"]:
            nu = row.get("next_upgrade") or {}
            lines.append(f"- {_cell(row['cluster'])} at {_cell(row['control_plane'])}; last upgrade operation {row.get('last_operation') or 'none recorded'}; next target {_cell(nu.get('target') or 'unknown')}{' (behind)' if nu.get('below_target') else ''}; last reviewed {row['last_run'] or 'never'}")
        lines.append("")
    if info.get("upgrading"):
        lines += [INFO_UPGRADING, ""]
        for row in info["upgrading"]:
            op = row["operation"]
            lines.append("- " + UPGRADING_LINE.format(cluster=_cell(row["cluster"]), operation=_cell(op["type"]), target=_cell(op["target"]), start=op["start"] or "?"))
        lines.append("")
    if info.get("rechecked"):
        lines += [INFO_RECHECKED, ""]
        for r in info["rechecked"]:
            errors = f"; reads that failed: {_cell('; '.join(r['errors']))}" if r["errors"] else ""
            not_recheckable = f"; {RECHECK_NOT_RECHECKABLE_TEXT.format(count=len(r['not_recheckable']))}" if r.get("not_recheckable") else ""
            refreshed = "; symptom set refreshed" if r.get("symptom_baseline") is not None else ""
            lines.append(f"- {_cell(r['cluster'])}: re-checked for {r['guards']} guard(s): {len(r['cleared'])} cleared{not_recheckable}{refreshed}{errors}")
        lines.append("")
    if info.get("removed"):
        lines += [INFO_REMOVED, ""]
        for key in info["removed"]:
            lines.append(f"- {_cell(key)}: no longer listed by its project; its ledger entry and guards were dropped")
        lines.append("")
    if info["failed_reads"]:
        lines += [INFO_FAILED_READS, ""]
        for line in info["failed_reads"]:
            lines.append(f"- {_cell(line)}")
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


def ledger_after(ledger: dict, reviews: list[dict], clusters: list[dict], seen_at: str, operations: dict[str, list[dict]] | None = None, removed: set[str] | None = None, skip: set[str] | None = None, refreshed: dict[str, list[str]] | None = None, advance: bool = True) -> dict:
    """Every enumerated cluster's current versions; `last_run` moves only for
    a cluster this run reviewed in full, so a failed or partial read is
    retried next time (`partial_read` records the attempt and re-selects
    it). `last_operation` is the latest upgrade operation the review saw;
    a cluster its project no longer lists is dropped."""
    entries = {k: v for k, v in (ledger.get("clusters") or {}).items() if k not in (removed or set())}
    if not advance:
        # A hand run (`--since`) reports and merges guards but leaves the
        # schedule's memory alone: no last_run, no symptom set.
        return {"version": LEDGER_VERSION, "updated_at": seen_at, LEDGER_PROJECTS_KEY: ledger.get(LEDGER_PROJECTS_KEY) or [], "clusters": entries}
    reviewed = {r["cluster"] for r in reviews if r["reviewed"]}
    baselines = {r["cluster"]: r.get("symptom_baseline") or [] for r in reviews}
    for key, symptoms in (refreshed or {}).items():
        if key in entries:
            entries[key]["symptoms"] = symptoms
            entries[key]["symptoms_refreshed"] = seen_at
    partial = {r["cluster"] for r in reviews if r["partial"] and not r["reviewed"]}
    for cluster in clusters:
        key = cluster_key(cluster["project"], cluster["location"], cluster["name"])
        if key in (skip or set()):
            # Upgrading now: nothing recorded until it is reviewed.
            continue
        current = versions_of(cluster)
        old = entries.get(key) or {}
        if key in reviewed or not old:
            starts = [op.get("startTime") for op in (operations or {}).get(key) or [] if op.get("startTime")]
            latest = parse_ts(max(starts)) if starts else None
            entries[key] = {
                "control_plane": current["control_plane"],
                "node_pools": current["node_pools"],
                "channel": current["channel"],
                "first_seen": old.get("first_seen") or seen_at,
                "last_run": seen_at if key in reviewed else old.get("last_run"),
                "last_operation": fmt_ts(latest) if latest else old.get("last_operation"),
                "symptoms": baselines.get(key) if key in reviewed else old.get("symptoms"),
            }
        if key in partial:
            entries.setdefault(key, {})["partial_read"] = seen_at
        elif key in reviewed:
            entries[key].pop("partial_read", None)
    return {"version": LEDGER_VERSION, "updated_at": seen_at, LEDGER_PROJECTS_KEY: ledger.get(LEDGER_PROJECTS_KEY) or [], "clusters": entries}


def collect(args: argparse.Namespace, *, run: RunFn | None = None, now: datetime | None = None) -> dict:
    # Resolved at call time, so a test that patches `default_run` is honoured.
    run = run or default_run
    clock_fixed = now is not None  # a caller-fixed clock also stamps the finish
    now = now or now_utc()
    seen_at = fmt_ts(now)
    since = parse_since(args.since, now)
    ledger_path = Path(args.ledger) if args.ledger else data_dir() / LEDGER_FILENAME
    guards_path = Path(args.guards) if args.guards else data_dir() / GUARDS_FILENAME
    if not ledger_path.exists():
        crash_records = sorted(ledger_path.parent.glob(CRASH_RECORD_GLOB))
        if crash_records:
            raise StateUnreadable(CRASH_RECORD_TEXT.format(path=crash_records[-1]))
    ledger = load_json(ledger_path, empty_ledger(), version=LEDGER_VERSION, now=now, move_aside=not args.dry_run)
    guards = load_json(guards_path, empty_guards(), version=GUARDS_VERSION, now=now, move_aside=not args.dry_run)
    forced = {c.strip() for c in args.cluster or [] if c.strip()}
    if getattr(args, "full", False) and forced:
        raise argparse.ArgumentTypeError(FULL_WITH_CLUSTER_TEXT)

    failed_reads: list[str] = []
    if args.project:
        projects = sorted({p.strip() for p in args.project if p.strip()})
    else:
        projects, failed_reads = discover_projects(run=run)
    if forced:
        projects = sorted(set(projects) | {c.split(CLUSTER_KEY_SEPARATOR)[0] for c in forced})

    clusters: list[dict] = []
    operations: list[dict] = []
    # Projects whose listing answered in full, with the clusters they hold:
    # a ledger entry or guard for a cluster missing from one is history.
    listed_projects: dict[str, set[str]] = {}
    for project in projects:
        found, list_error = enumerate_clusters(project, run=run)
        error = list_error
        if error:
            failed_reads.append(f"{project}: {error}")
        clusters.extend(found)
        known = [parse_ts((ledger.get("clusters") or {}).get(cluster_key(project, c["location"], c["name"]), {}).get("last_run")) for c in found]
        earliest = min([since] + [k for k in known if k])
        ops, error = list_operations(project, earliest, run=run)
        if error:
            failed_reads.append(f"{project}: {error}")
        for op in ops:
            op["project"] = project
        operations.extend(ops)
        if not list_error:
            listed_projects[project] = {cluster_key(project, c["location"], c["name"]) for c in found}
    if forced:
        clusters = [c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) in forced]
        missing = forced - {cluster_key(c["project"], c["location"], c["name"]) for c in clusters}
        for key in sorted(missing):
            failed_reads.append(f"{key}: named by --cluster but not listed in its project")

    selected, unchanged, upgrading = select_clusters(clusters, ledger, operations, since=since, forced=forced, widen=bool(args.since))
    log(f"{len(clusters)} cluster(s) in {len(projects)} project(s); reviewing {len(selected)}, {len(unchanged)} unchanged, {len(upgrading)} upgrading now")
    # One get-server-config per (project, location), in parallel, for every
    # cluster: the next target is printed for unchanged clusters too.
    locations = sorted({(c["project"], c["location"]) for c in clusters})
    server_configs: dict[tuple[str, str], dict | None] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for (project, location), (config, error) in zip(locations, pool.map(lambda pl: fetch_server_config(pl[0], pl[1], run=run), locations)):
            server_configs[(project, location)] = config
            if error:
                failed_reads.append(f"{project}/{location}: {error}")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        reviews = list(pool.map(lambda s: _safe_review(s, ledger, run=run, seen_at=seen_at, server_config=server_configs.get((s.cluster["project"], s.cluster["location"])), now=now), selected))
    reviews.sort(key=lambda r: r["cluster"])
    # Only a run invoked with --full is full: it records the fleet's project
    # set and may change it, prunes, writes the dated report and moves the
    # link. Every other run is scoped, whatever its --project set. A roster
    # project whose listing failed is that project's gap, not the run's: its
    # entries and guards stay, its known clusters go under "Reads that failed".
    fleet = set(ledger.get(LEDGER_PROJECTS_KEY) or [])
    scope_reasons = []
    if not getattr(args, "full", False):
        scope_reasons.append(NOT_FULL_TEXT)
    if forced:
        scope_reasons.append("--cluster named " + ", ".join(sorted(forced)))
    if args.since:
        scope_reasons.append("--since widens the window by hand")
    scoped = bool(scope_reasons)
    hand_run = bool(args.since)
    unlisted = {project for project in projects if project not in listed_projects}
    unlisted_clusters = {key for key in (ledger.get("clusters") or {}) if key.split(CLUSTER_KEY_SEPARATOR)[0] in unlisted}
    if scoped:
        new_fleet = fleet
        removed = set()
    else:
        new_fleet = set(projects)
        left = fleet - new_fleet
        removed = {key for key in (ledger.get("clusters") or {}) if key.split(CLUSTER_KEY_SEPARATOR)[0] in left}
        removed |= {key for project, listed in listed_projects.items() for key in (ledger.get("clusters") or {}) if key.startswith(project + CLUSTER_KEY_SEPARATOR) and key not in listed}
        for key in sorted(unlisted_clusters):
            error = next((e for e in failed_reads if e.startswith(key.split(CLUSTER_KEY_SEPARATOR)[0] + ": clusters list")), "clusters list failed")
            failed_reads.append(LISTING_FAILED_TEXT.format(cluster=key, error=error.split(": ", 1)[-1]))
    outside = set()
    for review in reviews:
        if review["project"] not in new_fleet:
            # Reviewed and reported, recorded nowhere.
            outside.add(review["cluster"])
            review["outside_fleet"] = True
            review["guards"] = []
            review["symptom_baseline"] = None
            review["what_happened"]["reasons"] = list(review["what_happened"]["reasons"]) + [OUTSIDE_FLEET_TEXT]
    for row in unchanged:
        cluster = next(c for c in clusters if cluster_key(c["project"], c["location"], c["name"]) == row["cluster"])
        row["next_upgrade"] = next_upgrade(cluster, server_configs.get((cluster["project"], cluster["location"])), now)
        row["last_operation"] = ((ledger.get("clusters") or {}).get(row["cluster"]) or {}).get("last_operation")

    # A partially read cluster is "reviewed" for the merge so a finding seen
    # again is refreshed; `answered` keeps it from dropping what it could not see.
    reviewed = {r["cluster"] for r in reviews if (r["reviewed"] or r["partial"]) and r["cluster"] not in outside}
    answered = {r["cluster"]: set(r["answered"]) for r in reviews}
    fresh_guards = [g for r in reviews for g in r["guards"]]
    new_guards = merge_guards(guards, fresh_guards, reviewed, seen_at, answered, removed)
    # Unchanged clusters holding live guards: re-read only what the guards
    # need, clear what is gone, refresh what is still there.
    held: dict[str, list[dict]] = {}
    for guard in new_guards["guards"]:
        held.setdefault(guard["cluster"], []).append(guard)
    by_key = {cluster_key(c["project"], c["location"], c["name"]): c for c in clusters}
    # A full run also refreshes every fleet cluster's stored symptom set, so
    # the next review knows what was there before its upgrade.
    to_recheck = [
        (row["cluster"], by_key[row["cluster"]], held.get(row["cluster"], []), not scoped and row["cluster"].split(CLUSTER_KEY_SEPARATOR)[0] in new_fleet)
        for row in unchanged
        if row["cluster"] in by_key and (row["cluster"] in held or (not scoped and row["cluster"].split(CLUSTER_KEY_SEPARATOR)[0] in new_fleet))
    ]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rechecks = list(pool.map(lambda item: _safe_recheck(item[0], item[1], item[2], run=run, refresh=item[3]), to_recheck))
    refreshed_baselines = {r["cluster"]: r["symptom_baseline"] for r in rechecks if r.get("symptom_baseline") is not None}
    cleared_ids = {gid for r in rechecks for gid in r["cleared"]}
    refreshed_ids = {gid for r in rechecks for gid in r["refreshed"]}
    new_guards["guards"] = [g for g in new_guards["guards"] if g["id"] not in cleared_ids]
    for guard in new_guards["guards"]:
        if guard["id"] in refreshed_ids:
            guard["last_seen"] = seen_at
    for r in rechecks:
        failed_reads.extend(f"{r['cluster']}: re-check: {e}" for e in r["errors"])
    new_ledger = ledger_after(ledger, reviews, clusters, seen_at, {s.key: s.operations for s in selected}, removed, {row["cluster"] for row in upgrading} | outside, refreshed_baselines, advance=not hand_run)
    new_ledger[LEDGER_PROJECTS_KEY] = sorted(new_fleet)

    # Read failures from inside a review join the top-level list so Info
    # names every one in one place.
    failed_reads = failed_reads + [f"{r['cluster']}: {e}" for r in reviews for e in r["read_errors"]]
    result = {
        "generated_at": seen_at,
        "since": fmt_ts(since),
        "projects": projects,
        "reviews": reviews,
        "unchanged": unchanged,
        "failed_reads": failed_reads,
        "removed_clusters": sorted(removed),
        "rechecks": rechecks,
        "upgrading": upgrading,
        "scoped": scoped,
        "scope_reason": "; ".join(scope_reasons),
        "fleet": sorted(new_fleet),
        "outside_fleet": sorted(outside),
        "sections": triage(reviews, unchanged, failed_reads, new_guards["guards"], seen_at, sorted(removed), rechecks, upgrading, in_scope={cluster_key(c["project"], c["location"], c["name"]) for c in clusters} if scoped else None, scope_reason="; ".join(scope_reasons), unlisted=unlisted_clusters),
        "ledger_path": str(ledger_path),
        "guards_path": str(guards_path),
        "guards": new_guards["guards"],
        "dry_run": bool(args.dry_run),
    }
    result["report"] = render_report(result)
    if args.dry_run:
        return result
    # Write order: report, JSON, guards, ledger last -- each atomic -- so a
    # crash leaves at most a report with no ledger advance. Names carry the
    # finish time; a scoped run never moves the fleet link.
    finish = (now if clock_fixed else now_utc()).strftime(REPORT_TS_FORMAT)
    suffix = SCOPED_SUFFIX if scoped else ""
    reports_dir = data_dir() / REPORTS_SUBDIR
    if args.report and Path(args.report).name == LATEST_REPORT_LINK:
        raise argparse.ArgumentTypeError(REPORT_IS_LINK_TEXT.format(name=LATEST_REPORT_LINK))
    if not getattr(args, "no_report", False):
        report_path = Path(args.report) if args.report else reports_dir / REPORT_FILENAME.format(ts=finish, scoped=suffix)
        write_report(report_path, result["report"], link=not scoped)
        result["report_path"] = str(report_path)
    json_path = Path(args.output) if args.output else reports_dir / REPORT_JSON_FILENAME.format(ts=finish, scoped=suffix)
    write_json_atomically(json_path, {k: v for k, v in result.items() if k != "report"})
    result["json_path"] = str(json_path)
    prune_reports(reports_dir)
    write_json_atomically(guards_path, new_guards)
    write_json_atomically(ledger_path, new_ledger)
    return result


def prune_reports(reports_dir: Path) -> list[str]:
    """Keep the newest `REPORTS_KEPT` full reports and the newest
    `REPORTS_KEPT` scoped ones (Markdown and JSON by their shared stamp)."""
    pruned = []
    if not reports_dir.is_dir():
        return pruned
    stamps: dict[bool, set[str]] = {False: set(), True: set()}
    for entry in reports_dir.iterdir():
        m = REPORT_NAME_RE.match(entry.name)
        if m:
            stamps[bool(m.group(2))].add(m.group(1))
    for scoped, found in stamps.items():
        for stamp in sorted(found, reverse=True)[REPORTS_KEPT:]:
            for ext in ("md", "json"):
                victim = reports_dir / f"{stamp}{SCOPED_SUFFIX if scoped else ''}.{ext}"
                with contextlib.suppress(FileNotFoundError):
                    victim.unlink()
                    pruned.append(victim.name)
    return pruned


def write_report(path: Path, text: str, *, link: bool = True) -> None:
    """The report through a temporary file, the latest link through a
    temporary symlink, each renamed into place: a reader never sees half.
    A scoped run writes no link: the link always names a fleet-wide report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + REPORT_TEMP_SUFFIX)
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    if not link:
        return
    link = path.parent / LATEST_REPORT_LINK
    temp_link = path.parent / (LATEST_REPORT_LINK + LINK_TEMP_SUFFIX)
    with contextlib.suppress(FileNotFoundError):
        temp_link.unlink()
    temp_link.symlink_to(path.name)
    os.replace(temp_link, link)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect what each new or upgraded cluster's last upgrade did, and classify what failed against the upgrade failure catalogue.")
    parser.add_argument("--project", action="append", help="GCP project to enumerate (repeatable); without it the active gcloud project plus every project `gcloud projects list` returns, as collect.py discovers")
    parser.add_argument("--cluster", action="append", help="<project>/<location>/<name>: restrict to this cluster and review it even if unchanged (repeatable)")
    parser.add_argument("--since", help=f"hand run: widen the window to <days>, <days>d or an RFC 3339 timestamp (default {DEFAULT_SINCE_DAYS} days for a cluster not yet in the ledger); makes the run scoped and leaves the ledger's last-run times alone")
    parser.add_argument("--ledger", help=f"ledger path (default ${STORE_HOME_ENV}/{LEDGER_FILENAME}, {DEFAULT_STORE_DIR}/{LEDGER_FILENAME})")
    parser.add_argument("--guards", help=f"guards path (default ${STORE_HOME_ENV}/{GUARDS_FILENAME})")
    parser.add_argument("--output", help=f"write the JSON result here instead of ${STORE_HOME_ENV}/{REPORTS_SUBDIR}/<finish-UTC>[{SCOPED_SUFFIX}].json")
    parser.add_argument("--report", help=f"write the Markdown report here instead of ${STORE_HOME_ENV}/{REPORTS_SUBDIR}/<finish-UTC>[{SCOPED_SUFFIX}].md (a full run also points {LATEST_REPORT_LINK} beside it at it)")
    parser.add_argument("--no-report", action="store_true", help="print the report without writing it to the store")
    parser.add_argument("--reset-ledger", action="store_true", help=f"archive the crash records ({CRASH_RECORD_GLOB}) under {ARCHIVE_SUBDIR}/ so the next run may start fresh; does nothing else")
    parser.add_argument("--full", action="store_true", help=f"the fleet-wide run: records and may change the ledger's fleet set, prunes departed projects and clusters, writes {REPORTS_SUBDIR}/<finish-UTC>.md and moves {LATEST_REPORT_LINK}; without it a run is scoped whatever its --project set")
    parser.add_argument("--dry-run", action="store_true", help="read everything, print the report, write nothing")
    return parser


def acquire_lock(path: Path):
    """An exclusive, non-blocking lock on `path` for the whole run; None
    when another run holds it. The handle keeps the lock until it is closed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 -- held for the run
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    os.utime(path, None)
    return handle


def lock_held_line(path: Path) -> str:
    try:
        since = fmt_ts(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
    except OSError:
        since = "unknown"
    return LOCK_HELD_TEXT.format(path=path, since=since)


def reset_ledger(store: Path, now: datetime) -> str:
    """Archive the crash records beside a missing ledger. The archive keeps
    them readable; only the operator's choice to run this clears the block."""
    records = sorted(store.glob(CRASH_RECORD_GLOB)) + sorted(store.glob(GUARDS_FILENAME + ".unreadable-*"))
    if not records:
        return RESET_LEDGER_NOTHING_TEXT
    archive = store / ARCHIVE_SUBDIR / now.strftime(REPORT_TS_FORMAT)
    archive.mkdir(parents=True, exist_ok=True)
    for record in records:
        os.replace(record, archive / record.name)
    return RESET_LEDGER_TEXT.format(count=len(records), archive=archive)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.reset_ledger:
        print(reset_ledger(data_dir(), now_utc()))
        return 0
    lock = None
    if not args.dry_run:
        lock_path = data_dir() / LOCK_FILENAME
        lock = acquire_lock(lock_path)
        if lock is None:
            print(lock_held_line(lock_path))
            return 0
    try:
        result = collect(args)
    except argparse.ArgumentTypeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    except StateUnreadable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    finally:
        if lock is not None:
            lock.close()
    sys.stdout.write(result["report"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
