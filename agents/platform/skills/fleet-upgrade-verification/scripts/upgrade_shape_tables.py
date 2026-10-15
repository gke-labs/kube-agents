#!/usr/bin/env python3
"""
upgrade_shape_tables.py — the facts behind the upgrade-failure catalogue's static shapes,
shared by the readiness rules under `readiness_rules/` and anything else that reasons about
an upgrade's shape.

One home for the tables the rules read. Every constant is named for the catalogue shape it
serves, so any other reader of the same shapes (a review of what an upgrade did, after the
fact) imports this module instead of carrying a copy. A table is appended to, never
rewritten: a constant keeps its name and its shape once it exists. Every table names its
source beside it; a date beside one is the day a page that moves was read. The tables
are deliberately small: a rule whose input has no row here grades `unknown` with the reason
rather than guessing (docs/designs/upgrade-readiness-checks.md, "What a run produces").
Nothing here is computed from a cluster and nothing here runs a command.

Each section names the catalogue entries it serves, as docs/designs/upgrade-failure-catalogue.md
numbers them. The tables ship with the rules registry before the per-entry rules that import
them, so a section with no reader in the tree yet is expected rather than stale.
"""

import json
import pkgutil
import re
from pathlib import Path

# ------------------------------------------------------------------ namespaces, kinds

# Namespaces GKE and its add-ons occupy: the set every fleet-audit collector spells
# (`fleet-audit/scripts/collect.py`, SYSTEM_NAMESPACES, copied rather than imported because
# each script ships standalone), plus the two prefixes those collectors skip, every `gke-*`
# namespace (gke-gmp-system, gke-managed-*) and Config Sync's. GKE upgrades what runs there
# with the version, so the readiness rules read nothing in them.
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
# Kinds whose spec carries a pod template. Bare Pods and Jobs are not read.
TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob")
# The pool a node belongs to, as a node label and as the selector a template pins with.
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"

# ------------------------------------------------------------------ entry 4: data on the node

# A cache is the acceptable use the catalogue names, so it is not here. Each word stands
# on its own in the name, a non-letter (a dash, an underscore, a digit, an end) on each
# side, so `restore`, `metadata`, `logdatadog`, `dequeue`, `firestore`, `dbus` and `walrus`
# do not match while `data-0`, `pg-wal`, `redis_data` and `db` do. A run of letters made
# of listed words and nothing else (`datastore`, `dbdata`, `statedb`) matches as the
# compound it is, the `+` below; a run with letters outside the words (`esdata`,
# `statefulset`, `datalog`) does not. The charts' spellings that run a word into an
# unlisted one (`pgdata`, `datadir`, `database`, `mysqldata`, `mongodata`, and `datalogdir`,
# Confluent's cp-zookeeper transaction-log volume) are named whole. `nginx-cache` matches
# nothing; `wal-e-bin` matches on `wal`, the name of a WAL directory more often than of
# WAL-E's binaries.
STATEFUL_VOLUME_WORDS = ("data", "state", "queue", "store", "storage", "journal", "persist", "persistent", "persistence", "db", "wal")
STATEFUL_VOLUME_CHART_SPELLINGS = ("pgdata", "datadir", "database", "mysqldata", "mongodata", "datalogdir")
STATEFUL_VOLUME_NAME_RE = re.compile(r"(?<![a-z])(?:" + "|".join(STATEFUL_VOLUME_CHART_SPELLINGS + STATEFUL_VOLUME_WORDS) + r")+(?![a-z])", re.I)
# Where a node exposes Local SSD: Compute Engine formats and mounts Local SSD under
# /mnt/disks (cloud.google.com/compute/docs/disks/add-local-ssd#formatandmount); GKE's
# ephemeral Local SSD lives under /mnt/stateful_partition on Container-Optimized OS
# (docs.cloud.google.com/kubernetes-engine/docs/concepts/local-ssd). Data there "does not
# persist when the Pod or node is deleted, repaired, upgraded, or experiences an
# unrecoverable error" (the same page).
LOCAL_SSD_HOSTPATH_PREFIXES = ("/mnt/disks", "/mnt/stateful_partition")
# Local PersistentVolumes are statically provisioned; the StorageClass names this
# provisioner (kubernetes.io/docs/concepts/storage/storage-classes/#local, and GKE's
# Local SSD persistent-volume how-to, which uses the local volume static provisioner).
LOCAL_VOLUME_PROVISIONER = "kubernetes.io/no-provisioner"

# ------------------------------------------------ entries 6, 9, 10: the audit-log read

