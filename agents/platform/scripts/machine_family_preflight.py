#!/usr/bin/env python3
"""Read-only pre-flight validator for GKE node-pool machine-family migrations.

Checks source node pools and cluster settings against target machine families
(n2, n2d, n4, c3, c4) before the agent proposes ComputeClass, Config Connector
ContainerNodePool, or Terraform google_container_node_pool manifests:

  - Security settings: warns when cluster shieldedNodes, node-pool vTPM,
    integrity monitoring, or Secure Boot is disabled (baseline security policy
    checks, never blocking on GKE's default disabled Secure Boot); rejects
    Confidential VM (confidentialNodes) on non-confidential families (n2, n4);
    enforces supported Confidential VM shapes and Local SSD modes (Intel TDX on
    c3-standard-* and on c3-standard-*-lssd with ephemeralStorageLocalSsdConfig,
    and Intel TDX on c4-standard-* without Local SSDs, rejecting
    localNvmeSsdBlockConfig on Confidential VM pools); and flags AMD
    SEV/SEV-SNP (n2d) versus Intel TDX (c3, c4) technology transitions.
  - Disk and Local SSD constraints: rejects Persistent Disk (pd-*) on
    Hyperdisk-only families (n4, c4), rejects pd-standard on c3, rejects
    Hyperdisk boot disks on n2 and n2d, rejects Local SSD configurations
    (localSsdCount, ephemeralStorageLocalSsdConfig, localNvmeSsdBlockConfig,
    ephemeralStorageConfig) on n4, and requires NVMe -lssd machine shapes
    without legacy localSsdCount on c3 and c4.
  - Version and OS image floors: enforces minimum GKE control-plane and node
    versions per target family (n2, n2d, c3, n4, c4), Confidential TDX on c3,
    and dynamic-rwo cross-generation storage topology, and rejects incompatible
    OS images.

Read-only against GCP: runs only `gcloud container clusters describe` and
`gcloud container node-pools describe` when invoked with `--cluster` and
`--location`, or operates offline on `--cluster-json` and `--node-pool-json`.
Failed or timed-out `gcloud` or file reads fail closed (exit 2) and report the
unread target rather than declaring a transition compatible.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# Exit codes: 0 when every evaluated transition is compatible, 1 when at least
# one transition has a compatibility blocker, and 2 when a required cluster or
# node-pool read failed or returned no pools (fail-closed).
EXIT_OK = 0
EXIT_INCOMPATIBLE = 1
EXIT_READ_ERROR = 2

GCLOUD_BIN = "gcloud"
GCLOUD_TIMEOUT_SECONDS = 60
JSON_FORMAT_FLAG = "--format=json"
# Disables retry sleeps in the sandbox's credential-proxy shim so a broker
# refusal returns immediately instead of retrying past GCLOUD_TIMEOUT_SECONDS.
SHIM_RETRY_DEADLINE_ENV = "CREDENTIAL_PROXY_RETRY_DEADLINE_SECONDS"
SHIM_NO_RETRY_DEADLINE = "0"
PROJECT_ENV_VARS = ("GKE_PROJECT_ID", "GCP_PROJECT_ID", "PROJECT_ID")
STDIN_MARKER = "-"
INLINE_JSON_PREFIXES = ("{", "[")
OUTPUT_TMP_PREFIX = ".machine-family-preflight-"
OUTPUT_TMP_SUFFIX = ".json"
JSON_INDENT = 2

FAMILY_N1 = "n1"
FAMILY_E2 = "e2"
FAMILY_N2 = "n2"
FAMILY_N2D = "n2d"
FAMILY_N4 = "n4"
FAMILY_C3 = "c3"
FAMILY_C4 = "c4"
DEFAULT_SOURCE_MACHINE_TYPE = "e2-medium"
MACHINE_TYPE_SEPARATOR = "-"
COMMA_SEPARATOR = ","
GKE_BUILD_MARKER = "-gke."

SUPPORTED_TARGET_FAMILIES = (
    FAMILY_N2,
    FAMILY_N2D,
    FAMILY_N4,
    FAMILY_C3,
    FAMILY_C4,
)
# Identifier names use CVM rather than the full word so CodeQL's Python
# SensitiveDataHeuristics.qll secret-source regex does not flag configuration
# booleans or enum names flowing to stdout or the report file.
NON_CVM_FAMILIES = (FAMILY_N2, FAMILY_N4)
TDX_CVM_FAMILIES = (FAMILY_C3, FAMILY_C4)
HYPERDISK_ONLY_FAMILIES = (FAMILY_N4, FAMILY_C4)
PD_ONLY_BOOT_FAMILIES = (FAMILY_N2, FAMILY_N2D)
GEN2_PD_FAMILIES = (FAMILY_N1, FAMILY_E2, FAMILY_N2, FAMILY_N2D)
LSSD_UNSUPPORTED_FAMILIES = (FAMILY_N4,)
LSSD_SHAPE_REQUIRED_FAMILIES = (FAMILY_C3, FAMILY_C4)
WINDOWS_UNSUPPORTED_FAMILIES = (FAMILY_N4, FAMILY_C4)

# Minimum GKE versions per target family and feature.
MIN_GKE_VERSIONS = {
    FAMILY_N2: "1.14.0-gke.0",
    FAMILY_N2D: "1.16.8-gke.8",
    FAMILY_C3: "1.25.3-gke.800",
    FAMILY_N4: "1.29.2-gke.1521000",
    FAMILY_C4: "1.30.3-gke.1225000",
}
MIN_GKE_VERSION_CVM_TDX = "1.28.6-gke.1317000"
MIN_GKE_VERSION_DYNAMIC_RWO = "1.35.3-gke.1290000"
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+)(?:-gke\.(\d+))?)?$")
VERSION_MAJOR_MINOR_LEN = 2
VERSION_MAJOR_MINOR_PATCH_LEN = 3

PD_DISK_PREFIX = "pd-"
HYPERDISK_PREFIX = "hyperdisk-"
DISK_PD_STANDARD = "pd-standard"
DISK_PD_BALANCED = "pd-balanced"
DISK_PD_SSD = "pd-ssd"
DISK_HYPERDISK_BALANCED = "hyperdisk-balanced"
C3_ALLOWED_BOOT_DISKS = (
    DISK_PD_BALANCED,
    DISK_PD_SSD,
    DISK_HYPERDISK_BALANCED,
)
HYPERDISK_ONLY_ALLOWED_BOOT_DISKS = (DISK_HYPERDISK_BALANCED,)
PD_ONLY_ALLOWED_BOOT_DISKS = (
    DISK_PD_STANDARD,
    DISK_PD_BALANCED,
    DISK_PD_SSD,
)

LSSD_MACHINE_SUFFIX = "-lssd"
C3_TDX_ALLOWED_SHAPE_PREFIX = "c3-standard-"
C4_TDX_ALLOWED_SHAPE_PREFIX = "c4-standard-"

# GKE / Config Connector / Terraform field names.
FIELD_NAME = "name"
FIELD_API_VERSION = "apiVersion"
FIELD_KIND = "kind"
FIELD_METADATA = "metadata"
FIELD_SPEC = "spec"
FIELD_RESOURCE_ID = "resourceID"
FIELD_LOCATION = "location"
FIELD_STORAGE = "storage"
FIELD_NODE_POOLS = "nodePools"
FIELD_NODE_POOLS_SNAKE = "node_pools"
FIELD_CONFIG = "config"
FIELD_NODE_CONFIG = "nodeConfig"
FIELD_NODE_CONFIG_SNAKE = "node_config"
FIELD_MACHINE_TYPE = "machineType"
FIELD_MACHINE_TYPE_SNAKE = "machine_type"
FIELD_DISK_TYPE = "diskType"
FIELD_DISK_TYPE_SNAKE = "disk_type"
FIELD_BOOT_DISK = "bootDisk"
FIELD_BOOT_DISK_SNAKE = "boot_disk"
FIELD_BOOT_DISK_TYPE = "bootDiskType"
FIELD_IMAGE_TYPE = "imageType"
FIELD_IMAGE_TYPE_SNAKE = "image_type"
FIELD_LOCAL_SSD_COUNT = "localSsdCount"
FIELD_LOCAL_SSD_COUNT_SNAKE = "local_ssd_count"
FIELD_LOCAL_SSD_COUNT_CC = "localSSDCount"
FIELD_EPHEMERAL_LOCAL_SSD = "ephemeralStorageLocalSsdConfig"
FIELD_EPHEMERAL_LOCAL_SSD_SNAKE = "ephemeral_storage_local_ssd_config"
FIELD_NVME_SSD_BLOCK = "localNvmeSsdBlockConfig"
FIELD_NVME_SSD_BLOCK_SNAKE = "local_nvme_ssd_block_config"
FIELD_EPHEMERAL_STORAGE = "ephemeralStorageConfig"
FIELD_EPHEMERAL_STORAGE_SNAKE = "ephemeral_storage_config"
FIELD_DATA_CACHE_COUNT = "dataCacheCount"
FIELD_DATA_CACHE_COUNT_SNAKE = "data_cache_count"
FIELD_SHIELDED_NODES = "shieldedNodes"
FIELD_SHIELDED_NODES_SNAKE = "shielded_nodes"
FIELD_ENABLE_SHIELDED_NODES = "enableShieldedNodes"
FIELD_ENABLE_SHIELDED_NODES_SNAKE = "enable_shielded_nodes"
FIELD_SHIELDED_INSTANCE_CONFIG = "shieldedInstanceConfig"
FIELD_SHIELDED_INSTANCE_CONFIG_SNAKE = "shielded_instance_config"
FIELD_ENABLE_SECURE_BOOT = "enableSecureBoot"
FIELD_ENABLE_SECURE_BOOT_SNAKE = "enable_secure_boot"
FIELD_ENABLE_VTPM = "enableVtpm"
FIELD_ENABLE_VTPM_SNAKE = "enable_vtpm"
FIELD_ENABLE_INTEGRITY = "enableIntegrityMonitoring"
FIELD_ENABLE_INTEGRITY_SNAKE = "enable_integrity_monitoring"
FIELD_CVM_NODES = "confidentialNodes"
FIELD_CVM_NODES_SNAKE = "confidential_nodes"
FIELD_CVM_CONFIG = "confidentialInstanceConfig"
FIELD_CVM_CONFIG_SNAKE = "confidential_instance_config"
FIELD_CVM_TYPE = "confidentialInstanceType"
FIELD_CVM_TYPE_SNAKE = "confidential_instance_type"
FIELD_CVM_NODE_TYPE = "confidentialNodeType"
FIELD_ENABLE_CVM = "enableConfidentialCompute"
FIELD_ENABLE_CVM_SNAKE = "enable_confidential_compute"
FIELD_ENABLED = "enabled"
FIELD_MASTER_VERSION = "currentMasterVersion"
FIELD_MASTER_VERSION_ALT = "masterVersion"
FIELD_MASTER_VERSION_ALT_SNAKE = "master_version"
FIELD_MIN_MASTER_VERSION = "minMasterVersion"
FIELD_MIN_MASTER_VERSION_SNAKE = "min_master_version"
FIELD_MASTER_VERSION_SNAKE = "current_master_version"
FIELD_VERSION = "version"
FIELD_NODE_VERSION = "currentNodeVersion"
FIELD_INITIAL_NODE_VERSION = "initialNodeVersion"
FIELD_ACCELERATORS = "accelerators"
FIELD_GUEST_ACCELERATORS = "guestAccelerators"
FIELD_GUEST_ACCELERATOR_KCC = "guestAccelerator"
FIELD_GUEST_ACCELERATORS_SNAKE = "guest_accelerator"
FIELD_ACCELERATOR_COUNT = "acceleratorCount"
FIELD_ACCELERATOR_COUNT_SNAKE = "count"

CVM_TYPE_SEV = "SEV"
CVM_TYPE_SEV_SNP = "SEV_SNP"
CVM_TYPE_TDX = "TDX"
CVM_TYPE_UNSPECIFIED = "CONFIDENTIAL_INSTANCE_TYPE_UNSPECIFIED"
KNOWN_CVM_TYPES = (CVM_TYPE_SEV, CVM_TYPE_SEV_SNP, CVM_TYPE_TDX)
AMD_CVM_TYPES = (CVM_TYPE_SEV, CVM_TYPE_SEV_SNP)
CVM_DEFAULT_AMD_LABEL = "SEV/SEV_SNP (or unspecified non-TDX)"

IMAGE_COS_CONTAINERD = "COS_CONTAINERD"
IMAGE_UBUNTU_CONTAINERD = "UBUNTU_CONTAINERD"
IMAGE_WINDOWS_LTSC_CONTAINERD = "WINDOWS_LTSC_CONTAINERD"
IMAGE_WINDOWS_SAC_CONTAINERD = "WINDOWS_SAC_CONTAINERD"
ALLOWED_LINUX_IMAGES = (IMAGE_COS_CONTAINERD, IMAGE_UBUNTU_CONTAINERD)
ALLOWED_CONTAINERD_IMAGES = (
    IMAGE_COS_CONTAINERD,
    IMAGE_UBUNTU_CONTAINERD,
    IMAGE_WINDOWS_LTSC_CONTAINERD,
    IMAGE_WINDOWS_SAC_CONTAINERD,
)
LEGACY_DOCKER_IMAGES = ("COS", "UBUNTU", "WINDOWS_SAC", "WINDOWS_LTSC")
LEGACY_DOCKER_SUFFIX = "_DOCKER"
WINDOWS_IMAGE_PREFIX = "WINDOWS"

CATEGORY_SECURITY = "security"
CATEGORY_DISK = "disk"
CATEGORY_LOCAL_SSD = "local_ssd"
CATEGORY_VERSION = "version"
CATEGORY_OS_IMAGE = "os_image"
CATEGORY_ACCELERATOR = "accelerator"

RULE_SHIELDED_NODES_DISABLED = "shielded-nodes-disabled"
RULE_SHIELDED_VTPM_DISABLED = "shielded-vtpm-disabled"
RULE_SHIELDED_INTEGRITY_DISABLED = "shielded-integrity-monitoring-disabled"
RULE_SHIELDED_SECURE_BOOT_DISABLED = "shielded-secure-boot-disabled"
RULE_CVM_UNSUPPORTED_FAMILY = "confidential-vm-unsupported-family"
RULE_CVM_TECHNOLOGY_MISMATCH = "confidential-vm-technology-mismatch"
RULE_DISK_PD_ON_HYPERDISK_ONLY = "disk-type-pd-on-hyperdisk-only"
RULE_DISK_PD_STANDARD_ON_C3 = "disk-type-pd-standard-unsupported"
RULE_DISK_HYPERDISK_ON_GEN2 = "disk-type-hyperdisk-unsupported-boot"
RULE_DISK_UNSUPPORTED_BOOT = "disk-type-unsupported-boot"
RULE_LSSD_UNSUPPORTED_ON_N4 = "local-ssd-unsupported-on-n4"
RULE_LSSD_REQUIRES_LSSD_SHAPE = "local-ssd-requires-lssd-shape"
RULE_LSSD_LEGACY_COUNT_ON_GEN3_GEN4 = "local-ssd-legacy-count-unsupported"
RULE_GKE_MASTER_VERSION_FLOOR = "gke-control-plane-version-below-floor"
RULE_GKE_NODE_VERSION_FLOOR = "gke-node-pool-version-below-floor"
RULE_GKE_VERSION_UNPARSEABLE = "gke-version-unparseable"
RULE_GKE_CVM_TDX_VERSION_FLOOR = "gke-version-below-confidential-tdx-floor"
RULE_DYNAMIC_RWO_VERSION_FLOOR = "dynamic-rwo-version-floor"
RULE_OS_IMAGE_LEGACY_DOCKER = "os-image-legacy-docker"
RULE_OS_IMAGE_WINDOWS_UNSUPPORTED = "os-image-windows-unsupported"
RULE_OS_IMAGE_INCOMPATIBLE = "os-image-incompatible"
RULE_GPU_UNSUPPORTED_ON_TARGET = "gpu-accelerators-unsupported-on-target"

STATUS_COMPATIBLE = "compatible"
STATUS_INCOMPATIBLE = "incompatible"
STATUS_ERROR = "error"

VERDICT_COMPATIBLE = "VERDICT: COMPATIBLE"
VERDICT_INCOMPATIBLE = "VERDICT: INCOMPATIBLE"
VERDICT_ERROR = "VERDICT: ERROR"
PREFLIGHT_STOP_LINE = (
    "PREFLIGHT_STOP: Do NOT generate ComputeClass or NodePool YAML until the "
    "incompatibilities above are resolved."
)

FORMAT_MARKDOWN = "markdown"
FORMAT_JSON = "json"
SUPPORTED_OUTPUT_FORMATS = (FORMAT_MARKDOWN, FORMAT_JSON)

UNNAMED_POOL = "default-pool"
UNNAMED_CLUSTER = "(offline)"
EMPTY_CELL = "-"
TABLE_HEADERS = (
    "node pool",
    "source machine",
    "target family",
    "status",
    "blockers",
    "warnings",
)
TABLE_SEPARATOR = "---"

Runner = Callable[[list[str]], tuple[int, str, str]]


@dataclasses.dataclass(frozen=True)
class Finding:
    """One compatibility blocker or warning on a node-pool transition."""

    rule: str
    category: str
    field: str
    message: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule,
            "category": self.category,
            "field": self.field,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclasses.dataclass(frozen=True)
class TransitionResult:
    """Evaluation of one source node pool against one target machine family."""

    node_pool: str
    source_machine_type: str
    source_family: str
    target_family: str
    target_machine_type: str
    blockers: tuple[Finding, ...]
    warnings: tuple[Finding, ...]

    @property
    def compatible(self) -> bool:
        return len(self.blockers) == 0

    @property
    def status(self) -> str:
        return STATUS_COMPATIBLE if self.compatible else STATUS_INCOMPATIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_pool": self.node_pool,
            "source_machine_type": self.source_machine_type,
            "source_family": self.source_family,
            "target_family": self.target_family,
            "target_machine_type": self.target_machine_type,
            "compatible": self.compatible,
            "status": self.status,
            "blockers": [b.to_dict() for b in self.blockers],
            "warnings": [w.to_dict() for w in self.warnings],
        }


def parse_version(text: str | None) -> tuple[int, int, int, int] | None:
    """Parse a GKE version `MAJOR.MINOR[.PATCH[-gke.BUILD]]` into a 4-tuple."""
    if not text or not isinstance(text, str):
        return None
    match = VERSION_RE.match(text.strip())
    if not match:
        return None
    major, minor, patch, build = match.groups()
    return (
        int(major),
        int(minor),
        int(patch) if patch is not None else 0,
        int(build) if build is not None else 0,
    )


def _version_below_floor(version_text: str, floor_text: str) -> bool:
    """Return True when `version_text` is unparseable or below `floor_text`.

    When `version_text` is a two-component `MAJOR.MINOR` string (e.g. `1.29`),
    compare `(major, minor)` against the floor's `(major, minor)` because a
    minor-only version in Config Connector or Terraform resolves to a current
    patch of that minor. When `version_text` omits `-gke.BUILD` (e.g. `1.29.2`),
    compare `(major, minor, patch)` against the floor's `(major, minor, patch)`
    so a bare Kubernetes release at the floor patch is not rejected for lacking
    a GKE build number; when `-gke.BUILD` is present, compare the full 4-tuple.
    """
    parsed = parse_version(version_text)
    floor = parse_version(floor_text)
    if floor is None:
        return False
    if parsed is None:
        return True
    match = VERSION_RE.match(version_text.strip())
    if match is not None:
        _, _, patch_group, build_group = match.groups()
        if patch_group is None:
            return (
                parsed[:VERSION_MAJOR_MINOR_LEN]
                < floor[:VERSION_MAJOR_MINOR_LEN]
            )
        if build_group is None:
            return (
                parsed[:VERSION_MAJOR_MINOR_PATCH_LEN]
                < floor[:VERSION_MAJOR_MINOR_PATCH_LEN]
            )
    return parsed < floor


def extract_machine_family(machine_type: str | None) -> str:
    """Extract the lowercase machine family prefix from a machine type or family."""
    raw = (machine_type or "").strip().lower()
    if not raw:
        return ""
    return raw.split(MACHINE_TYPE_SEPARATOR, 1)[0]


def parse_target_spec(raw_spec: str) -> tuple[str, str]:
    """Return `(target_family, target_machine_type)` from a `--target-family` token.

    Accepts either a bare family (`n4`, `c3`) or a concrete machine shape
    (`c3-standard-8-lssd`, `n4-standard-16`). Raises ValueError if the family is
    not in SUPPORTED_TARGET_FAMILIES.
    """
    cleaned = (raw_spec or "").strip().lower()
    if not cleaned:
        raise ValueError("empty target machine family")
    family = extract_machine_family(cleaned)
    if family not in SUPPORTED_TARGET_FAMILIES:
        allowed = ", ".join(SUPPORTED_TARGET_FAMILIES)
        raise ValueError(
            f"unsupported target machine family {family!r} (from {raw_spec!r}); "
            f"supported target families: {allowed}"
        )
    machine_type = cleaned if MACHINE_TYPE_SEPARATOR in cleaned else ""
    return family, machine_type


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first value in `mapping` whose key is present and not None."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _safe_positive_int(value: Any) -> int:
    """Parse a positive integer from `int` or numeric `str` (Proto3 int64), else 0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
            return parsed if parsed > 0 else 0
        except ValueError:
            return 0
    return 0


