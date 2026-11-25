"""Comprehensive deterministic Kubernetes, Service Mesh, Policy, GitOps, and Cloud IaC risk rules."""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from .parser import FieldDiff, ParsedResource, diff_objects
from .types import (
    ACTION_ADD,
    ACTION_DELETE,
    ACTION_MODIFY,
    BLAST_CRITICAL,
    BLAST_HIGH,
    BLAST_LOW,
    BLAST_MEDIUM,
    BLAST_UNKNOWN,
    DIM_BLAST_RADIUS,
    DIM_DISRUPTIVENESS,
    DIM_REVERTABILITY,
    DIM_SECURITY,
    DISRUPT_DESTRUCTIVE,
    DISRUPT_DISRUPTIVE,
    DISRUPT_NONE,
    DISRUPT_UNKNOWN,
    REVERT_DIFFICULT,
    REVERT_EASY,
    REVERT_IRREVERSIBLE,
    REVERT_MODERATE,
    REVERT_UNKNOWN,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    Reason,
    ResourceRef,
    ResourceVerdict,
    combine_blast_radius,
    combine_disruptiveness,
    combine_revertability,
    combine_risk,
    escalate_risk,
)

# ---------------------------------------------------------------------------
# Canonical Kubernetes / Platform / Cloud Registries
# ---------------------------------------------------------------------------

SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "kube-agents-system",
        "kubeagents-system",
        "gke-system",
        "gmp-system",
        "gke-managed-system",
        "gke-managed-cim",
        "istio-system",
        "config-management-system",
        "cnrm-system",
        "flux-system",
        "argocd",
        "cert-manager",
        "gatekeeper-system",
        "kyverno",
    }
)

ENV_LABEL_KEYS = ("kube-agents/environment", "environment", "env", "stage", "tier")
PRODUCTION_VALUES = frozenset({"prod", "production", "prd", "live"})

CLUSTER_SCOPED_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "Namespace"),
        ("", "Node"),
        ("", "PersistentVolume"),
        ("rbac.authorization.k8s.io", "ClusterRole"),
        ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
        ("apiextensions.k8s.io", "CustomResourceDefinition"),
        ("apiregistration.k8s.io", "APIService"),
        ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
        ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
        ("storage.k8s.io", "StorageClass"),
        ("storage.k8s.io", "CSIDriver"),
        ("storage.k8s.io", "CSINode"),
        ("storage.k8s.io", "VolumeAttachment"),
        ("scheduling.k8s.io", "PriorityClass"),
        ("node.k8s.io", "RuntimeClass"),
        ("networking.k8s.io", "IngressClass"),
        ("gateway.networking.k8s.io", "GatewayClass"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotContent"),
        ("kyverno.io", "ClusterPolicy"),
        ("templates.gatekeeper.sh", "ConstraintTemplate"),
        ("configmanagement.gke.io", "ConfigManagement"),
        ("cert-manager.io", "ClusterIssuer"),
        ("external-secrets.io", "ClusterSecretStore"),
    }
)

WORKLOAD_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("apps", "Deployment"),
        ("apps", "StatefulSet"),
        ("apps", "DaemonSet"),
        ("apps", "ReplicaSet"),
        ("batch", "Job"),
        ("batch", "CronJob"),
        ("", "Pod"),
    }
)

LOW_RISK_EDITABLE_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("apps", "Deployment"),
        ("batch", "CronJob"),
        ("autoscaling", "HorizontalPodAutoscaler"),
        ("", "ConfigMap"),
        ("", "Service"),
    }
)

DATA_STATEFUL_DELETE_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "PersistentVolumeClaim"),
        ("", "PersistentVolume"),
        ("", "Namespace"),
        ("snapshot.storage.k8s.io", "VolumeSnapshot"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotContent"),
        ("apiextensions.k8s.io", "CustomResourceDefinition"),
        ("storage.k8s.io", "StorageClass"),
        ("postgresql.cnpg.io", "Cluster"),
        ("postgresql.cnpg.io", "ScheduledBackup"),
        ("container.cnrm.cloud.google.com", "ContainerCluster"),
        ("container.cnrm.cloud.google.com", "ContainerNodePool"),
        ("sql.cnrm.cloud.google.com", "SQLInstance"),
        ("sql.cnrm.cloud.google.com", "SQLDatabase"),
        ("spanner.cnrm.cloud.google.com", "SpannerInstance"),
        ("spanner.cnrm.cloud.google.com", "SpannerDatabase"),
        ("alloydb.cnrm.cloud.google.com", "AlloyDBCluster"),
        ("alloydb.cnrm.cloud.google.com", "AlloyDBInstance"),
        ("storage.cnrm.cloud.google.com", "StorageBucket"),
        ("bigquery.cnrm.cloud.google.com", "BigQueryDataset"),
        ("bigquery.cnrm.cloud.google.com", "BigQueryTable"),
        ("compute.cnrm.cloud.google.com", "ComputeDisk"),
        ("compute.cnrm.cloud.google.com", "ComputeSnapshot"),
        ("kms.cnrm.cloud.google.com", "KMSCryptoKey"),
        ("secretmanager.cnrm.cloud.google.com", "SecretManagerSecret"),
        ("resourcemanager.cnrm.cloud.google.com", "Project"),
    }
)

RBAC_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("rbac.authorization.k8s.io", "Role"),
        ("rbac.authorization.k8s.io", "RoleBinding"),
        ("rbac.authorization.k8s.io", "ClusterRole"),
        ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
    }
)

CONTROL_PLANE_EXTENSION_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
        ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicy"),
        ("admissionregistration.k8s.io", "ValidatingAdmissionPolicyBinding"),
        ("apiregistration.k8s.io", "APIService"),
    }
)

GATEWAY_API_ROUTE_KINDS = frozenset({"HTTPRoute", "GRPCRoute", "TCPRoute", "TLSRoute", "UDPRoute"})
ISTIO_TRAFFIC_KINDS = frozenset({"VirtualService", "DestinationRule", "Gateway", "ServiceEntry", "Sidecar"})
ISTIO_SECURITY_KINDS = frozenset({"PeerAuthentication", "RequestAuthentication", "AuthorizationPolicy"})
ISTIO_PROXY_KINDS = frozenset({"EnvoyFilter", "WasmPlugin"})

GITOPS_RECONCILER_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("argoproj.io", "Application"),
        ("argoproj.io", "ApplicationSet"),
        ("argoproj.io", "AppProject"),
        ("kustomize.toolkit.fluxcd.io", "Kustomization"),
        ("helm.toolkit.fluxcd.io", "HelmRelease"),
        ("source.toolkit.fluxcd.io", "GitRepository"),
        ("source.toolkit.fluxcd.io", "HelmRepository"),
        ("source.toolkit.fluxcd.io", "OCIRepository"),
        ("source.toolkit.fluxcd.io", "Bucket"),
        ("configsync.gke.io", "RootSync"),
        ("configsync.gke.io", "RepoSync"),
        ("configmanagement.gke.io", "ConfigManagement"),
    }
)

CLOUD_IAC_GROUP_SUFFIXES = (
    ".cnrm.cloud.google.com",
    ".crossplane.io",
    ".upbound.io",
    ".services.k8s.aws",
    ".azure.com",
)

CORE_KNOWN_KINDS: frozenset[tuple[str, str]] = (
    WORKLOAD_KINDS
    | CLUSTER_SCOPED_KINDS
    | RBAC_KINDS
    | CONTROL_PLANE_EXTENSION_KINDS
    | GITOPS_RECONCILER_KINDS
    | DATA_STATEFUL_DELETE_KINDS
    | frozenset(
        {
            ("", "ConfigMap"),
            ("", "Secret"),
            ("", "Service"),
            ("", "ServiceAccount"),
            ("", "ResourceQuota"),
            ("", "LimitRange"),
            ("policy", "PodDisruptionBudget"),
            ("autoscaling", "HorizontalPodAutoscaler"),
            ("autoscaling.k8s.io", "VerticalPodAutoscaler"),
            ("networking.k8s.io", "Ingress"),
            ("networking.k8s.io", "NetworkPolicy"),
            ("gateway.networking.k8s.io", "Gateway"),
            ("gateway.networking.k8s.io", "HTTPRoute"),
            ("gateway.networking.k8s.io", "GRPCRoute"),
            ("gateway.networking.k8s.io", "TCPRoute"),
            ("gateway.networking.k8s.io", "TLSRoute"),
            ("gateway.networking.k8s.io", "UDPRoute"),
            ("gateway.networking.k8s.io", "ReferenceGrant"),
            ("networking.istio.io", "VirtualService"),
            ("networking.istio.io", "DestinationRule"),
            ("networking.istio.io", "Gateway"),
            ("networking.istio.io", "ServiceEntry"),
            ("networking.istio.io", "Sidecar"),
            ("networking.istio.io", "EnvoyFilter"),
            ("security.istio.io", "PeerAuthentication"),
            ("security.istio.io", "RequestAuthentication"),
            ("security.istio.io", "AuthorizationPolicy"),
            ("extensions.istio.io", "WasmPlugin"),
            ("kyverno.io", "Policy"),
            ("kyverno.io", "ClusterPolicy"),
            ("cert-manager.io", "Certificate"),
            ("cert-manager.io", "Issuer"),
            ("external-secrets.io", "ExternalSecret"),
            ("external-secrets.io", "SecretStore"),
            ("bitnami.com", "SealedSecret"),
        }
    )
)

# Closed-world allowlist of benign metadata keys editable at `low` risk
ALLOWED_LOW_RISK_METADATA_KEYS = frozenset(
    {
        "app.kubernetes.io/version",
        "app.kubernetes.io/part-of",
        "app.kubernetes.io/component",
        "description",
        "owner",
        "team",
        "cost-center",
        "contact",
        "documentation",
        "prometheus.io/scrape",
        "prometheus.io/port",
        "prometheus.io/path",
        "reloader.stakater.com/auto",
    }
)

PROTECTED_METADATA_EXACT = frozenset(
    {
        "environment",
        "env",
        "stage",
        "tier",
        "istio-injection",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/managed-by",
    }
)

PROTECTED_METADATA_PREFIXES = (
    "kube-agents/",
    "kubeagents.",
    "pod-security.kubernetes.io/",
    "istio.io/",
    "security.istio.io/",
    "iam.gke.io/",
    "eks.amazonaws.com/",
    "azure.workload.identity/",
    "argocd.argoproj.io/",
    "helm.sh/",
    "cnrm.cloud.google.com/",
    "nginx.ingress.kubernetes.io/",
    "kubernetes.io/",
    "k8s.io/",
    "networking.gke.io/",
    "cloud.google.com/",
    "storageclass.kubernetes.io/",
    "kubectl.kubernetes.io/",
)

# Secret-like key names & high-confidence credential patterns
_SECRET_KEY_NAME_RE = re.compile(
    r"(?:^|[._-])(?:PASSWORD|PASSWD|SECRET|PRIVATE[_-]?KEY|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"AUTH[_-]?TOKEN|BEARER|CLIENT[_-]?SECRET|CREDENTIALS?|DB[_-]?PASS|JWT[_-]?SECRET)(?:$|[._-])",
    re.IGNORECASE,
)

_DANGEROUS_ENV_KEY_RE = re.compile(
    r"^(?:LD_PRELOAD|LD_LIBRARY_PATH|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|"
    r"NODE_TLS_REJECT_UNAUTHORIZED|PYTHONHTTPSVERIFY|SSL_CERT_FILE|GODEBUG|JAVA_TOOL_OPTIONS)$",
    re.IGNORECASE,
)

_CREDENTIAL_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PEM private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GCP API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("API secret key", re.compile(r"\bsk-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_-]{20,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("JWT bearer token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

# Image tag immutability patterns (3-part semver or explicit/full git SHA; rejects 2-part floating tags like :16.2)
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z._-]+)?$")
_GIT_SHA_TAG_RE = re.compile(r"^(?:(?:sha|git)-[0-9a-f]{7,40}|[0-9a-f]{40})$")
_MUTABLE_TAG_TOKENS = frozenset(
    {
        "latest",
        "main",
        "master",
        "dev",
        "develop",
        "staging",
        "stage",
        "prod",
        "production",
        "canary",
        "edge",
        "nightly",
        "snapshot",
        "qa",
        "uat",
        "rc",
        "beta",
        "alpha",
        "test",
    }
)

# Host paths that grant node / cluster compromise (checked after posixpath.normpath)
_CRITICAL_HOST_PATHS = (
    "/",
    "/etc",
    "/etc/kubernetes",
    "/var/run",
    "/run",
    "/root",
    "/boot",
    "/lib",
    "/usr",
    "/var/lib/kubelet",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/proc",
    "/sys",
    "/dev",
)

# Kubernetes resource quantity parsing
_QTY_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$")
_CPU_SUFFIX_MULT = {
    "": 1000.0,
    "m": 1.0,
    "u": 1e-3,
    "n": 1e-6,
    "k": 1e6,
    "M": 1e9,
}
_MEM_SUFFIX_MULT = {
    "": 1.0,
    "Ki": 1024.0,
    "Mi": 1024.0**2,
    "Gi": 1024.0**3,
    "Ti": 1024.0**4,
    "Pi": 1024.0**5,
    "k": 1000.0,
    "K": 1000.0,
    "M": 1000.0**2,
    "G": 1000.0**3,
    "T": 1000.0**4,
    "P": 1000.0**5,
}

# Bounds for `low` risk workload edits
MAX_LOW_REPLICA_DELTA = 5
MAX_LOW_REPLICA_FACTOR = 3.0
MAX_LOW_REPLICA_TOTAL = 20
MAX_LOW_RESOURCE_FACTOR = 4.0
MAX_LOW_CONTAINER_CPU_M = 8000.0
MAX_LOW_CONTAINER_MEM_B = 16 * 1024**3
MIN_SAFE_CPU_M = 10.0
MIN_SAFE_MEM_B = 16 * 1024**2

# Superuser RBAC / IAM constants
_SUPERUSER_ROLES = frozenset({"cluster-admin", "system:masters"})
_CLUSTER_ADMIN_EQUIV_ROLES = frozenset({"admin", "edit"})
_SUPERUSER_SUBJECTS = frozenset(
    {"system:anonymous", "system:unauthenticated", "system:authenticated", "system:serviceaccounts"}
)
_PRIV_ESCALATION_VERBS = frozenset({"*", "impersonate", "escalate", "bind"})
_SENSITIVE_RBAC_RESOURCES = frozenset(
    {
        "*",
        "secrets",
        "pods/exec",
        "pods/attach",
        "nodes/proxy",
        "serviceaccounts/token",
        "certificatesigningrequests/approval",
        "mutatingwebhookconfigurations",
        "validatingwebhookconfigurations",
    }
)
_CRITICAL_IAM_ROLES = frozenset(
    {
        "roles/owner",
        "roles/editor",
        "roles/iam.securityadmin",
        "roles/iam.serviceaccountkeyadmin",
        "roles/container.admin",
        "roles/resourcemanager.projectiamadmin",
        "roles/resourcemanager.organizationadmin",
    }
)
_PUBLIC_IAM_MEMBERS = frozenset({"allusers", "allauthenticatedusers"})

_INGRESS_SNIPPET_KEYS = (
    "configuration-snippet",
    "server-snippet",
    "stream-snippet",
    "lua-resty",
)