# The audit-log rules (catalogue entries 6, 9 and 10): two paged `gcloud logging read`s
# per cluster (the removed-release read and the deprecated-or-kubectl read) over the seven
# days ending at the evaluation instant. Seven days sees a CronJob caller on any schedule a
# readiness check should catch (the seeded fixture writes every ten minutes) and a weekly
# batch job once. A page is AUDIT_LOG_PAGE_LIMIT entries (`--limit`); a read pages by
# timestamp bound for at most AUDIT_LOG_MAX_PAGES pages and starts no new page once
# AUDIT_LOG_READ_BUDGET_SECONDS have elapsed, the same figure as the report's per-call cap,
# so one read is at most one call over it; a read that still fills its last page is graded
# on what it saw and noted as sampled, never reported as an absence or an unknown.
AUDIT_LOG_WINDOW_DAYS = 7
AUDIT_LOG_PAGE_LIMIT = 1000
AUDIT_LOG_MAX_PAGES = 3
AUDIT_LOG_READ_BUDGET_SECONDS = 60

# kubectl skew (entry 10). The Kubernetes version skew policy, read 2026-10-09: "kubectl
# is supported within one minor version (older or newer) of kube-apiserver". The page
# says nothing about client-go, and client-go's README gives a compatibility matrix
# rather than a support window; a client-go user agent also carries the binary's
# version, not the library's, so the rule grades kubectl and names the rest unread.
KUBECTL_SKEW_MINORS = 1
KUBECTL_SKEW_SOURCE = "https://kubernetes.io/releases/version-skew-policy/#kubectl"
CLIENT_GO_COMPATIBILITY_SOURCE = "https://github.com/kubernetes/client-go#compatibility-matrix"

# Deprecated-but-served APIs with a named successor (entry 9), keyed as the audit-log
# helper names an API (`group/version resource`). Endpoints: Kubernetes v1.33 deprecated
# the API in favour of EndpointSlices (KEP-4974; the v1.33 upcoming-changes post, read
# 2026-10-09, says the deprecation "only impacts those who use the Endpoints API directly
# from workloads or scripts; these users should migrate to use EndpointSlices instead").
DEPRECATED_API_SUCCESSORS = {
    "core/v1 endpoints": "discovery.k8s.io/v1 EndpointSlice (Endpoints deprecated in 1.33, KEP-4974)",
}
DEPRECATED_API_SUCCESSORS_SOURCE = "https://kubernetes.io/blog/2025/03/26/kubernetes-v1-33-upcoming-changes/"

# ------------------------------------------------------------------ entry 8: changed defaults