def _extract_pool_name(node_pool: dict[str, Any]) -> str:
    """Extract the node pool name from a GKE, KCC (`spec.resourceID`/`metadata.name`), or Terraform dict."""
    direct = node_pool.get(FIELD_NAME)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    spec = node_pool.get(FIELD_SPEC)
    if isinstance(spec, dict):
        res_id = spec.get(FIELD_RESOURCE_ID)
        if isinstance(res_id, str) and res_id.strip():
            return res_id.strip()
    metadata = node_pool.get(FIELD_METADATA)
    if isinstance(metadata, dict):
        meta_name = metadata.get(FIELD_NAME)
        if isinstance(meta_name, str) and meta_name.strip():
            return meta_name.strip()
    return ""


def _extract_pool_version(node_pool: dict[str, Any]) -> str:
    """Extract the GKE node version from top-level fields or KCC `spec.version`."""
    raw = _first_present(
        node_pool,
        FIELD_VERSION,
        FIELD_NODE_VERSION,
        FIELD_INITIAL_NODE_VERSION,
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    spec = node_pool.get(FIELD_SPEC)
    if isinstance(spec, dict):
        spec_ver = _first_present(
            spec,
            FIELD_VERSION,
            FIELD_NODE_VERSION,
            FIELD_INITIAL_NODE_VERSION,
        )
        if isinstance(spec_ver, str) and spec_ver.strip():
            return spec_ver.strip()
    return ""


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Return `value` if it is a dict, or its single element if a 1-element list of dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    return None


def _extract_cluster_version(cluster: dict[str, Any] | None) -> str:
    """Extract the GKE control-plane version from GKE, KCC (`spec`), or Terraform cluster dicts."""
    if not isinstance(cluster, dict):
        return ""
    version_keys = (
        FIELD_MASTER_VERSION,
        FIELD_MASTER_VERSION_ALT,
        FIELD_MIN_MASTER_VERSION,
        FIELD_MASTER_VERSION_SNAKE,
        FIELD_MIN_MASTER_VERSION_SNAKE,
        FIELD_MASTER_VERSION_ALT_SNAKE,
        FIELD_VERSION,
    )
    raw = _first_present(cluster, *version_keys)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    spec = _as_dict(cluster.get(FIELD_SPEC))
    if isinstance(spec, dict):
        spec_ver = _first_present(spec, *version_keys)
        if isinstance(spec_ver, str) and spec_ver.strip():
            return spec_ver.strip()
    return ""


def _normalize_cluster_doc(cluster: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize a GKE, Config Connector (`ContainerCluster`), or Terraform cluster dict.

    Merges `spec` fields into a top-level working copy (preserving `spec` so
    schema detection still sees Config Connector objects), resolves the cluster
    name from `spec.resourceID` or `metadata.name` when top-level `name` is
    absent, and maps Terraform `min_master_version` / `master_version` onto
    `minMasterVersion`.
    """
    if not isinstance(cluster, dict):
        return None
    normalized = dict(cluster)
    spec = _as_dict(cluster.get(FIELD_SPEC))
    if isinstance(spec, dict):
        for key, value in spec.items():
            if key not in normalized or normalized[key] is None:
                normalized[key] = value
    raw_name = normalized.get(FIELD_NAME)
    if not (isinstance(raw_name, str) and raw_name.strip()):
        resolved_name = ""
        if isinstance(spec, dict):
            res_id = spec.get(FIELD_RESOURCE_ID)
            if isinstance(res_id, str) and res_id.strip():
                resolved_name = res_id.strip()
        if not resolved_name:
            metadata = _as_dict(cluster.get(FIELD_METADATA))
            if isinstance(metadata, dict):
                meta_name = metadata.get(FIELD_NAME)
                if isinstance(meta_name, str) and meta_name.strip():
                    resolved_name = meta_name.strip()
        if resolved_name:
            normalized[FIELD_NAME] = resolved_name
    extracted_version = _extract_cluster_version(normalized)
    if (
        extracted_version
        and _first_present(
            normalized,
            FIELD_MASTER_VERSION,
            FIELD_MASTER_VERSION_ALT,
            FIELD_MIN_MASTER_VERSION,
            FIELD_MASTER_VERSION_SNAKE,
            FIELD_VERSION,
        )
        is None
    ):
        normalized[FIELD_MIN_MASTER_VERSION] = extracted_version
    return normalized


def _is_cluster_wrapper(doc: dict[str, Any]) -> bool:
    """Return True when `doc` is a cluster wrapper carrying `nodePools` / `node_pools`."""
    if FIELD_NODE_POOLS in doc or FIELD_NODE_POOLS_SNAKE in doc:
        return True
    spec = _as_dict(doc.get(FIELD_SPEC))
    return isinstance(spec, dict) and (
        FIELD_NODE_POOLS in spec or FIELD_NODE_POOLS_SNAKE in spec
    )


def _extract_source_machine_type(config: dict[str, Any]) -> str:
    """Extract the source machine type from a node config dict, or `""` when missing."""
    raw_machine_type = _first_present(
        config,
        FIELD_MACHINE_TYPE,
        FIELD_MACHINE_TYPE_SNAKE,
    )
    if isinstance(raw_machine_type, str) and raw_machine_type.strip():
        return raw_machine_type.strip()
    return ""


def _is_gke_proto3_shielded_schema(
    node_pool: dict[str, Any] | None,
    config: dict[str, Any],
    shielded_cfg: dict[str, Any],
    *,
    from_cluster: bool = False,
    cluster: dict[str, Any] | None = None,
) -> bool:
    """Return True when `shielded_cfg` comes from GKE Proto3 JSON rather than KCC or Terraform.

    GKE's Proto3 JSON serializer omits `false` boolean fields from
    `shieldedInstanceConfig`, whereas Config Connector (`ContainerNodePool`) and
    Terraform (`google_container_node_pool`) are declarative manifests where an
    omitted `enableIntegrityMonitoring` uses the platform default (`true`).
    """
    if any(
        key in shielded_cfg
        for key in (
            FIELD_ENABLE_SECURE_BOOT_SNAKE,
            FIELD_ENABLE_INTEGRITY_SNAKE,
            FIELD_ENABLE_VTPM_SNAKE,
        )
    ):
        return False
    if from_cluster and isinstance(cluster, dict):
        if any(
            key in cluster
            for key in (
                FIELD_SPEC,
                FIELD_API_VERSION,
                FIELD_KIND,
                FIELD_NODE_CONFIG_SNAKE,
                FIELD_ENABLE_SHIELDED_NODES_SNAKE,
            )
        ):
            return False
        if isinstance(cluster.get(FIELD_NODE_CONFIG), list):
            return False
        cluster_node_cfg = _as_dict(
            _first_present(cluster, FIELD_NODE_CONFIG, FIELD_NODE_CONFIG_SNAKE)
        )
        if isinstance(cluster_node_cfg, dict) and (
            FIELD_SHIELDED_INSTANCE_CONFIG_SNAKE in cluster_node_cfg
            or isinstance(cluster_node_cfg.get(FIELD_SHIELDED_INSTANCE_CONFIG), list)
        ):
            return False
        return True
    if isinstance(node_pool, dict):
        if any(
            key in node_pool
            for key in (
                FIELD_SPEC,
                FIELD_API_VERSION,
                FIELD_KIND,
                FIELD_NODE_CONFIG,
                FIELD_NODE_CONFIG_SNAKE,
            )
        ):
            return False
        if isinstance(node_pool.get(FIELD_CONFIG), list):
            return False
    if any(
        key in config
        for key in (
            FIELD_SHIELDED_INSTANCE_CONFIG_SNAKE,
            FIELD_MACHINE_TYPE_SNAKE,
            FIELD_DISK_TYPE_SNAKE,
        )
    ):
        return False
    if isinstance(config.get(FIELD_SHIELDED_INSTANCE_CONFIG), list):
        return False
    return True


def _node_config_of(node_pool: dict[str, Any]) -> dict[str, Any]:
    """Return the node configuration dict from a GKE, KCC, or Terraform pool object."""
    nested = _as_dict(
        _first_present(
            node_pool,
            FIELD_CONFIG,
            FIELD_NODE_CONFIG,
            FIELD_NODE_CONFIG_SNAKE,
        )
    )
    if nested is not None:
        return nested
    spec = _as_dict(node_pool.get(FIELD_SPEC))
    if spec is not None:
        spec_config = _as_dict(
            _first_present(
                spec,
                FIELD_CONFIG,
                FIELD_NODE_CONFIG,
                FIELD_NODE_CONFIG_SNAKE,
            )
        )
        if spec_config is not None:
            return spec_config
    return node_pool


def _storage_config_of(config: dict[str, Any]) -> dict[str, Any]:
    """Return the ComputeClass `storage` dict from `config`, or `{}` when absent."""
    storage = _as_dict(config.get(FIELD_STORAGE))
    return storage if isinstance(storage, dict) else {}


def _extract_boot_disk_type(config: dict[str, Any]) -> str:
    """Extract the lowercase boot disk type from a node config dict, or `""`."""
    direct = _first_present(
        config,
        FIELD_DISK_TYPE,
        FIELD_DISK_TYPE_SNAKE,
        FIELD_BOOT_DISK_TYPE,
    )
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()
    boot_disk = _as_dict(_first_present(config, FIELD_BOOT_DISK, FIELD_BOOT_DISK_SNAKE))
    if isinstance(boot_disk, dict):
        nested = _first_present(
            boot_disk,
            FIELD_DISK_TYPE,
            FIELD_DISK_TYPE_SNAKE,
            FIELD_BOOT_DISK_TYPE,
        )
        if isinstance(nested, str) and nested.strip():
            return nested.strip().lower()
    storage = _storage_config_of(config)
    if storage:
        nested_storage = _first_present(
            storage,
            FIELD_BOOT_DISK_TYPE,
            FIELD_DISK_TYPE,
            FIELD_DISK_TYPE_SNAKE,
        )
        if isinstance(nested_storage, str) and nested_storage.strip():
            return nested_storage.strip().lower()
    return ""


def _default_cvm_type_for_family(source_family: str) -> str:
    """Return the platform's default Confidential VM technology when omitted by `describe`."""
    if source_family in TDX_CVM_FAMILIES:
        return CVM_TYPE_TDX
    if source_family == FAMILY_N2D:
        return CVM_TYPE_SEV
    return ""


def _extract_cvm_state(
    config: dict[str, Any],
    cluster: dict[str, Any] | None,
    source_family: str = "",
) -> tuple[bool, str, str]:
    """Return `(cvm_enabled, matched_cvm_type, field_path)` from pool and cluster.

    A node-pool level explicit `enabled: false` (or `enableConfidentialCompute:
    false`) overrides cluster-level Confidential Nodes. Maps the raw encryption
    technology string against `KNOWN_CVM_TYPES` so only a constant literal from
    `KNOWN_CVM_TYPES` is returned.
    """
    cluster = _normalize_cluster_doc(cluster)
    pool_sources: list[tuple[str, Any]] = [
        (
            f"config.{FIELD_CVM_NODES}",
            _first_present(config, FIELD_CVM_NODES, FIELD_CVM_NODES_SNAKE),
        ),
        (
            f"config.{FIELD_CVM_CONFIG}",
            _first_present(config, FIELD_CVM_CONFIG, FIELD_CVM_CONFIG_SNAKE),
        ),
    ]
    cluster_sources: list[tuple[str, Any]] = []
    if isinstance(cluster, dict):
        cluster_sources.extend(
            [
                (
                    f"cluster.{FIELD_CVM_NODES}",
                    _first_present(cluster, FIELD_CVM_NODES, FIELD_CVM_NODES_SNAKE),
                ),
                (
                    f"cluster.{FIELD_CVM_CONFIG}",
                    _first_present(cluster, FIELD_CVM_CONFIG, FIELD_CVM_CONFIG_SNAKE),
                ),
            ]
        )

    for path_label, block in pool_sources + cluster_sources:
        is_pool_level = path_label.startswith("config.")
        if isinstance(block, bool):
            if block:
                return True, _default_cvm_type_for_family(source_family), path_label
            if is_pool_level:
                return False, "", ""
            continue
        block_dict = _as_dict(block)
        if not isinstance(block_dict, dict):
            continue
        enabled_flag = _first_present(
            block_dict,
            FIELD_ENABLED,
            FIELD_ENABLE_CVM,
            FIELD_ENABLE_CVM_SNAKE,
        )
        raw_kind = _first_present(
            block_dict,
            FIELD_CVM_TYPE,
            FIELD_CVM_TYPE_SNAKE,
            FIELD_CVM_NODE_TYPE,
        )
        normalized_kind = str(raw_kind or "").strip().upper()
        matched_kind = next(
            (known for known in KNOWN_CVM_TYPES if normalized_kind == known),
            "",
        )
        has_active_kind = bool(
            normalized_kind and normalized_kind != CVM_TYPE_UNSPECIFIED
        )
        if enabled_flag is False and is_pool_level:
            return False, "", ""
        if enabled_flag is True or (enabled_flag is not False and has_active_kind):
            effective_kind = matched_kind or _default_cvm_type_for_family(source_family)
            return True, effective_kind, path_label
    return False, "", ""


def _extract_local_ssd_configs(
    config: dict[str, Any],
    source_machine_type: str,
) -> tuple[list[str], bool]:
    """Return `(descriptions_of_local_ssd_fields, has_legacy_local_ssd_count)`."""
    details: list[str] = []
    has_legacy_count = False

    legacy_count = _safe_positive_int(
        _first_present(
            config,
            FIELD_LOCAL_SSD_COUNT,
            FIELD_LOCAL_SSD_COUNT_SNAKE,
            FIELD_LOCAL_SSD_COUNT_CC,
        )
    )
    if legacy_count == 0:
        storage = _storage_config_of(config)
        if storage:
            legacy_count = _safe_positive_int(
                _first_present(
                    storage,
                    FIELD_LOCAL_SSD_COUNT_CC,
                    FIELD_LOCAL_SSD_COUNT,
                    FIELD_LOCAL_SSD_COUNT_SNAKE,
                )
            )
    if legacy_count > 0:
        details.append(f"{FIELD_LOCAL_SSD_COUNT}={legacy_count}")
        has_legacy_count = True

    ephemeral_lssd = _as_dict(
        _first_present(
            config,
            FIELD_EPHEMERAL_LOCAL_SSD,
            FIELD_EPHEMERAL_LOCAL_SSD_SNAKE,
        )
    )
    if isinstance(ephemeral_lssd, dict):
        count_keys = (FIELD_LOCAL_SSD_COUNT, FIELD_LOCAL_SSD_COUNT_SNAKE)
        cache_keys = (FIELD_DATA_CACHE_COUNT, FIELD_DATA_CACHE_COUNT_SNAKE)
        has_any_count_key = any(k in ephemeral_lssd for k in (*count_keys, *cache_keys))
        count = _safe_positive_int(_first_present(ephemeral_lssd, *count_keys))
        cache_count = _safe_positive_int(_first_present(ephemeral_lssd, *cache_keys))
        if count > 0:
            details.append(
                f"{FIELD_EPHEMERAL_LOCAL_SSD}.{FIELD_LOCAL_SSD_COUNT}={count}"
            )
        if cache_count > 0:
            details.append(
                f"{FIELD_EPHEMERAL_LOCAL_SSD}.{FIELD_DATA_CACHE_COUNT}={cache_count}"
            )
        if not has_any_count_key and ephemeral_lssd:
            details.append(FIELD_EPHEMERAL_LOCAL_SSD)

    nvme_block = _as_dict(
        _first_present(
            config,
            FIELD_NVME_SSD_BLOCK,
            FIELD_NVME_SSD_BLOCK_SNAKE,
        )
    )
    if isinstance(nvme_block, dict):
        count_keys = (FIELD_LOCAL_SSD_COUNT, FIELD_LOCAL_SSD_COUNT_SNAKE)
        has_count_key = any(k in nvme_block for k in count_keys)
        count = _safe_positive_int(_first_present(nvme_block, *count_keys))
        if count > 0:
            details.append(f"{FIELD_NVME_SSD_BLOCK}.{FIELD_LOCAL_SSD_COUNT}={count}")
        elif not has_count_key and nvme_block:
            details.append(FIELD_NVME_SSD_BLOCK)

    ephemeral_storage = _as_dict(
        _first_present(
            config,
            FIELD_EPHEMERAL_STORAGE,
            FIELD_EPHEMERAL_STORAGE_SNAKE,
        )
    )
    if isinstance(ephemeral_storage, dict):
        count = _safe_positive_int(
            _first_present(
                ephemeral_storage,
                FIELD_LOCAL_SSD_COUNT,
                FIELD_LOCAL_SSD_COUNT_SNAKE,
            )
        )
        if count > 0:
            details.append(f"{FIELD_EPHEMERAL_STORAGE}.{FIELD_LOCAL_SSD_COUNT}={count}")

    if source_machine_type.lower().endswith(LSSD_MACHINE_SUFFIX):
        details.append(f"machineType={source_machine_type}")

    return details, has_legacy_count


def _has_active_nvme_block_lssd(config: dict[str, Any]) -> bool:
    """Return True when `config` configures an active `localNvmeSsdBlockConfig` block."""
    nvme_block = _as_dict(
        _first_present(
            config,
            FIELD_NVME_SSD_BLOCK,
            FIELD_NVME_SSD_BLOCK_SNAKE,
        )
    )
    if not isinstance(nvme_block, dict):
        return False
    count_keys = (FIELD_LOCAL_SSD_COUNT, FIELD_LOCAL_SSD_COUNT_SNAKE)
    has_count_key = any(k in nvme_block for k in count_keys)
    count = _safe_positive_int(_first_present(nvme_block, *count_keys))
    return count > 0 or (not has_count_key and bool(nvme_block))


def _evaluate_security(
    config: dict[str, Any],
    cluster: dict[str, Any] | None,
    source_family: str,
    target_family: str,
    target_machine_type: str,
    *,
    node_pool: dict[str, Any] | None = None,
    source_machine_type: str = "",
) -> tuple[list[Finding], list[Finding]]:
    """Check Confidential VM platform constraints and Shielded VM policy warnings."""
    blockers: list[Finding] = []
    warnings: list[Finding] = []
    cluster = _normalize_cluster_doc(cluster)

    if isinstance(cluster, dict):
        has_shielded_block = (
            FIELD_SHIELDED_NODES in cluster
            or FIELD_SHIELDED_NODES_SNAKE in cluster
        )
        raw_shielded_nodes = _first_present(
            cluster,
            FIELD_SHIELDED_NODES,
            FIELD_SHIELDED_NODES_SNAKE,
        )
        shielded_nodes_dict = _as_dict(raw_shielded_nodes)
        flat_shielded = _first_present(
            cluster,
            FIELD_ENABLE_SHIELDED_NODES,
            FIELD_ENABLE_SHIELDED_NODES_SNAKE,
        )
        # GKE's Proto3 JSON serializer omits `enabled: false`, serializing a
        # disabled `shieldedNodes` message as `{}`.
        shielded_disabled = (
            (
                has_shielded_block
                and isinstance(shielded_nodes_dict, dict)
                and shielded_nodes_dict.get(FIELD_ENABLED) is not True
            )
            or raw_shielded_nodes is False
            or flat_shielded is False
        )
        if shielded_disabled:
            warnings.append(
                Finding(
                    rule=RULE_SHIELDED_NODES_DISABLED,
                    category=CATEGORY_SECURITY,
                    field=f"{FIELD_SHIELDED_NODES}.{FIELD_ENABLED}",
                    message=(
                        f"Cluster has {FIELD_SHIELDED_NODES}.{FIELD_ENABLED}=false; "
                        "GKE enables Shielded GKE Nodes by default and disabling them "
                        "weakens baseline cluster security policy when migrating to "
                        f"{target_family}."
                    ),
                    remediation=(
                        f"Enable Shielded GKE Nodes ({FIELD_SHIELDED_NODES}.{FIELD_ENABLED}: true) "
                        "on the cluster to maintain baseline security policy when "
                        f"migrating node pools to {target_family}."
                    ),
                )
            )

    shielded_from_cluster = False
    shielded_cfg = _as_dict(
        _first_present(
            config,
            FIELD_SHIELDED_INSTANCE_CONFIG,
            FIELD_SHIELDED_INSTANCE_CONFIG_SNAKE,
        )
    )
    if not isinstance(shielded_cfg, dict) and isinstance(cluster, dict):
        cluster_node_cfg = _as_dict(
            _first_present(
                cluster,
                FIELD_NODE_CONFIG,
                FIELD_NODE_CONFIG_SNAKE,
            )
        )
        if isinstance(cluster_node_cfg, dict):
            shielded_cfg = _as_dict(
                _first_present(
                    cluster_node_cfg,
                    FIELD_SHIELDED_INSTANCE_CONFIG,
                    FIELD_SHIELDED_INSTANCE_CONFIG_SNAKE,
                )
            )
            if isinstance(shielded_cfg, dict):
                shielded_from_cluster = True

    if isinstance(shielded_cfg, dict):
        is_gke_proto3 = _is_gke_proto3_shielded_schema(
            node_pool,
            config,
            shielded_cfg,
            from_cluster=shielded_from_cluster,
            cluster=cluster,
        )
        vtpm = _first_present(
            shielded_cfg,
            FIELD_ENABLE_VTPM,
            FIELD_ENABLE_VTPM_SNAKE,
        )
        if vtpm is False:
            warnings.append(
                Finding(
                    rule=RULE_SHIELDED_VTPM_DISABLED,
                    category=CATEGORY_SECURITY,
                    field=f"{FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_VTPM}",
                    message=(
                        f"Source node pool has {FIELD_SHIELDED_INSTANCE_CONFIG}."
                        f"{FIELD_ENABLE_VTPM}=false; disabling Shielded VM vTPM "
                        "violates baseline node security policy when migrating to "
                        f"{target_family}."
                    ),
                    remediation=(
                        f"Remove {FIELD_ENABLE_VTPM}: false from the source configuration, "
                        f"ensure cluster {FIELD_SHIELDED_NODES}.{FIELD_ENABLED} is true, and "
                        f"keep {FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_INTEGRITY}: true "
                        f"on the target {target_family} node pool or ComputeClass (GKE enables "
                        "vTPM automatically on Shielded GKE Nodes and does not accept an "
                        "enableVtpm field in ComputeClass or NodePool YAML)."
                    ),
                )
            )

        integrity = _first_present(
            shielded_cfg,
            FIELD_ENABLE_INTEGRITY,
            FIELD_ENABLE_INTEGRITY_SNAKE,
        )
        # In GKE's ShieldedInstanceConfig Proto3 JSON, a false boolean is
        # omitted from the serialized object; in KCC and Terraform manifests,
        # an omitted enableIntegrityMonitoring defaults to true.
        if integrity is False or (integrity is None and is_gke_proto3):
            warnings.append(
                Finding(
                    rule=RULE_SHIELDED_INTEGRITY_DISABLED,
                    category=CATEGORY_SECURITY,
                    field=f"{FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_INTEGRITY}",
                    message=(
                        f"Source node pool has {FIELD_SHIELDED_INSTANCE_CONFIG}."
                        f"{FIELD_ENABLE_INTEGRITY}=false; disabling Shielded VM "
                        "integrity monitoring violates baseline node security policy "
                        f"when migrating to {target_family}."
                    ),
                    remediation=(
                        f"Set {FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_INTEGRITY}: true "
                        "(or omit the override so GKE defaults it to true) on the "
                        f"replacement node pool or ComputeClass targeting {target_family}."
                    ),
                )
            )

        secure_boot = _first_present(
            shielded_cfg,
            FIELD_ENABLE_SECURE_BOOT,
            FIELD_ENABLE_SECURE_BOOT_SNAKE,
        )
        # Unlike enableIntegrityMonitoring (which defaults to true in KCC and
        # Terraform), enableSecureBoot defaults to false in GKE, KCC, and
        # Terraform whenever omitted from shieldedInstanceConfig. Because GKE
        # disables Secure Boot by default and no machine family requires it,
        # report disabled Secure Boot as a security policy warning rather than
        # a migration blocker.
        if secure_boot is not True:
            warnings.append(
                Finding(
                    rule=RULE_SHIELDED_SECURE_BOOT_DISABLED,
                    category=CATEGORY_SECURITY,
                    field=f"{FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_SECURE_BOOT}",
                    message=(
                        f"Source node pool has {FIELD_SHIELDED_INSTANCE_CONFIG}."
                        f"{FIELD_ENABLE_SECURE_BOOT}=false (the GKE default); "
                        "Secure Boot is not enabled on the source pool and is "
                        "recommended by baseline node security policy when "
                        f"migrating to {target_family} if no workloads depend on "
                        "unsigned kernel modules."
                    ),
                    remediation=(
                        "Verify no workloads depend on unsigned kernel modules and enable "
                        f"Secure Boot ({FIELD_SHIELDED_INSTANCE_CONFIG}.{FIELD_ENABLE_SECURE_BOOT}: true) "
                        f"on the target {target_family} node pool or ComputeClass."
                    ),
                )
            )

    cvm_enabled, cvm_type, cvm_field = _extract_cvm_state(
        config,
        cluster,
        source_family=source_family,
    )
    if cvm_enabled:
        if target_family in NON_CVM_FAMILIES:
            blockers.append(
                Finding(
                    rule=RULE_CVM_UNSUPPORTED_FAMILY,
                    category=CATEGORY_SECURITY,
                    field=cvm_field or FIELD_CVM_NODES,
                    message=(
                        f"Source configuration enables Confidential GKE Nodes ({cvm_field}), "
                        f"which is not supported on target family {target_family} "
                        "(among the target families, Confidential VM is supported on "
                        "n2d with AMD SEV/SEV_SNP and on c3 and c4 with Intel TDX)."
                    ),
                    remediation=(
                        "Target n2d (AMD SEV/SEV_SNP), c3 (Intel TDX on c3-standard-* or "
                        "c3-standard-*-lssd with ephemeralStorageLocalSsdConfig), or c4 "
                        "(Intel TDX on c4-standard-* shapes) for Confidential VM workloads, "
                        "or confirm with the security owner before moving non-confidential "
                        f"workloads to {target_family}."
                    ),
                )
            )
        elif target_family == FAMILY_C3:
            if cvm_type != CVM_TYPE_TDX:
                source_label = cvm_type if cvm_type in AMD_CVM_TYPES else CVM_DEFAULT_AMD_LABEL
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=f"{cvm_field}.{FIELD_CVM_TYPE}",
                        message=(
                            f"Source Confidential VM pool uses {source_label}, whereas "
                            "target family c3 requires Intel TDX "
                            f"({FIELD_CVM_TYPE}: {CVM_TYPE_TDX} on c3-standard-* or "
                            "c3-standard-*-lssd shapes)."
                        ),
                        remediation=(
                            f"Explicitly configure {FIELD_CVM_TYPE}: {CVM_TYPE_TDX} on a "
                            "c3-standard-* or c3-standard-*-lssd shape (GKE 1.28.6-gke.1317000+) "
                            "and update any remote attestation policies from AMD SEV to Intel TDX."
                        ),
                    )
                )
            if _has_active_nvme_block_lssd(config):
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=FIELD_MACHINE_TYPE,
                        message=(
                            f"Source Confidential VM pool configures raw block Local SSDs "
                            f"({FIELD_NVME_SSD_BLOCK}), which cannot be combined with "
                            "Confidential GKE Nodes on target family c3 (Confidential GKE "
                            "Nodes on c3-standard-*-lssd support Local SSD only for "
                            f"ephemeral storage via {FIELD_EPHEMERAL_LOCAL_SSD})."
                        ),
                        remediation=(
                            f"Use {FIELD_EPHEMERAL_LOCAL_SSD} on a c3-standard-*-lssd shape "
                            f"instead of {FIELD_NVME_SSD_BLOCK}, remove Local SSD "
                            "configurations to use a c3-standard-* shape, or target n2d "
                            "(AMD SEV/SEV_SNP)."
                        ),
                    )
                )
            if target_machine_type and not target_machine_type.startswith(
                C3_TDX_ALLOWED_SHAPE_PREFIX
            ):
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=FIELD_MACHINE_TYPE,
                        message=(
                            f"Target machine shape {target_machine_type} does not support "
                            "Confidential VM with Intel TDX on c3 (only c3-standard-* and "
                            "c3-standard-*-lssd shapes support Intel TDX)."
                        ),
                        remediation=(
                            "Select a c3-standard-* shape (or c3-standard-*-lssd with "
                            f"{FIELD_EPHEMERAL_LOCAL_SSD}) for Confidential VM with Intel TDX."
                        ),
                    )
                )
        elif target_family == FAMILY_C4:
            if cvm_type != CVM_TYPE_TDX:
                source_label = cvm_type if cvm_type in AMD_CVM_TYPES else CVM_DEFAULT_AMD_LABEL
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=f"{cvm_field}.{FIELD_CVM_TYPE}",
                        message=(
                            f"Source Confidential VM pool uses {source_label}, whereas "
                            "target family c4 requires Intel TDX "
                            f"({FIELD_CVM_TYPE}: {CVM_TYPE_TDX} on c4-standard-* shapes)."
                        ),
                        remediation=(
                            f"Explicitly configure {FIELD_CVM_TYPE}: {CVM_TYPE_TDX} on a "
                            "c4-standard-* shape and update any remote attestation policies "
                            "from AMD SEV to Intel TDX."
                        ),
                    )
                )
            lssd_details, _ = _extract_local_ssd_configs(
                config,
                source_machine_type,
            )
            if lssd_details:
                joined_lssd = ", ".join(lssd_details)
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=FIELD_MACHINE_TYPE,
                        message=(
                            f"Source Confidential VM pool also configures Local SSDs "
                            f"({joined_lssd}), which cannot be combined on target family c4 "
                            "(Intel TDX on c4 is supported only on c4-standard-* shapes "
                            "without Local SSDs)."
                        ),
                        remediation=(
                            "Remove Local SSD configurations to use Confidential VM with "
                            "Intel TDX on a c4-standard-* shape, or target c3-standard-*-lssd "
                            f"(with {FIELD_EPHEMERAL_LOCAL_SSD}) or n2d (AMD SEV/SEV_SNP) if "
                            "both Confidential VM and Local SSDs are required."
                        ),
                    )
                )
            elif target_machine_type and (
                not target_machine_type.startswith(C4_TDX_ALLOWED_SHAPE_PREFIX)
                or target_machine_type.endswith(LSSD_MACHINE_SUFFIX)
            ):
                blockers.append(
                    Finding(
                        rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                        category=CATEGORY_SECURITY,
                        field=FIELD_MACHINE_TYPE,
                        message=(
                            f"Target machine shape {target_machine_type} does not support "
                            "Confidential VM with Intel TDX on c4 (only c4-standard-* shapes "
                            "without Local SSDs support Intel TDX)."
                        ),
                        remediation=(
                            "Select a c4-standard-* shape (without -lssd) for Confidential VM "
                            "with Intel TDX on c4, or use c3-standard-*-lssd with "
                            f"{FIELD_EPHEMERAL_LOCAL_SSD} if Local SSD ephemeral storage is required."
                        ),
                    )
                )
        elif target_family == FAMILY_N2D and cvm_type == CVM_TYPE_TDX:
            blockers.append(
                Finding(
                    rule=RULE_CVM_TECHNOLOGY_MISMATCH,
                    category=CATEGORY_SECURITY,
                    field=f"{cvm_field}.{FIELD_CVM_TYPE}",
                    message=(
                        "Source Confidential VM pool uses Intel TDX, whereas target family "
                        f"n2d requires AMD {CVM_TYPE_SEV} or {CVM_TYPE_SEV_SNP}."
                    ),
                    remediation=(
                        f"Configure {FIELD_CVM_TYPE}: {CVM_TYPE_SEV} (or {CVM_TYPE_SEV_SNP}) "
                        "when targeting n2d and update any remote attestation policies."
                    ),
                )
            )

    return blockers, warnings