@dataclass
class ResourceEval:
    """Internal mutable accumulator for evaluating one Kubernetes resource."""

    file: str
    action: str
    ref: ResourceRef
    risk_level: str = RISK_LOW
    blast_radius: str = BLAST_LOW
    revertability: str = REVERT_EASY
    disruptiveness: str = DISRUPT_NONE
    supported: bool = True
    reasons: list[Reason] = field(default_factory=list)
    cpu_delta_m: float = 0.0
    mem_delta_bytes: float = 0.0
    security_sensitive: bool = False

    def add(
        self,
        code: str,
        dimension: str,
        severity: str,
        message: str,
        *,
        blast: str | None = None,
        revert: str | None = None,
        disrupt: str | None = None,
        field_path: str = "",
        supported: bool | None = None,
        security: bool = False,
    ) -> None:
        self.risk_level = combine_risk(self.risk_level, severity)
        if blast is not None:
            self.blast_radius = combine_blast_radius(self.blast_radius, blast)
        if revert is not None:
            self.revertability = combine_revertability(self.revertability, revert)
        if disrupt is not None:
            self.disruptiveness = combine_disruptiveness(self.disruptiveness, disrupt)
        if supported is False:
            self.supported = False
        if security:
            self.security_sensitive = True
        self.reasons.append(
            Reason(
                code=code,
                dimension=dimension,
                severity=severity,
                message=message,
                file=self.file,
                field_path=field_path,
                resource=self.ref,
            )
        )

    def to_verdict(self) -> ResourceVerdict:
        return ResourceVerdict(
            file=self.file,
            action=self.action,
            ref=self.ref,
            risk_level=self.risk_level,
            blast_radius=self.blast_radius,
            revertability=self.revertability,
            disruptiveness=self.disruptiveness,
            supported=self.supported,
        )


# ---------------------------------------------------------------------------
# Pure Utility Helpers
# ---------------------------------------------------------------------------

_PROD_TOKEN_SPLIT_RE = re.compile(r"[-_./\s]+")
_ALLOWED_METADATA_KEYS = frozenset(
    {"name", "generateName", "namespace", "labels", "annotations", "finalizers", "ownerReferences"}
)


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _as_list(ev: ResourceEval, val: Any, field_path: str) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    ev.add(
        "MALFORMED_FIELD_TYPE",
        DIM_SECURITY,
        RISK_HIGH,
        f"expected list at {field_path!r}, got {type(val).__name__}",
        disrupt=DISRUPT_DISRUPTIVE,
        field_path=field_path,
        security=True,
    )
    return []


def _as_dict(ev: ResourceEval, val: Any, field_path: str) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    ev.add(
        "MALFORMED_FIELD_TYPE",
        DIM_SECURITY,
        RISK_HIGH,
        f"expected mapping at {field_path!r}, got {type(val).__name__}",
        disrupt=DISRUPT_DISRUPTIVE,
        field_path=field_path,
        security=True,
    )
    return {}


def _find_kustomize_patches(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Find any `$patch` directives (e.g. `$patch: delete`) anywhere inside a manifest."""
    found: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in sorted(obj.items(), key=lambda kv: str(kv[0])):
            fp = f"{prefix}.{k}" if prefix else str(k)
            if k == "$patch":
                found.append((fp, v))
            else:
                found.extend(_find_kustomize_patches(v, fp))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            found.extend(_find_kustomize_patches(item, f"{prefix}[{idx}]"))
    return found


def parse_cpu_millicores(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw) * 1000.0
        return val if math.isfinite(val) and val >= 0 else None
    if not isinstance(raw, str):
        return None
    m = _QTY_RE.match(raw.strip())
    if not m:
        return None
    mult = _CPU_SUFFIX_MULT.get(m.group(2))
    if mult is None:
        return None
    val = float(m.group(1)) * mult
    return val if math.isfinite(val) and val >= 0 else None


def parse_memory_bytes(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) and val >= 0 else None
    if not isinstance(raw, str):
        return None
    m = _QTY_RE.match(raw.strip())
    if not m:
        return None
    mult = _MEM_SUFFIX_MULT.get(m.group(2))
    if mult is None:
        return None
    val = float(m.group(1)) * mult
    return val if math.isfinite(val) and val >= 0 else None


def split_image_ref(image: str) -> tuple[str, str, str]:
    """Return `(repo, tag, digest)` for a container image reference."""
    img = image.strip()
    digest = ""
    if "@" in img:
        img, digest = img.split("@", 1)
    # Tag follows the last `:` after the last `/`
    last_slash = img.rfind("/")
    last_colon = img.rfind(":")
    if last_colon > last_slash:
        repo = img[:last_colon]
        tag = img[last_colon + 1 :]
    else:
        repo = img
        tag = ""
    return repo, tag, digest


def is_immutable_image_pin(image: str) -> bool:
    """Return True if `image` is pinned by sha256 digest or strict immutable version/SHA tag."""
    img = image.strip()
    if not img or any(ch.isspace() for ch in img) or img.count("@") > 1:
        return False
    repo, tag, digest = split_image_ref(img)
    if not repo:
        return False
    if digest:
        return bool(_SHA256_DIGEST_RE.match(digest))
    if not tag:
        return False
    low = tag.lower()
    if low in _MUTABLE_TAG_TOKENS:
        return False
    if _GIT_SHA_TAG_RE.match(low):
        return True
    if _SEMVER_TAG_RE.match(tag):
        parts = re.split(r"[-._]", low)
        if any(p in _MUTABLE_TAG_TOKENS for p in parts):
            return False
        return True
    return False


def is_cloud_infra_group(group: str) -> bool:
    return any(group.endswith(sfx) or group == sfx.lstrip(".") for sfx in CLOUD_IAC_GROUP_SUFFIXES)


def is_cluster_scoped_kind(group: str, kind: str) -> bool:
    if (group, kind) in CLUSTER_SCOPED_KINDS:
        return True
    if group == "constraints.gatekeeper.sh":
        return True
    if group.endswith(".crossplane.io") or group.endswith(".upbound.io") or group in ("crossplane.io", "upbound.io"):
        return True
    if kind.startswith("Cluster") and is_known_kind(group, kind):
        return True
    return False


def is_known_kind(group: str, kind: str) -> bool:
    if (group, kind) in CORE_KNOWN_KINDS:
        return True
    if is_cloud_infra_group(group):
        return True
    if group == "constraints.gatekeeper.sh":
        return True
    if group.endswith(".cnpg.io"):
        return True
    return False


def _meta_dict(obj: dict[str, Any] | None, key: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("metadata")
    if not isinstance(meta, dict):
        return {}
    val = meta.get(key)
    return val if isinstance(val, dict) else {}


def _is_production_labels(labels: dict[str, Any]) -> bool:
    for key in ENV_LABEL_KEYS:
        val = labels.get(key)
        if isinstance(val, str):
            tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(val.strip().lower()) if t]
            if any(t in PRODUCTION_VALUES for t in tokens):
                return True
    return False


def _is_production_path(file_path: str) -> bool:
    for seg in file_path.replace("\\", "/").lower().split("/"):
        tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(seg) if t]
        if any(t in PRODUCTION_VALUES for t in tokens):
            return True
    return False


def _is_protected_metadata_key(key: str) -> bool:
    low = key.lower()
    if low in PROTECTED_METADATA_EXACT:
        return True
    return any(low.startswith(pfx) for pfx in PROTECTED_METADATA_PREFIXES)


def _scan_value_for_secrets(val: Any, path_hint: str = "") -> list[tuple[str, str]]:
    """Return list of `(field_hint, pattern_name)` for any leaked credentials."""
    hits: list[tuple[str, str]] = []
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        if '"type"' in val and '"service_account"' in val and '"private_key"' in val:
            hits.append((path_hint, "GCP service account JSON key"))
        for label, rx in _CREDENTIAL_VALUE_PATTERNS:
            if rx.search(val):
                hits.append((path_hint, label))
    elif isinstance(val, dict):
        for k in sorted(val.keys(), key=str):
            hits.extend(_scan_value_for_secrets(val[k], f"{path_hint}.{k}" if path_hint else str(k)))
    elif isinstance(val, list):
        for idx, item in enumerate(val):
            hits.extend(_scan_value_for_secrets(item, f"{path_hint}[{idx}]"))
    return hits


def _is_truthy_or_nonbool(val: Any) -> bool:
    """Fail-closed check for Kubernetes boolean fields that widen privileges when True."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return True  # Non-boolean like `"yes"` or `1` fails closed!


def _is_falsy_or_nonbool(val: Any) -> bool:
    """Fail-closed check for Kubernetes boolean fields that protect when True (e.g. runAsNonRoot)."""
    if val is None:
        return False
    if isinstance(val, bool):
        return not val
    return True  # Non-boolean like `"no"` fails closed!


def _extract_pod_spec(kind: str, obj: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        jt = spec.get("jobTemplate")
        if not isinstance(jt, dict):
            return None
        spec = jt.get("spec")
        if not isinstance(spec, dict):
            return None
    tmpl = spec.get("template")
    if not isinstance(tmpl, dict):
        return None
    ps = tmpl.get("spec")
    return ps if isinstance(ps, dict) else None


def _extract_pod_template_meta(kind: str, obj: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return {}
    if kind == "Pod":
        m = obj.get("metadata")
        return m if isinstance(m, dict) else {}
    if kind == "CronJob":
        jt = spec.get("jobTemplate")
        if not isinstance(jt, dict):
            return {}
        spec = jt.get("spec")
        if not isinstance(spec, dict):
            return {}
    tmpl = spec.get("template")
    if not isinstance(tmpl, dict):
        return {}
    m = tmpl.get("metadata")
    return m if isinstance(m, dict) else {}


def _is_critical_host_path(path_str: str) -> bool:
    raw = path_str.strip()
    if not raw:
        return False
    clean = posixpath.normpath(raw)
    if clean == ".":
        return True
    if not clean.startswith("/"):
        clean = "/" + clean
    for crit in _CRITICAL_HOST_PATHS:
        if crit == "/":
            if clean == "/":
                return True
        elif clean == crit or clean.startswith(crit + "/") or crit.startswith(clean + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Security, RBAC, IAM, PodSpec, and Kind Evaluators
# ---------------------------------------------------------------------------


def _check_security_hardening_removed(
    ev: ResourceEval,
    b_sc: dict[str, Any],
    a_sc: dict[str, Any],
    prefix: str,
) -> None:
    """Detect removal or weakening of securityContext hardening (including removing the whole block)."""
    if b_sc.get("runAsNonRoot") is True and a_sc.get("runAsNonRoot") is not True:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            f"removes or disables {prefix}.runAsNonRoot=true",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.runAsNonRoot",
            security=True,
        )
    if b_sc.get("readOnlyRootFilesystem") is True and a_sc.get("readOnlyRootFilesystem") is not True:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            f"removes or disables {prefix}.readOnlyRootFilesystem=true",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.readOnlyRootFilesystem",
            security=True,
        )
    if b_sc.get("allowPrivilegeEscalation") is False and a_sc.get("allowPrivilegeEscalation") is not False:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            f"removes or weakens {prefix}.allowPrivilegeEscalation=false",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.allowPrivilegeEscalation",
            security=True,
        )
    b_sp = b_sc.get("seccompProfile")
    a_sp = a_sc.get("seccompProfile")
    if isinstance(b_sp, dict) and str(b_sp.get("type") or "") not in ("", "Unconfined"):
        if not isinstance(a_sp, dict) or str(a_sp.get("type") or "") in ("", "Unconfined"):
            ev.add(
                "SECURITY_HARDENING_REMOVED",
                DIM_SECURITY,
                RISK_HIGH,
                f"removes or weakens {prefix}.seccompProfile",
                blast=BLAST_MEDIUM,
                field_path=f"{prefix}.seccompProfile",
                security=True,
            )
    b_aa = b_sc.get("appArmorProfile")
    a_aa = a_sc.get("appArmorProfile")
    if isinstance(b_aa, dict) and str(b_aa.get("type") or "") not in ("", "Unconfined"):
        if not isinstance(a_aa, dict) or str(a_aa.get("type") or "") in ("", "Unconfined"):
            ev.add(
                "SECURITY_HARDENING_REMOVED",
                DIM_SECURITY,
                RISK_HIGH,
                f"removes or weakens {prefix}.appArmorProfile",
                blast=BLAST_MEDIUM,
                field_path=f"{prefix}.appArmorProfile",
                security=True,
            )
    b_caps = b_sc.get("capabilities")
    a_caps = a_sc.get("capabilities")
    b_drop = b_caps.get("drop") if isinstance(b_caps, dict) and isinstance(b_caps.get("drop"), list) else []
    a_drop = a_caps.get("drop") if isinstance(a_caps, dict) and isinstance(a_caps.get("drop"), list) else []
    if {str(c).upper() for c in b_drop} - {str(c).upper() for c in a_drop}:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            f"removes dropped Linux capabilities from {prefix}.capabilities.drop",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.capabilities.drop",
            security=True,
        )