# Defaults that change at a minor (entry 8), keyed by the minor that changes them. A
# minor at or below DEFAULT_CHANGES_AS_OF with no key changes nothing in this table; a
# target above it is not covered, and the rule says `unknown`. Row fields: `setting`
# names what changes; `detector` names the object shape the row is matched against
# (namespace Pod Security labels, or workloads carrying a gitRepo volume); `side` says
# which component's version the change rides on, so the rule measures the minors crossed
# from the control plane (admission) or from the lowest node pool (a kubelet default);
# `levels` names the Pod Security Standards levels the row changes, restricted inheriting
# every baseline check; `tightens` says whether the change can reject a pod the previous
# minor admitted, which is what makes a namespace that follows `latest` a risk rather
# than a note; `change` is the clause as the source states it.
POD_SECURITY_STANDARDS_SOURCE = "https://kubernetes.io/docs/concepts/security/pod-security-standards/"
KUBERNETES_1_33_CHANGELOG_SOURCE = "https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.33.md"
KUBERNETES_1_36_RELEASE_SOURCE = "https://kubernetes.io/blog/2026/04/22/kubernetes-v1-36-release/"
DEFAULT_CHANGES_READ_ON = "2026-10-09"
DETECTOR_POD_SECURITY = "pod-security-admission"
DETECTOR_GITREPO_VOLUME = "gitrepo-volume"
SIDE_APISERVER = "apiserver"
SIDE_KUBELET = "kubelet"
PSA_LEVEL_PRIVILEGED = "privileged"
PSA_LEVEL_BASELINE = "baseline"
PSA_LEVEL_RESTRICTED = "restricted"
BASELINE_AND_RESTRICTED = (PSA_LEVEL_BASELINE, PSA_LEVEL_RESTRICTED)
RESTRICTED_ONLY = (PSA_LEVEL_RESTRICTED,)
NO_LEVELS = ()
PSA_SETTING = "Pod Security Admission {level}: {control}"
GITREPO_SETTING = "gitRepo volumes"
DEFAULT_CHANGES_AS_OF = (1, 36)
DEFAULT_CHANGES_BY_MINOR = {
    (1, 23): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_RESTRICTED, control="Running as Non-root user (v1.23+)"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": RESTRICTED_ONLY,
            "tightens": True,
            "change": "containers must not set runAsUser to 0",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 25): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_RESTRICTED, control="pod.spec.os.name"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": RESTRICTED_ONLY,
            "tightens": False,
            "change": "the restricted checks read pod.spec.os.name, so Windows pods are exempt from the Linux-only checks",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 27): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Sysctls"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": "net.ipv4.ip_local_reserved_ports joins the allowed sysctls",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 29): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Sysctls"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": (
                "net.ipv4.tcp_keepalive_time, net.ipv4.tcp_fin_timeout, net.ipv4.tcp_keepalive_intvl "
                "and net.ipv4.tcp_keepalive_probes join the allowed sysctls"
            ),
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 31): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="SELinux"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": "container_engine_t joins the allowed SELinux types",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 33): [
        {
            "setting": GITREPO_SETTING,
            "detector": DETECTOR_GITREPO_VOLUME,
            "side": SIDE_KUBELET,
            "levels": NO_LEVELS,
            "tightens": True,
            "change": (
                "the gitRepo volume plugin is disabled by default; it mounts only with the "
                "GitRepoVolumeDriver feature gate turned back on (kubernetes/kubernetes#129923)"
            ),
            "source": KUBERNETES_1_33_CHANGELOG_SOURCE,
        }
    ],
    (1, 34): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Host Probes / Lifecycle Hooks (v1.34+)"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": True,
            "change": "the host field of probes and lifecycle hooks must be unset",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 36): [
        {
            "setting": GITREPO_SETTING,
            "detector": DETECTOR_GITREPO_VOLUME,
            "side": SIDE_KUBELET,
            "levels": NO_LEVELS,
            "tightens": True,
            "change": "the gitRepo volume plugin is permanently disabled and cannot be turned back on",
            "source": KUBERNETES_1_36_RELEASE_SOURCE,
        }
    ],
}

# ------------------------------------------------------------------ entry 10: add-on support

# Third-party add-on support (entry 10). An add-on is recognised by the repository of its
# container image (registry kept, tag and digest stripped) and its release read from the
# tag. A support row maps an add-on release to the Kubernetes minors it supports,
# inclusive, as the vendor's page listed them on the day given. An add-on detected with
# no row here, or at a release the rows do not list, is reported `unknown` with the page
# to read; no range is ever inferred from a neighbouring release.
ADDON_IMAGES = {
    "quay.io/jetstack/cert-manager-controller": "cert-manager",
    "docker.io/istio/pilot": "istio",
    "istio/pilot": "istio",
    "nvcr.io/nvidia/gpu-operator": "nvidia-gpu-operator",
    "quay.io/argoproj/argocd": "argo-cd",
    "ghcr.io/fluxcd/source-controller": "flux",
    "registry.k8s.io/ingress-nginx/controller": "ingress-nginx",
    "quay.io/cilium/cilium": "cilium",
    "docker.io/calico/node": "calico",
    "calico/node": "calico",
    "registry.k8s.io/external-dns/external-dns": "external-dns",
    "quay.io/prometheus-operator/prometheus-operator": "prometheus-operator",
}
ADDON_SUPPORT = {
    "cert-manager": {
        "source": "https://cert-manager.io/docs/releases/",
        "read_on": "2026-10-09",
        # cert-manager release -> (lowest, highest) supported Kubernetes minor.
        "releases": {
            (1, 21): ((1, 33), (1, 36)),
            (1, 20): ((1, 32), (1, 35)),
        },
    },
}
# Image repositories whose last path element is a kubectl binary rather than an add-on:
# a workload running one is a client the skew policy covers.
KUBECTL_IMAGE_NAMES = ("kubectl",)

# ------------------------------------------------------------------ entry 12: node labels