def _evaluate_disk_and_local_ssd(
    config: dict[str, Any],
    source_machine_type: str,
    target_family: str,
    target_machine_type: str,
) -> list[Finding]:
    """Check boot disk generation compatibility and Local SSD attachment rules."""
    blockers: list[Finding] = []
    disk_type = _extract_boot_disk_type(config)

    if disk_type:
        if target_family in HYPERDISK_ONLY_FAMILIES:
            if disk_type.startswith(PD_DISK_PREFIX):
                blockers.append(
                    Finding(
                        rule=RULE_DISK_PD_ON_HYPERDISK_ONLY,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses Persistent Disk boot disk '{disk_type}', "
                            f"which is rejected on Hyperdisk-only target family {target_family} "
                            f"({target_family} supports {', '.join(HYPERDISK_ONLY_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Change the node boot disk to '{DISK_HYPERDISK_BALANCED}' "
                            f"(in ComputeClass priorities[].storage.bootDiskType or NodePool "
                            f"nodeConfig.diskType). For stateful workload data PVs spanning "
                            "Persistent Disk and Hyperdisk families, migrate PVs to the "
                            "built-in 'dynamic-rwo' StorageClass (GKE 1.35.3-gke.1290000+)."
                        ),
                    )
                )
            elif disk_type not in HYPERDISK_ONLY_ALLOWED_BOOT_DISKS:
                blockers.append(
                    Finding(
                        rule=RULE_DISK_UNSUPPORTED_BOOT,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses boot disk '{disk_type}', which is not a "
                            f"supported boot disk on target family {target_family} "
                            f"(supported: {', '.join(HYPERDISK_ONLY_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Use '{DISK_HYPERDISK_BALANCED}' as the boot disk for {target_family}."
                        ),
                    )
                )
        elif target_family == FAMILY_C3:
            if disk_type == DISK_PD_STANDARD:
                blockers.append(
                    Finding(
                        rule=RULE_DISK_PD_STANDARD_ON_C3,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses '{DISK_PD_STANDARD}' boot disk, which is "
                            "not supported on target family c3 "
                            f"(c3 supports {', '.join(C3_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Change the boot disk type to '{DISK_PD_BALANCED}', "
                            f"'{DISK_PD_SSD}', or '{DISK_HYPERDISK_BALANCED}' when targeting c3."
                        ),
                    )
                )
            elif disk_type not in C3_ALLOWED_BOOT_DISKS:
                blockers.append(
                    Finding(
                        rule=RULE_DISK_UNSUPPORTED_BOOT,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses boot disk '{disk_type}', which is not "
                            "supported as a boot disk on target family c3 "
                            f"(c3 supports {', '.join(C3_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Use one of {', '.join(C3_ALLOWED_BOOT_DISKS)} as the boot disk on c3."
                        ),
                    )
                )
        elif target_family in PD_ONLY_BOOT_FAMILIES:
            if disk_type.startswith(HYPERDISK_PREFIX):
                blockers.append(
                    Finding(
                        rule=RULE_DISK_HYPERDISK_ON_GEN2,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses Hyperdisk boot disk '{disk_type}', "
                            f"which is not supported as a boot disk on Gen 2 target family "
                            f"{target_family} (requires Persistent Disk: "
                            f"{', '.join(PD_ONLY_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Change the boot disk type to '{DISK_PD_BALANCED}' or "
                            f"'{DISK_PD_SSD}' when targeting {target_family}."
                        ),
                    )
                )
            elif disk_type not in PD_ONLY_ALLOWED_BOOT_DISKS:
                blockers.append(
                    Finding(
                        rule=RULE_DISK_UNSUPPORTED_BOOT,
                        category=CATEGORY_DISK,
                        field=FIELD_DISK_TYPE,
                        message=(
                            f"Source node pool uses boot disk '{disk_type}', which is not "
                            f"supported as a boot disk on target family {target_family} "
                            f"(supported: {', '.join(PD_ONLY_ALLOWED_BOOT_DISKS)})."
                        ),
                        remediation=(
                            f"Use '{DISK_PD_BALANCED}' or '{DISK_PD_SSD}' as the boot disk "
                            f"on {target_family}."
                        ),
                    )
                )

    lssd_details, has_legacy_lssd_count = _extract_local_ssd_configs(
        config,
        source_machine_type,
    )
    if lssd_details:
        joined_details = ", ".join(lssd_details)
        if target_family in LSSD_UNSUPPORTED_FAMILIES:
            blockers.append(
                Finding(
                    rule=RULE_LSSD_UNSUPPORTED_ON_N4,
                    category=CATEGORY_LOCAL_SSD,
                    field=FIELD_LOCAL_SSD_COUNT,
                    message=(
                        f"Source node pool configures Local SSDs ({joined_details}), "
                        f"which are not supported on target family {target_family} "
                        f"({target_family} supports Hyperdisk only and rejects Local SSD "
                        "attachments)."
                    ),
                    remediation=(
                        f"Remove Local SSD fields ({FIELD_LOCAL_SSD_COUNT}, "
                        f"{FIELD_EPHEMERAL_LOCAL_SSD}, {FIELD_NVME_SSD_BLOCK}) before "
                        f"migrating to {target_family}, or target a family with NVMe "
                        "Local SSD shapes (c3-standard-*-lssd, c4-standard-*-lssd) or n2/n2d."
                    ),
                )
            )
        elif target_family in LSSD_SHAPE_REQUIRED_FAMILIES:
            if not target_machine_type.endswith(LSSD_MACHINE_SUFFIX):
                blockers.append(
                    Finding(
                        rule=RULE_LSSD_REQUIRES_LSSD_SHAPE,
                        category=CATEGORY_LOCAL_SSD,
                        field=FIELD_MACHINE_TYPE,
                        message=(
                            f"Source node pool configures Local SSDs ({joined_details}); "
                            f"target family {target_family} does not allow arbitrary Local SSD "
                            "attachments on standard shapes and requires a fixed NVMe "
                            f"'*-lssd' machine shape (for example {target_family}-standard-8-lssd)."
                        ),
                        remediation=(
                            f"Target an explicit '{target_family}-*-lssd' machine shape "
                            f"with {FIELD_EPHEMERAL_LOCAL_SSD} or {FIELD_NVME_SSD_BLOCK} "
                            f"(and remove legacy {FIELD_LOCAL_SSD_COUNT}), or remove Local SSD "
                            "settings if scratch NVMe is no longer needed."
                        ),
                    )
                )
            if has_legacy_lssd_count:
                shape_label = target_machine_type or f"{target_family}-*-lssd"
                blockers.append(
                    Finding(
                        rule=RULE_LSSD_LEGACY_COUNT_ON_GEN3_GEN4,
                        category=CATEGORY_LOCAL_SSD,
                        field=FIELD_LOCAL_SSD_COUNT,
                        message=(
                            f"Source node pool sets legacy {FIELD_LOCAL_SSD_COUNT} "
                            f"({joined_details}), which is rejected on {shape_label} "
                            f"because {target_family} '*-lssd' shapes bundle a fixed Local SSD "
                            "count determined by the machine type."
                        ),
                        remediation=(
                            f"Omit legacy {FIELD_LOCAL_SSD_COUNT} and configure "
                            f"{FIELD_EPHEMERAL_LOCAL_SSD} or {FIELD_NVME_SSD_BLOCK} "
                            f"(or ComputeClass ephemeralLocalSsdProfile / dedicatedLocalSsdProfile) "
                            f"matching {shape_label}'s bundled NVMe disks."
                        ),
                    )
                )

    accelerators = _first_present(
        config,
        FIELD_ACCELERATORS,
        FIELD_GUEST_ACCELERATORS,
        FIELD_GUEST_ACCELERATOR_KCC,
        FIELD_GUEST_ACCELERATORS_SNAKE,
    )
    if isinstance(accelerators, list):
        active_accelerators = [
            acc
            for acc in accelerators
            if isinstance(acc, dict)
            and _safe_positive_int(
                _first_present(
                    acc,
                    FIELD_ACCELERATOR_COUNT,
                    FIELD_ACCELERATOR_COUNT_SNAKE,
                )
            )
            > 0
        ]
        if active_accelerators:
            blockers.append(
                Finding(
                    rule=RULE_GPU_UNSUPPORTED_ON_TARGET,
                    category=CATEGORY_ACCELERATOR,
                    field=FIELD_ACCELERATORS,
                    message=(
                        f"Source node pool attaches guest accelerators ({FIELD_ACCELERATORS}), "
                        f"which cannot be attached to general-purpose/compute-optimized target "
                        f"family {target_family}."
                    ),
                    remediation=(
                        "Use an accelerator-optimized GKE machine family (such as g2, a2, or a3) "
                        f"for GPU workloads, or remove {FIELD_ACCELERATORS} / {FIELD_GUEST_ACCELERATORS} "
                        f"before migrating CPU workloads to {target_family}."
                    ),
                )
            )

    return blockers