def _check_pod_spec_posture(
    ev: ResourceEval,
    pod_spec: dict[str, Any],
    *,
    before_pod_spec: dict[str, Any] | None = None,
    is_daemonset: bool = False,
) -> None:
    """Inspect a PodSpec (on add or modify) for privilege escalation, host escape, and secret mounts."""
    b_ps = before_pod_spec if isinstance(before_pod_spec, dict) else {}
    host_escape = False
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if flag in pod_spec and _is_truthy_or_nonbool(pod_spec.get(flag)):
            host_escape = True
            sev = RISK_CRITICAL if is_daemonset else RISK_HIGH
            blast = BLAST_CRITICAL if is_daemonset else BLAST_HIGH
            ev.add(
                "HOST_NAMESPACE_ACCESS",
                DIM_SECURITY,
                sev,
                f"enables {flag} on {ev.ref.gvk_label()} {ev.ref.name}",
                blast=blast,
                field_path=flag,
                security=True,
            )

    if "shareProcessNamespace" in pod_spec and _is_truthy_or_nonbool(pod_spec.get("shareProcessNamespace")):
        ev.add(
            "POD_SECURITY_POSTURE_WIDENED",
            DIM_SECURITY,
            RISK_HIGH,
            f"enables shareProcessNamespace on {ev.ref.gvk_label()} {ev.ref.name}",
            blast=BLAST_MEDIUM,
            field_path="shareProcessNamespace",
            security=True,
        )

    if before_pod_spec is not None:
        if b_ps.get("automountServiceAccountToken") is False and pod_spec.get("automountServiceAccountToken") is not False:
            ev.add(
                "SECURITY_HARDENING_REMOVED",
                DIM_SECURITY,
                RISK_HIGH,
                "enables or removes automountServiceAccountToken=false",
                blast=BLAST_MEDIUM,
                field_path="automountServiceAccountToken",
                security=True,
            )
        for net_sched_key in ("hostAliases", "dnsConfig", "dnsPolicy", "runtimeClassName", "nodeName", "imagePullSecrets"):
            if b_ps.get(net_sched_key) != pod_spec.get(net_sched_key):
                ev.add(
                    "POD_RUNTIME_OR_NETWORK_CHANGED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"modifies PodSpec {net_sched_key}",
                    blast=BLAST_MEDIUM,
                    field_path=net_sched_key,
                    security=True,
                )
    else:
        for add_key in ("hostAliases", "runtimeClassName", "nodeName"):
            if pod_spec.get(add_key):
                ev.add(
                    "POD_RUNTIME_OR_NETWORK_CHANGED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"sets PodSpec {add_key}",
                    blast=BLAST_MEDIUM,
                    field_path=add_key,
                    security=True,
                )

    # Volumes: hostPath, Secret mounts, and projected serviceAccountToken
    volumes = _as_list(ev, pod_spec.get("volumes"), "volumes")
    for idx, vol in enumerate(volumes):
        if not isinstance(vol, dict):
            ev.add(
                "MALFORMED_FIELD_TYPE",
                DIM_SECURITY,
                RISK_HIGH,
                f"volumes[{idx}] is not a mapping",
                field_path=f"volumes[{idx}]",
                security=True,
            )
            continue
        raw_vname = vol.get("name")
        if raw_vname is not None and not isinstance(raw_vname, str):
            ev.add(
                "MALFORMED_FIELD_TYPE",
                DIM_SECURITY,
                RISK_HIGH,
                f"volumes[{idx}].name must be a string",
                field_path=f"volumes[{idx}].name",
                security=True,
            )
        vname = raw_vname if isinstance(raw_vname, str) and raw_vname else str(idx)
        hp = vol.get("hostPath")
        if isinstance(hp, dict):
            host_escape = True
            hpath = str(hp.get("path") or "")
            crit = is_daemonset or _is_critical_host_path(hpath)
            ev.add(
                "HOST_PATH_MOUNT",
                DIM_SECURITY,
                RISK_CRITICAL if crit else RISK_HIGH,
                f"mounts hostPath {hpath!r} in volume {vname!r}",
                blast=BLAST_CRITICAL if crit else BLAST_HIGH,
                field_path=f"volumes.{vname}.hostPath",
                security=True,
            )
        if isinstance(vol.get("secret"), dict):
            ev.add(
                "SECRET_VOLUME_MOUNT",
                DIM_SECURITY,
                RISK_HIGH,
                f"mounts Kubernetes Secret in volume {vname!r}",
                blast=BLAST_MEDIUM,
                field_path=f"volumes.{vname}.secret",
                security=True,
            )
        proj = vol.get("projected")
        if isinstance(proj, dict):
            for src in _as_list(ev, proj.get("sources"), f"volumes.{vname}.projected.sources"):
                if not isinstance(src, dict):
                    continue
                if isinstance(src.get("secret"), dict):
                    ev.add(
                        "SECRET_VOLUME_MOUNT",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"mounts projected Secret in volume {vname!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"volumes.{vname}.projected",
                        security=True,
                    )
                if isinstance(src.get("serviceAccountToken"), dict):
                    ev.add(
                        "SERVICE_ACCOUNT_TOKEN_MOUNT",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"mounts projected serviceAccountToken in volume {vname!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"volumes.{vname}.projected.serviceAccountToken",
                        security=True,
                    )
        csi = vol.get("csi")
        if isinstance(csi, dict) and "secret" in str(csi.get("driver") or "").lower():
            ev.add(
                "SECRET_VOLUME_MOUNT",
                DIM_SECURITY,
                RISK_HIGH,
                f"mounts CSI secrets driver {csi.get('driver')!r} in volume {vname!r}",
                blast=BLAST_MEDIUM,
                field_path=f"volumes.{vname}.csi",
                security=True,
            )

    # Pod-level securityContext
    psc = _as_dict(ev, pod_spec.get("securityContext"), "securityContext")
    b_psc = b_ps.get("securityContext") if isinstance(b_ps.get("securityContext"), dict) else {}
    if before_pod_spec is not None and b_psc:
        _check_security_hardening_removed(ev, b_psc, psc, "securityContext")
    if psc:
        if "runAsNonRoot" in psc and _is_falsy_or_nonbool(psc.get("runAsNonRoot")):
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod securityContext.runAsNonRoot to false/non-bool",
                blast=BLAST_MEDIUM,
                field_path="securityContext.runAsNonRoot",
                security=True,
            )
        if psc.get("runAsUser") == 0 and _is_strict_int(psc.get("runAsUser")):
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod securityContext.runAsUser to 0 (root)",
                blast=BLAST_MEDIUM,
                field_path="securityContext.runAsUser",
                security=True,
            )
        if psc.get("fsGroup") == 0 and _is_strict_int(psc.get("fsGroup")):
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod securityContext.fsGroup to 0 (root group)",
                blast=BLAST_MEDIUM,
                field_path="securityContext.fsGroup",
                security=True,
            )
        if psc.get("sysctls"):
            _as_list(ev, psc.get("sysctls"), "securityContext.sysctls")
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "configures pod kernel securityContext.sysctls",
                blast=BLAST_HIGH,
                field_path="securityContext.sysctls",
                security=True,
            )
        if psc.get("supplementalGroups"):
            sg = _as_list(ev, psc.get("supplementalGroups"), "securityContext.supplementalGroups")
            if sg and (0 in sg or before_pod_spec is None or sg != b_psc.get("supplementalGroups")):
                ev.add(
                    "SECURITY_CONTEXT_LOOSENED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    "sets pod securityContext.supplementalGroups",
                    blast=BLAST_MEDIUM,
                    field_path="securityContext.supplementalGroups",
                    security=True,
                )
        if psc.get("seLinuxOptions") and (before_pod_spec is None or psc.get("seLinuxOptions") != b_psc.get("seLinuxOptions")):
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod securityContext.seLinuxOptions",
                blast=BLAST_MEDIUM,
                field_path="securityContext.seLinuxOptions",
                security=True,
            )
        wo = psc.get("windowsOptions")
        if isinstance(wo, dict) and "hostProcess" in wo and _is_truthy_or_nonbool(wo.get("hostProcess")):
            host_escape = True
            ev.add(
                "HOST_NAMESPACE_ACCESS",
                DIM_SECURITY,
                RISK_CRITICAL if is_daemonset else RISK_HIGH,
                "sets pod securityContext.windowsOptions.hostProcess",
                blast=BLAST_CRITICAL if is_daemonset else BLAST_HIGH,
                field_path="securityContext.windowsOptions.hostProcess",
                security=True,
            )
        sp = psc.get("seccompProfile")
        if isinstance(sp, dict) and str(sp.get("type") or "") == "Unconfined":
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod seccompProfile.type to Unconfined",
                blast=BLAST_MEDIUM,
                field_path="securityContext.seccompProfile.type",
                security=True,
            )
        aa = psc.get("appArmorProfile")
        if isinstance(aa, dict) and str(aa.get("type") or "") == "Unconfined":
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                "sets pod appArmorProfile.type to Unconfined",
                blast=BLAST_MEDIUM,
                field_path="securityContext.appArmorProfile.type",
                security=True,
            )

    # Incomplete workload fragment check (e.g. Kustomize patch missing spec.template.spec.containers)
    raw_ctrs = pod_spec.get("containers")
    if not isinstance(raw_ctrs, list) or len(raw_ctrs) == 0:
        b_ctrs = b_ps.get("containers")
        if isinstance(b_ctrs, list) and len(b_ctrs) > 0:
            ev.add(
                "CONTAINER_REMOVED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                "removes all containers from workload PodSpec",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="containers",
            )
        else:
            ev.add(
                "INCOMPLETE_WORKLOAD_FRAGMENT",
                DIM_BLAST_RADIUS,
                RISK_UNKNOWN,
                f"{ev.ref.gvk_label()} {ev.ref.name!r} omits spec containers (possible unrendered Kustomize patch fragment)",
                blast=BLAST_UNKNOWN,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_UNKNOWN,
                field_path="containers",
                supported=False,
            )

    # Containers (regular, init, ephemeral)
    privileged_any = False
    for list_key in ("containers", "initContainers", "ephemeralContainers"):
        ctrs = _as_list(ev, pod_spec.get(list_key), list_key)
        seen_names: set[str] = set()
        for idx, ctr in enumerate(ctrs):
            if not isinstance(ctr, dict):
                ev.add(
                    "INVALID_CONTAINER_SPEC",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"{list_key}[{idx}] is not a mapping",
                    field_path=f"{list_key}[{idx}]",
                )
                continue
            cname = ctr.get("name")
            cimg = ctr.get("image")
            if not isinstance(cname, str) or not cname.strip() or not isinstance(cimg, str) or not cimg.strip():
                ev.add(
                    "INVALID_CONTAINER_SPEC",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"{list_key}[{idx}] is missing required name or image",
                    field_path=f"{list_key}[{idx}]",
                )
            elif cname in seen_names:
                ev.add(
                    "DUPLICATE_CONTAINER_NAME",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"{list_key} defines duplicate container name {cname!r}",
                    field_path=f"{list_key}[{idx}].name",
                    security=True,
                )
            else:
                seen_names.add(cname)
            label = cname if isinstance(cname, str) and cname.strip() else str(idx)

            # Secret env / envFrom & plaintext secret leaks
            for env_item in _as_list(ev, ctr.get("env"), f"{list_key}.{label}.env"):
                if not isinstance(env_item, dict):
                    continue
                vf = env_item.get("valueFrom")
                if isinstance(vf, dict) and isinstance(vf.get("secretKeyRef"), dict):
                    ev.add(
                        "SECRET_ENV_REF",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} mounts Secret via env.valueFrom.secretKeyRef",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.env",
                        security=True,
                    )
                val = env_item.get("value")
                if isinstance(val, str):
                    for _, pat_name in _scan_value_for_secrets(val):
                        ev.add(
                            "PLAINTEXT_SECRET_LEAK",
                            DIM_SECURITY,
                            RISK_CRITICAL,
                            f"plaintext {pat_name} detected in container {label!r} env {env_item.get('name')!r}",
                            blast=BLAST_HIGH,
                            revert=REVERT_IRREVERSIBLE,
                            field_path=f"{list_key}.{label}.env",
                            security=True,
                        )
            for ef in _as_list(ev, ctr.get("envFrom"), f"{list_key}.{label}.envFrom"):
                if isinstance(ef, dict) and isinstance(ef.get("secretRef"), dict):
                    ev.add(
                        "SECRET_ENV_REF",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} mounts Secret via envFrom.secretRef",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.envFrom",
                        security=True,
                    )

            # Host ports
            for port in _as_list(ev, ctr.get("ports"), f"{list_key}.{label}.ports"):
                if isinstance(port, dict) and port.get("hostPort"):
                    ev.add(
                        "HOST_PORT_BINDING",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} binds hostPort {port.get('hostPort')}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.ports",
                        security=True,
                    )

            # Container securityContext
            sc = _as_dict(ev, ctr.get("securityContext"), f"{list_key}.{label}.securityContext")
            if sc:
                if "privileged" in sc and _is_truthy_or_nonbool(sc.get("privileged")):
                    privileged_any = True
                    ev.add(
                        "PRIVILEGED_CONTAINER",
                        DIM_SECURITY,
                        RISK_CRITICAL if (is_daemonset or host_escape) else RISK_HIGH,
                        f"container {label!r} sets securityContext.privileged",
                        blast=BLAST_CRITICAL if (is_daemonset or host_escape) else BLAST_HIGH,
                        field_path=f"{list_key}.{label}.securityContext.privileged",
                        security=True,
                    )
                if "allowPrivilegeEscalation" in sc and _is_truthy_or_nonbool(sc.get("allowPrivilegeEscalation")):
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets allowPrivilegeEscalation",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.allowPrivilegeEscalation",
                        security=True,
                    )
                if "runAsNonRoot" in sc and _is_falsy_or_nonbool(sc.get("runAsNonRoot")):
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets runAsNonRoot to false/non-bool",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.runAsNonRoot",
                        security=True,
                    )
                if sc.get("runAsUser") == 0 and _is_strict_int(sc.get("runAsUser")):
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets runAsUser to 0 (root)",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.runAsUser",
                        security=True,
                    )
                if str(sc.get("procMount") or "") == "Unmasked":
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets procMount to Unmasked",
                        blast=BLAST_HIGH,
                        field_path=f"{list_key}.{label}.securityContext.procMount",
                        security=True,
                    )
                sp = sc.get("seccompProfile")
                if isinstance(sp, dict) and str(sp.get("type") or "") == "Unconfined":
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets seccompProfile.type to Unconfined",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.seccompProfile",
                        security=True,
                    )
                aa = sc.get("appArmorProfile")
                if isinstance(aa, dict) and str(aa.get("type") or "") == "Unconfined":
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets appArmorProfile.type to Unconfined",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.appArmorProfile",
                        security=True,
                    )
                if sc.get("seLinuxOptions"):
                    ev.add(
                        "SECURITY_CONTEXT_LOOSENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets securityContext.seLinuxOptions",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.seLinuxOptions",
                        security=True,
                    )
                c_wo = sc.get("windowsOptions")
                if isinstance(c_wo, dict) and "hostProcess" in c_wo and _is_truthy_or_nonbool(c_wo.get("hostProcess")):
                    host_escape = True
                    ev.add(
                        "HOST_NAMESPACE_ACCESS",
                        DIM_SECURITY,
                        RISK_CRITICAL if is_daemonset else RISK_HIGH,
                        f"container {label!r} sets securityContext.windowsOptions.hostProcess",
                        blast=BLAST_CRITICAL if is_daemonset else BLAST_HIGH,
                        field_path=f"{list_key}.{label}.securityContext.windowsOptions.hostProcess",
                        security=True,
                    )
                caps = _as_dict(ev, sc.get("capabilities"), f"{list_key}.{label}.securityContext.capabilities")
                added_raw = _as_list(ev, caps.get("add"), f"{list_key}.{label}.securityContext.capabilities.add")
                if added_raw:
                    added_caps = [str(c).upper() for c in added_raw]
                    crit_caps = any(c in ("ALL", "SYS_ADMIN", "SYS_PTRACE", "NET_ADMIN") for c in added_caps)
                    ev.add(
                        "LINUX_CAPABILITY_ADDED",
                        DIM_SECURITY,
                        RISK_CRITICAL if (crit_caps and (is_daemonset or host_escape)) else RISK_HIGH,
                        f"container {label!r} adds Linux capabilities {added_caps}",
                        blast=BLAST_HIGH if crit_caps else BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.securityContext.capabilities.add",
                        security=True,
                    )

            # Extended resources (GPU/TPU) & resource claims
            res = _as_dict(ev, ctr.get("resources"), f"{list_key}.{label}.resources")
            if res:
                if res.get("claims"):
                    _as_list(ev, res.get("claims"), f"{list_key}.{label}.resources.claims")
                    ev.add(
                        "EXTENDED_RESOURCE_REQUEST",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} attaches dynamic resources.claims",
                        blast=BLAST_HIGH,
                        field_path=f"{list_key}.{label}.resources.claims",
                        security=True,
                    )
                for section in ("requests", "limits"):
                    rmap = _as_dict(ev, res.get(section), f"{list_key}.{label}.resources.{section}")
                    for rkey in sorted(rmap.keys(), key=str):
                        if "/" in str(rkey) or "gpu" in str(rkey).lower() or "tpu" in str(rkey).lower():
                            ev.add(
                                "EXTENDED_RESOURCE_REQUEST",
                                DIM_BLAST_RADIUS,
                                RISK_HIGH,
                                f"container {label!r} specifies extended/accelerator resource {rkey!r}",
                                blast=BLAST_MEDIUM,
                                field_path=f"{list_key}.{label}.resources.{section}.{rkey}",
                            )

    if privileged_any and host_escape:
        ev.add(
            "NODE_ROOTKIT_POSTURE",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"combines privileged container with host namespace/path access on {ev.ref.gvk_label()} {ev.ref.name}",
            blast=BLAST_CRITICAL,
            security=True,
        )


def _check_rbac_superuser(ev: ResourceEval, obj: dict[str, Any]) -> None:
    """Detect cluster-admin/admin/edit bindings, wildcard rules, privilege escalation verbs, and broad subjects."""
    kind = ev.ref.kind
    in_system = ev.ref.namespace in SYSTEM_NAMESPACES
    if kind in ("ClusterRoleBinding", "RoleBinding"):
        role_ref = _as_dict(ev, obj.get("roleRef"), "roleRef")
        rname = str(role_ref.get("name") or "")
        if rname in _SUPERUSER_ROLES and (kind == "ClusterRoleBinding" or in_system):
            ev.add(
                "CRITICAL_PRIVILEGED_RBAC",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"{kind} {ev.ref.name!r} binds superuser role {rname!r}",
                blast=BLAST_CRITICAL,
                security=True,
            )
        elif kind == "ClusterRoleBinding" and rname in _CLUSTER_ADMIN_EQUIV_ROLES:
            ev.add(
                "CRITICAL_PRIVILEGED_RBAC",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"ClusterRoleBinding {ev.ref.name!r} grants cluster-wide built-in {rname!r} privileges (Secrets/Pods across all namespaces)",
                blast=BLAST_CRITICAL,
                security=True,
            )
        for subj in _as_list(ev, obj.get("subjects"), "subjects"):
            if not isinstance(subj, dict):
                continue
            sname = str(subj.get("name") or "")
            if sname in _SUPERUSER_SUBJECTS:
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} grants permissions to broad/unauthenticated subject {sname!r}",
                    blast=BLAST_CRITICAL,
                    security=True,
                )
    if kind in ("ClusterRole", "Role"):
        if kind == "ClusterRole" and obj.get("aggregationRule") is not None:
            agg = _as_dict(ev, obj.get("aggregationRule"), "aggregationRule")
            ev.add(
                "RBAC_AGGREGATION_RULE",
                DIM_SECURITY,
                RISK_HIGH,
                f"ClusterRole {ev.ref.name!r} defines dynamic aggregationRule",
                blast=BLAST_HIGH,
                field_path="aggregationRule",
                security=True,
            )
        for idx, rule in enumerate(_as_list(ev, obj.get("rules"), "rules")):
            if not isinstance(rule, dict):
                continue
            verbs = {str(v).lower() for v in _as_list(ev, rule.get("verbs"), f"rules[{idx}].verbs")}
            resources = {str(r).lower() for r in _as_list(ev, rule.get("resources"), f"rules[{idx}].resources")}
            non_res = _as_list(ev, rule.get("nonResourceURLs"), f"rules[{idx}].nonResourceURLs")
            is_cluster = kind == "ClusterRole" or in_system
            if verbs & _PRIV_ESCALATION_VERBS and (is_cluster or (resources & _SENSITIVE_RBAC_RESOURCES)):
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} rule[{idx}] grants wildcard or privilege-escalation verbs {sorted(verbs & _PRIV_ESCALATION_VERBS)}",
                    blast=BLAST_CRITICAL if is_cluster else BLAST_HIGH,
                    field_path=f"rules[{idx}]",
                    security=True,
                )
            elif is_cluster and (resources & _SENSITIVE_RBAC_RESOURCES) and (verbs - {"get", "list", "watch"} or "secrets" in resources or "*" in resources):
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} rule[{idx}] grants cluster-wide access to sensitive resources {sorted(resources & _SENSITIVE_RBAC_RESOURCES)}",
                    blast=BLAST_CRITICAL,
                    field_path=f"rules[{idx}]",
                    security=True,
                )
            elif "*" in [str(u) for u in non_res]:
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} rule[{idx}] grants wildcard nonResourceURLs",
                    blast=BLAST_CRITICAL,
                    field_path=f"rules[{idx}]",
                    security=True,
                )