# Labels the kubelet still sets but Kubernetes has deprecated in favour of GA names
# (kubernetes.io/docs/reference/labels-annotations-taints/: the beta arch and os labels
# are "deprecated", instance-type and the failure-domain pair "starting in v1.17"; the arch
# and os deprecation is the Kubernetes 1.14 changelog's). A node image that stops setting
# one strands every pod selecting on it.
DEPRECATED_NODE_LABEL_PREFIXES = ("beta.kubernetes.io/", "failure-domain.beta.kubernetes.io/")
DEPRECATED_NODE_LABELS = {
    "beta.kubernetes.io/arch": ("kubernetes.io/arch", "1.14"),
    "beta.kubernetes.io/os": ("kubernetes.io/os", "1.14"),
    "beta.kubernetes.io/instance-type": ("node.kubernetes.io/instance-type", "1.17"),
    "failure-domain.beta.kubernetes.io/region": ("topology.kubernetes.io/region", "1.17"),
    "failure-domain.beta.kubernetes.io/zone": ("topology.kubernetes.io/zone", "1.17"),
}
# Labels (or label values) a minor dropped from the nodes that carried them, as
# (label, value or None for any value, (major, minor), source). kubeadm stopped setting
# `node-role.kubernetes.io/master` on new control planes in 1.24
# (kubernetes/kubernetes#107533, KEP-2067; GKE never set it on a worker node); GKE 1.24
# removed Docker node images, so no node at or above it carries
# `cloud.google.com/gke-container-runtime=docker`
# (docs.cloud.google.com/kubernetes-engine/docs/deprecations/docker-containerd). No GKE
# removal of a label with no value is verified (the catalogue's entry 12 says so).
DROPPED_NODE_LABELS = (
    ("node-role.kubernetes.io/master", None, (1, 24), "kubeadm 1.24 stops setting it (kubernetes/kubernetes#107533)"),
    ("cloud.google.com/gke-container-runtime", "docker", (1, 24), "GKE 1.24 removes Docker node images"),
)

# ------------------------------------------------------------------ entry 13: the container runtime

# Node image types as `nodePools[].config.imageType` spells them.
IMAGE_TYPE_COS_CONTAINERD = "COS_CONTAINERD"
IMAGE_TYPE_UBUNTU_CONTAINERD = "UBUNTU_CONTAINERD"
# The containerd major a node image ships at a GKE minor, by node operating system
# family. GKE: "Linux nodes that run GKE 1.32 or earlier, with containerd node images,
# use containerd 1.7 or earlier versions"; "Linux nodes that run GKE 1.33 use containerd
# 2.0"; "Windows Server nodes that run GKE 1.34 or earlier ... use containerd 1.7 or
# earlier versions"; "Windows Server nodes that run GKE 1.35 use containerd 2.0".
# Source: https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/migrate-containerd-2
# The table is the target side of a comparison. For the current side, read the node's
# own `status.nodeInfo.containerRuntimeVersion` where the nodes were read: the
# upgrade-failure reproduction (docs/designs/upgrade-failure-reproductions.md, entry 13)
# saw a patch inside 1.31 move a node from containerd 1.7.34 to 2.0.10, so a running
# node can be ahead of what the table says for its minor.
NODE_OS_LINUX = "linux"
NODE_OS_WINDOWS = "windows"
CONTAINERD_MAJOR_1 = 1
CONTAINERD_MAJOR_2 = 2
# Per node OS family, (first minor that ships containerd 2, 2); a minor below it ships
# CONTAINERD_MAJOR_BEFORE_TABLE, containerd 1.
CONTAINERD_MAJOR_BY_GKE_MINOR = {
    NODE_OS_LINUX: ((1, 33), CONTAINERD_MAJOR_2),
    NODE_OS_WINDOWS: ((1, 35), CONTAINERD_MAJOR_2),
}
CONTAINERD_MAJOR_BEFORE_TABLE = CONTAINERD_MAJOR_1
# The node operating system family of each GKE node image type, as `nodePools[].config.imageType`
# spells them (upper case). Every supported type runs containerd; the Docker-based types
# (`COS`, `UBUNTU`, `WINDOWS_LTSC`) left with 1.24.
# Source: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-images
NODE_IMAGE_OS_FAMILY = {
    IMAGE_TYPE_COS_CONTAINERD: NODE_OS_LINUX,
    IMAGE_TYPE_UBUNTU_CONTAINERD: NODE_OS_LINUX,
    "COS": NODE_OS_LINUX,
    "UBUNTU": NODE_OS_LINUX,
    "WINDOWS_LTSC_CONTAINERD": NODE_OS_WINDOWS,
    "WINDOWS_SAC_CONTAINERD": NODE_OS_WINDOWS,
    "WINDOWS_LTSC": NODE_OS_WINDOWS,
    "WINDOWS_SAC": NODE_OS_WINDOWS,
}
# An image type the table does not name (a new one, or an empty field on an Autopilot
# record) is read as Linux, the family every GKE node pool defaults to.
NODE_IMAGE_DEFAULT_OS_FAMILY = NODE_OS_LINUX

# ------------------------------------------------------------------ entries 14 and 15: cgroup v2