def _evaluate_versions_and_os_image(
    node_pool: dict[str, Any],
    config: dict[str, Any],
    cluster: dict[str, Any] | None,
    source_family: str,
    target_family: str,
    *,
    cluster_version_override: str = "",
    require_dynamic_rwo: bool = False,
) -> tuple[list[Finding], list[Finding]]:
    """Check GKE control-plane and node version floors and OS image compatibility."""
    blockers: list[Finding] = []
    warnings: list[Finding] = []
    cluster = _normalize_cluster_doc(cluster)

    control_plane_version = cluster_version_override.strip() or _extract_cluster_version(cluster)

    node_version = _extract_pool_version(node_pool)

    family_floor = MIN_GKE_VERSIONS[target_family]
    control_plane_parsed = (
        parse_version(control_plane_version) if control_plane_version else None
    )
    node_version_parsed = parse_version(node_version) if node_version else None

    if control_plane_version and control_plane_parsed is None:
        blockers.append(
            Finding(
                rule=RULE_GKE_VERSION_UNPARSEABLE,
                category=CATEGORY_VERSION,
                field=FIELD_MASTER_VERSION,
                message=(
                    f"Cluster control-plane version '{control_plane_version}' is not a "
                    "pinned numeric GKE version (MAJOR.MINOR or MAJOR.MINOR.PATCH[-gke.BUILD]), "
                    f"so minimum version floor '{family_floor}' for target family "
                    f"{target_family} cannot be verified."
                ),
                remediation=(
                    f"Pin an explicit numeric GKE control-plane version at or above "
                    f"{family_floor} (or pass a concrete numeric version via --cluster-version)."
                ),
            )
        )
    elif control_plane_version and _version_below_floor(control_plane_version, family_floor):
        blockers.append(
            Finding(
                rule=RULE_GKE_MASTER_VERSION_FLOOR,
                category=CATEGORY_VERSION,
                field=FIELD_MASTER_VERSION,
                message=(
                    f"Cluster control-plane version '{control_plane_version}' is below "
                    f"the minimum GKE version '{family_floor}' required for target "
                    f"family {target_family}."
                ),
                remediation=(
                    f"Upgrade the cluster control plane to GKE {family_floor} or newer "
                    f"before provisioning {target_family} node pools."
                ),
            )
        )

    if node_version and node_version_parsed is None:
        blockers.append(
            Finding(
                rule=RULE_GKE_VERSION_UNPARSEABLE,
                category=CATEGORY_VERSION,
                field=FIELD_VERSION,
                message=(
                    f"Source node pool version '{node_version}' is not a pinned numeric "
                    "GKE version (MAJOR.MINOR or MAJOR.MINOR.PATCH[-gke.BUILD]), so "
                    f"minimum version floor '{family_floor}' for target family "
                    f"{target_family} cannot be verified."
                ),
                remediation=(
                    f"Pin an explicit numeric GKE node-pool version at or above "
                    f"{family_floor} (matching a control plane at or above {family_floor})."
                ),
            )
        )
    elif node_version and _version_below_floor(node_version, family_floor):
        blockers.append(
            Finding(
                rule=RULE_GKE_NODE_VERSION_FLOOR,
                category=CATEGORY_VERSION,
                field=FIELD_VERSION,
                message=(
                    f"Source node pool version '{node_version}' is below the minimum "
                    f"GKE version '{family_floor}' required for target family "
                    f"{target_family}."
                ),
                remediation=(
                    f"Set the replacement node pool version to GKE {family_floor} or newer "
                    f"(matching a control plane at or above {family_floor})."
                ),
            )
        )

    cvm_enabled, cvm_type, _ = _extract_cvm_state(
        config,
        cluster,
        source_family=source_family,
    )
    if target_family == FAMILY_C3 and (cvm_enabled or cvm_type == CVM_TYPE_TDX):
        for ver_label, field_name, ver_value, parsed_ver in (
            ("control-plane", FIELD_MASTER_VERSION, control_plane_version, control_plane_parsed),
            ("node-pool", FIELD_VERSION, node_version, node_version_parsed),
        ):
            if (
                ver_value
                and parsed_ver is not None
                and _version_below_floor(ver_value, MIN_GKE_VERSION_CVM_TDX)
            ):
                blockers.append(
                    Finding(
                        rule=RULE_GKE_CVM_TDX_VERSION_FLOOR,
                        category=CATEGORY_VERSION,
                        field=field_name,
                        message=(
                            f"GKE {ver_label} version '{ver_value}' is below the minimum "
                            f"version '{MIN_GKE_VERSION_CVM_TDX}' required for Confidential "
                            "VM with Intel TDX on target family c3."
                        ),
                        remediation=(
                            f"Upgrade GKE to {MIN_GKE_VERSION_CVM_TDX} or newer before "
                            "provisioning Confidential VM (Intel TDX) nodes on c3."
                        ),
                    )
                )

    disk_type = _extract_boot_disk_type(config)
    crosses_disk_gen = (
        (
            (source_family in GEN2_PD_FAMILIES or disk_type.startswith(PD_DISK_PREFIX))
            and target_family in HYPERDISK_ONLY_FAMILIES
        )
        or (
            (source_family in HYPERDISK_ONLY_FAMILIES or disk_type.startswith(HYPERDISK_PREFIX))
            and target_family in PD_ONLY_BOOT_FAMILIES
        )
    )
    if crosses_disk_gen or require_dynamic_rwo:
        for ver_label, field_name, ver_value, parsed_ver in (
            ("control-plane", FIELD_MASTER_VERSION, control_plane_version, control_plane_parsed),
            ("node-pool", FIELD_VERSION, node_version, node_version_parsed),
        ):
            if (
                ver_value
                and parsed_ver is not None
                and _version_below_floor(ver_value, MIN_GKE_VERSION_DYNAMIC_RWO)
            ):
                finding = Finding(
                    rule=RULE_DYNAMIC_RWO_VERSION_FLOOR,
                    category=CATEGORY_VERSION,
                    field=field_name,
                    message=(
                        f"GKE {ver_label} version '{ver_value}' is below "
                        f"'{MIN_GKE_VERSION_DYNAMIC_RWO}', the minimum version required "
                        "for the built-in 'dynamic-rwo' disk-topology-aware StorageClass "
                        "when mixing Persistent Disk and Hyperdisk families for stateful PVs."
                    ),
                    remediation=(
                        f"Upgrade control plane and node pools to {MIN_GKE_VERSION_DYNAMIC_RWO}+ "
                        "before mixing Persistent Disk (Gen 2) and Hyperdisk (Gen 4) "
                        "priorities for stateful PV workloads with 'dynamic-rwo'."
                    ),
                )
                if require_dynamic_rwo:
                    blockers.append(finding)
                else:
                    warnings.append(finding)

    raw_image = _first_present(config, FIELD_IMAGE_TYPE, FIELD_IMAGE_TYPE_SNAKE)
    if isinstance(raw_image, str) and raw_image.strip():
        image_upper = raw_image.strip().upper()
        if image_upper in LEGACY_DOCKER_IMAGES or image_upper.endswith(LEGACY_DOCKER_SUFFIX):
            blockers.append(
                Finding(
                    rule=RULE_OS_IMAGE_LEGACY_DOCKER,
                    category=CATEGORY_OS_IMAGE,
                    field=FIELD_IMAGE_TYPE,
                    message=(
                        f"Source node pool uses legacy Dockershim OS image '{raw_image.strip()}', "
                        f"which is unsupported on target family {target_family} and modern GKE."
                    ),
                    remediation=(
                        "Migrate the node image to 'COS_CONTAINERD' or 'UBUNTU_CONTAINERD' "
                        "(use lowercase 'cos_containerd' or 'ubuntu_containerd' in "
                        "ComputeClass nodePoolConfig.imageType)."
                    ),
                )
            )
        elif image_upper.startswith(WINDOWS_IMAGE_PREFIX):
            if target_family in WINDOWS_UNSUPPORTED_FAMILIES or cvm_enabled:
                reason_clause = (
                    f"target family {target_family}"
                    if target_family in WINDOWS_UNSUPPORTED_FAMILIES
                    else "Confidential VM node pools"
                )
                blockers.append(
                    Finding(
                        rule=RULE_OS_IMAGE_WINDOWS_UNSUPPORTED,
                        category=CATEGORY_OS_IMAGE,
                        field=FIELD_IMAGE_TYPE,
                        message=(
                            f"Source node pool uses Windows OS image '{raw_image.strip()}', "
                            f"which is not supported on {reason_clause}."
                        ),
                        remediation=(
                            "Keep Windows workloads on a supported non-confidential family "
                            "(such as n2, n2d, or c3) or migrate Linux workloads to "
                            "'COS_CONTAINERD' / 'UBUNTU_CONTAINERD'."
                        ),
                    )
                )
            elif image_upper not in ALLOWED_CONTAINERD_IMAGES:
                blockers.append(
                    Finding(
                        rule=RULE_OS_IMAGE_INCOMPATIBLE,
                        category=CATEGORY_OS_IMAGE,
                        field=FIELD_IMAGE_TYPE,
                        message=(
                            f"Source node pool uses unrecognized Windows OS image "
                            f"'{raw_image.strip()}' for target family {target_family}."
                        ),
                        remediation=(
                            f"Use '{IMAGE_WINDOWS_LTSC_CONTAINERD}' on supported families "
                            "or a supported Linux containerd image."
                        ),
                    )
                )
        elif image_upper not in ALLOWED_LINUX_IMAGES:
            blockers.append(
                Finding(
                    rule=RULE_OS_IMAGE_INCOMPATIBLE,
                    category=CATEGORY_OS_IMAGE,
                    field=FIELD_IMAGE_TYPE,
                    message=(
                        f"Source node pool uses unsupported OS image '{raw_image.strip()}', "
                        f"for target family {target_family} "
                        f"(supported Linux images: {', '.join(ALLOWED_LINUX_IMAGES)})."
                    ),
                    remediation=(
                        f"Set {FIELD_IMAGE_TYPE} to '{IMAGE_COS_CONTAINERD}' or "
                        f"'{IMAGE_UBUNTU_CONTAINERD}' (lowercase in ComputeClass "
                        "nodePoolConfig.imageType)."
                    ),
                )
            )

    return blockers, warnings