def _check_cloud_iam(ev: ResourceEval, obj: dict[str, Any]) -> None:
    """Detect GCP KCC IAM superuser roles (`roles/owner`, `roles/editor`, `allUsers`)."""
    spec = _as_dict(ev, obj.get("spec"), "spec")
    if not spec:
        return
    roles_and_members: list[tuple[str, list[str]]] = []
    if isinstance(spec.get("role"), str):
        member = str(spec.get("member") or "")
        roles_and_members.append((spec["role"], [member] if member else []))
    for idx, binding in enumerate(_as_list(ev, spec.get("bindings"), "spec.bindings")):
        if isinstance(binding, dict):
            role = str(binding.get("role") or "")
            members = [str(m) for m in _as_list(ev, binding.get("members"), f"spec.bindings[{idx}].members")]
            roles_and_members.append((role, members))
    for role, members in roles_and_members:
        if role.lower() in _CRITICAL_IAM_ROLES:
            ev.add(
                "CRITICAL_CLOUD_IAM",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"grants privileged cloud IAM role {role!r} on {ev.ref.gvk_label()} {ev.ref.name!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_DIFFICULT,
                security=True,
            )
        for mem in members:
            if mem.lower() in _PUBLIC_IAM_MEMBERS:
                ev.add(
                    "CRITICAL_CLOUD_IAM_PUBLIC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"grants public cloud IAM access to {mem!r} on {ev.ref.gvk_label()} {ev.ref.name!r}",
                    blast=BLAST_CRITICAL,
                    revert=REVERT_DIFFICULT,
                    security=True,
                )


def _check_metadata_edits(
    ev: ResourceEval,
    before_obj: dict[str, Any],
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
    *,
    allow_low_metadata: bool,
) -> None:
    """Validate metadata.labels, metadata.annotations, finalizers, ownerReferences, and system metadata fields."""
    a_meta = after_obj.get("metadata")
    if isinstance(a_meta, dict):
        _as_dict(ev, a_meta.get("labels"), "metadata.labels")
        _as_dict(ev, a_meta.get("annotations"), "metadata.annotations")

    # Check production label / risk-class tampering explicitly
    b_labels = _meta_dict(before_obj, "labels")
    a_labels = _meta_dict(after_obj, "labels")
    b_annos = _meta_dict(before_obj, "annotations")
    a_annos = _meta_dict(after_obj, "annotations")

    if _is_production_labels(b_labels) and not _is_production_labels(a_labels):
        ev.add(
            "RISK_METADATA_TAMPERING",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"removes or downgrades production environment label on {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="metadata.labels",
            security=True,
        )

    old_override = b_annos.get("kube-agents/risk-class")
    new_override = a_annos.get("kube-agents/risk-class")
    if old_override is not None and new_override != old_override:
        ev.add(
            "RISK_METADATA_TAMPERING",
            DIM_SECURITY,
            RISK_CRITICAL,
            "modifies or removes kube-agents/risk-class override annotation",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="metadata.annotations.kube-agents/risk-class",
            security=True,
        )

    for d in diffs:
        if not d.tokens or d.tokens[0] != "metadata":
            continue
        sub = d.tokens[1] if len(d.tokens) > 1 else ""
        if sub in ("finalizers", "ownerReferences"):
            ev.add(
                "LIFECYCLE_METADATA_MODIFIED",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"modifies metadata.{sub} on {ev.ref.gvk_label()} {ev.ref.name!r}",
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DESTRUCTIVE if d.op == "remove" else DISRUPT_NONE,
                field_path=d.dotted,
            )
        elif sub in ("labels", "annotations"):
            key = d.tokens[2] if len(d.tokens) > 2 else ""
            if isinstance(d.after, str):
                for _, pat_name in _scan_value_for_secrets(d.after):
                    ev.add(
                        "PLAINTEXT_SECRET_LEAK",
                        DIM_SECURITY,
                        RISK_CRITICAL,
                        f"plaintext {pat_name} detected in metadata.{sub}.{key}",
                        blast=BLAST_HIGH,
                        revert=REVERT_IRREVERSIBLE,
                        field_path=d.dotted,
                        security=True,
                    )
            if any(sn in key.lower() for sn in _INGRESS_SNIPPET_KEYS):
                ev.add(
                    "INGRESS_SNIPPET_INJECTION",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"sets raw proxy snippet annotation {key!r}",
                    blast=BLAST_CRITICAL,
                    field_path=d.dotted,
                    security=True,
                )
            elif _is_protected_metadata_key(key):
                ev.add(
                    "PROTECTED_METADATA_MODIFIED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"modifies protected metadata.{sub} key {key!r}",
                    blast=BLAST_MEDIUM,
                    field_path=d.dotted,
                    security=True,
                )
            elif not allow_low_metadata or key not in ALLOWED_LOW_RISK_METADATA_KEYS:
                ev.add(
                    "UNALLOWLISTED_METADATA_EDIT",
                    DIM_BLAST_RADIUS,
                    RISK_MEDIUM,
                    f"modifies metadata.{sub} key {key!r} outside low-risk allowlist",
                    field_path=d.dotted,
                )
            else:
                ev.add(
                    "ALLOWLISTED_METADATA_EDIT",
                    DIM_BLAST_RADIUS,
                    RISK_LOW,
                    f"updates allowlisted metadata.{sub} key {key!r}",
                    field_path=d.dotted,
                )
        elif sub and sub not in _ALLOWED_METADATA_KEYS:
            ev.add(
                "METADATA_SYSTEM_FIELD_EDIT",
                DIM_SECURITY,
                RISK_HIGH,
                f"modifies system or unexpected metadata field metadata.{sub}",
                blast=BLAST_MEDIUM,
                field_path=d.dotted,
                security=True,
            )