# The node pool's cgroup mode as `nodePools[].config.effectiveCgroupMode` reports it
# (GKE REST v1 NodeConfig), and the pin an operator sets through
# `nodePools[].config.linuxNodeConfig.cgroupMode`.
CGROUP_V2_MODE = "EFFECTIVE_CGROUP_MODE_V2"
CGROUP_V1_MODE = "EFFECTIVE_CGROUP_MODE_V1"
CGROUP_MODE_V1_PIN = "CGROUP_MODE_V1"
CGROUP_MODE_V2_PIN = "CGROUP_MODE_V2"
# GKE's cgroup v2 timeline (docs.cloud.google.com/kubernetes-engine/docs/how-to/migrate-cgroupv2):
# v2 is the default for new nodes from 1.26, GKE migrates v1 pools to v2 from 1.33, and
# removes v1 support at 1.35, which is where a pin to v1 ends.
CGROUP_V2_MIGRATION_MINOR = 33
CGROUP_V1_REMOVAL_MINOR = 35
# Runtimes that read their memory limit from cgroup v1 paths, by image repository and
# the first version that reads cgroup v2 (kubernetes.io/docs/concepts/architecture/cgroups/:
# OpenJDK/HotSpot "jdk8u372, 11.0.16, 15 and later"). Pinned tags only: `8-jre` names no
# update, so a rule grades it `unknown`.
JAVA_IMAGE_REPOS = ("eclipse-temurin", "openjdk", "adoptopenjdk")
JAVA_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?:u(\d+)|\.(\d+)\.(\d+))")
# The bare major a floating tag carries (`openjdk:17`, `eclipse-temurin:8-jre`): enough to
# clear a major at or above the first one with cgroup v2 support, not enough to grade 8 or 11.
JAVA_MAJOR_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?!\d)")
JDK8_CGROUP_V2_UPDATE = 372
JDK11_CGROUP_V2_PATCH = (0, 16)
JDK_FIRST_MAJOR_WITH_CGROUP_V2 = 15
# .NET read cgroup v2 from 5.0: dotnet/runtime#30337 ("Add cgroup v2 support to .NET Core",
# milestone 5.0.0) and its runtime change dotnet/runtime#34334; .NET Core 3.1 and 2.1 read
# cgroup v1 only.
DOTNET_IMAGE_REPO_MARKERS = ("mcr.microsoft.com/dotnet/", "microsoft/dotnet")
DOTNET_TAG_RE = re.compile(r"^(\d+)\.(\d+)")
DOTNET_CGROUP_V2_VERSION = (5, 0)
# From Kubernetes 1.28 the kubelet sets memory.oom.group on every container on a cgroup v2
# node (kubernetes/kubernetes#117070); `singleProcessOOMKill` in the node system config
# turns it off, from GKE 1.32.4-gke.1132000 and 1.33.0-gke.1748000
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/node-system-config). The REST record
# spells the field `linuxNodeConfig.singleProcessOomKill`.
GROUP_OOM_KILL_KUBELET_MINOR = 28
SINGLE_PROCESS_OOM_KILL_FIELD = "singleProcessOomKill"
# Entry 15 is a heuristic on the command and the image: the process count is not readable
# from the API. Programs whose job is to run several services in one container
# (supervisord, s6-overlay's s6-svscan, runit's runsvdir), a shell script that puts a
# command in the background (`a & b & wait`, `nginx -p 8080& php-fpm`; the script is the
# word after any short flag group carrying `c`, `-c`, `-ec`, `-lc`, and
# `readiness_rules.shell_forks` reads it, quotes, comments and heredocs included), and
# images whose documented entrypoint is such a supervisor: phusion/baseimage and
# phusion/passenger run services under runit (github.com/phusion/baseimage-docker),
# webdevops/php-nginx and richarvey/nginx-php-fpm run nginx and PHP-FPM under supervisord
# (their READMEs).
SUPERVISOR_PROGRAMS = ("supervisord", "s6-svscan", "runsvdir")
SHELL_PROGRAMS = ("sh", "bash", "ash", "dash", "zsh")
SHELL_COMMAND_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")
MULTI_PROCESS_IMAGE_REPOS = ("phusion/baseimage", "phusion/passenger", "webdevops/php-nginx", "richarvey/nginx-php-fpm")

# ------------------------------------------------------------------ entry 17: node networking agents