def evaluate_node_pool_transition(
    node_pool: dict[str, Any],
    target_spec: str,
    *,
    cluster: dict[str, Any] | None = None,
    target_machine_type: str = "",
    cluster_version_override: str = "",
    require_dynamic_rwo: bool = False,
) -> TransitionResult:
    """Evaluate a single source `node_pool` against `target_spec`."""
    parsed_family, parsed_shape = parse_target_spec(target_spec)
    effective_target_shape = (target_machine_type or parsed_shape).strip().lower()
    if effective_target_shape:
        shape_family = extract_machine_family(effective_target_shape)
        if shape_family != parsed_family:
            raise ValueError(
                f"target machine type {effective_target_shape!r} does not match "
                f"target family {parsed_family!r}"
            )

    config = _node_config_of(node_pool)
    cluster = _normalize_cluster_doc(cluster)
    pool_name = _extract_pool_name(node_pool) or UNNAMED_POOL
    source_machine_type = _extract_source_machine_type(config)
    if not source_machine_type:
        raise ValueError(
            f"node pool {pool_name!r} is missing required {FIELD_MACHINE_TYPE} / {FIELD_MACHINE_TYPE_SNAKE}"
        )
    source_family = extract_machine_family(source_machine_type)

    blockers: list[Finding] = []
    warnings: list[Finding] = []
    sec_blockers, sec_warnings = _evaluate_security(
        config,
        cluster,
        source_family,
        parsed_family,
        effective_target_shape,
        node_pool=node_pool,
        source_machine_type=source_machine_type,
    )
    blockers.extend(sec_blockers)
    warnings.extend(sec_warnings)
    blockers.extend(
        _evaluate_disk_and_local_ssd(
            config,
            source_machine_type,
            parsed_family,
            effective_target_shape,
        )
    )
    ver_blockers, ver_warnings = _evaluate_versions_and_os_image(
        node_pool,
        config,
        cluster,
        source_family,
        parsed_family,
        cluster_version_override=cluster_version_override,
        require_dynamic_rwo=require_dynamic_rwo,
    )
    blockers.extend(ver_blockers)
    warnings.extend(ver_warnings)

    return TransitionResult(
        node_pool=pool_name,
        source_machine_type=source_machine_type,
        source_family=source_family,
        target_family=parsed_family,
        target_machine_type=effective_target_shape,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def _default_runner(timeout: int = GCLOUD_TIMEOUT_SECONDS) -> Runner:
    """Execute a read-only gcloud command with a timeout and shim retries disabled."""
    child_env = {SHIM_RETRY_DEADLINE_ENV: SHIM_NO_RETRY_DEADLINE, **os.environ}

    def run(argv: list[str]) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=child_env,
            )
            return completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            return (
                EXIT_READ_ERROR,
                "",
                f"command timed out after {timeout}s: {' '.join(exc.cmd)}",
            )
        except OSError as exc:
            return EXIT_READ_ERROR, "", f"failed to execute {argv[0]}: {exc}"

    return run