def _check_workload_modify(
    ev: ResourceEval,
    before_obj: dict[str, Any],
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Evaluate modifications to Deployment, StatefulSet, DaemonSet, ReplicaSet, Job, CronJob, Pod."""
    kind = ev.ref.kind
    is_daemonset = kind == "DaemonSet"
    b_spec = before_obj.get("spec") if isinstance(before_obj.get("spec"), dict) else {}
    a_spec = after_obj.get("spec") if isinstance(after_obj.get("spec"), dict) else {}

    # 1. Immutable fields & strategy
    for d in diffs:
        t = d.tokens
        if len(t) >= 2 and t[0] == "spec" and t[1] == "selector":
            ev.add(
                "IMMUTABLE_FIELD_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies immutable {kind} spec.selector",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path=d.dotted,
            )
        if kind == "StatefulSet" and len(t) >= 2 and t[0] == "spec" and t[1] in ("serviceName", "podManagementPolicy", "volumeClaimTemplates"):
            ev.add(
                "IMMUTABLE_FIELD_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies immutable StatefulSet spec.{t[1]}",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path=d.dotted,
            )
        if kind == "Job" and len(t) >= 2 and t[0] == "spec" and t[1] == "template":
            ev.add(
                "IMMUTABLE_FIELD_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                "modifies immutable Job spec.template",
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path=d.dotted,
            )

    if kind == "Deployment":
        a_strat = a_spec.get("strategy")
        b_strat = b_spec.get("strategy")
        if a_strat != b_strat:
            if a_strat is not None and not isinstance(a_strat, dict):
                ev.add(
                    "MALFORMED_FIELD_TYPE",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    "Deployment spec.strategy is not a mapping",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.strategy",
                )
            elif isinstance(a_strat, dict) and str(a_strat.get("type") or "") == "Recreate":
                ev.add(
                    "RECREATE_STRATEGY_SET",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    "switches Deployment rollout strategy to Recreate (causes downtime)",
                    revert=REVERT_MODERATE,
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.strategy.type",
                )
            else:
                ru = a_strat.get("rollingUpdate") if isinstance(a_strat, dict) else None
                max_unavail = ru.get("maxUnavailable") if isinstance(ru, dict) else None
                aggressive_unavail = False
                if isinstance(max_unavail, str) and max_unavail.strip().endswith("%"):
                    try:
                        pct = float(max_unavail.strip()[:-1])
                        if pct > 50.0 or not math.isfinite(pct):
                            aggressive_unavail = True
                    except ValueError:
                        aggressive_unavail = True
                elif max_unavail is not None and not _is_strict_int(max_unavail):
                    aggressive_unavail = True
                elif _is_strict_int(max_unavail) and max_unavail > 1:
                    b_rep_est = b_spec.get("replicas", 1)
                    if _is_strict_int(b_rep_est) and b_rep_est > 0 and max_unavail / b_rep_est > 0.5:
                        aggressive_unavail = True
                if aggressive_unavail:
                    ev.add(
                        "AGGRESSIVE_ROLLOUT_STRATEGY",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"sets aggressive Deployment rollingUpdate.maxUnavailable={max_unavail!r}",
                        revert=REVERT_MODERATE,
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.strategy.rollingUpdate.maxUnavailable",
                    )
                else:
                    ev.add(
                        "ROLLOUT_STRATEGY_MODIFIED",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"modifies Deployment {ev.ref.name!r} rollout strategy",
                        field_path="spec.strategy",
                    )

    if kind == "StatefulSet":
        ret = a_spec.get("persistentVolumeClaimRetentionPolicy")
        b_ret = b_spec.get("persistentVolumeClaimRetentionPolicy")
        if isinstance(ret, dict) and str(ret.get("whenDeleted") or "") == "Delete":
            if not (isinstance(b_ret, dict) and str(b_ret.get("whenDeleted") or "") == "Delete"):
                ev.add(
                    "STATEFULSET_PVC_DELETE_POLICY",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    "sets StatefulSet persistentVolumeClaimRetentionPolicy.whenDeleted to Delete",
                    revert=REVERT_DIFFICULT,
                    disrupt=DISRUPT_DESTRUCTIVE,
                    field_path="spec.persistentVolumeClaimRetentionPolicy.whenDeleted",
                )

    if kind in ("CronJob", "Job"):
        if "suspend" in a_spec or "suspend" in b_spec:
            a_susp = a_spec.get("suspend")
            b_susp = b_spec.get("suspend")
            if a_susp != b_susp:
                if a_susp is not None and not isinstance(a_susp, bool):
                    ev.add(
                        "MALFORMED_FIELD_TYPE",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"sets non-boolean spec.suspend={a_susp!r} on {kind} {ev.ref.name!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.suspend",
                    )
                elif a_susp is True:
                    ev.add(
                        "CRONJOB_SUSPENDED",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"suspends {kind} {ev.ref.name!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.suspend",
                    )
                else:
                    ev.add(
                        "CRONJOB_RESUMED",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"resumes suspended {kind} {ev.ref.name!r}",
                        field_path="spec.suspend",
                    )

    if kind == "Job":
        ev.add(
            "JOB_EFFECTFUL_CHANGE",
            DIM_REVERTABILITY,
            RISK_HIGH,
            f"modifies batch/Job {ev.ref.name!r} (one-off executions are not cleanly revertable)",
            revert=REVERT_IRREVERSIBLE,
        )

    # 2. Replica changes (strict int validation; removing `replicas` defaults to 1 in K8s)
    b_has_rep = "replicas" in b_spec and b_spec["replicas"] is not None
    a_has_rep = "replicas" in a_spec and a_spec["replicas"] is not None
    b_rep_raw = b_spec.get("replicas", 1) if b_has_rep else 1
    a_rep_raw = a_spec.get("replicas", 1) if a_has_rep else 1
    invalid_rep = (a_has_rep and not _is_strict_int(a_rep_raw)) or (_is_strict_int(a_rep_raw) and a_rep_raw < 0)
    b_rep = b_rep_raw if _is_strict_int(b_rep_raw) and b_rep_raw >= 0 else 1
    a_rep = a_rep_raw if _is_strict_int(a_rep_raw) and a_rep_raw >= 0 else 1
    if invalid_rep:
        ev.add(
            "INVALID_REPLICAS",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sets invalid non-integer or negative spec.replicas={a_rep_raw!r} on {kind} {ev.ref.name!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.replicas",
        )
    elif "replicas" in b_spec or "replicas" in a_spec:
        if b_rep != a_rep:
            if a_rep <= 0:
                ev.add(
                    "WORKLOAD_SCALED_TO_ZERO",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"scales {kind} {ev.ref.name!r} from {b_rep} to {a_rep}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.replicas",
                )
            elif a_rep < b_rep:
                drop_frac = (b_rep - a_rep) / max(b_rep, 1)
                if drop_frac > 0.5 or (b_rep >= 2 and a_rep == 1):
                    ev.add(
                        "WORKLOAD_STEEP_SCALE_DOWN",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"reduces {kind} {ev.ref.name!r} replicas sharply from {b_rep} to {a_rep}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.replicas",
                    )
                else:
                    ev.add(
                        "WORKLOAD_SCALE_DOWN",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"reduces {kind} {ev.ref.name!r} replicas from {b_rep} to {a_rep}",
                        field_path="spec.replicas",
                    )
            else:
                delta = a_rep - b_rep
                factor = a_rep / max(b_rep, 1)
                if kind == "StatefulSet" and isinstance(a_spec.get("volumeClaimTemplates"), list) and a_spec["volumeClaimTemplates"]:
                    ret = a_spec.get("persistentVolumeClaimRetentionPolicy")
                    when_scaled = str(ret.get("whenScaled") or "Retain") if isinstance(ret, dict) else "Retain"
                    if when_scaled != "Delete":
                        ev.add(
                            "STATEFULSET_SCALE_RETAINS_PVCS",
                            DIM_REVERTABILITY,
                            RISK_MEDIUM,
                            f"scales StatefulSet {ev.ref.name!r} ({b_rep} -> {a_rep}) with retained PVCs",
                            revert=REVERT_MODERATE,
                            field_path="spec.replicas",
                        )
                    else:
                        ev.add(
                            "WORKLOAD_REPLICA_BUMP",
                            DIM_BLAST_RADIUS,
                            RISK_LOW if (delta <= MAX_LOW_REPLICA_DELTA and factor <= MAX_LOW_REPLICA_FACTOR and a_rep <= MAX_LOW_REPLICA_TOTAL) else RISK_MEDIUM,
                            f"scales {kind} {ev.ref.name!r} replicas from {b_rep} to {a_rep}",
                            field_path="spec.replicas",
                        )
                elif delta <= MAX_LOW_REPLICA_DELTA and factor <= MAX_LOW_REPLICA_FACTOR and a_rep <= MAX_LOW_REPLICA_TOTAL:
                    ev.add(
                        "WORKLOAD_REPLICA_BUMP",
                        DIM_BLAST_RADIUS,
                        RISK_LOW,
                        f"modest replica increase from {b_rep} to {a_rep} within safe bounds",
                        field_path="spec.replicas",
                    )
                else:
                    ev.add(
                        "WORKLOAD_LARGE_SCALE_UP",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"large replica increase on {kind} {ev.ref.name!r} from {b_rep} to {a_rep}",
                        blast=BLAST_MEDIUM if a_rep > MAX_LOW_REPLICA_TOTAL else BLAST_LOW,
                        field_path="spec.replicas",
                    )

    # 3. Pod template label removal (detaches pods from Services/PDBs/NetworkPolicies)
    b_tmeta = _extract_pod_template_meta(kind, before_obj)
    a_tmeta = _extract_pod_template_meta(kind, after_obj)
    b_tlbl = b_tmeta.get("labels") if isinstance(b_tmeta.get("labels"), dict) else {}
    a_tlbl = a_tmeta.get("labels") if isinstance(a_tmeta.get("labels"), dict) else {}
    for k, old_v in sorted(b_tlbl.items(), key=str):
        if k not in a_tlbl or a_tlbl[k] != old_v:
            ev.add(
                "POD_TEMPLATE_LABEL_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"changes or removes pod template label {k!r} (may detach Service/PDB/NetworkPolicy selectors)",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path=f"spec.template.metadata.labels.{k}",
            )

    # 4. PodSpec posture & container-level diffs
    b_ps = _extract_pod_spec(kind, before_obj) or {}
    a_ps = _extract_pod_spec(kind, after_obj) or {}
    _check_pod_spec_posture(ev, a_ps, before_pod_spec=b_ps, is_daemonset=is_daemonset)

    # DaemonSet always fans out across every matching node
    if is_daemonset:
        ev.add(
            "DAEMONSET_NODE_FANOUT",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"modifies DaemonSet {ev.ref.name!r} (rolls out across every matching cluster node)",
            blast=BLAST_HIGH,
        )

    # Track per-container diffs (images, resources, command/args/lifecycle, removals/additions)
    handled_prefixes: set[tuple[str, ...]] = {
        ("metadata",),
        ("spec", "replicas"),
        ("spec", "selector"),
        ("spec", "strategy"),
        ("spec", "suspend"),
    }
    pod_prefix: tuple[str, ...]
    if kind == "Pod":
        pod_prefix = ("spec",)
    elif kind == "CronJob":
        pod_prefix = ("spec", "jobTemplate", "spec", "template", "spec")
    else:
        pod_prefix = ("spec", "template", "spec")

    # Compare container lists for additions, removals, image changes, resources, command/args
    has_duplicate_or_unnamed_containers = False
    for list_key in ("containers", "initContainers", "ephemeralContainers"):
        b_list = b_ps.get(list_key) if isinstance(b_ps.get(list_key), list) else []
        a_list = a_ps.get(list_key) if isinstance(a_ps.get(list_key), list) else []
        b_by_name: dict[str, dict[str, Any]] = {}
        a_by_name: dict[str, dict[str, Any]] = {}
        for c in b_list:
            if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
                has_duplicate_or_unnamed_containers = True
                continue
            if c["name"] in b_by_name:
                has_duplicate_or_unnamed_containers = True
                continue
            b_by_name[c["name"]] = c
        for c in a_list:
            if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
                has_duplicate_or_unnamed_containers = True
                continue
            if c["name"] in a_by_name:
                has_duplicate_or_unnamed_containers = True
                continue
            a_by_name[c["name"]] = c

        for cname in sorted(set(b_by_name.keys()) | set(a_by_name.keys())):
            if cname not in b_by_name:
                ev.add(
                    "CONTAINER_ADDED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"adds {list_key} entry {cname!r}",
                    field_path=f"{'.'.join(pod_prefix)}.{list_key}.{cname}",
                    security=True,
                )
                continue
            if cname not in a_by_name:
                ev.add(
                    "CONTAINER_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes {list_key} entry {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{'.'.join(pod_prefix)}.{list_key}.{cname}",
                )
                continue

            bc, ac = b_by_name[cname], a_by_name[cname]
            c_path = f"{'.'.join(pod_prefix)}.{list_key}.{cname}"

            # Container securityContext hardening removal check (including whole block removal)
            b_csc = bc.get("securityContext") if isinstance(bc.get("securityContext"), dict) else {}
            a_csc = ac.get("securityContext") if isinstance(ac.get("securityContext"), dict) else {}
            if b_csc:
                _check_security_hardening_removed(ev, b_csc, a_csc, f"{c_path}.securityContext")

            # Image diff
            b_img = str(bc.get("image") or "")
            a_img = str(ac.get("image") or "")
            if b_img != a_img:
                b_repo, _, _ = split_image_ref(b_img)
                a_repo, _, _ = split_image_ref(a_img)
                if b_repo != a_repo:
                    ev.add(
                        "IMAGE_REPOSITORY_CHANGED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"changes container {cname!r} image repository from {b_repo!r} to {a_repo!r}",
                        field_path=f"{c_path}.image",
                        security=True,
                    )
                elif not is_immutable_image_pin(a_img):
                    ev.add(
                        "UNPINNED_IMAGE_TAG",
                        DIM_REVERTABILITY,
                        RISK_MEDIUM,
                        f"sets container {cname!r} image to mutable/unpinned reference {a_img!r}",
                        revert=REVERT_UNKNOWN,
                        field_path=f"{c_path}.image",
                    )
                else:
                    ev.add(
                        "PINNED_IMAGE_UPDATE",
                        DIM_BLAST_RADIUS,
                        RISK_LOW,
                        f"updates container {cname!r} image to pinned reference {a_img!r}",
                        field_path=f"{c_path}.image",
                    )

            # Command / args / workingDir / lifecycle
            for exec_key in ("command", "args", "workingDir", "lifecycle"):
                if bc.get(exec_key) != ac.get(exec_key):
                    ev.add(
                        "CONTAINER_ENTRYPOINT_CHANGED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"modifies container {cname!r} {exec_key}",
                        field_path=f"{c_path}.{exec_key}",
                        security=True,
                    )

            # Health probe removals
            for probe_key in ("readinessProbe", "livenessProbe", "startupProbe"):
                if bc.get(probe_key) is not None and ac.get(probe_key) is None:
                    ev.add(
                        "HEALTH_PROBE_REMOVED",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"removes {probe_key} from container {cname!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{c_path}.{probe_key}",
                    )

            # Resources requests/limits
            _check_container_resources(ev, cname, c_path, bc, ac, b_rep=b_rep, a_rep=a_rep)

            # Removals of ports / volumeMounts / env
            for sub_list_key in ("ports", "volumeMounts", "env"):
                b_items = bc.get(sub_list_key) if isinstance(bc.get(sub_list_key), list) else []
                a_items = ac.get(sub_list_key) if isinstance(ac.get(sub_list_key), list) else []
                if len(a_items) < len(b_items):
                    ev.add(
                        "CONTAINER_COMPONENT_REMOVED",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"removes entries from container {cname!r} {sub_list_key}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{c_path}.{sub_list_key}",
                    )

    # Check scheduling changes on DaemonSet (`nodeSelector`, `affinity`, `tolerations`)
    if is_daemonset:
        for sched_key in ("nodeSelector", "affinity", "tolerations"):
            if b_ps.get(sched_key) != a_ps.get(sched_key):
                ev.add(
                    "DAEMONSET_SCHEDULING_CHANGED",
                    DIM_BLAST_RADIUS,
                    RISK_HIGH,
                    f"modifies DaemonSet {sched_key} across cluster nodes",
                    blast=BLAST_HIGH,
                    disrupt=DISRUPT_DISRUPTIVE if (b_ps.get(sched_key) and not a_ps.get(sched_key)) else DISRUPT_NONE,
                    field_path=f"spec.template.spec.{sched_key}",
                )

    # Volume removals (safe against unhashable non-string volume names)
    b_vols = b_ps.get("volumes") if isinstance(b_ps.get("volumes"), list) else []
    a_vols = a_ps.get("volumes") if isinstance(a_ps.get("volumes"), list) else []
    b_vnames = {v["name"] for v in b_vols if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]}
    a_vnames = {v["name"] for v in a_vols if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]}
    if b_vnames - a_vnames:
        ev.add(
            "VOLUME_REMOVED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"removes volumes {sorted(b_vnames - a_vnames)}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{'.'.join(pod_prefix)}.volumes",
        )

    # ServiceAccount change on PodSpec
    if b_ps.get("serviceAccountName") != a_ps.get("serviceAccountName") or b_ps.get("serviceAccount") != a_ps.get("serviceAccount"):
        ev.add(
            "SERVICE_ACCOUNT_CHANGED",
            DIM_SECURITY,
            RISK_HIGH,
            f"changes PodSpec serviceAccountName from {b_ps.get('serviceAccountName')!r} to {a_ps.get('serviceAccountName')!r}",
            blast=BLAST_MEDIUM,
            field_path=f"{'.'.join(pod_prefix)}.serviceAccountName",
            security=True,
        )

    # Catch-all for any other unallowlisted field changes -> at least `medium`
    for d in diffs:
        t = d.tokens
        if any(t[: len(p)] == p for p in handled_prefixes):
            continue
        # Only skip container `image` and `resources.{requests,limits}.{cpu,memory}`
        # when container names are clean & unique (never skip `resources.claims`,
        # `ephemeral-storage`, `hugepages-*`, or duplicate-named containers).
        if not has_duplicate_or_unnamed_containers and t[: len(pod_prefix)] == pod_prefix and len(t) >= len(pod_prefix) + 3:
            sub = t[len(pod_prefix)]
            if sub in ("containers", "initContainers", "ephemeralContainers"):
                leaf = t[len(pod_prefix) + 2]
                if leaf == "image" and len(t) == len(pod_prefix) + 3:
                    continue
                if (
                    leaf == "resources"
                    and len(t) == len(pod_prefix) + 5
                    and t[len(pod_prefix) + 3] in ("requests", "limits")
                    and t[len(pod_prefix) + 4] in ("cpu", "memory")
                ):
                    continue
        if ev.risk_level == RISK_LOW or (
            t[: len(pod_prefix)] == pod_prefix
            and len(t) >= len(pod_prefix) + 4
            and t[len(pod_prefix) + 2] == "resources"
            and not (t[len(pod_prefix) + 3] in ("requests", "limits") and len(t) == len(pod_prefix) + 5 and t[len(pod_prefix) + 4] in ("cpu", "memory"))
        ):
            ev.add(
                "UNALLOWLISTED_FIELD_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"modifies field {d.dotted!r} outside the low-risk allowlist",
                field_path=d.dotted,
            )


def _check_container_resources(
    ev: ResourceEval,
    cname: str,
    c_path: str,
    bc: dict[str, Any],
    ac: dict[str, Any],
    *,
    b_rep: int,
    a_rep: int,
) -> None:
    b_res = bc.get("resources") if isinstance(bc.get("resources"), dict) else {}
    a_res = ac.get("resources") if isinstance(ac.get("resources"), dict) else {}
    b_req = b_res.get("requests") if isinstance(b_res.get("requests"), dict) else {}
    a_req = a_res.get("requests") if isinstance(a_res.get("requests"), dict) else {}
    b_lim = b_res.get("limits") if isinstance(b_res.get("limits"), dict) else {}
    a_lim = a_res.get("limits") if isinstance(a_res.get("limits"), dict) else {}

    # Check request <= limit on `after`
    a_cpu_req = parse_cpu_millicores(a_req.get("cpu"))
    a_cpu_lim = parse_cpu_millicores(a_lim.get("cpu"))
    a_mem_req = parse_memory_bytes(a_req.get("memory"))
    a_mem_lim = parse_memory_bytes(a_lim.get("memory"))

    if a_cpu_req is not None and a_cpu_lim is not None and a_cpu_req > a_cpu_lim:
        ev.add(
            "RESOURCE_REQUEST_EXCEEDS_LIMIT",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"container {cname!r} CPU request exceeds limit",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{c_path}.resources",
        )
    if a_mem_req is not None and a_mem_lim is not None and a_mem_req > a_mem_lim:
        ev.add(
            "RESOURCE_REQUEST_EXCEEDS_LIMIT",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"container {cname!r} memory request exceeds limit",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{c_path}.resources",
        )

    # Track compound PR-wide capacity delta (replicas * requests)
    b_cpu_req = parse_cpu_millicores(b_req.get("cpu")) or 0.0
    b_mem_req = parse_memory_bytes(b_req.get("memory")) or 0.0
    ev.cpu_delta_m += ((a_cpu_req or 0.0) * a_rep) - (b_cpu_req * b_rep)
    ev.mem_delta_bytes += ((a_mem_req or 0.0) * a_rep) - (b_mem_req * b_rep)

    for section, b_map, a_map in (("requests", b_req, a_req), ("limits", b_lim, a_lim)):
        for rname, parser_fn, floor_val, max_val in (
            ("cpu", parse_cpu_millicores, MIN_SAFE_CPU_M, MAX_LOW_CONTAINER_CPU_M),
            ("memory", parse_memory_bytes, MIN_SAFE_MEM_B, MAX_LOW_CONTAINER_MEM_B),
        ):
            bv_raw = b_map.get(rname)
            av_raw = a_map.get(rname)
            if bv_raw == av_raw:
                continue
            bv = parser_fn(bv_raw) if bv_raw is not None else None
            av = parser_fn(av_raw) if av_raw is not None else None
            fp = f"{c_path}.resources.{section}.{rname}"
            if av_raw is not None and av is None:
                ev.add(
                    "INVALID_RESOURCE_QUANTITY",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"unparseable {rname} quantity {av_raw!r} on container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=fp,
                )
            elif bv is not None and av is None:
                ev.add(
                    "CONTAINER_RESOURCE_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes {section}.{rname} on container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=fp,
                )
            elif bv is None and av is not None:
                if av < floor_val:
                    ev.add(
                        "CONTAINER_RESOURCE_BELOW_FLOOR",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"sets {section}.{rname} below safe floor on container {cname!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=fp,
                    )
                elif av > max_val:
                    ev.add(
                        "CONTAINER_RESOURCE_LARGE_INCREASE",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"sets large {section}.{rname}={av_raw!r} on container {cname!r}",
                        field_path=fp,
                    )
                else:
                    ev.add(
                        "CONTAINER_RESOURCE_ADJUSTMENT",
                        DIM_BLAST_RADIUS,
                        RISK_LOW,
                        f"sets bounded {section}.{rname}={av_raw!r} on container {cname!r}",
                        field_path=fp,
                    )
            elif bv is not None and av is not None:
                if av < bv:
                    drop = (bv - av) / max(bv, 1.0)
                    if drop > 0.5 or av < floor_val:
                        ev.add(
                            "CONTAINER_RESOURCE_STEEP_REDUCTION",
                            DIM_DISRUPTIVENESS,
                            RISK_HIGH,
                            f"sharply reduces {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=fp,
                        )
                    else:
                        ev.add(
                            "CONTAINER_RESOURCE_REDUCED",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"reduces {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            field_path=fp,
                        )
                else:
                    factor = av / max(bv, 1.0)
                    if factor <= MAX_LOW_RESOURCE_FACTOR and av <= max_val:
                        ev.add(
                            "CONTAINER_RESOURCE_ADJUSTMENT",
                            DIM_BLAST_RADIUS,
                            RISK_LOW,
                            f"modestly increases {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            field_path=fp,
                        )
                    else:
                        ev.add(
                            "CONTAINER_RESOURCE_LARGE_INCREASE",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"large increase of {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            field_path=fp,
                        )


_CONFIGMAP_ALLOWED_TOP_KEYS = frozenset({"apiVersion", "kind", "metadata", "data", "binaryData", "immutable"})


def _check_configmap(
    ev: ResourceEval,
    action: str,
    before_obj: dict[str, Any] | None,
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Evaluate ConfigMap additions and edits (secret leak defense, immutable ratchet, key removal)."""
    for top_k in sorted(after_obj.keys(), key=str):
        if top_k not in _CONFIGMAP_ALLOWED_TOP_KEYS:
            ev.add(
                "CONFIGMAP_UNEXPECTED_FIELD",
                DIM_SECURITY,
                RISK_HIGH,
                f"ConfigMap {ev.ref.name!r} sets unexpected top-level field {top_k!r}",
                field_path=str(top_k),
                security=True,
            )

    imm_val = after_obj.get("immutable")
    if imm_val is not None and not isinstance(imm_val, bool):
        ev.add(
            "MALFORMED_FIELD_TYPE",
            DIM_SECURITY,
            RISK_HIGH,
            f"ConfigMap {ev.ref.name!r} sets non-boolean immutable={imm_val!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="immutable",
            security=True,
        )
    elif action == ACTION_ADD and imm_val is True:
        ev.add(
            "CONFIGMAP_IMMUTABLE_CREATED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"creates immutable ConfigMap {ev.ref.name!r} (cannot be updated in place)",
            revert=REVERT_DIFFICULT,
            field_path="immutable",
        )

    for field_name in ("data", "binaryData"):
        raw_amap = after_obj.get(field_name)
        if raw_amap is None:
            continue
        amap = _as_dict(ev, raw_amap, field_name)
        if not amap:
            continue
        bmap = before_obj.get(field_name) if isinstance(before_obj, dict) and isinstance(before_obj.get(field_name), dict) else {}
        for k in sorted(amap.keys(), key=str):
            ks = str(k)
            val = amap[k]
            # Always scan added/modified keys for credential leaks, non-string types, and secret key names
            if action == ACTION_ADD or bmap.get(k) != val:
                if _SECRET_KEY_NAME_RE.search(ks):
                    ev.add(
                        "PLAINTEXT_SECRET_KEY_IN_CONFIGMAP",
                        DIM_SECURITY,
                        RISK_CRITICAL,
                        f"ConfigMap {ev.ref.name!r} writes secret-named key {ks!r}",
                        blast=BLAST_HIGH,
                        revert=REVERT_IRREVERSIBLE,
                        field_path=f"{field_name}.{ks}",
                        security=True,
                    )
                if _DANGEROUS_ENV_KEY_RE.match(ks):
                    ev.add(
                        "DANGEROUS_RUNTIME_CONFIG_KEY",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"ConfigMap {ev.ref.name!r} sets security-sensitive runtime key {ks!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{field_name}.{ks}",
                        security=True,
                    )
                for _, pat_name in _scan_value_for_secrets(val):
                    ev.add(
                        "PLAINTEXT_SECRET_LEAK",
                        DIM_SECURITY,
                        RISK_CRITICAL,
                        f"plaintext {pat_name} detected in ConfigMap {ev.ref.name!r} key {ks!r}",
                        blast=BLAST_HIGH,
                        revert=REVERT_IRREVERSIBLE,
                        field_path=f"{field_name}.{ks}",
                        security=True,
                    )
                if not isinstance(val, str):
                    ev.add(
                        "CONFIGMAP_NON_STRING_VALUE",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"ConfigMap {ev.ref.name!r} {field_name}.{ks} has non-string value type {type(val).__name__}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{field_name}.{ks}",
                        security=True,
                    )
                elif field_name == "binaryData":
                    ev.add(
                        "CONFIGMAP_BINARY_DATA",
                        DIM_BLAST_RADIUS,
                        RISK_MEDIUM,
                        f"ConfigMap {ev.ref.name!r} writes opaque binaryData key {ks!r}",
                        field_path=f"binaryData.{ks}",
                    )
                else:
                    is_complex = (
                        "\n" in val
                        or "://" in val
                        or ks.lower().endswith((".yaml", ".yml", ".json", ".sh", ".conf", ".ini", ".toml", ".xml", ".sql"))
                    )
                    if action == ACTION_MODIFY and k in bmap:
                        if val == "" and bmap.get(k):
                            ev.add(
                                "CONFIGMAP_VALUE_WIPED",
                                DIM_DISRUPTIVENESS,
                                RISK_HIGH,
                                f"wipes ConfigMap {ev.ref.name!r} key {ks!r} to empty string",
                                disrupt=DISRUPT_DISRUPTIVE,
                                field_path=f"data.{ks}",
                            )
                        else:
                            ev.add(
                                "CONFIGMAP_KEY_MODIFIED",
                                DIM_BLAST_RADIUS,
                                RISK_MEDIUM,
                                f"modifies existing ConfigMap {ev.ref.name!r} key {ks!r}",
                                field_path=f"data.{ks}",
                            )
                    elif is_complex:
                        ev.add(
                            "CONFIGMAP_STRUCTURED_OR_URL_VALUE",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"adds structured/multiline/URL value to ConfigMap {ev.ref.name!r} key {ks!r}",
                            field_path=f"data.{ks}",
                        )
                    else:
                        ev.add(
                            "CONFIGMAP_SAFE_SCALAR_ADDED",
                            DIM_BLAST_RADIUS,
                            RISK_LOW,
                            f"adds simple scalar key {ks!r} to ConfigMap {ev.ref.name!r}",
                            field_path=f"data.{ks}",
                        )

    if action == ACTION_MODIFY and before_obj is not None:
        if before_obj.get("immutable") is True:
            ev.add(
                "IMMUTABLE_CONFIGMAP_MODIFIED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies immutable ConfigMap {ev.ref.name!r}",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path="immutable",
            )
        elif after_obj.get("immutable") is True:
            ev.add(
                "CONFIGMAP_IMMUTABLE_RATCHET",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"sets immutable: true on ConfigMap {ev.ref.name!r} (one-way ratchet)",
                revert=REVERT_DIFFICULT,
                field_path="immutable",
            )
        for field_name in ("data", "binaryData"):
            bmap = before_obj.get(field_name) if isinstance(before_obj.get(field_name), dict) else {}
            amap = after_obj.get(field_name) if isinstance(after_obj.get(field_name), dict) else {}
            for k in sorted(set(bmap.keys()) - set(amap.keys()), key=str):
                ev.add(
                    "CONFIGMAP_KEY_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_MEDIUM,
                    f"removes key {k!r} from ConfigMap {ev.ref.name!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{field_name}.{k}",
                )
    if action == ACTION_ADD and not ev.reasons:
        ev.add(
            "CONFIGMAP_CREATED",
            DIM_BLAST_RADIUS,
            RISK_LOW,
            f"creates empty ConfigMap {ev.ref.name!r}",
        )


def _check_pdb(ev: ResourceEval, action: str, obj: dict[str, Any]) -> None:
    """Evaluate PodDisruptionBudget creation or modification."""
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    sel = spec.get("selector")
    empty_sel = not isinstance(sel, dict) or (not sel.get("matchLabels") and not sel.get("matchExpressions"))
    negative_only_sel = False
    if isinstance(sel, dict) and not sel.get("matchLabels") and isinstance(sel.get("matchExpressions"), list):
        ops = {str(e.get("operator") or "") for e in sel["matchExpressions"] if isinstance(e, dict)}
        if ops and ops.issubset({"NotIn", "DoesNotExist"}):
            negative_only_sel = True

    max_unavail = spec.get("maxUnavailable")
    min_avail = spec.get("minAvailable")
    blocks_drains = (
        max_unavail in (0, "0", "0%")
        or min_avail in ("100%",)
    )
    if empty_sel or negative_only_sel or blocks_drains:
        ev.add(
            "PDB_DRAIN_DEADLOCK_RISK",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"PodDisruptionBudget {ev.ref.name!r} has broad/empty selector or zero disruption allowance (can block node drains)",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
        return
    if action == ACTION_MODIFY:
        ev.add(
            "PDB_MODIFIED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"modifies PodDisruptionBudget {ev.ref.name!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    else:
        # Offline static analysis cannot verify live replica count or overlapping PDBs,
        # so adding a PDB offline is `medium` with `moderate` revertability (requiring
        # live cluster verification via the `no-pdb` recipe path before auto-merge).
        ev.add(
            "PDB_ADDED_OFFLINE",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"adds PodDisruptionBudget {ev.ref.name!r} (offline classifier cannot verify live workload readiness or overlapping PDBs)",
            revert=REVERT_MODERATE,
            field_path="spec",
        )


def _check_service(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any], diffs: list[FieldDiff]) -> None:
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    svc_type = str(a_spec.get("type") or "ClusterIP")
    ext_ips = _as_list(ev, a_spec.get("externalIPs"), "spec.externalIPs")
    if ext_ips:
        ev.add(
            "SERVICE_EXTERNAL_IPS",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"Service {ev.ref.name!r} sets spec.externalIPs (traffic hijack risk)",
            blast=BLAST_CRITICAL,
            field_path="spec.externalIPs",
            security=True,
        )
    if action == ACTION_ADD:
        if svc_type in ("LoadBalancer", "NodePort", "ExternalName"):
            ev.add(
                "PUBLIC_EXPOSURE_SERVICE",
                DIM_SECURITY,
                RISK_HIGH,
                f"creates externally exposed {svc_type} Service {ev.ref.name!r}",
                blast=BLAST_HIGH,
                field_path="spec.type",
                security=True,
            )
        else:
            ev.add(
                "NEW_SERVICE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"creates new {svc_type} Service {ev.ref.name!r}",
            )
        return

    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    b_type = str(b_spec.get("type") or "ClusterIP")
    if b_type != svc_type:
        if svc_type in ("LoadBalancer", "NodePort", "ExternalName"):
            ev.add(
                "PUBLIC_EXPOSURE_SERVICE",
                DIM_SECURITY,
                RISK_HIGH,
                f"changes Service {ev.ref.name!r} type from {b_type} to {svc_type}",
                blast=BLAST_HIGH,
                field_path="spec.type",
                security=True,
            )
        else:
            ev.add(
                "SERVICE_TYPE_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"changes Service {ev.ref.name!r} type from {b_type} to {svc_type}",
                blast=BLAST_MEDIUM,
                disrupt=DISRUPT_DESTRUCTIVE if b_type == "LoadBalancer" else DISRUPT_DISRUPTIVE,
                field_path="spec.type",
            )
    if b_spec.get("externalName") != a_spec.get("externalName"):
        ev.add(
            "SERVICE_EXTERNAL_NAME_CHANGED",
            DIM_SECURITY,
            RISK_HIGH,
            f"retargets ExternalName Service {ev.ref.name!r} spec.externalName (DNS/egress hijack risk)",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.externalName",
            security=True,
        )
    if b_spec.get("clusterIP") != a_spec.get("clusterIP") or b_spec.get("clusterIPs") != a_spec.get("clusterIPs"):
        ev.add(
            "IMMUTABLE_FIELD_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"modifies immutable Service {ev.ref.name!r} clusterIP",
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE,
            field_path="spec.clusterIP",
        )
    if b_spec.get("selector") != a_spec.get("selector"):
        ev.add(
            "SERVICE_SELECTOR_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"retargets Service {ev.ref.name!r} spec.selector",
            blast=BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.selector",
        )
    if b_spec.get("ports") != a_spec.get("ports"):
        ev.add(
            "SERVICE_PORTS_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"modifies Service {ev.ref.name!r} spec.ports",
            blast=BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.ports",
        )
    if b_spec.get("externalTrafficPolicy") != a_spec.get("externalTrafficPolicy"):
        ev.add(
            "SERVICE_TRAFFIC_POLICY_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"changes Service {ev.ref.name!r} externalTrafficPolicy",
            blast=BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.externalTrafficPolicy",
        )
    b_lb_ranges = b_spec.get("loadBalancerSourceRanges")
    a_lb_ranges_raw = a_spec.get("loadBalancerSourceRanges")
    a_lb_ranges = _as_list(ev, a_lb_ranges_raw, "spec.loadBalancerSourceRanges")
    if svc_type == "LoadBalancer" and isinstance(b_lb_ranges, list) and b_lb_ranges and not a_lb_ranges:
        ev.add(
            "LOADBALANCER_SOURCE_RANGES_REMOVED",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"removes loadBalancerSourceRanges on LoadBalancer Service {ev.ref.name!r} (opens to public internet)",
            blast=BLAST_CRITICAL,
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.loadBalancerSourceRanges",
            security=True,
        )
    elif a_lb_ranges and ({"0.0.0.0/0", "::/0"} & {str(x) for x in a_lb_ranges}) and a_lb_ranges != b_lb_ranges:
        ev.add(
            "PUBLIC_EXPOSURE_SERVICE",
            DIM_SECURITY,
            RISK_HIGH,
            f"opens Service {ev.ref.name!r} loadBalancerSourceRanges to public CIDR",
            blast=BLAST_HIGH,
            field_path="spec.loadBalancerSourceRanges",
            security=True,
        )
    for d in diffs:
        if d.tokens and d.tokens[0] != "metadata" and ev.risk_level == RISK_LOW:
            ev.add(
                "UNALLOWLISTED_FIELD_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"modifies Service field {d.dotted!r}",
                field_path=d.dotted,
            )


def _check_hpa(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any], diffs: list[FieldDiff]) -> None:
    if action == ACTION_ADD:
        ev.add("NEW_HPA", DIM_BLAST_RADIUS, RISK_MEDIUM, f"creates new HorizontalPodAutoscaler {ev.ref.name!r}")
        return
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    if b_spec.get("scaleTargetRef") != a_spec.get("scaleTargetRef"):
        ev.add(
            "HPA_TARGET_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"changes HPA {ev.ref.name!r} scaleTargetRef",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.scaleTargetRef",
        )
    b_min_raw = b_spec.get("minReplicas", 1)
    a_min_raw = a_spec.get("minReplicas", 1)
    b_max_raw = b_spec.get("maxReplicas", 1)
    a_max_raw = a_spec.get("maxReplicas", 1)
    a_min_valid = _is_strict_int(a_min_raw) and a_min_raw >= 0
    a_max_valid = _is_strict_int(a_max_raw) and a_max_raw >= 1
    b_min_valid = _is_strict_int(b_min_raw) and b_min_raw >= 0
    b_max_valid = _is_strict_int(b_max_raw) and b_max_raw >= 1
    if (
        ("minReplicas" in a_spec and a_spec["minReplicas"] is not None and not a_min_valid)
        or ("maxReplicas" in a_spec and not a_max_valid)
        or (a_min_valid and a_max_valid and a_min_raw > a_max_raw)
    ):
        ev.add(
            "INVALID_HPA_REPLICAS",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sets invalid HPA {ev.ref.name!r} minReplicas={a_min_raw!r} / maxReplicas={a_max_raw!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    elif a_min_valid and b_min_valid and a_min_raw == 0 and b_min_raw > 0:
        ev.add(
            "HPA_SCALED_TO_ZERO",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"lowers HPA {ev.ref.name!r} minReplicas from {b_min_raw} to 0",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.minReplicas",
        )
    elif a_max_valid and b_min_valid and a_max_raw < b_min_raw:
        ev.add(
            "HPA_CAPACITY_REDUCED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"lowers HPA {ev.ref.name!r} maxReplicas ({a_max_raw}) below prior minReplicas ({b_min_raw})",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.maxReplicas",
        )
    elif (a_min_valid and b_min_valid and a_min_raw < b_min_raw) or (
        a_max_valid and b_max_valid and a_max_raw < b_max_raw
    ):
        ev.add(
            "HPA_BOUNDS_LOWERED",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"lowers HPA {ev.ref.name!r} replica bounds",
            field_path="spec",
        )
    elif (a_min_valid and b_min_valid and a_min_raw > b_min_raw) or (
        a_max_valid and b_max_valid and a_max_raw > b_max_raw
    ):
        if a_max_valid and a_max_raw <= MAX_LOW_REPLICA_TOTAL and a_min_valid and a_min_raw <= MAX_LOW_REPLICA_TOTAL:
            ev.add(
                "HPA_BOUNDS_BUMPED",
                DIM_BLAST_RADIUS,
                RISK_LOW,
                f"modestly increases HPA {ev.ref.name!r} replica bounds within safe ceiling",
                field_path="spec",
            )
        else:
            ev.add(
                "HPA_LARGE_INCREASE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"raises HPA {ev.ref.name!r} maxReplicas above {MAX_LOW_REPLICA_TOTAL}",
                blast=BLAST_MEDIUM,
                field_path="spec.maxReplicas",
            )
    for d in diffs:
        if d.tokens and d.tokens[0] != "metadata" and not (
            d.tokens[0] == "spec" and len(d.tokens) >= 2 and d.tokens[1] in ("minReplicas", "maxReplicas", "scaleTargetRef")
        ):
            ev.add(
                "HPA_POLICY_MODIFIED",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"modifies HPA field {d.dotted!r}",
                field_path=d.dotted,
            )


def _check_pvc(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    if action == ACTION_ADD:
        ev.add(
            "PVC_CREATED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"creates PersistentVolumeClaim {ev.ref.name!r}",
            revert=REVERT_DIFFICULT,
        )
        return
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    for imm in ("storageClassName", "accessModes", "volumeMode", "volumeName", "dataSource", "dataSourceRef"):
        if b_spec.get(imm) != a_spec.get(imm):
            ev.add(
                "IMMUTABLE_FIELD_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies immutable PVC {ev.ref.name!r} field spec.{imm}",
                blast=BLAST_MEDIUM,
                revert=REVERT_DIFFICULT if imm in ("dataSource", "dataSourceRef") else REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path=f"spec.{imm}",
            )
    b_res = b_spec.get("resources") if isinstance(b_spec.get("resources"), dict) else {}
    b_req = b_res.get("requests") if isinstance(b_res.get("requests"), dict) else {}
    a_res = _as_dict(ev, a_spec.get("resources"), "spec.resources")
    a_req = _as_dict(ev, a_res.get("requests"), "spec.resources.requests")
    b_sz = parse_memory_bytes(b_req.get("storage"))
    a_sz = parse_memory_bytes(a_req.get("storage"))
    if a_req.get("storage") is not None and a_sz is None:
        ev.add(
            "INVALID_RESOURCE_QUANTITY",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"unparseable PVC {ev.ref.name!r} storage request {a_req.get('storage')!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.resources.requests.storage",
        )
    elif b_sz is not None and a_sz is not None and b_sz != a_sz:
        if a_sz < b_sz:
            ev.add(
                "PVC_STORAGE_SHRUNK",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"attempts to shrink PVC {ev.ref.name!r} storage (unsupported in-place; requires destructive recreate)",
                blast=BLAST_MEDIUM,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path="spec.resources.requests.storage",
            )
        else:
            ev.add(
                "PVC_STORAGE_EXPANDED",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"expands PVC {ev.ref.name!r} storage (one-way ratchet; Kubernetes forbids shrinking on revert)",
                blast=BLAST_MEDIUM,
                revert=REVERT_DIFFICULT,
                field_path="spec.resources.requests.storage",
            )
    if not ev.reasons:
        ev.add(
            "PVC_MODIFIED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"modifies PersistentVolumeClaim {ev.ref.name!r}",
            revert=REVERT_MODERATE,
        )


def _kyverno_rules_weakened(b_spec: dict[str, Any], a_spec: dict[str, Any]) -> bool:
    """Detect modern Kyverno per-rule validate.failureAction weakening or added exclude blocks."""
    b_rules = b_spec.get("rules") if isinstance(b_spec.get("rules"), list) else []
    a_rules = a_spec.get("rules") if isinstance(a_spec.get("rules"), list) else []
    b_by_name = {
        str(r.get("name")): r for r in b_rules if isinstance(r, dict) and isinstance(r.get("name"), str)
    }
    a_by_name = {
        str(r.get("name")): r for r in a_rules if isinstance(r, dict) and isinstance(r.get("name"), str)
    }
    if set(b_by_name.keys()) - set(a_by_name.keys()):
        return True
    for rname, br in b_by_name.items():
        ar = a_by_name.get(rname)
        if not isinstance(ar, dict):
            return True
        b_val = br.get("validate") if isinstance(br.get("validate"), dict) else {}
        a_val = ar.get("validate") if isinstance(ar.get("validate"), dict) else {}
        if str(b_val.get("failureAction") or "").lower() == "enforce" and str(a_val.get("failureAction") or "").lower() != "enforce":
            return True
        if ar.get("exclude") and ar.get("exclude") != br.get("exclude"):
            return True
    return False


def _check_policy_engine(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate Kyverno and OPA Gatekeeper admission policies."""
    kind = ev.ref.kind
    is_cluster = is_cluster_scoped_kind(ev.ref.group, kind) or ev.ref.namespace == ""
    spec = after_obj.get("spec") if isinstance(after_obj.get("spec"), dict) else {}
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}

    # Detect weakening validationFailureAction / per-rule failureAction / enforcementAction / exclude
    weakened = False
    if str(b_spec.get("validationFailureAction") or "").lower() == "enforce" and str(spec.get("validationFailureAction") or "").lower() != "enforce":
        weakened = True
    if str(b_spec.get("enforcementAction") or "deny").lower() == "deny" and str(spec.get("enforcementAction") or "deny").lower() in ("dryrun", "warn"):
        weakened = True
    if action == ACTION_MODIFY and _kyverno_rules_weakened(b_spec, spec):
        weakened = True

    sev = RISK_CRITICAL if (weakened or is_cluster) else RISK_HIGH
    ev.add(
        "POLICY_ENGINE_WEAKENED" if weakened else "POLICY_ENGINE_CHANGE",
        DIM_SECURITY,
        sev,
        f"{'weakens' if weakened else action + 's'} admission policy {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL if is_cluster else BLAST_HIGH,
        security=True,
    )


def _check_istio_security(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate Istio PeerAuthentication, RequestAuthentication, AuthorizationPolicy."""
    mesh_wide = ev.ref.namespace in ("", "istio-system")
    spec = after_obj.get("spec") if isinstance(after_obj.get("spec"), dict) else {}
    mtls = spec.get("mtls") if isinstance(spec.get("mtls"), dict) else {}
    mtls_mode = str(mtls.get("mode") or "").upper()
    loosened_mtls = ev.ref.kind == "PeerAuthentication" and mtls_mode in ("DISABLE", "PERMISSIVE")
    broad_allow = ev.ref.kind == "AuthorizationPolicy" and str(spec.get("action") or "ALLOW").upper() == "ALLOW" and not spec.get("selector")
    sev = RISK_CRITICAL if (mesh_wide and (loosened_mtls or broad_allow or action == ACTION_MODIFY)) else RISK_HIGH
    ev.add(
        "MESH_SECURITY_POLICY_CHANGE",
        DIM_SECURITY,
        sev,
        f"{action}s Istio {ev.ref.kind} {ev.ref.name!r} in {ev.ref.namespace or 'cluster'}",
        blast=BLAST_CRITICAL if mesh_wide else BLAST_HIGH,
        security=True,
    )


def _check_gitops_reconciler(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate ArgoCD, Flux, and Config Sync reconcilers."""
    kind = ev.ref.kind
    spec = after_obj.get("spec") if isinstance(after_obj.get("spec"), dict) else {}
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}

    prune_enabled = False
    if spec.get("prune") is True and b_spec.get("prune") is not True:
        prune_enabled = True
    sync_pol = spec.get("syncPolicy")
    if isinstance(sync_pol, dict) and isinstance(sync_pol.get("automated"), dict) and sync_pol["automated"].get("prune") is True:
        b_sp = b_spec.get("syncPolicy") if isinstance(b_spec.get("syncPolicy"), dict) else {}
        if not (isinstance(b_sp.get("automated"), dict) and b_sp["automated"].get("prune") is True):
            prune_enabled = True

    root_reconciler = kind in ("RootSync", "ApplicationSet", "AppProject", "ConfigManagement")
    source_changed = (
        action == ACTION_MODIFY
        and (spec.get("source") != b_spec.get("source") or spec.get("sources") != b_spec.get("sources") or spec.get("git") != b_spec.get("git") or spec.get("url") != b_spec.get("url"))
    )
    sev = RISK_CRITICAL if (root_reconciler or prune_enabled or source_changed) else RISK_HIGH
    ev.add(
        "GITOPS_RECONCILER_CHANGE",
        DIM_BLAST_RADIUS,
        sev,
        f"{action}s GitOps reconciler {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL if root_reconciler else BLAST_HIGH,
        revert=REVERT_DIFFICULT,
        disrupt=DISRUPT_DESTRUCTIVE if (prune_enabled or source_changed) else DISRUPT_NONE,
        security=True,
    )


# ---------------------------------------------------------------------------
# Main Entry Point for One Kubernetes Resource
# ---------------------------------------------------------------------------


def evaluate_resource(
    file_path: str,
    action: str,
    before: ParsedResource | None,
    after: ParsedResource | None,
    *,
    ns_labels: dict[str, dict[str, Any]] | None = None,
) -> ResourceEval:
    """Deterministically evaluate risk across all 4 axes for a single Kubernetes resource."""
    primary = after if after is not None else before
    assert primary is not None
    ref = primary.ref
    gk = (ref.group, ref.kind)
    ev = ResourceEval(file=file_path, action=action, ref=ref)

    in_system_ns = ref.namespace in SYSTEM_NAMESPACES
    is_cluster_scoped = is_cluster_scoped_kind(ref.group, ref.kind)

    # Determine production environment from object labels, PR Namespace labels, or path
    obj_labels = _meta_dict(primary.obj, "labels")
    prior_labels = _meta_dict(before.obj, "labels") if before is not None else {}
    ns_lbl = (ns_labels or {}).get(ref.namespace, {})
    is_prod = (
        _is_production_labels(obj_labels)
        or _is_production_labels(prior_labels)
        or _is_production_labels(ns_lbl)
        or _is_production_path(file_path)
    )

    # Helper to apply production escalation consistently before returning
    def _finalize() -> ResourceEval:
        if not ev.reasons:
            ev.add(
                "UNMODELED_RESOURCE_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"{action}s {ref.gvk_label()} {ref.name!r}",
            )
        if is_prod and ev.risk_level in (RISK_LOW, RISK_MEDIUM, RISK_HIGH):
            escalate_high = (
                ev.disruptiveness in (DISRUPT_DISRUPTIVE, DISRUPT_DESTRUCTIVE)
                or ev.revertability == REVERT_IRREVERSIBLE
                or ev.security_sensitive
            )
            new_level = escalate_risk(ev.risk_level, high_impact=escalate_high)
            if new_level != ev.risk_level:
                ev.add(
                    "PRODUCTION_ENVIRONMENT_ESCALATION",
                    DIM_BLAST_RADIUS,
                    new_level,
                    f"escalated {ev.risk_level} -> {new_level} because target is production",
                    blast=BLAST_HIGH if ev.blast_radius in (BLAST_LOW, BLAST_MEDIUM) else ev.blast_radius,
                )
        return ev

    # 0. Kustomize strategic-merge `$patch` directives (e.g. `$patch: delete`)
    if action != ACTION_DELETE:
        patches = _find_kustomize_patches(primary.obj)
        if patches:
            is_delete_patch = any(str(v).strip().lower() == "delete" for _, v in patches)
            if is_delete_patch:
                crit = is_prod or is_cluster_scoped or gk in DATA_STATEFUL_DELETE_KINDS or in_system_ns
                ev.add(
                    "KUSTOMIZE_DELETE_PATCH",
                    DIM_DISRUPTIVENESS,
                    RISK_CRITICAL if crit else RISK_HIGH,
                    f"manifest contains Kustomize `$patch: delete` directive targeting {ref.gvk_label()} {ref.name!r}",
                    blast=BLAST_CRITICAL if crit else BLAST_HIGH,
                    revert=REVERT_IRREVERSIBLE if gk in DATA_STATEFUL_DELETE_KINDS else REVERT_DIFFICULT,
                    disrupt=DISRUPT_DESTRUCTIVE,
                    field_path=patches[0][0],
                )
                return _finalize()
            ev.add(
                "KUSTOMIZE_PATCH_DIRECTIVE",
                DIM_BLAST_RADIUS,
                RISK_UNKNOWN,
                f"manifest contains unrendered Kustomize `$patch` directive on {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_UNKNOWN,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_UNKNOWN,
                field_path=patches[0][0],
                supported=False,
            )
            return _finalize()

    # 1. Self-modification of kube-agents CRDs -> always critical
    if ref.group == "kubeagents.x-k8s.io":
        ev.add(
            "KUBE_AGENTS_SELF_MODIFICATION",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{action}s kube-agents control-plane CR {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DESTRUCTIVE if action == ACTION_DELETE else DISRUPT_NONE,
            security=True,
        )
        return ev

    # 2. Unknown / unsupported CRD handling (never silently `low` or `medium`!)
    if not is_known_kind(ref.group, ref.kind):
        if action == ACTION_DELETE:
            ev.add(
                "UNKNOWN_CRD_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if in_system_ns else RISK_HIGH,
                f"deletes unsupported/custom resource {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_HIGH if in_system_ns else BLAST_UNKNOWN,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_DESTRUCTIVE,
                supported=False,
            )
        elif in_system_ns:
            ev.add(
                "UNKNOWN_CRD_IN_SYSTEM_NS",
                DIM_SECURITY,
                RISK_HIGH,
                f"{action}s unsupported/custom resource {ref.gvk_label()} {ref.name!r} in system namespace {ref.namespace!r}",
                blast=BLAST_HIGH,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_UNKNOWN,
                supported=False,
                security=True,
            )
        else:
            ev.add(
                "UNSUPPORTED_RESOURCE_KIND",
                DIM_BLAST_RADIUS,
                RISK_UNKNOWN,
                f"resource kind {ref.gvk_label()} is not modeled by the deterministic risk classifier",
                blast=BLAST_UNKNOWN,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_UNKNOWN,
                supported=False,
            )
        return ev

    # 3. Missing namespace on a known namespaced resource -> fail closed as unknown
    if not is_cluster_scoped and not ref.namespace:
        ev.add(
            "MISSING_NAMESPACE",
            DIM_BLAST_RADIUS,
            RISK_UNKNOWN if action != ACTION_DELETE else RISK_HIGH,
            f"namespaced resource {ref.gvk_label()} {ref.name!r} omits metadata.namespace",
            blast=BLAST_UNKNOWN,
            revert=REVERT_UNKNOWN,
            disrupt=DISRUPT_DESTRUCTIVE if action == ACTION_DELETE else DISRUPT_UNKNOWN,
            supported=False,
        )
        return ev

    # 4. Handle deletions cleanly across all known resource families
    if action == ACTION_DELETE:
        if gk == ("", "Namespace"):
            ev.add(
                "CRITICAL_NAMESPACE_DELETE",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL,
                f"deletes Namespace {ref.name!r} and cascade-destroys every workload, Secret, and PVC inside it",
                blast=BLAST_CRITICAL,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
            )
            return _finalize()
        if in_system_ns:
            ev.add(
                "CRITICAL_SYSTEM_NAMESPACE_DELETE",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL,
                f"deletes {ref.gvk_label()} {ref.name!r} in system namespace {ref.namespace!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_IRREVERSIBLE if gk in DATA_STATEFUL_DELETE_KINDS else REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                security=True,
            )
            return _finalize()
        if gk in DATA_STATEFUL_DELETE_KINDS:
            ev.add(
                "CRITICAL_STATEFUL_OR_INFRA_DELETE",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL,
                f"deletes stateful or cluster/cloud infrastructure resource {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_CRITICAL if is_cluster_scoped or is_cloud_infra_group(ref.group) else BLAST_HIGH,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
            )
            return _finalize()
        if gk in CONTROL_PLANE_EXTENSION_KINDS or ref.group in ("kyverno.io", "templates.gatekeeper.sh", "constraints.gatekeeper.sh"):
            ev.add(
                "CRITICAL_CONTROL_PLANE_DELETE",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"deletes admission/policy control {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                security=True,
            )
            return _finalize()
        if gk == ("", "Secret") or ref.kind in ("SealedSecret", "ExternalSecret"):
            ev.add(
                "SECRET_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if is_prod else RISK_HIGH,
                f"deletes {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_MEDIUM,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
                security=True,
            )
            return _finalize()
        if gk == ("apps", "StatefulSet"):
            ev.add(
                "STATEFULSET_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if is_prod else RISK_HIGH,
                f"deletes StatefulSet {ref.name!r}",
                blast=BLAST_MEDIUM,
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DESTRUCTIVE,
            )
            return _finalize()
        if is_cloud_infra_group(ref.group):
            ev.add(
                "CLOUD_INFRA_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if is_prod else RISK_HIGH,
                f"deletes cloud infrastructure resource {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_HIGH,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
            )
            return _finalize()
        ev.add(
            "RESOURCE_DELETED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"deletes {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_LOW,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE,
        )
        return _finalize()

    # Compute diffs up front on MODIFY so metadata tampering, system metadata edits,
    # and plaintext secret leaks are checked universally across EVERY resource kind.
    diffs = diff_objects(before.sanitized, after.sanitized) if (action == ACTION_MODIFY and before and after) else []

    # Universal plaintext credential scan across all non-Secret fields (e.g. args,
    # command, PVC/Namespace annotations, Istio/mesh configs, etc.)
    for secret_path, pat_name in _scan_value_for_secrets(primary.sanitized):
        ev.add(
            "PLAINTEXT_SECRET_LEAK",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"plaintext {pat_name} detected in {ref.gvk_label()} {ref.name!r} at {secret_path}",
            blast=BLAST_HIGH,
            revert=REVERT_IRREVERSIBLE,
            field_path=secret_path,
            security=True,
        )

    if action == ACTION_MODIFY and before is not None:
        _check_metadata_edits(
            ev,
            before.obj,
            primary.obj,
            diffs,
            allow_low_metadata=(gk in LOW_RISK_EDITABLE_KINDS),
        )
    elif action == ACTION_ADD:
        a_meta = primary.obj.get("metadata")
        if isinstance(a_meta, dict):
            _as_dict(ev, a_meta.get("labels"), "metadata.labels")
            _as_dict(ev, a_meta.get("annotations"), "metadata.annotations")
            for mk in sorted(a_meta.keys(), key=str):
                if mk not in _ALLOWED_METADATA_KEYS:
                    ev.add(
                        "METADATA_SYSTEM_FIELD_EDIT",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"sets system or unexpected metadata field metadata.{mk}",
                        blast=BLAST_MEDIUM,
                        field_path=f"metadata.{mk}",
                        security=True,
                    )
        for meta_field in ("labels", "annotations"):
            for mk, _ in sorted(_meta_dict(primary.obj, meta_field).items(), key=str):
                if any(sn in str(mk).lower() for sn in _INGRESS_SNIPPET_KEYS):
                    ev.add(
                        "INGRESS_SNIPPET_INJECTION",
                        DIM_SECURITY,
                        RISK_CRITICAL,
                        f"sets raw proxy snippet annotation {mk!r}",
                        blast=BLAST_CRITICAL,
                        field_path=f"metadata.{meta_field}.{mk}",
                        security=True,
                    )

    # 5. System namespace create/modify
    if in_system_ns:
        ev.add(
            "SYSTEM_NAMESPACE_TARGET",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s {ref.gvk_label()} {ref.name!r} inside system namespace {ref.namespace!r}",
            blast=BLAST_HIGH,
            security=True,
        )

    # 5b. Cluster Node objects (cordoning, tainting, node config) -> critical
    if gk == ("", "Node"):
        ev.add(
            "NODE_RESOURCE_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_CRITICAL,
            f"{action}s cluster Node {ref.name!r} (affects node scheduling, taints, and workloads)",
            blast=BLAST_CRITICAL,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    # 6. Control-plane admission webhooks & APIService
    if gk in CONTROL_PLANE_EXTENSION_KINDS:
        ev.add(
            "CRITICAL_CONTROL_PLANE_EXTENSION",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{action}s cluster-wide API/admission extension {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_MODERATE,
            security=True,
        )
        return _finalize()

    # 7. CRDs (create / modify)
    if gk == ("apiextensions.k8s.io", "CustomResourceDefinition"):
        ev.add(
            "CRD_SCHEMA_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{action}s CustomResourceDefinition {ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            security=True,
        )
        return _finalize()

    # 8. Namespace create / modify
    if gk == ("", "Namespace"):
        if action == ACTION_ADD:
            ev.add(
                "NAMESPACE_CREATED",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"creates Namespace {ref.name!r}",
                blast=BLAST_HIGH,
                revert=REVERT_DIFFICULT,
            )
        else:
            b_lbl = _meta_dict(before.obj if before else None, "labels")
            a_lbl = _meta_dict(primary.obj, "labels")
            _psa_rank = {"restricted": 3, "baseline": 2, "privileged": 1, "": 1}
            old_psa = str(b_lbl.get("pod-security.kubernetes.io/enforce") or "").strip().lower()
            new_psa = str(a_lbl.get("pod-security.kubernetes.io/enforce") or "").strip().lower()
            if _psa_rank.get(old_psa, 1) > _psa_rank.get(new_psa, 1):
                ev.add(
                    "POD_SECURITY_STANDARD_LOOSENED",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"loosens pod-security.kubernetes.io/enforce on Namespace {ref.name!r} ({old_psa!r} -> {new_psa!r})",
                    blast=BLAST_CRITICAL,
                    security=True,
                )
            else:
                ev.add(
                    "NAMESPACE_MODIFIED",
                    DIM_BLAST_RADIUS,
                    RISK_HIGH,
                    f"modifies Namespace {ref.name!r}",
                    blast=BLAST_HIGH,
                )
        return _finalize()

    # 9. RBAC & ServiceAccount
    if gk in RBAC_KINDS:
        ev.add(
            "RBAC_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s RBAC {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_MODERATE if (action == ACTION_MODIFY and before and before.obj.get("roleRef") != primary.obj.get("roleRef")) else REVERT_EASY,
            disrupt=DISRUPT_DESTRUCTIVE if (action == ACTION_MODIFY and before and before.obj.get("roleRef") != primary.obj.get("roleRef")) else DISRUPT_NONE,
            security=True,
        )
        _check_rbac_superuser(ev, primary.obj)
        return _finalize()

    if gk == ("", "ServiceAccount"):
        ev.add(
            "IDENTITY_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s ServiceAccount {ref.name!r}",
            blast=BLAST_MEDIUM,
            security=True,
        )
        return _finalize()

    # 10. Secrets / SealedSecret / ExternalSecret / SecretStore / ClusterSecretStore
    if gk in (("external-secrets.io", "SecretStore"), ("external-secrets.io", "ClusterSecretStore")):
        is_cluster_store = ref.kind == "ClusterSecretStore"
        ev.add(
            "SECRET_STORE_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL if is_cluster_store else RISK_HIGH,
            f"{action}s {ref.gvk_label()} {ref.name!r} (secret backend trust boundary)",
            blast=BLAST_CRITICAL if is_cluster_store else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            security=True,
        )
        return _finalize()

    if gk == ("", "Secret") or ref.kind in ("SealedSecret", "ExternalSecret"):
        is_imm_edit = action == ACTION_MODIFY and before is not None and before.obj.get("immutable") is True
        ev.add(
            "SECRET_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DESTRUCTIVE if is_imm_edit else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    # 11. Policy Engines (Kyverno, Gatekeeper)
    if ref.group in ("kyverno.io", "templates.gatekeeper.sh", "constraints.gatekeeper.sh"):
        _check_policy_engine(ev, action, before.obj if before else None, primary.obj)
        return _finalize()

    # 12. GitOps Reconcilers
    if gk in GITOPS_RECONCILER_KINDS:
        _check_gitops_reconciler(ev, action, before.obj if before else None, primary.obj)
        return _finalize()

    # 13. Cloud Infrastructure CRDs (KCC / Crossplane / Upbound / ACK / ASO) & CNPG DBs
    if is_cloud_infra_group(ref.group) or ref.group.endswith(".cnpg.io"):
        ev.add(
            "CLOUD_INFRA_CHANGE",
            DIM_REVERTABILITY,
            RISK_HIGH,
            f"{action}s cloud/database infrastructure resource {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL if ref.kind in ("ContainerCluster", "Project", "ComputeNetwork") else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            security=True,
        )
        if "iam" in ref.group.lower() or "iam" in ref.kind.lower():
            _check_cloud_iam(ev, primary.obj)
        return _finalize()

    # 14. Istio Mesh Resources
    if ref.group.endswith(".istio.io"):
        if ref.kind in ISTIO_PROXY_KINDS:
            mesh_wide = ref.namespace in ("", "istio-system")
            ev.add(
                "ISTIO_PROXY_EXTENSION",
                DIM_SECURITY,
                RISK_CRITICAL if mesh_wide else RISK_HIGH,
                f"{action}s Istio data-plane proxy extension {ref.kind} {ref.name!r}",
                blast=BLAST_CRITICAL if mesh_wide else BLAST_HIGH,
                disrupt=DISRUPT_DISRUPTIVE,
                security=True,
            )
            return _finalize()
        if ref.kind in ISTIO_SECURITY_KINDS:
            _check_istio_security(ev, action, before.obj if before else None, primary.obj)
            return _finalize()
        ev.add(
            "ISTIO_TRAFFIC_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{action}s Istio traffic resource {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if ref.namespace == "istio-system" else BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()

    # 15. Gateway API Resources
    if ref.group == "gateway.networking.k8s.io":
        if ref.kind == "ReferenceGrant":
            ev.add(
                "CROSS_NAMESPACE_REFERENCE_GRANT",
                DIM_SECURITY,
                RISK_HIGH,
                f"{action}s Gateway API ReferenceGrant {ref.name!r} (cross-namespace access)",
                blast=BLAST_HIGH,
                security=True,
            )
        elif ref.kind in ("Gateway", "GatewayClass"):
            ev.add(
                "GATEWAY_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{action}s Gateway API {ref.kind} {ref.name!r}",
                blast=BLAST_HIGH,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
                security=True,
            )
        else:
            ev.add(
                "TRAFFIC_ROUTE_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{action}s Gateway API route {ref.kind} {ref.name!r}",
                blast=BLAST_MEDIUM,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            )
        return _finalize()

    # 16. Ingress / IngressClass / NetworkPolicy
    if gk == ("networking.k8s.io", "Ingress") or gk == ("networking.k8s.io", "IngressClass"):
        annos = _meta_dict(primary.obj, "annotations")
        has_snippet = any(any(sn in str(k).lower() for sn in _INGRESS_SNIPPET_KEYS) for k in annos)
        ev.add(
            "INGRESS_SNIPPET_INJECTION" if has_snippet else "INGRESS_CHANGE",
            DIM_SECURITY if has_snippet else DIM_BLAST_RADIUS,
            RISK_CRITICAL if has_snippet else RISK_HIGH,
            f"{action}s {ref.kind} {ref.name!r}" + (" with raw proxy snippet annotation" if has_snippet else ""),
            blast=BLAST_CRITICAL if has_snippet else BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    if gk == ("networking.k8s.io", "NetworkPolicy"):
        ev.add(
            "NETWORK_POLICY_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s NetworkPolicy {ref.name!r} (can immediately block or expose pod traffic)",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    # 17. Storage (PVC, PV, StorageClass, CSIDriver, VolumeSnapshot)
    if gk == ("", "PersistentVolumeClaim"):
        _check_pvc(ev, action, before.obj if before else None, primary.obj)
        return _finalize()
    if gk == ("storage.k8s.io", "StorageClass"):
        annos = _meta_dict(primary.obj, "annotations")
        is_default = str(annos.get("storageclass.kubernetes.io/is-default-class") or "").lower() == "true"
        ev.add(
            "STORAGE_CLASS_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_CRITICAL if is_default else RISK_HIGH,
            f"{action}s StorageClass {ref.name!r}",
            blast=BLAST_CRITICAL if is_default else BLAST_HIGH,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()
    if gk in (("", "PersistentVolume"), ("storage.k8s.io", "CSIDriver"), ("snapshot.storage.k8s.io", "VolumeSnapshot"), ("snapshot.storage.k8s.io", "VolumeSnapshotContent")):
        ev.add(
            "CLUSTER_STORAGE_CHANGE",
            DIM_REVERTABILITY,
            RISK_HIGH,
            f"{action}s storage resource {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
        )
        return _finalize()

    # 18. PriorityClass / RuntimeClass / ResourceQuota / LimitRange / CertManager
    if gk == ("scheduling.k8s.io", "PriorityClass"):
        val = primary.obj.get("value")
        is_glob = primary.obj.get("globalDefault") is True or (isinstance(val, int) and val >= 1_000_000_000)
        ev.add(
            "PRIORITY_CLASS_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_CRITICAL if is_glob else RISK_HIGH,
            f"{action}s PriorityClass {ref.name!r}",
            blast=BLAST_CRITICAL if is_glob else BLAST_HIGH,
            revert=REVERT_MODERATE,
        )
        return _finalize()
    if gk == ("node.k8s.io", "RuntimeClass"):
        ev.add(
            "RUNTIME_CLASS_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{action}s RuntimeClass {ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_MODERATE,
            security=True,
        )
        return _finalize()
    if gk in (("", "ResourceQuota"), ("", "LimitRange")):
        spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
        hard = spec.get("hard") if isinstance(spec.get("hard"), dict) else {}
        zero_pods = str(hard.get("pods") or "").strip() == "0"
        ev.add(
            "QUOTA_OR_LIMIT_CHANGE",
            DIM_DISRUPTIVENESS if zero_pods else DIM_BLAST_RADIUS,
            RISK_HIGH if (zero_pods or action == ACTION_MODIFY) else RISK_MEDIUM,
            f"{action}s {ref.kind} {ref.name!r}",
            blast=BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE if zero_pods else DISRUPT_NONE,
        )
        return _finalize()
    if ref.group == "cert-manager.io":
        ev.add(
            "CERT_MANAGER_CHANGE",
            DIM_SECURITY,
            RISK_HIGH if is_cluster_scoped else RISK_MEDIUM,
            f"{action}s cert-manager {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
            security=True,
        )
        return _finalize()

    # 19. PodDisruptionBudget
    if gk == ("policy", "PodDisruptionBudget"):
        _check_pdb(ev, action, primary.obj)
        return _finalize()

    # 20. VPA
    if gk == ("autoscaling.k8s.io", "VerticalPodAutoscaler"):
        spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
        up = spec.get("updatePolicy") if isinstance(spec.get("updatePolicy"), dict) else {}
        mode = str(up.get("updateMode") or "Auto")
        active = mode in ("Auto", "Recreate")
        ev.add(
            "VPA_CHANGE",
            DIM_DISRUPTIVENESS if active else DIM_BLAST_RADIUS,
            RISK_HIGH if active else RISK_MEDIUM,
            f"{action}s VerticalPodAutoscaler {ref.name!r} with updateMode={mode!r}",
            disrupt=DISRUPT_DISRUPTIVE if active else DISRUPT_NONE,
        )
        return _finalize()

    # 21. Remaining modify-capable kinds (ConfigMap, Service, HPA, Workloads)
    if gk == ("", "ConfigMap"):
        _check_configmap(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("", "Service"):
        _check_service(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("autoscaling", "HorizontalPodAutoscaler"):
        _check_hpa(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk in WORKLOAD_KINDS:
        if action == ACTION_ADD:
            ps = _extract_pod_spec(ref.kind, primary.obj) or {}
            _check_pod_spec_posture(ev, ps, is_daemonset=(ref.kind == "DaemonSet"))
            if ref.kind == "Job":
                ev.add(
                    "JOB_CREATED",
                    DIM_REVERTABILITY,
                    RISK_HIGH,
                    f"creates batch/Job {ref.name!r} (one-off execution is not cleanly revertable)",
                    revert=REVERT_IRREVERSIBLE,
                )
            elif ref.kind == "DaemonSet":
                ev.add(
                    "NEW_DAEMONSET",
                    DIM_BLAST_RADIUS,
                    RISK_HIGH,
                    f"creates DaemonSet {ref.name!r} across cluster nodes",
                    blast=BLAST_HIGH,
                )
            else:
                ev.add(
                    "NEW_WORKLOAD",
                    DIM_BLAST_RADIUS,
                    RISK_MEDIUM,
                    f"creates new {ref.kind} {ref.name!r}",
                    revert=REVERT_MODERATE if ref.kind in ("Pod", "ReplicaSet") else REVERT_EASY,
                )
        else:
            assert before is not None
            _check_workload_modify(ev, before.obj, primary.obj, diffs)

    return _finalize()