# Labels that name the node image rather than the pool: the OS distribution (`cos`,
# `ubuntu`), which GKE's Autopilot selector allow-list names and every node carries, and
# the container runtime label of the Docker-to-containerd migration
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/migrate-containerd).
NODE_IMAGE_LABELS = ("cloud.google.com/gke-os-distribution", "cloud.google.com/gke-container-runtime")
# Host paths that couple an agent to the node image: the kernel's module tree, and the
# network plugin's configuration and binary directories the kubelet reads
# (kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/:
# `/etc/cni/net.d` and `/opt/cni/bin`; GKE's node images keep their plugins under
# `/home/kubernetes/bin`).
KERNEL_MODULE_HOST_PATHS = ("/lib/modules", "/usr/lib/modules")
CNI_HOST_PATHS = ("/etc/cni/net.d", "/opt/cni/bin", "/home/kubernetes/bin")
KERNEL_MODULE_COMMANDS = ("modprobe", "insmod")

# ------------------------------------------------------------------ entry 18: GPU drivers

GPU_RESOURCE = "nvidia.com/gpu"
# A CUDA pin in an image tag (`nvidia/cuda:12.2.0-base`), or in the two env values that name
# a toolkit version: `CUDA_VERSION` as NVIDIA's CUDA images set it (`12.2.0`) and
# `NVIDIA_REQUIRE_CUDA`, the container toolkit's requirement string
# (`cuda>=12.2 brand=tesla,driver>=470,driver<471`). No other env is read:
# `TORCH_CUDA_ARCH_LIST` and `TF_CUDA_COMPUTE_CAPABILITIES` carry compute capabilities.
CUDA_IMAGE_PIN_RE = re.compile(r"cuda[:/_-]?(\d+\.\d+)", re.I)
CUDA_VERSION_ENV = "CUDA_VERSION"
CUDA_VERSION_VALUE_RE = re.compile(r"^\s*(\d+\.\d+)")
NVIDIA_REQUIRE_CUDA_ENV = "NVIDIA_REQUIRE_CUDA"
NVIDIA_REQUIRE_CUDA_RE = re.compile(r"cuda>=(\d+\.\d+)")
# The accelerator a template selects, and the pool record's fields for the accelerator and
# its driver install mode (GKE REST v1 NodeConfig.accelerators[]).
ACCELERATOR_LABEL = "cloud.google.com/gke-accelerator"
GPU_DRIVER_DEFAULT = "DEFAULT"
GPU_DRIVER_LATEST = "LATEST"
GPU_DRIVER_INSTALLATION_DISABLED = "INSTALLATION_DISABLED"
# On Autopilot the workload selects the driver with this label, `default` when absent or
# `latest` (docs.cloud.google.com/kubernetes-engine/docs/how-to/autopilot-gpus), and GKE
# provisions the node on Container-Optimized OS
# (docs.cloud.google.com/kubernetes-engine/docs/concepts/node-images).
AUTOPILOT_GPU_DRIVER_LABEL = "cloud.google.com/gke-gpu-driver-version"
AUTOPILOT_GPU_DRIVER_LATEST = "latest"
# The NVIDIA driver branch GKE installs per minor, (default, latest), from the GPU how-to
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus, "Install drivers
# automatically", read 2026-10-09). The rows entered are 1.26 to 1.33 for
# Container-Optimized OS and 1.31 to 1.33 for Ubuntu; the page also lists Ubuntu rows below
# 1.31, not entered here, so an Ubuntu target there is `unknown` until a reader copies them
# from the page. A target outside a table is `unknown`.
GKE_COS_GPU_DRIVERS = {
    (1, 26): (470, 550),
    (1, 27): (470, 550),
    (1, 28): (535, 550),
    (1, 29): (535, 550),
    (1, 30): (535, 550),
    (1, 31): (535, 580),
    (1, 32): (535, 580),
    (1, 33): (535, 580),
}
GKE_UBUNTU_GPU_DRIVERS = {
    (1, 31): (535, 535),
    (1, 32): (535, 535),
    (1, 33): (535, 535),
}
GKE_GPU_DRIVERS_BY_IMAGE_TYPE = {
    IMAGE_TYPE_COS_CONTAINERD: GKE_COS_GPU_DRIVERS,
    IMAGE_TYPE_UBUNTU_CONTAINERD: GKE_UBUNTU_GPU_DRIVERS,
}
# The driver branch a CUDA major needs at all (NVIDIA CUDA Toolkit release notes, "CUDA
# Toolkit and Corresponding Driver Versions", with minor version compatibility: any 11.x
# on R450 and later, any 12.x on R525 and later, any 13.x on R580 and later), and the
# branch each toolkit's GA release names as its own minimum, below which a 12.x
# application runs under minor version compatibility and loses the features newer than
# the driver. Branches are the integer part of the driver version.
CUDA_MAJOR_MIN_DRIVER = {11: 450, 12: 525, 13: 580}
CUDA_TOOLKIT_MIN_DRIVER = {
    (11, 0): 450,
    (11, 1): 455,
    (11, 2): 460,
    (11, 3): 465,
    (11, 4): 470,
    (11, 5): 495,
    (11, 6): 510,
    (11, 7): 515,
    (11, 8): 520,
    (12, 0): 525,
    (12, 1): 530,
    (12, 2): 535,
    (12, 3): 545,
    (12, 4): 550,
    (12, 5): 555,
    (12, 6): 560,
    (12, 8): 570,
    (12, 9): 575,
    (13, 0): 580,
    (13, 1): 590,
    (13, 2): 595,
    (13, 3): 610,
    (13, 4): 615,
}