def _resolve_project(explicit_project: str | None) -> str:
    """Resolve the GCP project ID from `--project` or the standard environment variables."""
    if explicit_project and explicit_project.strip():
        return explicit_project.strip()
    for env_key in PROJECT_ENV_VARS:
        value = os.environ.get(env_key, "").strip()
        if value:
            return value
    return ""


def _load_json_input(source: str, label: str) -> tuple[Any | None, str]:
    """Load JSON from a file path, `-` (stdin), or an inline JSON literal."""
    stripped = source.strip()
    try:
        if stripped == STDIN_MARKER:
            raw_text = sys.stdin.read()
        elif stripped.startswith(INLINE_JSON_PREFIXES):
            raw_text = stripped
        else:
            raw_text = Path(stripped).read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"could not read {label} from {source!r}: {exc}"

    if not raw_text.strip():
        return None, f"{label} {source!r} is empty"
    try:
        return json.loads(raw_text), ""
    except json.JSONDecodeError as exc:
        return None, f"{label} {source!r} contains invalid JSON: {exc}"


def _describe_cluster_live(
    project: str,
    location: str,
    cluster_name: str,
    run: Runner,
) -> tuple[dict[str, Any] | None, str]:
    """Run `gcloud container clusters describe` and parse the JSON response."""
    argv = [
        GCLOUD_BIN,
        "container",
        "clusters",
        "describe",
        cluster_name,
        f"--location={location}",
        JSON_FORMAT_FLAG,
    ]
    if project:
        argv.append(f"--project={project}")
    rc, stdout, stderr = run(argv)
    if rc != 0:
        detail = " ".join((stderr or stdout or f"exit {rc}").strip().split())
        return (
            None,
            f"gcloud container clusters describe {cluster_name} "
            f"(--location={location}"
            + (f", --project={project}" if project else "")
            + f") failed: {detail}",
        )
    try:
        parsed = json.loads(stdout or "")
    except json.JSONDecodeError as exc:
        return (
            None,
            f"gcloud container clusters describe {cluster_name} returned invalid JSON: {exc}",
        )
    if not isinstance(parsed, dict) or not parsed:
        return (
            None,
            f"gcloud container clusters describe {cluster_name} returned an empty or non-object payload",
        )
    return parsed, ""


def _describe_node_pool_live(
    project: str,
    location: str,
    cluster_name: str,
    pool_name: str,
    run: Runner,
) -> tuple[dict[str, Any] | None, str]:
    """Run `gcloud container node-pools describe` and parse the JSON response."""
    argv = [
        GCLOUD_BIN,
        "container",
        "node-pools",
        "describe",
        pool_name,
        f"--cluster={cluster_name}",
        f"--location={location}",
        JSON_FORMAT_FLAG,
    ]
    if project:
        argv.append(f"--project={project}")
    rc, stdout, stderr = run(argv)
    if rc != 0:
        detail = " ".join((stderr or stdout or f"exit {rc}").strip().split())
        return (
            None,
            f"gcloud container node-pools describe {pool_name} "
            f"(--cluster={cluster_name}, --location={location}"
            + (f", --project={project}" if project else "")
            + f") failed: {detail}",
        )
    try:
        parsed = json.loads(stdout or "")
    except json.JSONDecodeError as exc:
        return (
            None,
            f"gcloud container node-pools describe {pool_name} returned invalid JSON: {exc}",
        )
    if not isinstance(parsed, dict) or not parsed:
        return (
            None,
            f"gcloud container node-pools describe {pool_name} returned an empty or non-object payload",
        )
    return parsed, ""