# ------------------------------------------------------------------ entry 20: retired registries

# Registry hosts that stopped publishing. `k8s.gcr.io` was frozen on 2023-04-03 and has
# redirected to registry.k8s.io since 2023-03-20 (kubernetes.io/blog/2023/02/06/
# k8s-gcr-io-freeze-announcement/, kubernetes.io/blog/2023/03/10/image-registry-redirect/);
# `gcr.io/google-containers` and its underscore spelling are the project the alias stood
# for until 2020-04-01 (kubernetes/kubernetes#55129, kubernetes/kubeadm#2051);
# `gcr.io/kubernetes-helm` served Helm 2's Tiller, unsupported since 2020-11-13
# (helm.sh/blog/helm-2-becomes-unsupported/). A rebuilt node has no image cache and pulls
# from the host again.
RETIRED_IMAGE_HOSTS = ("k8s.gcr.io/", "gcr.io/google-containers/", "gcr.io/google_containers/", "gcr.io/kubernetes-helm/")
RETIRED_IMAGE_HOST_NOTES = {
    "k8s.gcr.io/": "frozen 2023-04-03, a redirect to registry.k8s.io since 2023-03-20",
    "gcr.io/google-containers/": "the project behind k8s.gcr.io, read-only since 2020-04-01",
    "gcr.io/google_containers/": "the project behind k8s.gcr.io, read-only since 2020-04-01",
    "gcr.io/kubernetes-helm/": "Helm 2's registry, unsupported since 2020-11-13",
}

# ------------------------------------------------------------------ entry 6: the removal table

# The removal table the GitOps scan reads (`api_deprecation_scan.py`), reused here for
# the replacement column of a removed-API finding. The same file, so the two reads can
# never disagree about a removal. It is read through the module's loader
# (`pkgutil.get_data`), which is the file beside this module on a checkout or in the
# skill directory and the shipped copy under the daily watch, whose loader serves the
# skill's data files beside its modules; `__file__` there is a path the sandbox lacks.
REMOVED_APIS_NAME = "removed_apis.json"
REMOVED_APIS_UNREADABLE = "{name} could not be read beside {module}: {error}"
REMOVED_APIS_SHAPE = "not an object with a list under {key!r}"
# Each row holds the three fields this reader uses as non-empty strings, a subset of what
# `api_deprecation_scan.load_table` requires of the same file (it also reads `as_of` and
# refuses a duplicate): a row of another shape would otherwise drop out of the lookup and
# read as a removal with no replacement.
REMOVED_APIS_ROW_SHAPE = "row {index} under {key!r} is not an object with non-empty strings under {fields}"
REMOVED_APIS_ROW_FIELDS = ("api_version", "kind", "replacement")
NO_LOADER_DATA = "the module's loader serves no data"
REMOVED_APIS_KEY = "removed"
REMOVED_API_VERSION_KEY = "api_version"
REMOVED_KIND_KEY = "kind"
REMOVED_REPLACEMENT_KEY = "replacement"
# How a lower-cased kind becomes the resource name the audit log carries: `FlowSchema`
# -> `flowschemas`, `Ingress` -> `ingresses`, `PodSecurityPolicy` -> `podsecuritypolicies`.
PLURAL_SUFFIXES = ("s", "es")
PLURAL_Y_TO_IES = ("y", "ies")
API_SEPARATOR = " "
GROUP_VERSION_SEPARATOR = "/"
CORE_GROUP = "core"
# An audit entry with no resourceName names its API from the methodName, which spells a
# `*.k8s.io` group without the suffix (`io.k8s.networking.v1beta1` is `networking.k8s.io`),
# and the `apps`-style groups the same way, so a lookup tries both spellings.
K8S_IO_GROUP_SUFFIX = ".k8s.io"