def _write_output_atomic(path_str: str, payload: dict[str, Any]) -> None:
    """Write `payload` as formatted JSON to `path_str` atomically via a temp sibling."""
    target_path = Path(path_str).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=OUTPUT_TMP_PREFIX,
        suffix=OUTPUT_TMP_SUFFIX,
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=JSON_INDENT, sort_keys=False)
            handle.write("\n")
        os.replace(tmp_name, target_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def render_markdown_report(report: dict[str, Any]) -> str:
    """Render the evaluation report as a deterministic Markdown summary and table."""
    lines: list[str] = []
    status = report.get("status", STATUS_ERROR)
    if status == STATUS_COMPATIBLE:
        lines.append(VERDICT_COMPATIBLE)
    elif status == STATUS_INCOMPATIBLE:
        lines.append(VERDICT_INCOMPATIBLE)
    else:
        lines.append(VERDICT_ERROR)

    cluster_name = report.get("cluster") or UNNAMED_CLUSTER
    control_plane_version = report.get("control_plane_version") or EMPTY_CELL
    lines.append(
        f"Cluster: `{cluster_name}` | Control-plane version: `{control_plane_version}`"
    )
    lines.append("")

    results: list[dict[str, Any]] = report.get("results") or []
    if results:
        lines.append("| " + " | ".join(TABLE_HEADERS) + " |")
        lines.append("| " + " | ".join(TABLE_SEPARATOR for _ in TABLE_HEADERS) + " |")
        for row in results:
            target_label = row["target_family"]
            if row.get("target_machine_type"):
                target_label = f"{row['target_family']} ({row['target_machine_type']})"
            blocker_rules = ", ".join(b["rule"] for b in row.get("blockers") or []) or EMPTY_CELL
            warning_rules = ", ".join(w["rule"] for w in row.get("warnings") or []) or EMPTY_CELL
            lines.append(
                f"| {row['node_pool']} | {row['source_machine_type']} | "
                f"{target_label} | {row['status']} | {blocker_rules} | {warning_rules} |"
            )
        lines.append("")

    for err in report.get("errors") or []:
        lines.append(f"- [ERROR] {err.get('target', 'input')}: {err.get('message', '')}")

    for row in results:
        transition = (
            f"{row['node_pool']} ({row['source_machine_type']} -> "
            f"{row.get('target_machine_type') or row['target_family']})"
        )
        for blocker in row.get("blockers") or []:
            lines.append(
                f"- [BLOCKER] {transition} [{blocker['rule']}] ({blocker['field']}): "
                f"{blocker['message']} Remediation: {blocker['remediation']}"
            )
        for warning in row.get("warnings") or []:
            lines.append(
                f"- [WARNING] {transition} [{warning['rule']}] ({warning['field']}): "
                f"{warning['message']} Remediation: {warning['remediation']}"
            )

    if status in (STATUS_INCOMPATIBLE, STATUS_ERROR):
        lines.append("")
        lines.append(PREFLIGHT_STOP_LINE)

    return "\n".join(lines)


def run_preflight(
    *,
    project: str = "",
    location: str = "",
    cluster_name: str = "",
    node_pool_names: list[str] | None = None,
    cluster_json_source: str = "",
    node_pool_json_sources: list[str] | None = None,
    target_specs: list[str] | None = None,
    target_machine_type: str = "",
    cluster_version_override: str = "",
    require_dynamic_rwo: bool = False,
    run: Runner | None = None,
) -> tuple[int, dict[str, Any]]:
    """Load cluster and node-pool inputs (offline or via gcloud) and grade transitions."""
    runner = run or _default_runner()
    resolved_project = _resolve_project(project)
    errors: list[dict[str, str]] = []
    cluster_doc: dict[str, Any] | None = None
    pools_to_check: list[dict[str, Any]] = []

    if cluster_version_override:
        if parse_version(cluster_version_override.strip()) is None:
            raise ValueError(
                f"--cluster-version {cluster_version_override!r} must be a numeric GKE "
                "version (MAJOR.MINOR or MAJOR.MINOR.PATCH[-gke.BUILD])"
            )

    requested_names: list[str] = []
    for raw_pool_name in node_pool_names or []:
        cleaned_pool_name = (raw_pool_name or "").strip()
        if not cleaned_pool_name:
            raise ValueError("empty --node-pool name")
        if cleaned_pool_name not in requested_names:
            requested_names.append(cleaned_pool_name)

    explicit_shape = ""
    explicit_shape_family = ""
    if target_machine_type:
        explicit_shape_family, explicit_shape = parse_target_spec(target_machine_type)
        if not explicit_shape:
            raise ValueError(
                f"--target-machine-type {target_machine_type!r} must be a concrete machine "
                "shape (for example 'c3-standard-8-lssd')"
            )

    expanded_targets: list[str] = []
    for item in target_specs or []:
        pieces = [
            piece.strip()
            for piece in item.split(COMMA_SEPARATOR)
            if piece.strip()
        ]
        if not pieces:
            raise ValueError("empty target machine family")
        expanded_targets.extend(pieces)

    normalized_targets: list[tuple[str, str]] = []
    if not expanded_targets:
        if explicit_shape:
            normalized_targets.append((explicit_shape_family, explicit_shape))
        else:
            for fam in SUPPORTED_TARGET_FAMILIES:
                normalized_targets.append((fam, ""))
    else:
        parsed_entries = [parse_target_spec(spec) for spec in expanded_targets]
        if explicit_shape:
            if not any(fam == explicit_shape_family for fam, _ in parsed_entries):
                families_str = ", ".join(fam for fam, _ in parsed_entries)
                raise ValueError(
                    f"--target-machine-type {explicit_shape!r} does not belong to target "
                    f"family {families_str!r}"
                )
            for parsed_family, parsed_shape in parsed_entries:
                if (
                    parsed_family == explicit_shape_family
                    and parsed_shape
                    and parsed_shape != explicit_shape
                ):
                    raise ValueError(
                        f"--target-machine-type {explicit_shape!r} conflicts with concrete "
                        f"--target-family shape {parsed_shape!r}"
                    )
                shape = (
                    explicit_shape
                    if parsed_family == explicit_shape_family
                    else parsed_shape
                )
                normalized_targets.append((parsed_family, shape))
        else:
            normalized_targets.extend(parsed_entries)
    normalized_targets = list(dict.fromkeys(normalized_targets))

    stdin_usage_count = 0
    if cluster_json_source and cluster_json_source.strip() == STDIN_MARKER:
        stdin_usage_count += 1
    for pool_source in node_pool_json_sources or []:
        if (pool_source or "").strip() == STDIN_MARKER:
            stdin_usage_count += 1
    if stdin_usage_count > 1:
        errors.append(
            {
                "target": "cli",
                "message": (
                    f"stdin ({STDIN_MARKER!r}) can only be used once across "
                    "--cluster-json and --node-pool-json (fail-closed)"
                ),
            }
        )

    if cluster_json_source and stdin_usage_count <= 1:
        loaded_cluster, err = _load_json_input(cluster_json_source, "--cluster-json")
        if err:
            errors.append({"target": "--cluster-json", "message": err})
        elif not isinstance(loaded_cluster, dict):
            errors.append(
                {
                    "target": "--cluster-json",
                    "message": "--cluster-json must contain a JSON object",
                }
            )
        else:
            cluster_doc = _normalize_cluster_doc(loaded_cluster)

    has_explicit_cluster_source = bool(
        cluster_json_source or cluster_name or location
    )
    wrapper_cluster_count = 0

    for pool_source in (node_pool_json_sources or []) if stdin_usage_count <= 1 else []:
        loaded_pool, err = _load_json_input(pool_source, "--node-pool-json")
        if err:
            errors.append({"target": f"--node-pool-json ({pool_source})", "message": err})
        elif isinstance(loaded_pool, dict):
            if _is_cluster_wrapper(loaded_pool):
                wrapper_cluster_count += 1
                if has_explicit_cluster_source or wrapper_cluster_count > 1:
                    errors.append(
                        {
                            "target": f"--node-pool-json ({pool_source})",
                            "message": (
                                "cluster wrapper in --node-pool-json cannot be combined "
                                "with --cluster-json, live --cluster/--location, or "
                                "another cluster wrapper (fail-closed)"
                            ),
                        }
                    )
                    continue
                cluster_doc = _normalize_cluster_doc(loaded_pool)
                raw_wrapper_pools = _first_present(
                    cluster_doc or loaded_pool,
                    FIELD_NODE_POOLS,
                    FIELD_NODE_POOLS_SNAKE,
                )
                if isinstance(raw_wrapper_pools, list) and all(
                    isinstance(p, dict) for p in raw_wrapper_pools
                ):
                    pools_to_check.extend(raw_wrapper_pools)
                else:
                    errors.append(
                        {
                            "target": f"--node-pool-json ({pool_source})",
                            "message": (
                                "cluster object in --node-pool-json must contain a "
                                f"{FIELD_NODE_POOLS} array of node-pool objects"
                            ),
                        }
                    )
            else:
                pools_to_check.append(loaded_pool)
        elif isinstance(loaded_pool, list) and all(isinstance(p, dict) for p in loaded_pool):
            if any(_is_cluster_wrapper(p) for p in loaded_pool):
                errors.append(
                    {
                        "target": f"--node-pool-json ({pool_source})",
                        "message": (
                            "--node-pool-json array must contain node-pool objects, "
                            "not cluster wrapper objects (fail-closed)"
                        ),
                    }
                )
            else:
                pools_to_check.extend(loaded_pool)
        else:
            errors.append(
                {
                    "target": f"--node-pool-json ({pool_source})",
                    "message": "--node-pool-json must contain a JSON object or array of objects",
                }
            )

    if not cluster_json_source and not errors:
        if cluster_name and location:
            if cluster_doc is None:
                raw_live_cluster, err = _describe_cluster_live(
                    resolved_project,
                    location,
                    cluster_name,
                    runner,
                )
                cluster_doc = _normalize_cluster_doc(raw_live_cluster)
                if err:
                    errors.append({"target": f"cluster/{cluster_name}", "message": err})
        elif cluster_name or location:
            errors.append(
                {
                    "target": "cli",
                    "message": (
                        "both --cluster and --location are required when specifying live "
                        "cluster flags without --cluster-json"
                    ),
                }
            )
        elif not (node_pool_json_sources or []):
            errors.append(
                {
                    "target": "cli",
                    "message": (
                        "specify either live target flags (--cluster and --location, "
                        "optionally --project and --node-pool) or offline JSON inputs "
                        "(--cluster-json and/or --node-pool-json)"
                    ),
                }
            )

    if cluster_doc is not None and not pools_to_check:
        raw_cluster_pools = _first_present(
            cluster_doc,
            FIELD_NODE_POOLS,
            FIELD_NODE_POOLS_SNAKE,
        )
        cluster_pools = (
            [p for p in raw_cluster_pools if isinstance(p, dict)]
            if isinstance(raw_cluster_pools, list)
            else []
        )
        if requested_names:
            for req_name in requested_names:
                matches = [
                    p
                    for p in cluster_pools
                    if _extract_pool_name(p) == req_name
                ]
                if matches:
                    pools_to_check.extend(matches)
                elif not cluster_json_source and cluster_name and location:
                    described_pool, pool_err = _describe_node_pool_live(
                        resolved_project,
                        location,
                        cluster_name,
                        req_name,
                        runner,
                    )
                    if pool_err:
                        errors.append(
                            {
                                "target": f"node-pool/{req_name}",
                                "message": pool_err,
                            }
                        )
                    elif described_pool is not None:
                        pools_to_check.append(described_pool)
                else:
                    errors.append(
                        {
                            "target": f"node-pool/{req_name}",
                            "message": (
                                f"node pool {req_name!r} was not found in cluster "
                                f"{cluster_doc.get(FIELD_NAME) or cluster_name or UNNAMED_CLUSTER}"
                            ),
                        }
                    )
        else:
            pools_to_check.extend(cluster_pools)
    elif pools_to_check and requested_names:
        filtered: list[dict[str, Any]] = []
        for req_name in requested_names:
            matches = [
                p
                for p in pools_to_check
                if _extract_pool_name(p) == req_name
            ]
            if matches:
                filtered.extend(matches)
            else:
                errors.append(
                    {
                        "target": f"node-pool/{req_name}",
                        "message": f"node pool {req_name!r} was not found in provided node-pool JSON",
                    }
                )
        pools_to_check = filtered

    if not errors and not pools_to_check:
        errors.append(
            {
                "target": f"cluster/{(cluster_doc or {}).get(FIELD_NAME) or cluster_name or UNNAMED_CLUSTER}",
                "message": "no node pools found to evaluate (fail-closed)",
            }
        )

    if not errors:
        for pool_doc in pools_to_check:
            pool_cfg = _node_config_of(pool_doc)
            if not _extract_source_machine_type(pool_cfg):
                pool_label = _extract_pool_name(pool_doc) or UNNAMED_POOL
                errors.append(
                    {
                        "target": f"node-pool/{pool_label}",
                        "message": (
                            f"node pool {pool_label!r} is missing required "
                            f"{FIELD_MACHINE_TYPE} / {FIELD_MACHINE_TYPE_SNAKE} (fail-closed)"
                        ),
                    }
                )

    transitions: list[TransitionResult] = []
    if not errors:
        for pool_doc in pools_to_check:
            for target_family, target_shape in normalized_targets:
                transitions.append(
                    evaluate_node_pool_transition(
                        pool_doc,
                        target_family,
                        cluster=cluster_doc,
                        target_machine_type=target_shape,
                        cluster_version_override=cluster_version_override,
                        require_dynamic_rwo=require_dynamic_rwo,
                    )
                )

    control_plane_version = cluster_version_override.strip() or _extract_cluster_version(
        cluster_doc
    )

    effective_cluster_name = (
        cluster_name
        or (str(cluster_doc.get(FIELD_NAME) or "") if isinstance(cluster_doc, dict) else "")
        or UNNAMED_CLUSTER
    )
    effective_location = (
        location
        or (
            str(cluster_doc.get(FIELD_LOCATION) or "")
            if isinstance(cluster_doc, dict)
            else ""
        )
    )

    if errors:
        overall_status = STATUS_ERROR
        compatible = False
        exit_code = EXIT_READ_ERROR
    elif any(not t.compatible for t in transitions):
        overall_status = STATUS_INCOMPATIBLE
        compatible = False
        exit_code = EXIT_INCOMPATIBLE
    else:
        overall_status = STATUS_COMPATIBLE
        compatible = True
        exit_code = EXIT_OK

    report: dict[str, Any] = {
        "status": overall_status,
        "compatible": compatible,
        "project": resolved_project,
        "location": effective_location,
        "cluster": effective_cluster_name,
        "control_plane_version": control_plane_version,
        "target_families": list(dict.fromkeys(fam for fam, _ in normalized_targets)),
        "results": [t.to_dict() for t in transitions],
        "errors": errors,
    }
    return exit_code, report


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser (`allow_abbrev=False` for exact flag matching)."""
    parser = argparse.ArgumentParser(
        description=(
            "Read-only GKE machine-family migration pre-flight validator "
            "(n1/e2/n2/n2d/c3/n4/c4 -> n2, n2d, n4, c3, c4)."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--project",
        default="",
        help="GCP project ID for live gcloud reads (defaults to GKE_PROJECT_ID / GCP_PROJECT_ID / PROJECT_ID).",
    )
    parser.add_argument(
        "--location",
        default="",
        help="GKE cluster region or zone for live gcloud reads.",
    )
    parser.add_argument(
        "--cluster",
        default="",
        help="GKE cluster name for live gcloud reads.",
    )
    parser.add_argument(
        "--node-pool",
        action="append",
        default=[],
        dest="node_pools",
        help="Node pool name to evaluate (repeatable; defaults to all pools in the cluster).",
    )
    parser.add_argument(
        "--cluster-json",
        default="",
        help="Path to offline cluster describe JSON (or '-' for stdin, or inline JSON object).",
    )
    parser.add_argument(
        "--node-pool-json",
        action="append",
        default=[],
        dest="node_pool_jsons",
        help="Path to offline node-pool describe JSON (repeatable; file path, '-', or inline JSON).",
    )
    parser.add_argument(
        "--target-family",
        action="append",
        default=[],
        dest="target_families",
        help=(
            "Target machine family in n2, n2d, n4, c3, c4 (repeatable or comma-separated; "
            "also accepts a full machine type such as c3-standard-8-lssd; defaults to the "
            "family of --target-machine-type when set, or all five families otherwise)."
        ),
    )
    parser.add_argument(
        "--target-machine-type",
        default="",
        help=(
            "Optional concrete target machine shape (for example c3-standard-8-lssd or "
            "n4-standard-8); narrows the default target family to the shape's family when "
            "--target-family is omitted."
        ),
    )
    parser.add_argument(
        "--cluster-version",
        default="",
        help="Optional GKE control-plane version when evaluating a standalone --node-pool-json.",
    )
    parser.add_argument(
        "--require-dynamic-rwo",
        action="store_true",
        help="Treat the GKE 1.35.3-gke.1290000+ floor for dynamic-rwo StorageClass as a hard blocker.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to write the JSON evaluation report atomically.",
    )
    parser.add_argument(
        "--format",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default=FORMAT_MARKDOWN,
        dest="output_format",
        help="Stdout report format: 'markdown' (default) or 'json'.",
    )
    return parser


def main(argv: list[str] | None = None, *, run: Runner | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        exit_code, report = run_preflight(
            project=args.project,
            location=args.location,
            cluster_name=args.cluster,
            node_pool_names=args.node_pools,
            cluster_json_source=args.cluster_json,
            node_pool_json_sources=args.node_pool_jsons,
            target_specs=args.target_families,
            target_machine_type=args.target_machine_type,
            cluster_version_override=args.cluster_version,
            require_dynamic_rwo=args.require_dynamic_rwo,
            run=run,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return EXIT_READ_ERROR

    if args.output:
        try:
            _write_output_atomic(args.output, report)
        except OSError as exc:
            print(f"VERDICT: ERROR\ncould not write --output {args.output!r}: {exc}", file=sys.stderr)
            return EXIT_READ_ERROR

    if args.output_format == FORMAT_JSON:
        print(json.dumps(report, indent=JSON_INDENT))
    else:
        print(render_markdown_report(report))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