# `MAJOR.MINOR` at the start of a version string, with or without a `v`, a patch or a
# `-gke.BUILD` suffix: the minor is what every table here is keyed by.
MINOR_RE = re.compile(r"^v?(\d+)\.(\d+)")
MINOR_FORMAT = "{major}.{minor}"

_REMOVED_APIS_CACHE: list | None = None


def containerd_major_at(minor: tuple[int, int], os_family: str) -> int:
    """The containerd major the node image ships at `minor` for `os_family`."""
    first_minor, major = CONTAINERD_MAJOR_BY_GKE_MINOR[os_family]
    return major if minor >= first_minor else CONTAINERD_MAJOR_BEFORE_TABLE


def node_image_os_family(image_type: str) -> str:
    return NODE_IMAGE_OS_FAMILY.get(str(image_type or "").upper(), NODE_IMAGE_DEFAULT_OS_FAMILY)


def _removed_apis() -> list[dict]:
    """The removal table, read once through the module's loader. A table that cannot be
    read, is not an object with a list under `removed`, or has a row that is not an object
    with non-empty strings under `api_version`, `kind` and `replacement` (what the scan's own
    loader requires), raises, so a rule that needs it is `unknown` naming the table rather
    than reporting a removal with no replacement."""
    global _REMOVED_APIS_CACHE
    if _REMOVED_APIS_CACHE is None:
        try:
            raw_bytes = pkgutil.get_data(__name__, REMOVED_APIS_NAME)
            if raw_bytes is None:
                raise OSError(NO_LOADER_DATA)
            raw = json.loads(raw_bytes)
            removed = raw.get(REMOVED_APIS_KEY) if isinstance(raw, dict) else None
            if not isinstance(removed, list):  # a table of another shape is unreadable too, not empty
                raise ValueError(REMOVED_APIS_SHAPE.format(key=REMOVED_APIS_KEY))
            for index, row in enumerate(removed):  # and so is a row of another shape, which would otherwise drop out of the lookup
                if not isinstance(row, dict) or not all(isinstance(row.get(field), str) and row[field].strip() for field in REMOVED_APIS_ROW_FIELDS):
                    raise ValueError(REMOVED_APIS_ROW_SHAPE.format(index=index, key=REMOVED_APIS_KEY, fields=", ".join(repr(f) for f in REMOVED_APIS_ROW_FIELDS)))
        except (OSError, ValueError) as e:
            raise RuntimeError(REMOVED_APIS_UNREADABLE.format(name=REMOVED_APIS_NAME, module=__name__, error=e)) from e
        _REMOVED_APIS_CACHE = list(removed)
    return _REMOVED_APIS_CACHE


def _kind_matches_resource(kind: str, resource: str) -> bool:
    lowered = str(kind or "").lower()
    candidates = {lowered + suffix for suffix in PLURAL_SUFFIXES}
    if lowered.endswith(PLURAL_Y_TO_IES[0]):
        candidates.add(lowered[: -len(PLURAL_Y_TO_IES[0])] + PLURAL_Y_TO_IES[1])
    return str(resource or "").lower() in candidates


def replacement_for(api: str) -> str | None:
    """The removal table's replacement for `group/version resource`, or None when it has none."""
    if not isinstance(api, str) or API_SEPARATOR not in api:
        return None
    group_version, resource = api.split(API_SEPARATOR, 1)
    group, _, version = group_version.partition(GROUP_VERSION_SEPARATOR)
    spellings = {group_version}
    if group == CORE_GROUP:
        spellings.add(version)
    elif group.endswith(K8S_IO_GROUP_SUFFIX):
        spellings.add(group[: -len(K8S_IO_GROUP_SUFFIX)] + GROUP_VERSION_SEPARATOR + version)
    elif group:
        spellings.add(group + K8S_IO_GROUP_SUFFIX + GROUP_VERSION_SEPARATOR + version)
    for entry in _removed_apis():
        if entry.get(REMOVED_API_VERSION_KEY) in spellings and _kind_matches_resource(entry.get(REMOVED_KIND_KEY, ""), resource):
            return entry.get(REMOVED_REPLACEMENT_KEY)
    return None


def parse_minor(text) -> tuple[int, int] | None:
    """`1.32`, `v1.29.0`, `1.35.1-gke.1000` -> (major, minor); None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = MINOR_RE.match(text.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def format_minor(minor: tuple) -> str:
    """`(1, 35)` -> `1.35`; a longer version tuple is read for its first two parts."""
    return MINOR_FORMAT.format(major=minor[0], minor=minor[1])
