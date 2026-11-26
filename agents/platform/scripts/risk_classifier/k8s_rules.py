"""Comprehensive deterministic Kubernetes, Service Mesh, Policy, GitOps, and Cloud IaC risk rules."""

from __future__ import annotations

import base64
import binascii
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
        "gmp-public",
        "gke-managed-system",
        "gke-managed-cim",
        "gke-mcs",
        "istio-system",
        "config-management-system",
        "cnrm-system",
        "flux-system",
        "argocd",
        "cert-manager",
        "gatekeeper-system",
        "kyverno",
        "calico-system",
        "metallb-system",
    }
)

ENV_LABEL_KEYS = ("kube-agents/environment", "environment", "env", "stage", "tier")
PRODUCTION_VALUES = frozenset({"prod", "production", "prd", "live"})
PRODUCTION_PATH_VALUES = frozenset({"prod", "production", "prd"})

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
        ("admissionregistration.k8s.io", "MutatingAdmissionPolicy"),
        ("admissionregistration.k8s.io", "MutatingAdmissionPolicyBinding"),
        ("flowcontrol.apiserver.k8s.io", "FlowSchema"),
        ("flowcontrol.apiserver.k8s.io", "PriorityLevelConfiguration"),
        ("audit.k8s.io", "Policy"),
        ("certificates.k8s.io", "CertificateSigningRequest"),
        ("certificates.k8s.io", "ClusterTrustBundle"),
        ("authentication.k8s.io", "TokenReview"),
        ("authentication.k8s.io", "SelfSubjectReview"),
        ("authorization.k8s.io", "SubjectAccessReview"),
        ("authorization.k8s.io", "SelfSubjectAccessReview"),
        ("authorization.k8s.io", "SelfSubjectRulesReview"),
        ("storage.k8s.io", "StorageClass"),
        ("storage.k8s.io", "CSIDriver"),
        ("storage.k8s.io", "CSINode"),
        ("storage.k8s.io", "VolumeAttachment"),
        ("scheduling.k8s.io", "PriorityClass"),
        ("node.k8s.io", "RuntimeClass"),
        ("networking.k8s.io", "IngressClass"),
        ("networking.k8s.io", "ServiceCIDR"),
        ("networking.k8s.io", "IPAddress"),
        ("resource.k8s.io", "DeviceClass"),
        ("resource.k8s.io", "ResourceSlice"),
        ("gateway.networking.k8s.io", "GatewayClass"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotContent"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotClass"),
        ("kyverno.io", "ClusterPolicy"),
        ("templates.gatekeeper.sh", "ConstraintTemplate"),
        ("configmanagement.gke.io", "ConfigManagement"),
        ("cert-manager.io", "ClusterIssuer"),
        ("external-secrets.io", "ClusterSecretStore"),
        ("cloud.google.com", "ComputeClass"),
        ("autoscaling.x-k8s.io", "CapacityQuota"),
        ("networking.gke.io", "GKENetworkParamSet"),
        ("networking.gke.io", "Network"),
        ("monitoring.googleapis.com", "ClusterPodMonitoring"),
        ("monitoring.googleapis.com", "ClusterNodeMonitoring"),
        ("monitoring.googleapis.com", "ClusterRules"),
        ("monitoring.googleapis.com", "GlobalRules"),
        ("container.gke.io", "Cluster"),
        ("container.gke.io", "NodePool"),
        ("kueue.x-k8s.io", "ClusterQueue"),
        ("kueue.x-k8s.io", "ResourceFlavor"),
        ("kueue.x-k8s.io", "WorkloadPriorityClass"),
        ("keda.sh", "ClusterTriggerAuthentication"),
    }
)

WORKLOAD_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("apps", "Deployment"),
        ("argoproj.io", "Rollout"),
        ("apps", "StatefulSet"),
        ("apps", "DaemonSet"),
        ("apps", "ReplicaSet"),
        ("batch", "Job"),
        ("batch", "CronJob"),
        ("", "Pod"),
        ("", "ReplicationController"),
    }
)

LOW_RISK_EDITABLE_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("apps", "Deployment"),
        ("argoproj.io", "Rollout"),
        ("batch", "CronJob"),
        ("autoscaling", "HorizontalPodAutoscaler"),
        ("keda.sh", "ScaledObject"),
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
        ("snapshot.storage.k8s.io", "VolumeSnapshotClass"),
        ("apiextensions.k8s.io", "CustomResourceDefinition"),
        ("storage.k8s.io", "StorageClass"),
        ("networking.k8s.io", "ServiceCIDR"),
        ("networking.k8s.io", "IPAddress"),
        ("certificates.k8s.io", "ClusterTrustBundle"),
        ("networking.gke.io", "ManagedCertificate"),
        ("postgresql.cnpg.io", "Cluster"),
        ("postgresql.cnpg.io", "ScheduledBackup"),
        ("container.cnrm.cloud.google.com", "ContainerCluster"),
        ("container.cnrm.cloud.google.com", "ContainerNodePool"),
        ("container.gke.io", "Cluster"),
        ("container.gke.io", "NodePool"),
        ("sql.cnrm.cloud.google.com", "SQLInstance"),
        ("sql.cnrm.cloud.google.com", "SQLDatabase"),
        ("spanner.cnrm.cloud.google.com", "SpannerInstance"),
        ("spanner.cnrm.cloud.google.com", "SpannerDatabase"),
        ("alloydb.cnrm.cloud.google.com", "AlloyDBCluster"),
        ("alloydb.cnrm.cloud.google.com", "AlloyDBInstance"),
        ("redis.cnrm.cloud.google.com", "RedisInstance"),
        ("redis.cnrm.cloud.google.com", "RedisCluster"),
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
        ("admissionregistration.k8s.io", "MutatingAdmissionPolicy"),
        ("admissionregistration.k8s.io", "MutatingAdmissionPolicyBinding"),
        ("apiregistration.k8s.io", "APIService"),
    }
)

FLOWCONTROL_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("flowcontrol.apiserver.k8s.io", "FlowSchema"),
        ("flowcontrol.apiserver.k8s.io", "PriorityLevelConfiguration"),
    }
)

CERT_AND_AUTH_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("certificates.k8s.io", "CertificateSigningRequest"),
        ("certificates.k8s.io", "ClusterTrustBundle"),
        ("", "TokenRequest"),
        ("authentication.k8s.io", "TokenRequest"),
        ("authentication.k8s.io", "TokenReview"),
        ("authentication.k8s.io", "SelfSubjectReview"),
        ("authorization.k8s.io", "SubjectAccessReview"),
        ("authorization.k8s.io", "SelfSubjectAccessReview"),
        ("authorization.k8s.io", "SelfSubjectRulesReview"),
        ("authorization.k8s.io", "LocalSubjectAccessReview"),
    }
)

COORDINATION_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("coordination.k8s.io", "Lease"),
        ("coordination.k8s.io", "LeaseCandidate"),
    }
)

DRA_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("resource.k8s.io", "DeviceClass"),
        ("resource.k8s.io", "ResourceSlice"),
        ("resource.k8s.io", "ResourceClaim"),
        ("resource.k8s.io", "ResourceClaimTemplate"),
    }
)

GKE_NETWORKING_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("networking.gke.io", "GCPBackendPolicy"),
        ("networking.gke.io", "GCPGatewayPolicy"),
        ("networking.gke.io", "HealthCheckPolicy"),
        ("networking.gke.io", "FrontendConfig"),
        ("networking.gke.io", "ManagedCertificate"),
        ("networking.gke.io", "ServiceExport"),
        ("networking.gke.io", "ServiceImport"),
        ("networking.gke.io", "ServiceNetworkEndpointGroup"),
        ("networking.gke.io", "GKENetworkParamSet"),
        ("networking.gke.io", "Network"),
        ("net.gke.io", "ServiceExport"),
        ("net.gke.io", "ServiceImport"),
        ("cloud.google.com", "BackendConfig"),
    }
)

GKE_COMPUTE_AUTOSCALING_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("cloud.google.com", "ComputeClass"),
        ("autoscaling.x-k8s.io", "ProvisioningRequest"),
        ("autoscaling.x-k8s.io", "CapacityBuffer"),
        ("autoscaling.x-k8s.io", "CapacityQuota"),
    }
)

MONITORING_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("monitoring.googleapis.com", "PodMonitoring"),
        ("monitoring.googleapis.com", "ClusterPodMonitoring"),
        ("monitoring.googleapis.com", "ClusterNodeMonitoring"),
        ("monitoring.googleapis.com", "Rules"),
        ("monitoring.googleapis.com", "ClusterRules"),
        ("monitoring.googleapis.com", "GlobalRules"),
        ("monitoring.googleapis.com", "OperatorConfig"),
        ("monitoring.coreos.com", "ServiceMonitor"),
        ("monitoring.coreos.com", "PodMonitor"),
        ("monitoring.coreos.com", "PrometheusRule"),
        ("monitoring.coreos.com", "Probe"),
        ("monitoring.coreos.com", "ScrapeConfig"),
        ("monitoring.coreos.com", "AlertmanagerConfig"),
        ("monitoring.coreos.com", "Prometheus"),
        ("monitoring.coreos.com", "Alertmanager"),
        ("monitoring.coreos.com", "ThanosRuler"),
    }
)

GATEWAY_API_ROUTE_KINDS = frozenset({"HTTPRoute", "GRPCRoute", "TCPRoute", "TLSRoute", "UDPRoute"})
ISTIO_TRAFFIC_KINDS = frozenset({"VirtualService", "DestinationRule", "Gateway", "ServiceEntry", "Sidecar", "WorkloadEntry", "WorkloadGroup"})
ISTIO_SECURITY_KINDS = frozenset({"PeerAuthentication", "RequestAuthentication", "AuthorizationPolicy"})
ISTIO_PROXY_KINDS = frozenset({"EnvoyFilter", "WasmPlugin", "ProxyConfig"})

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
    "container.gke.io",
    ".crossplane.io",
    ".upbound.io",
    ".services.k8s.aws",
    ".azure.com",
)

AI_ML_WORKLOAD_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("ray.io", "RayCluster"),
        ("ray.io", "RayJob"),
        ("ray.io", "RayService"),
        ("jobset.x-k8s.io", "JobSet"),
        ("leaderworkerset.x-k8s.io", "LeaderWorkerSet"),
        ("kueue.x-k8s.io", "ClusterQueue"),
        ("kueue.x-k8s.io", "LocalQueue"),
        ("kueue.x-k8s.io", "ResourceFlavor"),
        ("kueue.x-k8s.io", "WorkloadPriorityClass"),
    }
)

CORE_KNOWN_KINDS: frozenset[tuple[str, str]] = (
    WORKLOAD_KINDS
    | CLUSTER_SCOPED_KINDS
    | RBAC_KINDS
    | CONTROL_PLANE_EXTENSION_KINDS
    | FLOWCONTROL_KINDS
    | CERT_AND_AUTH_KINDS
    | COORDINATION_KINDS
    | DRA_KINDS
    | GKE_NETWORKING_KINDS
    | GKE_COMPUTE_AUTOSCALING_KINDS
    | MONITORING_KINDS
    | GITOPS_RECONCILER_KINDS
    | DATA_STATEFUL_DELETE_KINDS
    | AI_ML_WORKLOAD_KINDS
    | frozenset(
        {
            ("", "ConfigMap"),
            ("", "Secret"),
            ("", "Service"),
            ("", "Endpoints"),
            ("", "ServiceAccount"),
            ("", "PodTemplate"),
            ("", "ResourceQuota"),
            ("", "LimitRange"),
            ("discovery.k8s.io", "EndpointSlice"),
            ("policy", "PodDisruptionBudget"),
            ("autoscaling", "HorizontalPodAutoscaler"),
            ("autoscaling.k8s.io", "VerticalPodAutoscaler"),
            ("keda.sh", "ScaledObject"),
            ("keda.sh", "ScaledJob"),
            ("keda.sh", "TriggerAuthentication"),
            ("keda.sh", "ClusterTriggerAuthentication"),
            ("networking.k8s.io", "Ingress"),
            ("networking.k8s.io", "NetworkPolicy"),
            ("gateway.networking.k8s.io", "Gateway"),
            ("gateway.networking.k8s.io", "HTTPRoute"),
            ("gateway.networking.k8s.io", "GRPCRoute"),
            ("gateway.networking.k8s.io", "TCPRoute"),
            ("gateway.networking.k8s.io", "TLSRoute"),
            ("gateway.networking.k8s.io", "UDPRoute"),
            ("gateway.networking.k8s.io", "ReferenceGrant"),
            ("gateway.networking.k8s.io", "ReferencePolicy"),
            ("gateway.networking.k8s.io", "BackendTLSPolicy"),
            ("gateway.networking.k8s.io", "BackendLBPolicy"),
            ("networking.istio.io", "VirtualService"),
            ("networking.istio.io", "DestinationRule"),
            ("networking.istio.io", "Gateway"),
            ("networking.istio.io", "ServiceEntry"),
            ("networking.istio.io", "Sidecar"),
            ("networking.istio.io", "WorkloadEntry"),
            ("networking.istio.io", "WorkloadGroup"),
            ("networking.istio.io", "ProxyConfig"),
            ("networking.istio.io", "EnvoyFilter"),
            ("security.istio.io", "PeerAuthentication"),
            ("security.istio.io", "RequestAuthentication"),
            ("security.istio.io", "AuthorizationPolicy"),
            ("extensions.istio.io", "WasmPlugin"),
            ("telemetry.istio.io", "Telemetry"),
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
    "sidecar.istio.io/",
    "traffic.sidecar.istio.io/",
    "ambient.istio.io/",
    "security.istio.io/",
    "iam.gke.io/",
    "eks.amazonaws.com/",
    "azure.workload.identity/",
    "argocd.argoproj.io/",
    "fluxcd.io/",
    "kustomize.toolkit.fluxcd.io/",
    "helm.toolkit.fluxcd.io/",
    "configsync.gke.io/",
    "configmanagement.gke.io/",
    "helm.sh/",
    "cnrm.cloud.google.com/",
    "nginx.ingress.kubernetes.io/",
    "service.beta.kubernetes.io/",
    "service.kubernetes.io/",
    "external-dns.alpha.kubernetes.io/",
    "cert-manager.io/",
    "container.apparmor.security.beta.kubernetes.io/",
    "seccomp.security.alpha.kubernetes.io/",
    "k8s.v1.cni.cncf.io/",
    "vault.hashicorp.com/",
    "secrets-store.csi.k8s.io/",
    "kubernetes.io/",
    "k8s.io/",
    "networking.gke.io/",
    "cloud.google.com/",
    "storageclass.kubernetes.io/",
    "kubectl.kubernetes.io/",
)

# Secret-like key names & high-confidence credential patterns
_CAMEL_BOUNDARY_1_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_2_RE = re.compile(r"([a-z0-9])([A-Z])")
_SECRET_KEY_EXACT_FILENAMES = frozenset(
    {
        "tls.key",
        "server.key",
        "client.key",
        "ca.key",
        "privkey.pem",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        ".dockerconfigjson",
        ".dockercfg",
        ".npmrc",
        ".netrc",
        ".htpasswd",
        ".pgpass",
        ".pypirc",
        "credentials.json",
        "service-account.json",
    }
)
_SECRET_KEY_NAME_RE = re.compile(
    r"(?:^|[._-])(?:"
    r"PASSWORD|PASSWD|SECRET|PRIVATE[_-]?KEY|SIGNING[_-]?KEY|ENCRYPTION[_-]?KEY|MASTER[_-]?KEY|"
    r"API[_-]?KEY|ACCESS[_-]?KEY|SECRET[_-]?KEY|STRIPE[_-]?KEY|WEBHOOK[_-]?SECRET|"
    r"AUTH[_-]?TOKEN|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|SESSION[_-]?TOKEN|ID[_-]?TOKEN|API[_-]?TOKEN|"
    r"BEARER(?:[_-]?TOKEN)?|GITHUB[_-]?TOKEN|GITLAB[_-]?TOKEN|SLACK[_-]?TOKEN|"
    r"CLIENT[_-]?SECRET|CREDENTIALS?|DB[_-]?PASS|JWT[_-]?SECRET"
    r")(?:$|[._-])",
    re.IGNORECASE,
)


def _is_secret_key_name(key: str) -> bool:
    low = key.strip().lower()
    if low in _SECRET_KEY_EXACT_FILENAMES:
        return True
    snake = _CAMEL_BOUNDARY_2_RE.sub(r"\1_\2", _CAMEL_BOUNDARY_1_RE.sub(r"\1_\2", key.strip()))
    return bool(_SECRET_KEY_NAME_RE.search(key) or _SECRET_KEY_NAME_RE.search(snake))


_DANGEROUS_ENV_KEY_RE = re.compile(
    r"^(?:LD_PRELOAD|LD_LIBRARY_PATH|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY|"
    r"NODE_TLS_REJECT_UNAUTHORIZED|NODE_OPTIONS|NODE_EXTRA_CA_CERTS|"
    r"PYTHONHTTPSVERIFY|PYTHONPATH|PYTHONSTARTUP|RUBYOPT|PERL5OPT|BASH_ENV|"
    r"SSL_CERT_FILE|SSL_CERT_DIR|REQUESTS_CA_BUNDLE|CURL_CA_BUNDLE|"
    r"GODEBUG|JAVA_TOOL_OPTIONS|JDK_JAVA_OPTIONS|JVM_ARGS|GLIBC_TUNABLES|DOTNET_STARTUP_HOOKS)$",
    re.IGNORECASE,
)

_CREDENTIAL_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PEM private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GCP API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("API secret key", re.compile(r"\bsk-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_-]{20,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("HuggingFace token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("npm access token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("SendGrid API key", re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")),
    ("JWT bearer token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "URI with embedded password",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqp|amqps)://[^/\s:@]*:[^/\s@]{3,}@"),
    ),
)

# Image tag immutability patterns (3-part semver or explicit/full git SHA; rejects 2-part floating tags like :16.2)
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMVER_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z._-]+)?(?:\+[0-9A-Za-z._-]+)?$")
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
    "/lib64",
    "/bin",
    "/sbin",
    "/usr",
    "/home",
    "/var/lib/kubelet",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/var/lib/crio",
    "/var/lib/etcd",
    "/proc",
    "/sys",
    "/dev",
)

# Raw / network / SAN volume plugin fields in PodSpec.volumes
_EXTERNAL_STORAGE_VOLUME_KEYS = (
    "nfs",
    "iscsi",
    "fc",
    "rbd",
    "cephfs",
    "glusterfs",
    "cinder",
    "vsphereVolume",
    "awsElasticBlockStore",
    "gcePersistentDisk",
    "azureDisk",
    "azureFile",
    "portworxVolume",
    "quobyte",
    "scaleIO",
    "storageos",
    "flocker",
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
    {
        "system:anonymous",
        "system:unauthenticated",
        "system:authenticated",
        "system:serviceaccounts",
        "system:serviceaccounts:kube-system",
    }
)
_PRIV_ESCALATION_VERBS = frozenset({"*", "impersonate", "escalate", "bind"})
_SENSITIVE_RBAC_RESOURCES = frozenset(
    {
        "*",
        "secrets",
        "pods/exec",
        "pods/attach",
        "pods/ephemeralcontainers",
        "pods/portforward",
        "nodes/proxy",
        "serviceaccounts/token",
        "certificatesigningrequests",
        "certificatesigningrequests/approval",
        "signers",
        "tokenreviews",
        "clusterroles",
        "clusterrolebindings",
        "roles",
        "rolebindings",
        "mutatingwebhookconfigurations",
        "validatingwebhookconfigurations",
        "validatingadmissionpolicies",
        "validatingadmissionpolicybindings",
        "mutatingadmissionpolicies",
        "mutatingadmissionpolicybindings",
        "apiservices",
        "customresourcedefinitions",
    }
)
_CRITICAL_IAM_ROLES = frozenset(
    {
        "roles/owner",
        "roles/editor",
        "roles/iam.securityadmin",
        "roles/iam.roleadmin",
        "roles/iam.serviceaccountkeyadmin",
        "roles/iam.serviceaccounttokencreator",
        "roles/container.admin",
        "roles/container.clusteradmin",
        "roles/resourcemanager.projectiamadmin",
        "roles/resourcemanager.folderadmin",
        "roles/resourcemanager.folderiamadmin",
        "roles/resourcemanager.organizationadmin",
    }
)
_PUBLIC_IAM_MEMBERS = frozenset({"allusers", "allauthenticatedusers"})

_INGRESS_SNIPPET_KEYS = (
    "-snippet",
    "configuration-snippet",
    "server-snippet",
    "stream-snippet",
    "lua-resty",
    "fastcgi-params-configmap",
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


_ACTION_VERBS: dict[str, str] = {
    ACTION_ADD: "adds",
    ACTION_MODIFY: "modifies",
    ACTION_DELETE: "deletes",
}


def _action_verb(action: str) -> str:
    return _ACTION_VERBS.get(action, f"{action}es" if action.endswith("s") else (f"{action[:-1]}ies" if action.endswith("y") else f"{action}s"))


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
        try:
            val = float(raw) * 1000.0
        except OverflowError:
            return None
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
        try:
            val = float(raw)
        except OverflowError:
            return None
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


def _is_zero_quota_value(raw: Any) -> bool:
    """Return True if a ResourceQuota or LimitRange quantity evaluates to zero."""
    if raw is None or isinstance(raw, bool):
        return False
    if isinstance(raw, (int, float)):
        return raw == 0
    if parse_cpu_millicores(raw) == 0.0 or parse_memory_bytes(raw) == 0.0:
        return True
    if isinstance(raw, str) and raw.strip() in ("0", "0.0", "+0", "-0"):
        return True
    return False


def _opt_str_field(
    ev: ResourceEval | None, obj: dict[str, Any], key: str, field_path: str
) -> tuple[bool, str]:
    """Extract an optional string field, flagging non-string values on `ev` if present."""
    val = obj.get(key)
    if val is None:
        return True, ""
    if isinstance(val, str):
        return True, val
    if ev is not None:
        ev.add(
            "MALFORMED_FIELD_TYPE",
            DIM_SECURITY,
            RISK_HIGH,
            f"expected string at {field_path}.{key}, got {type(val).__name__}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{field_path}.{key}",
            security=True,
        )
    return False, ""


def _req_str_field(
    ev: ResourceEval | None, obj: dict[str, Any], key: str, field_path: str
) -> tuple[bool, str]:
    """Extract a required string field, flagging missing/non-string values on `ev` if present."""
    val = obj.get(key)
    if isinstance(val, str):
        return True, val
    if ev is not None and val is not None:
        ev.add(
            "MALFORMED_FIELD_TYPE",
            DIM_SECURITY,
            RISK_HIGH,
            f"expected string at {field_path}.{key}, got {type(val).__name__}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{field_path}.{key}",
            security=True,
        )
    return False, ""


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
    if "://" in img or "?" in img or "#" in img:
        return False
    repo, tag, digest = split_image_ref(img)
    if not repo or repo.startswith("/") or repo.endswith("/") or ".." in repo.split("/"):
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
        parts = re.split(r"[-._+]", low)
        if any(p in _MUTABLE_TAG_TOKENS for p in parts):
            return False
        return True
    return False


def is_kube_agents_group(group: str) -> bool:
    low = group.lower()
    return (
        low in ("kubeagents.x-k8s.io", "kube-agents.x-k8s.io")
        or low.endswith(".kubeagents.x-k8s.io")
        or low.endswith(".kube-agents.x-k8s.io")
    )


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


_NON_PROD_COMPOUND_RE = re.compile(
    r"(?:^|(?<=[-_./\s]))(?:non|not|pre)[-_.]*(?:production|prod|prd|live)(?=$|[-_./\s])"
)


def _strip_negated_prod_compounds(text: str) -> str:
    return _NON_PROD_COMPOUND_RE.sub(" ", text)


def _is_production_labels(labels: dict[str, Any]) -> bool:
    for key in ENV_LABEL_KEYS:
        val = labels.get(key)
        if isinstance(val, str):
            cleaned = _strip_negated_prod_compounds(val.strip().lower())
            tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(cleaned) if t]
            if any(t in PRODUCTION_VALUES for t in tokens):
                return True
    return False


def _is_production_namespace(namespace: str) -> bool:
    if not isinstance(namespace, str) or not namespace.strip():
        return False
    cleaned = _strip_negated_prod_compounds(namespace.strip().lower()).strip()
    if cleaned == "live":
        return True
    tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(cleaned) if t]
    return any(t in PRODUCTION_PATH_VALUES for t in tokens)


def _is_production_path(file_path: str) -> bool:
    parts = [p for p in file_path.replace("\\", "/").lower().split("/") if p]
    if not parts:
        return False
    dir_parts, filename = parts[:-1], parts[-1]
    for seg in dir_parts:
        cleaned = _strip_negated_prod_compounds(seg).strip()
        if cleaned == "live":
            return True
        tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(cleaned) if t]
        if any(t in PRODUCTION_PATH_VALUES for t in tokens):
            return True
    cleaned_file = _strip_negated_prod_compounds(filename)
    file_tokens = [t for t in _PROD_TOKEN_SPLIT_RE.split(cleaned_file) if t]
    return any(t in PRODUCTION_PATH_VALUES for t in file_tokens)


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
    if kind == "PodTemplate":
        tmpl = obj.get("template")
        if not isinstance(tmpl, dict):
            spec = obj.get("spec")
            tmpl = spec.get("template") if isinstance(spec, dict) else None
        if not isinstance(tmpl, dict):
            return None
        ps = tmpl.get("spec")
        return ps if isinstance(ps, dict) else None
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
    if kind == "PodTemplate":
        tmpl = obj.get("template")
        if not isinstance(tmpl, dict):
            spec = obj.get("spec")
            tmpl = spec.get("template") if isinstance(spec, dict) else None
        if not isinstance(tmpl, dict):
            return {}
        m = tmpl.get("metadata")
        return m if isinstance(m, dict) else {}
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
    clean = "/" + clean.lstrip("/")
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


def _check_uid_gid_field(ev: ResourceEval, sc: dict[str, Any], key: str, prefix: str) -> None:
    if key not in sc or sc.get(key) is None:
        return
    val = sc.get(key)
    if not _is_strict_int(val) or val < 0:
        ev.add(
            "MALFORMED_FIELD_TYPE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{prefix}.{key} must be a non-negative integer, got {val!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path=f"{prefix}.{key}",
            security=True,
        )
    elif val == 0:
        ev.add(
            "SECURITY_CONTEXT_LOOSENED",
            DIM_SECURITY,
            RISK_HIGH,
            f"sets {prefix}.{key} to 0 (root)",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.{key}",
            security=True,
        )


def _check_confinement_profile(ev: ResourceEval, sc: dict[str, Any], key: str, prefix: str) -> None:
    if key not in sc or sc.get(key) is None:
        return
    raw = sc.get(key)
    prof = _as_dict(ev, raw, f"{prefix}.{key}")
    if not isinstance(raw, dict):
        return
    ptype = prof.get("type")
    if not isinstance(ptype, str) or ptype not in ("RuntimeDefault", "Localhost"):
        ev.add(
            "SECURITY_CONTEXT_LOOSENED",
            DIM_SECURITY,
            RISK_HIGH,
            f"sets {prefix}.{key}.type to non-hardened value {ptype!r}",
            blast=BLAST_MEDIUM,
            field_path=f"{prefix}.{key}.type",
            security=True,
        )
    elif ptype == "Localhost":
        lp = prof.get("localhostProfile")
        if not isinstance(lp, str) or not lp.strip() or ".." in lp.replace("\\", "/").split("/"):
            ev.add(
                "SECURITY_CONTEXT_LOOSENED",
                DIM_SECURITY,
                RISK_HIGH,
                f"sets invalid or traversal {prefix}.{key}.localhostProfile={lp!r}",
                blast=BLAST_MEDIUM,
                field_path=f"{prefix}.{key}.localhostProfile",
                security=True,
            )


def _check_proc_mount(ev: ResourceEval, sc: dict[str, Any], prefix: str) -> None:
    if "procMount" not in sc or sc.get("procMount") is None:
        return
    pm = sc.get("procMount")
    if not isinstance(pm, str) or pm.strip() != "Default":
        ev.add(
            "SECURITY_CONTEXT_LOOSENED",
            DIM_SECURITY,
            RISK_HIGH,
            f"sets {prefix}.procMount to {pm!r} (only 'Default' is hardened)",
            blast=BLAST_HIGH,
            field_path=f"{prefix}.procMount",
            security=True,
        )


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
    for id_key in ("runAsUser", "runAsGroup", "fsGroup"):
        b_id = b_sc.get(id_key)
        a_id = a_sc.get(id_key)
        if _is_strict_int(b_id) and b_id > 0 and (not _is_strict_int(a_id) or a_id <= 0):
            ev.add(
                "SECURITY_HARDENING_REMOVED",
                DIM_SECURITY,
                RISK_HIGH,
                f"removes or weakens non-root {prefix}.{id_key}={b_id}",
                blast=BLAST_MEDIUM,
                field_path=f"{prefix}.{id_key}",
                security=True,
            )
    for prof_key in ("seccompProfile", "appArmorProfile"):
        b_p = b_sc.get(prof_key)
        a_p = a_sc.get(prof_key)
        if isinstance(b_p, dict) and b_p.get("type") in ("RuntimeDefault", "Localhost"):
            weakened = (
                not isinstance(a_p, dict)
                or a_p.get("type") not in ("RuntimeDefault", "Localhost")
                or (b_p.get("type") == "Localhost" and a_p != b_p)
            )
            if weakened:
                ev.add(
                    "SECURITY_HARDENING_REMOVED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"removes or weakens {prefix}.{prof_key}",
                    blast=BLAST_MEDIUM,
                    field_path=f"{prefix}.{prof_key}",
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


def _check_probe_and_lifecycle_security(
    ev: ResourceEval,
    ctr: dict[str, Any],
    before_ctr: dict[str, Any] | None,
    prefix: str,
) -> None:
    """Inspect container probes and lifecycle hooks for exec RCE and kubelet host SSRF."""
    for pk in ("readinessProbe", "livenessProbe", "startupProbe"):
        raw_p = ctr.get(pk)
        if raw_p is None:
            continue
        pdict = _as_dict(ev, raw_p, f"{prefix}.{pk}")
        if not isinstance(raw_p, dict):
            continue
        b_pdict = before_ctr.get(pk) if isinstance(before_ctr, dict) and isinstance(before_ctr.get(pk), dict) else {}
        if "exec" in pdict and pdict.get("exec") is not None:
            _as_dict(ev, pdict.get("exec"), f"{prefix}.{pk}.exec")
            if before_ctr is None or pdict.get("exec") != b_pdict.get("exec"):
                ev.add(
                    "PROBE_EXEC_COMMAND_CHANGED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"sets or modifies {pk}.exec command on {prefix}",
                    blast=BLAST_MEDIUM,
                    field_path=f"{prefix}.{pk}.exec",
                    security=True,
                )
        for hk in ("httpGet", "tcpSocket"):
            hsub = pdict.get(hk)
            if isinstance(hsub, dict) and hsub.get("host"):
                b_hsub = b_pdict.get(hk) if isinstance(b_pdict.get(hk), dict) else {}
                if before_ctr is None or hsub.get("host") != b_hsub.get("host"):
                    ev.add(
                        "PROBE_HOST_SSRF_RISK",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"sets custom kubelet probe target {hk}.host={hsub.get('host')!r} on {prefix}.{pk}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{prefix}.{pk}.{hk}.host",
                        security=True,
                    )

    raw_lc = ctr.get("lifecycle")
    if raw_lc is not None:
        lc = _as_dict(ev, raw_lc, f"{prefix}.lifecycle")
        b_lc = before_ctr.get("lifecycle") if isinstance(before_ctr, dict) and isinstance(before_ctr.get("lifecycle"), dict) else {}
        for hook in ("postStart", "preStop"):
            if lc.get(hook) is None:
                continue
            hd = _as_dict(ev, lc.get(hook), f"{prefix}.lifecycle.{hook}")
            b_hd = b_lc.get(hook) if isinstance(b_lc.get(hook), dict) else {}
            if hd.get("exec") is not None and (before_ctr is None or hd.get("exec") != b_hd.get("exec")):
                _as_dict(ev, hd.get("exec"), f"{prefix}.lifecycle.{hook}.exec")
                ev.add(
                    "LIFECYCLE_EXEC_HOOK",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"defines or modifies lifecycle.{hook}.exec command on {prefix}",
                    blast=BLAST_MEDIUM,
                    field_path=f"{prefix}.lifecycle.{hook}.exec",
                    security=True,
                )
            for hk in ("httpGet", "tcpSocket"):
                hsub = hd.get(hk)
                if isinstance(hsub, dict) and hsub.get("host"):
                    b_hsub = b_hd.get(hk) if isinstance(b_hd.get(hk), dict) else {}
                    if before_ctr is None or hsub.get("host") != b_hsub.get("host"):
                        ev.add(
                            "PROBE_HOST_SSRF_RISK",
                            DIM_SECURITY,
                            RISK_HIGH,
                            f"sets custom kubelet lifecycle {hook}.{hk}.host={hsub.get('host')!r} on {prefix}",
                            blast=BLAST_MEDIUM,
                            field_path=f"{prefix}.lifecycle.{hook}.{hk}.host",
                            security=True,
                        )


def _check_pod_spec_posture(
    ev: ResourceEval,
    pod_spec: dict[str, Any],
    *,
    before_pod_spec: dict[str, Any] | None = None,
    is_daemonset: bool = False,
    require_containers: bool = True,
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

    # K8s User Namespaces: `hostUsers: false` is hardened (isolated userns); `hostUsers: true` is host userns
    if "hostUsers" in pod_spec and pod_spec.get("hostUsers") is not None:
        hu = pod_spec.get("hostUsers")
        if not isinstance(hu, bool):
            ev.add(
                "MALFORMED_FIELD_TYPE",
                DIM_SECURITY,
                RISK_HIGH,
                f"sets non-boolean PodSpec hostUsers={hu!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="hostUsers",
                security=True,
            )
        elif hu is True and before_pod_spec is not None and b_ps.get("hostUsers") is not True:
            if b_ps.get("hostUsers") is False:
                ev.add(
                    "SECURITY_HARDENING_REMOVED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    "disables user-namespace isolation (hostUsers: false -> true)",
                    blast=BLAST_HIGH,
                    field_path="hostUsers",
                    security=True,
                )
            else:
                ev.add(
                    "POD_SECURITY_POSTURE_WIDENED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    "explicitly sets PodSpec hostUsers=true",
                    blast=BLAST_MEDIUM,
                    field_path="hostUsers",
                    security=True,
                )
    elif before_pod_spec is not None and b_ps.get("hostUsers") is False:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            "removes PodSpec hostUsers=false user-namespace isolation",
            blast=BLAST_HIGH,
            field_path="hostUsers",
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

    if "automountServiceAccountToken" in pod_spec and pod_spec.get("automountServiceAccountToken") is not None:
        asat = pod_spec.get("automountServiceAccountToken")
        if not isinstance(asat, bool):
            ev.add(
                "MALFORMED_FIELD_TYPE",
                DIM_SECURITY,
                RISK_HIGH,
                f"sets non-boolean automountServiceAccountToken={asat!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="automountServiceAccountToken",
                security=True,
            )
        elif asat is True and before_pod_spec is not None and b_ps.get("automountServiceAccountToken") is not True:
            if b_ps.get("automountServiceAccountToken") is False:
                ev.add(
                    "SECURITY_HARDENING_REMOVED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    "enables or removes automountServiceAccountToken=false",
                    blast=BLAST_MEDIUM,
                    field_path="automountServiceAccountToken",
                    security=True,
                )
            else:
                ev.add(
                    "SERVICE_ACCOUNT_TOKEN_MOUNT",
                    DIM_SECURITY,
                    RISK_HIGH,
                    "explicitly enables PodSpec automountServiceAccountToken=true",
                    blast=BLAST_MEDIUM,
                    field_path="automountServiceAccountToken",
                    security=True,
                )
    elif before_pod_spec is not None and b_ps.get("automountServiceAccountToken") is False:
        ev.add(
            "SECURITY_HARDENING_REMOVED",
            DIM_SECURITY,
            RISK_HIGH,
            "enables or removes automountServiceAccountToken=false",
            blast=BLAST_MEDIUM,
            field_path="automountServiceAccountToken",
            security=True,
        )

    if before_pod_spec is not None:
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
        for add_key in ("hostAliases", "dnsConfig", "runtimeClassName", "nodeName"):
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
        if pod_spec.get("dnsPolicy") and str(pod_spec.get("dnsPolicy")) != "ClusterFirst":
            ev.add(
                "POD_RUNTIME_OR_NETWORK_CHANGED",
                DIM_SECURITY,
                RISK_HIGH,
                f"sets non-default PodSpec dnsPolicy={pod_spec.get('dnsPolicy')!r}",
                blast=BLAST_MEDIUM,
                field_path="dnsPolicy",
                security=True,
            )

    # Index prior volumes & containers by name for diff-aware secret/CSI/PVC checks on MODIFY
    b_vols_by_name: dict[str, dict[str, Any]] = {}
    if before_pod_spec is not None and isinstance(b_ps.get("volumes"), list):
        for bv in b_ps["volumes"]:
            if isinstance(bv, dict) and isinstance(bv.get("name"), str) and bv["name"]:
                b_vols_by_name[bv["name"]] = bv

    secret_bearing_vnames: set[str] = set()
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
        vol_changed = before_pod_spec is None or vname not in b_vols_by_name or b_vols_by_name[vname] != vol

        if "hostPath" in vol and vol.get("hostPath") is not None:
            hp = _as_dict(ev, vol.get("hostPath"), f"volumes.{vname}.hostPath")
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
        if "gitRepo" in vol and vol.get("gitRepo") is not None:
            _as_dict(ev, vol.get("gitRepo"), f"volumes.{vname}.gitRepo")
            host_escape = True
            ev.add(
                "DEPRECATED_GITREPO_VOLUME",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"mounts deprecated gitRepo volume {vname!r} (kubelet host execution hazard)",
                blast=BLAST_CRITICAL,
                field_path=f"volumes.{vname}.gitRepo",
                security=True,
            )
        if "flexVolume" in vol and vol.get("flexVolume") is not None:
            _as_dict(ev, vol.get("flexVolume"), f"volumes.{vname}.flexVolume")
            host_escape = True
            ev.add(
                "EXTERNAL_OR_RAW_STORAGE_VOLUME",
                DIM_SECURITY,
                RISK_CRITICAL if is_daemonset else RISK_HIGH,
                f"mounts node-driver flexVolume in volume {vname!r}",
                blast=BLAST_HIGH,
                field_path=f"volumes.{vname}.flexVolume",
                security=True,
            )
        for ext_vk in _EXTERNAL_STORAGE_VOLUME_KEYS:
            if ext_vk in vol and vol.get(ext_vk) is not None:
                _as_dict(ev, vol.get(ext_vk), f"volumes.{vname}.{ext_vk}")
                ev.add(
                    "EXTERNAL_OR_RAW_STORAGE_VOLUME",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"mounts direct {ext_vk} storage volume {vname!r}",
                    blast=BLAST_HIGH,
                    field_path=f"volumes.{vname}.{ext_vk}",
                    security=True,
                )
        if "secret" in vol and vol.get("secret") is not None:
            _as_dict(ev, vol.get("secret"), f"volumes.{vname}.secret")
            secret_bearing_vnames.add(vname)
            if vol_changed:
                ev.add(
                    "SECRET_VOLUME_MOUNT",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"mounts Kubernetes Secret in volume {vname!r}",
                    blast=BLAST_MEDIUM,
                    field_path=f"volumes.{vname}.secret",
                    security=True,
                )
        if "projected" in vol and vol.get("projected") is not None:
            proj = _as_dict(ev, vol.get("projected"), f"volumes.{vname}.projected")
            for src in _as_list(ev, proj.get("sources"), f"volumes.{vname}.projected.sources"):
                if not isinstance(src, dict):
                    continue
                if "secret" in src and src.get("secret") is not None:
                    _as_dict(ev, src.get("secret"), f"volumes.{vname}.projected.secret")
                    secret_bearing_vnames.add(vname)
                    if vol_changed:
                        ev.add(
                            "SECRET_VOLUME_MOUNT",
                            DIM_SECURITY,
                            RISK_HIGH,
                            f"mounts projected Secret in volume {vname!r}",
                            blast=BLAST_MEDIUM,
                            field_path=f"volumes.{vname}.projected",
                            security=True,
                        )
                if "serviceAccountToken" in src and src.get("serviceAccountToken") is not None:
                    _as_dict(ev, src.get("serviceAccountToken"), f"volumes.{vname}.projected.serviceAccountToken")
                    secret_bearing_vnames.add(vname)
                    if vol_changed:
                        ev.add(
                            "SERVICE_ACCOUNT_TOKEN_MOUNT",
                            DIM_SECURITY,
                            RISK_HIGH,
                            f"mounts projected serviceAccountToken in volume {vname!r}",
                            blast=BLAST_MEDIUM,
                            field_path=f"volumes.{vname}.projected.serviceAccountToken",
                            security=True,
                        )
                if "clusterTrustBundle" in src and src.get("clusterTrustBundle") is not None:
                    _as_dict(ev, src.get("clusterTrustBundle"), f"volumes.{vname}.projected.clusterTrustBundle")
                    if vol_changed:
                        ev.add(
                            "POD_SECURITY_POSTURE_WIDENED",
                            DIM_SECURITY,
                            RISK_HIGH,
                            f"mounts projected clusterTrustBundle in volume {vname!r}",
                            blast=BLAST_MEDIUM,
                            field_path=f"volumes.{vname}.projected.clusterTrustBundle",
                            security=True,
                        )
        if "csi" in vol and vol.get("csi") is not None:
            csi = _as_dict(ev, vol.get("csi"), f"volumes.{vname}.csi")
            secret_bearing_vnames.add(vname)
            if vol_changed:
                drv = str(csi.get("driver") or "")
                is_sec_csi = any(tok in drv.lower() for tok in ("secret", "vault", "kms", "cert"))
                ev.add(
                    "SECRET_VOLUME_MOUNT" if is_sec_csi else "CSI_VOLUME_MOUNT",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"mounts CSI driver {drv!r} in volume {vname!r}",
                    blast=BLAST_MEDIUM if is_sec_csi else BLAST_HIGH,
                    field_path=f"volumes.{vname}.csi",
                    security=True,
                )
        if "image" in vol and vol.get("image") is not None:
            _as_dict(ev, vol.get("image"), f"volumes.{vname}.image")
            secret_bearing_vnames.add(vname)
            if vol_changed:
                ev.add(
                    "OCI_IMAGE_VOLUME_MOUNT",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"mounts OCI image volume {vname!r}",
                    blast=BLAST_MEDIUM,
                    field_path=f"volumes.{vname}.image",
                    security=True,
                )
        for pvc_vk in ("persistentVolumeClaim", "ephemeral"):
            if pvc_vk in vol and vol.get(pvc_vk) is not None:
                _as_dict(ev, vol.get(pvc_vk), f"volumes.{vname}.{pvc_vk}")
                if vol_changed:
                    ev.add(
                        "STATEFUL_OR_EPHEMERAL_VOLUME",
                        DIM_REVERTABILITY,
                        RISK_MEDIUM,
                        f"attaches {pvc_vk} storage in volume {vname!r}",
                        revert=REVERT_MODERATE,
                        field_path=f"volumes.{vname}.{pvc_vk}",
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
        _check_uid_gid_field(ev, psc, "runAsUser", "securityContext")
        _check_uid_gid_field(ev, psc, "runAsGroup", "securityContext")
        _check_uid_gid_field(ev, psc, "fsGroup", "securityContext")
        _check_proc_mount(ev, psc, "securityContext")
        _check_confinement_profile(ev, psc, "seccompProfile", "securityContext")
        _check_confinement_profile(ev, psc, "appArmorProfile", "securityContext")
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
            if sg and (0 in sg or any(not _is_strict_int(g) or g < 0 for g in sg) or before_pod_spec is None or sg != b_psc.get("supplementalGroups")):
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
        if wo is not None:
            wo_d = _as_dict(ev, wo, "securityContext.windowsOptions")
            if "hostProcess" in wo_d and _is_truthy_or_nonbool(wo_d.get("hostProcess")):
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

    # Incomplete workload fragment check (e.g. Kustomize patch missing spec.template.spec.containers)
    raw_ctrs = pod_spec.get("containers")
    if require_containers and (not isinstance(raw_ctrs, list) or len(raw_ctrs) == 0):
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

    if before_pod_spec is None and pod_spec.get("ephemeralContainers"):
        ev.add(
            "EPHEMERAL_CONTAINER_SPECIFIED",
            DIM_SECURITY,
            RISK_HIGH,
            f"{ev.ref.gvk_label()} {ev.ref.name!r} defines ephemeralContainers in static manifest",
            blast=BLAST_MEDIUM,
            field_path="ephemeralContainers",
            security=True,
        )

    # Containers (regular, init, ephemeral)
    privileged_any = False
    for list_key in ("containers", "initContainers", "ephemeralContainers"):
        ctrs = _as_list(ev, pod_spec.get(list_key), list_key)
        b_ctrs_by_name: dict[str, dict[str, Any]] = {}
        if before_pod_spec is not None and isinstance(b_ps.get(list_key), list):
            for bc_item in b_ps[list_key]:
                if isinstance(bc_item, dict) and isinstance(bc_item.get("name"), str) and bc_item["name"].strip():
                    b_ctrs_by_name[bc_item["name"]] = bc_item

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
            b_ctr = b_ctrs_by_name.get(cname) if isinstance(cname, str) else None
            b_env_by_name: dict[str, dict[str, Any]] = {}
            if isinstance(b_ctr, dict) and isinstance(b_ctr.get("env"), list):
                for be in b_ctr["env"]:
                    if isinstance(be, dict) and isinstance(be.get("name"), str):
                        b_env_by_name[be["name"]] = be

            # Secret env / envFrom, dangerous env keys, & plaintext secret leaks
            for env_item in _as_list(ev, ctr.get("env"), f"{list_key}.{label}.env"):
                if not isinstance(env_item, dict):
                    continue
                ename = str(env_item.get("name") or "")
                env_changed = b_ctr is None or b_env_by_name.get(ename) != env_item
                if ename and _DANGEROUS_ENV_KEY_RE.match(ename) and env_changed:
                    ev.add(
                        "DANGEROUS_RUNTIME_CONFIG_KEY",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets security-sensitive env key {ename!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.env.{ename}",
                        security=True,
                    )
                vf = env_item.get("valueFrom")
                if vf is not None:
                    vf_d = _as_dict(ev, vf, f"{list_key}.{label}.env.{ename}.valueFrom")
                    if "secretKeyRef" in vf_d and vf_d.get("secretKeyRef") is not None:
                        _as_dict(ev, vf_d.get("secretKeyRef"), f"{list_key}.{label}.env.{ename}.valueFrom.secretKeyRef")
                        if env_changed:
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
                if val is not None and str(val) != "" and ename and _is_secret_key_name(ename) and env_changed:
                    ev.add(
                        "PLAINTEXT_SECRET_KEY_IN_ENV",
                        DIM_SECURITY,
                        RISK_CRITICAL,
                        f"container {label!r} sets literal value on secret-named env key {ename!r}",
                        blast=BLAST_HIGH,
                        revert=REVERT_IRREVERSIBLE,
                        field_path=f"{list_key}.{label}.env.{ename}",
                        security=True,
                    )
                if isinstance(val, str):
                    for _, pat_name in _scan_value_for_secrets(val):
                        ev.add(
                            "PLAINTEXT_SECRET_LEAK",
                            DIM_SECURITY,
                            RISK_CRITICAL,
                            f"plaintext {pat_name} detected in container {label!r} env {ename!r}",
                            blast=BLAST_HIGH,
                            revert=REVERT_IRREVERSIBLE,
                            field_path=f"{list_key}.{label}.env",
                            security=True,
                        )
            b_ef_list = b_ctr.get("envFrom") if isinstance(b_ctr, dict) and isinstance(b_ctr.get("envFrom"), list) else []
            for ef in _as_list(ev, ctr.get("envFrom"), f"{list_key}.{label}.envFrom"):
                if isinstance(ef, dict) and "secretRef" in ef and ef.get("secretRef") is not None:
                    _as_dict(ev, ef.get("secretRef"), f"{list_key}.{label}.envFrom.secretRef")
                    if b_ctr is None or ef not in b_ef_list:
                        ev.add(
                            "SECRET_ENV_REF",
                            DIM_SECURITY,
                            RISK_HIGH,
                            f"container {label!r} mounts Secret via envFrom.secretRef",
                            blast=BLAST_MEDIUM,
                            field_path=f"{list_key}.{label}.envFrom",
                            security=True,
                        )

            # Volume mounts (mountPropagation, new/widened mounts of secret/CSI volumes) & raw block volumeDevices
            b_vm_tuples: set[tuple[str, str, str, str]] = set()
            for vm in (
                b_ctr.get("volumeMounts")
                if isinstance(b_ctr, dict) and isinstance(b_ctr.get("volumeMounts"), list)
                else []
            ):
                if not isinstance(vm, dict):
                    continue
                ok_n, bn = _req_str_field(None, vm, "name", "")
                ok_p, bp = _req_str_field(None, vm, "mountPath", "")
                ok_s, bs = _opt_str_field(None, vm, "subPath", "")
                ok_e, be = _opt_str_field(None, vm, "subPathExpr", "")
                if ok_n and ok_p and ok_s and ok_e:
                    b_vm_tuples.add((bn, bp, bs, be))

            vm_field_path = f"{list_key}.{label}.volumeMounts"
            for vm in _as_list(ev, ctr.get("volumeMounts"), vm_field_path):
                if not isinstance(vm, dict):
                    continue
                mp_mode = vm.get("mountPropagation")
                if mp_mode is not None and str(mp_mode) != "None":
                    ev.add(
                        "POD_SECURITY_POSTURE_WIDENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets volumeMount mountPropagation={mp_mode!r}",
                        blast=BLAST_HIGH,
                        field_path=vm_field_path,
                        security=True,
                    )
                ok_n, vm_name = _req_str_field(ev, vm, "name", vm_field_path)
                ok_p, vm_mp = _req_str_field(ev, vm, "mountPath", vm_field_path)
                ok_s, vm_sp = _opt_str_field(ev, vm, "subPath", vm_field_path)
                ok_e, vm_spe = _opt_str_field(ev, vm, "subPathExpr", vm_field_path)
                if (
                    before_pod_spec is not None
                    and ok_n
                    and vm_name in secret_bearing_vnames
                    and (
                        not (ok_p and ok_s and ok_e)
                        or (vm_name, vm_mp, vm_sp, vm_spe) not in b_vm_tuples
                    )
                ):
                    ev.add(
                        "SECRET_VOLUME_MOUNT",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} adds or modifies volumeMount of secret/CSI volume {vm_name!r}",
                        blast=BLAST_MEDIUM,
                        field_path=vm_field_path,
                        security=True,
                    )

            vdevs = _as_list(ev, ctr.get("volumeDevices"), f"{list_key}.{label}.volumeDevices")
            if vdevs:
                ev.add(
                    "RAW_BLOCK_DEVICE_MOUNT",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"container {label!r} mounts raw block volumeDevices",
                    blast=BLAST_HIGH,
                    field_path=f"{list_key}.{label}.volumeDevices",
                    security=True,
                )

            tm_path = ctr.get("terminationMessagePath")
            if tm_path is not None and (not isinstance(tm_path, str) or tm_path.strip() != "/dev/termination-log" or ".." in tm_path):
                if b_ctr is None or b_ctr.get("terminationMessagePath") != tm_path:
                    ev.add(
                        "POD_SECURITY_POSTURE_WIDENED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {label!r} sets custom terminationMessagePath={tm_path!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{list_key}.{label}.terminationMessagePath",
                        security=True,
                    )

            # Probes & lifecycle hooks (exec commands & kubelet SSRF)
            _check_probe_and_lifecycle_security(ev, ctr, b_ctr, f"{list_key}.{label}")

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
            b_csc = b_ctr.get("securityContext") if isinstance(b_ctr, dict) and isinstance(b_ctr.get("securityContext"), dict) else {}
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
                _check_uid_gid_field(ev, sc, "runAsUser", f"{list_key}.{label}.securityContext")
                _check_uid_gid_field(ev, sc, "runAsGroup", f"{list_key}.{label}.securityContext")
                _check_proc_mount(ev, sc, f"{list_key}.{label}.securityContext")
                _check_confinement_profile(ev, sc, "seccompProfile", f"{list_key}.{label}.securityContext")
                _check_confinement_profile(ev, sc, "appArmorProfile", f"{list_key}.{label}.securityContext")
                if sc.get("seLinuxOptions") and (b_ctr is None or sc.get("seLinuxOptions") != b_csc.get("seLinuxOptions")):
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
                if c_wo is not None:
                    c_wo_d = _as_dict(ev, c_wo, f"{list_key}.{label}.securityContext.windowsOptions")
                    if "hostProcess" in c_wo_d and _is_truthy_or_nonbool(c_wo_d.get("hostProcess")):
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
            b_res = b_ctr.get("resources") if isinstance(b_ctr, dict) and isinstance(b_ctr.get("resources"), dict) else {}
            if res:
                if res.get("claims") and (b_ctr is None or res.get("claims") != b_res.get("claims")):
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
                    b_rmap = b_res.get(section) if isinstance(b_res.get(section), dict) else {}
                    for rkey in sorted(rmap.keys(), key=str):
                        if "/" in str(rkey) or "gpu" in str(rkey).lower() or "tpu" in str(rkey).lower():
                            if b_ctr is None or b_rmap.get(rkey) != rmap.get(rkey):
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


_PROTECTED_ADD_EXEMPT_KEYS = frozenset(
    {
        "environment",
        "env",
        "stage",
        "tier",
        "app.kubernetes.io/instance",
        "app.kubernetes.io/managed-by",
        "storageclass.kubernetes.io/is-default-class",
    }
)


def _check_rbac_superuser(ev: ResourceEval, obj: dict[str, Any]) -> None:
    """Detect cluster-admin/admin/edit bindings, wildcard rules, privilege escalation verbs, and broad subjects."""
    kind = ev.ref.kind
    in_system = ev.ref.namespace in SYSTEM_NAMESPACES
    if kind in ("ClusterRoleBinding", "RoleBinding"):
        role_ref = _as_dict(ev, obj.get("roleRef"), "roleRef")
        rname = str(role_ref.get("name") or "")
        if rname in _SUPERUSER_ROLES and (kind == "ClusterRoleBinding" or in_system or rname == "system:masters"):
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
            _as_dict(ev, obj.get("aggregationRule"), "aggregationRule")
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
            matches_sens = bool(resources & _SENSITIVE_RBAC_RESOURCES) or any(
                r == "pods/*" or r.endswith(("/exec", "/attach", "/proxy", "/ephemeralcontainers")) for r in resources
            )
            if (verbs & (_PRIV_ESCALATION_VERBS | {"approve", "sign"})) and (is_cluster or matches_sens):
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} rule[{idx}] grants wildcard or privilege-escalation verbs {sorted(verbs)}",
                    blast=BLAST_CRITICAL if is_cluster else BLAST_HIGH,
                    field_path=f"rules[{idx}]",
                    security=True,
                )
            elif is_cluster and matches_sens and (verbs - {"get", "list", "watch"} or "secrets" in resources or "*" in resources):
                ev.add(
                    "CRITICAL_PRIVILEGED_RBAC",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"{kind} {ev.ref.name!r} rule[{idx}] grants cluster-wide access to sensitive resources {sorted(resources)}",
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


def _check_metadata_add(ev: ResourceEval, obj: dict[str, Any]) -> None:
    """Validate metadata fields, finalizers, ownerReferences, and protected keys on ACTION_ADD."""
    a_meta = obj.get("metadata")
    if not isinstance(a_meta, dict):
        return
    _as_dict(ev, a_meta.get("labels"), "metadata.labels")
    _as_dict(ev, a_meta.get("annotations"), "metadata.annotations")
    if a_meta.get("generateName") is not None:
        ev.add(
            "METADATA_IDENTITY_MODIFIED",
            DIM_SECURITY,
            RISK_HIGH,
            f"sets non-deterministic metadata.generateName on {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
            field_path="metadata.generateName",
            security=True,
        )
    if a_meta.get("finalizers") is not None:
        fins = _as_list(ev, a_meta.get("finalizers"), "metadata.finalizers")
        if fins:
            ev.add(
                "LIFECYCLE_METADATA_MODIFIED",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"sets metadata.finalizers on {ev.ref.gvk_label()} {ev.ref.name!r}",
                revert=REVERT_DIFFICULT,
                field_path="metadata.finalizers",
                security=True,
            )
    if a_meta.get("ownerReferences") is not None:
        owners = _as_list(ev, a_meta.get("ownerReferences"), "metadata.ownerReferences")
        if owners:
            ev.add(
                "LIFECYCLE_METADATA_MODIFIED",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"sets metadata.ownerReferences on {ev.ref.gvk_label()} {ev.ref.name!r} (GC cascade-delete hazard)",
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path="metadata.ownerReferences",
                security=True,
            )
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
        for mk, _ in sorted(_meta_dict(obj, meta_field).items(), key=str):
            mks = str(mk)
            low_k = mks.lower()
            if low_k == "kube-agents/risk-class":
                ev.add(
                    "RISK_METADATA_TAMPERING",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    "sets kube-agents/risk-class override annotation",
                    blast=BLAST_HIGH,
                    field_path=f"metadata.{meta_field}.{mks}",
                    security=True,
                )
            elif any(sn in low_k for sn in _INGRESS_SNIPPET_KEYS):
                ev.add(
                    "INGRESS_SNIPPET_INJECTION",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"sets raw proxy snippet annotation {mks!r}",
                    blast=BLAST_CRITICAL,
                    field_path=f"metadata.{meta_field}.{mks}",
                    security=True,
                )
            elif low_k not in _PROTECTED_ADD_EXEMPT_KEYS and _is_protected_metadata_key(mks):
                ev.add(
                    "PROTECTED_METADATA_MODIFIED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"sets protected metadata.{meta_field} key {mks!r}",
                    blast=BLAST_MEDIUM,
                    field_path=f"metadata.{meta_field}.{mks}",
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
        if "finalizers" in a_meta and a_meta.get("finalizers") is not None:
            _as_list(ev, a_meta.get("finalizers"), "metadata.finalizers")
        if "ownerReferences" in a_meta and a_meta.get("ownerReferences") is not None:
            _as_list(ev, a_meta.get("ownerReferences"), "metadata.ownerReferences")

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
    if (old_override is not None or new_override is not None) and new_override != old_override:
        ev.add(
            "RISK_METADATA_TAMPERING",
            DIM_SECURITY,
            RISK_CRITICAL,
            "sets, modifies, or removes kube-agents/risk-class override annotation",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if old_override is not None else DISRUPT_NONE,
            field_path="metadata.annotations.kube-agents/risk-class",
            security=True,
        )

    for d in diffs:
        if not d.tokens or d.tokens[0] != "metadata":
            continue
        sub = d.tokens[1] if len(d.tokens) > 1 else ""
        if sub in ("name", "generateName", "namespace"):
            ev.add(
                "METADATA_IDENTITY_MODIFIED",
                DIM_SECURITY,
                RISK_HIGH,
                f"modifies resource identity field metadata.{sub} on {ev.ref.gvk_label()} {ev.ref.name!r}",
                blast=BLAST_MEDIUM,
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DISRUPTIVE,
                field_path=d.dotted,
                security=True,
            )
        elif sub in ("finalizers", "ownerReferences"):
            is_destructive = d.op == "remove" or sub == "ownerReferences"
            ev.add(
                "LIFECYCLE_METADATA_MODIFIED",
                DIM_REVERTABILITY,
                RISK_HIGH,
                f"modifies metadata.{sub} on {ev.ref.gvk_label()} {ev.ref.name!r}",
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DESTRUCTIVE if is_destructive else DISRUPT_NONE,
                field_path=d.dotted,
                security=True,
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
                    revert=REVERT_MODERATE,
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


def _check_pod_template_metadata(
    ev: ResourceEval,
    kind: str,
    before_obj: dict[str, Any] | None,
    after_obj: dict[str, Any],
    *,
    action: str,
) -> None:
    """Inspect Pod template metadata (`spec.template.metadata`) for selector breaks & protected annotations."""
    if kind == "Pod":
        return
    a_tmeta = _extract_pod_template_meta(kind, after_obj)
    b_tmeta = _extract_pod_template_meta(kind, before_obj) if before_obj is not None else {}
    tmeta_prefix = (
        "template.metadata"
        if kind == "PodTemplate"
        else ("spec.jobTemplate.spec.template.metadata" if kind == "CronJob" else "spec.template.metadata")
    )
    if a_tmeta:
        _as_dict(ev, a_tmeta.get("labels"), f"{tmeta_prefix}.labels")
        _as_dict(ev, a_tmeta.get("annotations"), f"{tmeta_prefix}.annotations")

    a_tlbl = a_tmeta.get("labels") if isinstance(a_tmeta.get("labels"), dict) else {}
    b_tlbl = b_tmeta.get("labels") if isinstance(b_tmeta.get("labels"), dict) else {}
    a_tann = a_tmeta.get("annotations") if isinstance(a_tmeta.get("annotations"), dict) else {}
    b_tann = b_tmeta.get("annotations") if isinstance(b_tmeta.get("annotations"), dict) else {}

    if action == ACTION_MODIFY:
        for k, old_v in sorted(b_tlbl.items(), key=str):
            if k not in a_tlbl or a_tlbl[k] != old_v:
                ev.add(
                    "POD_TEMPLATE_LABEL_CHANGED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"changes or removes pod template label {k!r} (may detach Service/PDB/NetworkPolicy selectors)",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{tmeta_prefix}.labels.{k}",
                )

    for sub_name, b_map, a_map in (("labels", b_tlbl, a_tlbl), ("annotations", b_tann, a_tann)):
        all_keys = set(a_map.keys()) if action == ACTION_ADD else (set(a_map.keys()) | set(b_map.keys()))
        for k in sorted(all_keys, key=str):
            if action == ACTION_MODIFY and b_map.get(k) == a_map.get(k):
                continue
            ks = str(k)
            low_k = ks.lower()
            if low_k == "kube-agents/risk-class":
                ev.add(
                    "RISK_METADATA_TAMPERING",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"sets or modifies kube-agents/risk-class on {tmeta_prefix}.{sub_name}",
                    blast=BLAST_HIGH,
                    field_path=f"{tmeta_prefix}.{sub_name}.{ks}",
                    security=True,
                )
            elif any(sn in low_k for sn in _INGRESS_SNIPPET_KEYS):
                ev.add(
                    "INGRESS_SNIPPET_INJECTION",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"sets raw proxy snippet key {ks!r} on {tmeta_prefix}.{sub_name}",
                    blast=BLAST_CRITICAL,
                    field_path=f"{tmeta_prefix}.{sub_name}.{ks}",
                    security=True,
                )
            elif (action == ACTION_MODIFY or low_k not in _PROTECTED_ADD_EXEMPT_KEYS) and _is_protected_metadata_key(ks):
                ev.add(
                    "PROTECTED_METADATA_MODIFIED",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"{_action_verb(action)} protected {tmeta_prefix}.{sub_name} key {ks!r}",
                    blast=BLAST_MEDIUM,
                    field_path=f"{tmeta_prefix}.{sub_name}.{ks}",
                    security=True,
                )


_MAX_UNAVAILABLE_PCT_LIMIT = 50.0
_MAX_UNAVAILABLE_RATIO_LIMIT = 0.5


def _is_aggressive_max_unavailable(max_unavail: Any, replicas_est: int = 1) -> bool:
    if isinstance(max_unavail, str) and max_unavail.strip().endswith("%"):
        try:
            pct = float(max_unavail.strip()[:-1])
            return pct > _MAX_UNAVAILABLE_PCT_LIMIT or pct < 0.0 or not math.isfinite(pct)
        except ValueError:
            return True
    if max_unavail is not None and not _is_strict_int(max_unavail):
        return True
    if _is_strict_int(max_unavail):
        if max_unavail < 0:
            return True
        if (
            max_unavail > 1
            and replicas_est > 0
            and max_unavail / replicas_est > _MAX_UNAVAILABLE_RATIO_LIMIT
        ):
            return True
    return False


def _check_workload_add(ev: ResourceEval, obj: dict[str, Any]) -> None:
    """Evaluate creation (ACTION_ADD) of Deployment, Rollout, StatefulSet, DaemonSet, ReplicaSet, ReplicationController, Job, CronJob, Pod."""
    kind = ev.ref.kind
    is_daemonset = kind == "DaemonSet"
    spec = _as_dict(ev, obj.get("spec"), "spec")
    ps = _extract_pod_spec(kind, obj) or {}
    has_workload_ref = kind == "Rollout" and isinstance(spec.get("workloadRef"), dict) and "template" not in spec

    _check_pod_template_metadata(ev, kind, None, obj, action=ACTION_ADD)
    _check_pod_spec_posture(ev, ps, is_daemonset=is_daemonset, require_containers=not has_workload_ref)

    # Validate replicas on new controller workloads
    rep = 1
    if kind in ("Deployment", "Rollout", "StatefulSet", "ReplicaSet", "ReplicationController") and "replicas" in spec and spec["replicas"] is not None:
        rep_raw = spec.get("replicas")
        if not _is_strict_int(rep_raw) or rep_raw < 0:
            ev.add(
                "INVALID_REPLICAS",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"sets invalid non-integer or negative spec.replicas={rep_raw!r} on {kind} {ev.ref.name!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.replicas",
            )
        elif rep_raw == 0:
            rep = 0
            ev.add(
                "WORKLOAD_SCALED_TO_ZERO",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"creates {kind} {ev.ref.name!r} with spec.replicas=0",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.replicas",
            )
        else:
            rep = rep_raw
            if rep > MAX_LOW_REPLICA_TOTAL:
                ev.add(
                    "WORKLOAD_LARGE_SCALE_UP",
                    DIM_BLAST_RADIUS,
                    RISK_HIGH,
                    f"creates {kind} {ev.ref.name!r} with high replica count {rep} (above {MAX_LOW_REPLICA_TOTAL})",
                    blast=BLAST_HIGH if rep >= 50 else BLAST_MEDIUM,
                    field_path="spec.replicas",
                )

    if kind == "Rollout" and "strategy" in spec and spec.get("strategy") is not None:
        rstrat = _as_dict(ev, spec.get("strategy"), "spec.strategy")
        for rkey in ("canary", "blueGreen"):
            if rkey in rstrat and rstrat.get(rkey) is not None:
                _as_dict(ev, rstrat.get(rkey), f"spec.strategy.{rkey}")

    if kind == "Deployment" and "strategy" in spec and spec.get("strategy") is not None:
        strat = _as_dict(ev, spec.get("strategy"), "spec.strategy")
        if str(strat.get("type") or "") == "Recreate":
            ev.add(
                "RECREATE_STRATEGY_SET",
                DIM_REVERTABILITY,
                RISK_MEDIUM,
                f"creates Deployment {ev.ref.name!r} with Recreate strategy",
                revert=REVERT_MODERATE,
                field_path="spec.strategy.type",
            )
        else:
            ru = strat.get("rollingUpdate") if isinstance(strat.get("rollingUpdate"), dict) else {}
            if _is_aggressive_max_unavailable(ru.get("maxUnavailable"), rep):
                ev.add(
                    "AGGRESSIVE_ROLLOUT_STRATEGY",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"sets invalid or aggressive Deployment rollingUpdate.maxUnavailable={ru.get('maxUnavailable')!r}",
                    revert=REVERT_MODERATE,
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.strategy.rollingUpdate.maxUnavailable",
                )

    if kind in ("CronJob", "Job") and "suspend" in spec and spec.get("suspend") is not None:
        susp = spec.get("suspend")
        if not isinstance(susp, bool):
            ev.add(
                "MALFORMED_FIELD_TYPE",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"sets non-boolean spec.suspend={susp!r} on {kind} {ev.ref.name!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.suspend",
            )
        elif susp is True:
            ev.add(
                "CRONJOB_SUSPENDED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"creates suspended {kind} {ev.ref.name!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.suspend",
            )

    if kind == "StatefulSet":
        vcts = _as_list(ev, spec.get("volumeClaimTemplates"), "spec.volumeClaimTemplates")
        if vcts:
            ev.add(
                "STATEFULSET_CREATES_PVCS",
                DIM_REVERTABILITY,
                RISK_MEDIUM,
                f"creates StatefulSet {ev.ref.name!r} with persistent volumeClaimTemplates",
                revert=REVERT_DIFFICULT,
                field_path="spec.volumeClaimTemplates",
            )
        ret = spec.get("persistentVolumeClaimRetentionPolicy")
        if ret is not None:
            ret_d = _as_dict(ev, ret, "spec.persistentVolumeClaimRetentionPolicy")
            if str(ret_d.get("whenDeleted") or "") == "Delete" or str(ret_d.get("whenScaled") or "") == "Delete":
                ev.add(
                    "STATEFULSET_PVC_DELETE_POLICY",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    "creates StatefulSet with persistentVolumeClaimRetentionPolicy Delete",
                    revert=REVERT_DIFFICULT,
                    disrupt=DISRUPT_DESTRUCTIVE,
                    field_path="spec.persistentVolumeClaimRetentionPolicy",
                )

    # Validate container image pinning & CPU/memory bounds + compound capacity delta on ADD
    pod_prefix = (
        "spec"
        if kind == "Pod"
        else ("spec.jobTemplate.spec.template.spec" if kind == "CronJob" else "spec.template.spec")
    )
    for list_key in ("containers", "initContainers", "ephemeralContainers"):
        ctrs = ps.get(list_key) if isinstance(ps.get(list_key), list) else []
        for idx, ctr in enumerate(ctrs):
            if not isinstance(ctr, dict):
                continue
            cname = ctr.get("name") if isinstance(ctr.get("name"), str) and ctr.get("name").strip() else str(idx)
            c_path = f"{pod_prefix}.{list_key}.{cname}"
            cimg = ctr.get("image")
            if isinstance(cimg, str) and cimg.strip() and not is_immutable_image_pin(cimg):
                ev.add(
                    "UNPINNED_IMAGE_TAG",
                    DIM_REVERTABILITY,
                    RISK_MEDIUM,
                    f"container {cname!r} uses mutable/unpinned image reference {cimg!r}",
                    revert=REVERT_UNKNOWN,
                    field_path=f"{c_path}.image",
                )
            res = ctr.get("resources") if isinstance(ctr.get("resources"), dict) else {}
            req = res.get("requests") if isinstance(res.get("requests"), dict) else {}
            lim = res.get("limits") if isinstance(res.get("limits"), dict) else {}
            cpu_req = parse_cpu_millicores(req.get("cpu"))
            cpu_lim = parse_cpu_millicores(lim.get("cpu"))
            mem_req = parse_memory_bytes(req.get("memory"))
            mem_lim = parse_memory_bytes(lim.get("memory"))
            if cpu_req is not None and cpu_lim is not None and cpu_req > cpu_lim:
                ev.add(
                    "RESOURCE_REQUEST_EXCEEDS_LIMIT",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"container {cname!r} CPU request exceeds limit",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.resources",
                )
            if mem_req is not None and mem_lim is not None and mem_req > mem_lim:
                ev.add(
                    "RESOURCE_REQUEST_EXCEEDS_LIMIT",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"container {cname!r} memory request exceeds limit",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.resources",
                )
            if list_key == "containers":
                ev.cpu_delta_m += (cpu_req or 0.0) * rep
                ev.mem_delta_bytes += (mem_req or 0.0) * rep
            for section, rmap in (("requests", req), ("limits", lim)):
                for rname, parser_fn, floor_val, max_val in (
                    ("cpu", parse_cpu_millicores, MIN_SAFE_CPU_M, MAX_LOW_CONTAINER_CPU_M),
                    ("memory", parse_memory_bytes, MIN_SAFE_MEM_B, MAX_LOW_CONTAINER_MEM_B),
                ):
                    rv_raw = rmap.get(rname)
                    if rv_raw is None:
                        continue
                    rv = parser_fn(rv_raw)
                    fp = f"{c_path}.resources.{section}.{rname}"
                    if rv is None:
                        ev.add(
                            "INVALID_RESOURCE_QUANTITY",
                            DIM_DISRUPTIVENESS,
                            RISK_HIGH,
                            f"unparseable {rname} quantity {rv_raw!r} on container {cname!r}",
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=fp,
                        )
                    elif rv < floor_val:
                        ev.add(
                            "CONTAINER_RESOURCE_BELOW_FLOOR",
                            DIM_DISRUPTIVENESS,
                            RISK_HIGH,
                            f"sets {section}.{rname} below safe floor on container {cname!r}",
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=fp,
                        )
                    elif rv > max_val:
                        ev.add(
                            "CONTAINER_RESOURCE_LARGE_INCREASE",
                            DIM_BLAST_RADIUS,
                            RISK_HIGH,
                            f"sets large {section}.{rname}={rv_raw!r} on container {cname!r}",
                            blast=BLAST_HIGH,
                            field_path=fp,
                        )

    if kind == "Job":
        ev.add(
            "JOB_CREATED",
            DIM_REVERTABILITY,
            RISK_HIGH,
            f"creates batch/Job {ev.ref.name!r} (one-off execution is not cleanly revertable)",
            revert=REVERT_IRREVERSIBLE,
        )
    elif kind == "DaemonSet":
        ev.add(
            "NEW_DAEMONSET",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"creates DaemonSet {ev.ref.name!r} across cluster nodes",
            blast=BLAST_HIGH,
        )
    else:
        ev.add(
            "NEW_WORKLOAD",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"creates new {kind} {ev.ref.name!r}",
            revert=REVERT_MODERATE if kind in ("Pod", "ReplicaSet", "ReplicationController", "StatefulSet") else REVERT_EASY,
        )


def _check_pod_template(
    ev: ResourceEval,
    action: str,
    before_obj: dict[str, Any] | None,
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Evaluate core v1/PodTemplate resources (top-level `template.spec` and `template.metadata`)."""
    _check_pod_template_metadata(ev, "PodTemplate", before_obj, after_obj, action=action)
    a_ps = _extract_pod_spec("PodTemplate", after_obj) or {}
    b_ps = _extract_pod_spec("PodTemplate", before_obj) if action == ACTION_MODIFY and before_obj is not None else None
    _check_pod_spec_posture(ev, a_ps, before_pod_spec=b_ps, require_containers=True)
    for list_key in ("containers", "initContainers", "ephemeralContainers"):
        for idx, ctr in enumerate(a_ps.get(list_key) if isinstance(a_ps.get(list_key), list) else []):
            if isinstance(ctr, dict) and isinstance(ctr.get("image"), str) and not is_immutable_image_pin(ctr["image"]):
                cname = ctr.get("name") if isinstance(ctr.get("name"), str) and ctr.get("name").strip() else str(idx)
                ev.add(
                    "UNPINNED_IMAGE_TAG",
                    DIM_REVERTABILITY,
                    RISK_MEDIUM,
                    f"PodTemplate container {cname!r} uses mutable/unpinned image {ctr['image']!r}",
                    revert=REVERT_UNKNOWN,
                    field_path=f"template.spec.{list_key}.{cname}.image",
                )
    if action == ACTION_MODIFY and before_obj is not None:
        for list_key in ("containers", "initContainers", "ephemeralContainers"):
            b_ctrs = {
                c["name"]: c
                for c in ((b_ps or {}).get(list_key) if isinstance((b_ps or {}).get(list_key), list) else [])
                if isinstance(c, dict) and isinstance(c.get("name"), str)
            }
            a_ctrs = {
                c["name"]: c
                for c in (a_ps.get(list_key) if isinstance(a_ps.get(list_key), list) else [])
                if isinstance(c, dict) and isinstance(c.get("name"), str)
            }
            for cname in sorted(set(b_ctrs.keys()) | set(a_ctrs.keys())):
                if cname not in b_ctrs:
                    ev.add("CONTAINER_ADDED", DIM_SECURITY, RISK_HIGH, f"adds PodTemplate {list_key} {cname!r}", security=True)
                elif cname not in a_ctrs:
                    ev.add("CONTAINER_REMOVED", DIM_DISRUPTIVENESS, RISK_HIGH, f"removes PodTemplate {list_key} {cname!r}", disrupt=DISRUPT_DISRUPTIVE)
                else:
                    bc, ac = b_ctrs[cname], a_ctrs[cname]
                    b_repo, _, _ = split_image_ref(str(bc.get("image") or ""))
                    a_repo, _, _ = split_image_ref(str(ac.get("image") or ""))
                    if b_repo != a_repo:
                        ev.add("IMAGE_REPOSITORY_CHANGED", DIM_SECURITY, RISK_HIGH, f"changes PodTemplate {list_key} {cname!r} image repository", security=True)
                    for exec_key in ("command", "args", "workingDir", "lifecycle"):
                        if bc.get(exec_key) != ac.get(exec_key):
                            ev.add("CONTAINER_ENTRYPOINT_CHANGED", DIM_SECURITY, RISK_HIGH, f"modifies PodTemplate {list_key} {cname!r} {exec_key}", security=True)
                    b_csc = bc.get("securityContext") if isinstance(bc.get("securityContext"), dict) else {}
                    a_csc = ac.get("securityContext") if isinstance(ac.get("securityContext"), dict) else {}
                    if b_csc:
                        _check_security_hardening_removed(ev, b_csc, a_csc, f"template.spec.{list_key}.{cname}.securityContext")
    ev.add(
        "POD_TEMPLATE_CHANGE",
        DIM_BLAST_RADIUS,
        RISK_MEDIUM,
        f"{_action_verb(action)} PodTemplate {ev.ref.name!r} (referenced by downstream controllers/buffers)",
        revert=REVERT_MODERATE,
    )


def _check_workload_modify(
    ev: ResourceEval,
    before_obj: dict[str, Any],
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Evaluate modifications to Deployment, StatefulSet, DaemonSet, ReplicaSet, ReplicationController, Job, CronJob, Pod."""
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
        if kind == "Pod" and len(t) >= 2 and t[0] == "spec" and t[1] in (
            "volumes",
            "restartPolicy",
            "serviceAccountName",
            "serviceAccount",
            "securityContext",
            "hostNetwork",
            "hostPID",
            "hostIPC",
            "hostUsers",
            "nodeName",
        ):
            ev.add(
                "IMMUTABLE_FIELD_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies immutable standalone Pod field spec.{t[1]}",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                field_path=d.dotted,
            )

    b_has_rep_early = "replicas" in b_spec and b_spec["replicas"] is not None
    a_has_rep_early = "replicas" in a_spec and a_spec["replicas"] is not None
    b_rep_est = (
        b_spec["replicas"]
        if b_has_rep_early and _is_strict_int(b_spec["replicas"]) and b_spec["replicas"] >= 0
        else 1
    )
    a_rep_est = (
        a_spec["replicas"]
        if a_has_rep_early and _is_strict_int(a_spec["replicas"]) and a_spec["replicas"] >= 0
        else 1
    )
    eff_rep_est = min(b_rep_est, a_rep_est)

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
                if _is_aggressive_max_unavailable(max_unavail, eff_rep_est):
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
                        revert=REVERT_MODERATE,
                        field_path="spec.strategy",
                    )
        else:
            if a_rep_est < b_rep_est and isinstance(a_strat, dict):
                ru = a_strat.get("rollingUpdate")
                max_unavail = ru.get("maxUnavailable") if isinstance(ru, dict) else None
                if _is_aggressive_max_unavailable(max_unavail, eff_rep_est):
                    ev.add(
                        "AGGRESSIVE_ROLLOUT_STRATEGY",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"replica scale-down makes Deployment rollingUpdate.maxUnavailable={max_unavail!r} aggressive",
                        revert=REVERT_MODERATE,
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.strategy.rollingUpdate.maxUnavailable",
                    )
            if (
                isinstance(a_strat, dict)
                and str(a_strat.get("type") or "") == "Recreate"
                and a_spec.get("template") != b_spec.get("template")
            ):
                ev.add(
                    "RECREATE_WORKLOAD_ROLLOUT",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"triggers pod rollout on Deployment {ev.ref.name!r} with Recreate strategy (causes downtime)",
                    revert=REVERT_MODERATE,
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.template",
                )

    if kind == "Rollout":
        if a_spec.get("strategy") != b_spec.get("strategy"):
            a_rstrat = a_spec.get("strategy")
            if a_rstrat is not None and not isinstance(a_rstrat, dict):
                ev.add(
                    "MALFORMED_FIELD_TYPE",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    "Rollout spec.strategy is not a mapping",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.strategy",
                )
            else:
                ev.add(
                    "ROLLOUT_STRATEGY_MODIFIED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"modifies Argo Rollout {ev.ref.name!r} progressive delivery strategy",
                    revert=REVERT_MODERATE,
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.strategy",
                )
        if a_spec.get("workloadRef") != b_spec.get("workloadRef"):
            ev.add(
                "ROLLOUT_STRATEGY_MODIFIED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"modifies Argo Rollout {ev.ref.name!r} workloadRef target",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.workloadRef",
            )

    if kind in ("DaemonSet", "StatefulSet"):
        a_ustrat = a_spec.get("updateStrategy")
        b_ustrat = b_spec.get("updateStrategy")
        if a_ustrat != b_ustrat:
            if a_ustrat is not None and not isinstance(a_ustrat, dict):
                ev.add(
                    "MALFORMED_FIELD_TYPE",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"{kind} spec.updateStrategy is not a mapping",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.updateStrategy",
                )
            else:
                ru = a_ustrat.get("rollingUpdate") if isinstance(a_ustrat, dict) else None
                max_unavail = ru.get("maxUnavailable") if isinstance(ru, dict) else None
                if _is_aggressive_max_unavailable(max_unavail, eff_rep_est):
                    ev.add(
                        "AGGRESSIVE_ROLLOUT_STRATEGY",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"sets aggressive {kind} updateStrategy.rollingUpdate.maxUnavailable={max_unavail!r}",
                        revert=REVERT_MODERATE,
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path="spec.updateStrategy.rollingUpdate.maxUnavailable",
                    )
        elif a_rep_est < b_rep_est and isinstance(a_ustrat, dict):
            ru = a_ustrat.get("rollingUpdate")
            max_unavail = ru.get("maxUnavailable") if isinstance(ru, dict) else None
            if _is_aggressive_max_unavailable(max_unavail, eff_rep_est):
                ev.add(
                    "AGGRESSIVE_ROLLOUT_STRATEGY",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"replica scale-down makes {kind} updateStrategy.rollingUpdate.maxUnavailable={max_unavail!r} aggressive",
                    revert=REVERT_MODERATE,
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.updateStrategy.rollingUpdate.maxUnavailable",
                )

    if kind == "StatefulSet":
        ev.add(
            "STATEFULSET_MODIFIED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"modifies StatefulSet {ev.ref.name!r} (ordered stateful workload)",
            revert=REVERT_MODERATE,
        )
        ret = a_spec.get("persistentVolumeClaimRetentionPolicy")
        b_ret = b_spec.get("persistentVolumeClaimRetentionPolicy")
        for ret_key in ("whenDeleted", "whenScaled"):
            if isinstance(ret, dict) and str(ret.get(ret_key) or "") == "Delete":
                if not (isinstance(b_ret, dict) and str(b_ret.get(ret_key) or "") == "Delete"):
                    ev.add(
                        "STATEFULSET_PVC_DELETE_POLICY",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"sets StatefulSet persistentVolumeClaimRetentionPolicy.{ret_key} to Delete",
                        revert=REVERT_DIFFICULT,
                        disrupt=DISRUPT_DESTRUCTIVE,
                        field_path=f"spec.persistentVolumeClaimRetentionPolicy.{ret_key}",
                    )
    elif kind == "Pod":
        ev.add(
            "STANDALONE_POD_MODIFIED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"modifies standalone Pod {ev.ref.name!r} (no controller-managed rolling rollout)",
            revert=REVERT_MODERATE,
        )
    elif kind in ("ReplicaSet", "ReplicationController"):
        ev.add(
            "DIRECT_REPLICASET_MODIFIED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"modifies {kind} {ev.ref.name!r} directly outside a Deployment rollout",
            revert=REVERT_MODERATE,
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
                        revert=REVERT_MODERATE,
                        field_path="spec.suspend",
                    )
        if kind == "CronJob" and a_spec.get("concurrencyPolicy") != b_spec.get("concurrencyPolicy"):
            if str(a_spec.get("concurrencyPolicy") or "") == "Replace":
                ev.add(
                    "CRONJOB_CONCURRENCY_REPLACE",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"sets CronJob {ev.ref.name!r} concurrencyPolicy=Replace (terminates active jobs)",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path="spec.concurrencyPolicy",
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
            ret_pol = a_spec.get("persistentVolumeClaimRetentionPolicy")
            sts_deletes_pvcs = (
                kind == "StatefulSet"
                and isinstance(a_spec.get("volumeClaimTemplates"), list)
                and bool(a_spec["volumeClaimTemplates"])
                and isinstance(ret_pol, dict)
                and str(ret_pol.get("whenScaled") or "") == "Delete"
            )
            if a_rep < b_rep and sts_deletes_pvcs:
                ev.add(
                    "STATEFULSET_PVC_DESTRUCTIVE_SCALE_DOWN",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"scales down StatefulSet {ev.ref.name!r} ({b_rep} -> {a_rep}) with whenScaled=Delete (destroys PVCs)",
                    revert=REVERT_IRREVERSIBLE,
                    disrupt=DISRUPT_DESTRUCTIVE,
                    field_path="spec.replicas",
                )
            elif a_rep <= 0:
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
                        revert=REVERT_MODERATE,
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

    # 3. Pod template metadata (selector label removal + protected annotations/labels)
    _check_pod_template_metadata(ev, kind, before_obj, after_obj, action=ACTION_MODIFY)

    # 4. PodSpec posture & container-level diffs
    b_ps = _extract_pod_spec(kind, before_obj) or {}
    a_ps = _extract_pod_spec(kind, after_obj) or {}
    has_workload_ref = (
        kind == "Rollout"
        and isinstance(a_spec.get("workloadRef"), dict)
        and "template" not in a_spec
        and "template" not in b_spec
    )
    _check_pod_spec_posture(
        ev, a_ps, before_pod_spec=b_ps, is_daemonset=is_daemonset, require_containers=not has_workload_ref
    )

    # DaemonSet always fans out across every matching node
    if is_daemonset:
        ev.add(
            "DAEMONSET_NODE_FANOUT",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"modifies DaemonSet {ev.ref.name!r} (rolls out across every matching cluster node)",
            blast=BLAST_HIGH,
            revert=REVERT_MODERATE,
        )

    # Track per-container diffs (images, resources, command/args/lifecycle, removals/additions)
    handled_prefixes: set[tuple[str, ...]] = {
        ("metadata",),
        ("spec", "replicas"),
        ("spec", "selector"),
        ("spec", "strategy"),
        ("spec", "workloadRef"),
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

            # Image diff (including digest-pin stripping check)
            b_img = str(bc.get("image") or "")
            a_img = str(ac.get("image") or "")
            if b_img != a_img:
                b_repo, _, b_digest = split_image_ref(b_img)
                a_repo, _, a_digest = split_image_ref(a_img)
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
                elif b_digest and not a_digest:
                    ev.add(
                        "IMAGE_DIGEST_PIN_REMOVED",
                        DIM_REVERTABILITY,
                        RISK_MEDIUM,
                        f"strips immutable @sha256 digest pin from container {cname!r} image ({b_img!r} -> {a_img!r})",
                        revert=REVERT_MODERATE,
                        field_path=f"{c_path}.image",
                    )
                else:
                    ev.add(
                        "PINNED_IMAGE_UPDATE",
                        DIM_BLAST_RADIUS,
                        RISK_LOW,
                        f"updates container {cname!r} image to pinned reference {a_img!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{c_path}.image",
                    )

            # Command / args / workingDir / lifecycle / restartPolicy (sidecar initContainer mode)
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
            if bc.get("restartPolicy") != ac.get("restartPolicy"):
                ev.add(
                    "CONTAINER_LIFECYCLE_MODE_CHANGED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"modifies {list_key} {cname!r} restartPolicy ({bc.get('restartPolicy')!r} -> {ac.get('restartPolicy')!r})",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.restartPolicy",
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

            # Identity-aware removals/replacements of ports, volumeMounts, volumeDevices, env, envFrom
            b_ports = bc.get("ports") if isinstance(bc.get("ports"), list) else []
            a_ports = ac.get("ports") if isinstance(ac.get("ports"), list) else []
            b_port_ids: set[tuple[int | str, str, str]] = set()
            for p in b_ports:
                if not isinstance(p, dict):
                    continue
                cp = p.get("containerPort")
                proto = p.get("protocol")
                pname = p.get("name")
                if (
                    isinstance(cp, (int, str))
                    and not isinstance(cp, bool)
                    and (proto is None or isinstance(proto, str))
                    and (pname is None or isinstance(pname, str))
                ):
                    b_port_ids.add((cp, (proto or "TCP").upper(), pname or ""))
            a_port_ids: set[tuple[int | str, str, str]] = set()
            for p in a_ports:
                if not isinstance(p, dict):
                    continue
                cp = p.get("containerPort")
                proto = p.get("protocol")
                pname = p.get("name")
                if (
                    isinstance(cp, (int, str))
                    and not isinstance(cp, bool)
                    and (proto is None or isinstance(proto, str))
                    and (pname is None or isinstance(pname, str))
                ):
                    a_port_ids.add((cp, (proto or "TCP").upper(), pname or ""))
                else:
                    ev.add(
                        "MALFORMED_FIELD_TYPE",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"container {cname!r} has non-scalar port fields",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{c_path}.ports",
                        security=True,
                    )
            if len(a_ports) < len(b_ports) or (b_port_ids - a_port_ids):
                ev.add(
                    "CONTAINER_COMPONENT_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes or changes exposed containerPort entries on container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.ports",
                )

            b_vms = bc.get("volumeMounts") if isinstance(bc.get("volumeMounts"), list) else []
            a_vms = ac.get("volumeMounts") if isinstance(ac.get("volumeMounts"), list) else []
            b_vm_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for vm in b_vms:
                if not isinstance(vm, dict):
                    continue
                ok_n, bn = _req_str_field(None, vm, "name", "")
                ok_p, bp = _req_str_field(None, vm, "mountPath", "")
                ok_s, bs = _opt_str_field(None, vm, "subPath", "")
                ok_e, be = _opt_str_field(None, vm, "subPathExpr", "")
                if ok_n and ok_p and ok_s and ok_e:
                    b_vm_map[(bn, bp, bs, be)] = vm
            a_vm_map: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for vm in a_vms:
                if not isinstance(vm, dict):
                    continue
                ok_n, an = _req_str_field(ev, vm, "name", f"{c_path}.volumeMounts")
                ok_p, ap = _req_str_field(ev, vm, "mountPath", f"{c_path}.volumeMounts")
                ok_s, asub = _opt_str_field(ev, vm, "subPath", f"{c_path}.volumeMounts")
                ok_e, aexpr = _opt_str_field(ev, vm, "subPathExpr", f"{c_path}.volumeMounts")
                if ok_n and ok_p and ok_s and ok_e:
                    a_vm_map[(an, ap, asub, aexpr)] = vm
            if len(a_vms) < len(b_vms) or (set(b_vm_map.keys()) - set(a_vm_map.keys())):
                ev.add(
                    "CONTAINER_COMPONENT_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes or relocates volumeMounts on container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.volumeMounts",
                )
            for vm_key, b_vm in b_vm_map.items():
                a_vm = a_vm_map.get(vm_key)
                if isinstance(a_vm, dict) and b_vm.get("readOnly") is True and a_vm.get("readOnly") is not True:
                    ev.add(
                        "SECURITY_HARDENING_REMOVED",
                        DIM_SECURITY,
                        RISK_HIGH,
                        f"removes readOnly=true on container {cname!r} volumeMount {vm_key[0]!r}",
                        blast=BLAST_MEDIUM,
                        field_path=f"{c_path}.volumeMounts",
                        security=True,
                    )

            b_vds = bc.get("volumeDevices") if isinstance(bc.get("volumeDevices"), list) else []
            a_vds = ac.get("volumeDevices") if isinstance(ac.get("volumeDevices"), list) else []
            b_vd_ids: set[tuple[str, str]] = {
                (vd["name"], vd["devicePath"])
                for vd in b_vds
                if isinstance(vd, dict) and isinstance(vd.get("name"), str) and isinstance(vd.get("devicePath"), str)
            }
            a_vd_ids: set[tuple[str, str]] = set()
            for vd in a_vds:
                if not isinstance(vd, dict):
                    continue
                ok_n, dn = _req_str_field(ev, vd, "name", f"{c_path}.volumeDevices")
                ok_p, dp = _req_str_field(ev, vd, "devicePath", f"{c_path}.volumeDevices")
                if ok_n and ok_p:
                    a_vd_ids.add((dn, dp))
            if len(a_vds) < len(b_vds) or (b_vd_ids - a_vd_ids):
                ev.add(
                    "CONTAINER_COMPONENT_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes volumeDevices from container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.volumeDevices",
                )

            b_envs = bc.get("env") if isinstance(bc.get("env"), list) else []
            a_envs = ac.get("env") if isinstance(ac.get("env"), list) else []
            b_env_map = {e["name"]: e for e in b_envs if isinstance(e, dict) and isinstance(e.get("name"), str)}
            a_env_map = {e["name"]: e for e in a_envs if isinstance(e, dict) and isinstance(e.get("name"), str)}
            if len(a_envs) < len(b_envs) or (set(b_env_map.keys()) - set(a_env_map.keys())):
                ev.add(
                    "CONTAINER_COMPONENT_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes env entries from container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.env",
                )
            for ek, b_ev_item in b_env_map.items():
                a_ev_item = a_env_map.get(ek)
                if isinstance(a_ev_item, dict) and b_ev_item.get("value") and a_ev_item.get("value") == "" and not a_ev_item.get("valueFrom"):
                    ev.add(
                        "CONTAINER_ENV_WIPED",
                        DIM_DISRUPTIVENESS,
                        RISK_HIGH,
                        f"wipes env {ek!r} to empty string on container {cname!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=f"{c_path}.env.{ek}",
                    )

            b_efs = bc.get("envFrom") if isinstance(bc.get("envFrom"), list) else []
            a_efs = ac.get("envFrom") if isinstance(ac.get("envFrom"), list) else []
            if len(a_efs) < len(b_efs) or any(item not in a_efs for item in b_efs):
                ev.add(
                    "CONTAINER_COMPONENT_REMOVED",
                    DIM_DISRUPTIVENESS,
                    RISK_HIGH,
                    f"removes envFrom entries from container {cname!r}",
                    disrupt=DISRUPT_DISRUPTIVE,
                    field_path=f"{c_path}.envFrom",
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

    # Volume removals or in-place volume source swaps (safe against unhashable non-string volume names)
    b_vols = b_ps.get("volumes") if isinstance(b_ps.get("volumes"), list) else []
    a_vols = a_ps.get("volumes") if isinstance(a_ps.get("volumes"), list) else []
    b_vol_map = {v["name"]: v for v in b_vols if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]}
    a_vol_map = {v["name"]: v for v in a_vols if isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"]}
    dropped_or_swapped_vols: list[str] = []
    for vname, bv in sorted(b_vol_map.items()):
        av = a_vol_map.get(vname)
        if av is None:
            dropped_or_swapped_vols.append(vname)
            continue
        b_src_keys = {str(k) for k in bv.keys() if k != "name"}
        a_src_keys = {str(k) for k in av.keys() if k != "name"}
        if b_src_keys != a_src_keys or (
            (b_src_keys & {"configMap", "downwardAPI", "emptyDir"}) and bv != av
        ):
            dropped_or_swapped_vols.append(vname)
    if dropped_or_swapped_vols:
        ev.add(
            "VOLUME_REMOVED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"removes or replaces volume sources {dropped_or_swapped_vols}",
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
        if ev.risk_level == RISK_LOW or ev.revertability == REVERT_EASY or (
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
                revert=REVERT_MODERATE,
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
                        revert=REVERT_MODERATE,
                        disrupt=DISRUPT_DISRUPTIVE,
                        field_path=fp,
                    )
                else:
                    ev.add(
                        "CONTAINER_RESOURCE_ADJUSTMENT",
                        DIM_BLAST_RADIUS,
                        RISK_LOW,
                        f"sets bounded {section}.{rname}={av_raw!r} on container {cname!r}",
                        disrupt=DISRUPT_DISRUPTIVE,
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
                            DIM_DISRUPTIVENESS,
                            RISK_MEDIUM,
                            f"reduces {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            revert=REVERT_MODERATE,
                            disrupt=DISRUPT_DISRUPTIVE,
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
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=fp,
                        )
                    else:
                        ev.add(
                            "CONTAINER_RESOURCE_LARGE_INCREASE",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"large increase of {section}.{rname} on container {cname!r} ({bv_raw} -> {av_raw})",
                            revert=REVERT_MODERATE,
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=fp,
                        )


_CONFIGMAP_ALLOWED_TOP_KEYS = frozenset({"apiVersion", "kind", "metadata", "data", "binaryData", "immutable"})

_KNOWN_GROUP_VERSIONS: dict[str, frozenset[str]] = {
    "": frozenset({"v1"}),
    "apps": frozenset({"v1"}),
    "argoproj.io": frozenset({"v1alpha1"}),
    "batch": frozenset({"v1", "v1beta1"}),
    "rbac.authorization.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "policy": frozenset({"v1", "v1beta1"}),
    "autoscaling": frozenset({"v2", "v2beta2", "v2beta1", "v1"}),
    "keda.sh": frozenset({"v1alpha1"}),
    "networking.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "storage.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "scheduling.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "node.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "admissionregistration.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "apiextensions.k8s.io": frozenset({"v1", "v1beta1"}),
    "apiregistration.k8s.io": frozenset({"v1", "v1beta1"}),
    "coordination.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "discovery.k8s.io": frozenset({"v1", "v1beta1"}),
    "certificates.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "authentication.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
    "authorization.k8s.io": frozenset({"v1", "v1beta1"}),
    "flowcontrol.apiserver.k8s.io": frozenset({"v1", "v1beta3", "v1beta2", "v1beta1"}),
    "resource.k8s.io": frozenset({"v1", "v1beta2", "v1beta1", "v1alpha3", "v1alpha2"}),
    "audit.k8s.io": frozenset({"v1", "v1beta1", "v1alpha1"}),
}


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
                if _is_secret_key_name(ks):
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
                    try:
                        decoded_bin = base64.b64decode(val.encode("ascii", errors="ignore"), validate=False)
                    except (binascii.Error, ValueError):
                        decoded_bin = b""
                    if decoded_bin:
                        for _, pat_name in _scan_value_for_secrets(decoded_bin):
                            ev.add(
                                "PLAINTEXT_SECRET_LEAK",
                                DIM_SECURITY,
                                RISK_CRITICAL,
                                f"plaintext {pat_name} detected in ConfigMap {ev.ref.name!r} binaryData.{ks}",
                                blast=BLAST_HIGH,
                                revert=REVERT_IRREVERSIBLE,
                                field_path=f"binaryData.{ks}",
                                security=True,
                            )
                    if action == ACTION_MODIFY and k in bmap and val == "" and bmap.get(k):
                        ev.add(
                            "CONFIGMAP_VALUE_WIPED",
                            DIM_DISRUPTIVENESS,
                            RISK_HIGH,
                            f"wipes ConfigMap {ev.ref.name!r} binaryData.{ks} to empty string",
                            disrupt=DISRUPT_DISRUPTIVE,
                            field_path=f"binaryData.{ks}",
                        )
                    else:
                        ev.add(
                            "CONFIGMAP_BINARY_DATA",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"ConfigMap {ev.ref.name!r} writes opaque binaryData key {ks!r}",
                            revert=REVERT_MODERATE,
                            field_path=f"binaryData.{ks}",
                        )
                else:
                    s_trim = val.strip()
                    is_complex = (
                        "\n" in val
                        or "://" in val
                        or len(val) > 256
                        or s_trim.startswith(("{", "["))
                        or ks.lower().endswith((".yaml", ".yml", ".json", ".sh", ".conf", ".ini", ".toml", ".xml", ".sql", ".properties", ".env"))
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
                                revert=REVERT_MODERATE,
                                field_path=f"data.{ks}",
                            )
                    elif is_complex:
                        ev.add(
                            "CONFIGMAP_STRUCTURED_OR_URL_VALUE",
                            DIM_BLAST_RADIUS,
                            RISK_MEDIUM,
                            f"adds structured/multiline/URL value to ConfigMap {ev.ref.name!r} key {ks!r}",
                            revert=REVERT_MODERATE,
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
    if not ev.reasons:
        if action == ACTION_ADD:
            ev.add(
                "CONFIGMAP_CREATED",
                DIM_BLAST_RADIUS,
                RISK_LOW,
                f"creates empty ConfigMap {ev.ref.name!r}",
            )
        else:
            ev.add(
                "CONFIGMAP_SAFE_SCALAR_ADDED",
                DIM_BLAST_RADIUS,
                RISK_LOW,
                f"updates benign structure on ConfigMap {ev.ref.name!r}",
            )


def _parse_int_or_pct(val: Any) -> tuple[str, float] | None:
    """Parse Kubernetes IntOrString (`int` >= 0 or `NN%` in 0..100)."""
    if _is_strict_int(val) and val >= 0:
        return ("int", float(val))
    if isinstance(val, str) and val.strip().endswith("%"):
        try:
            pct = float(val.strip()[:-1])
            if math.isfinite(pct) and 0.0 <= pct <= 100.0:
                return ("pct", pct)
        except ValueError:
            return None
    return None


def _check_pdb(ev: ResourceEval, action: str, obj: dict[str, Any]) -> None:
    """Evaluate PodDisruptionBudget creation or modification."""
    spec = _as_dict(ev, obj.get("spec"), "spec")
    sel = spec.get("selector")
    empty_sel = not isinstance(sel, dict) or (not sel.get("matchLabels") and not sel.get("matchExpressions"))
    negative_only_sel = False
    if isinstance(sel, dict) and not sel.get("matchLabels") and isinstance(sel.get("matchExpressions"), list):
        ops = {str(e.get("operator") or "") for e in sel["matchExpressions"] if isinstance(e, dict)}
        if ops and ops.issubset({"NotIn", "DoesNotExist"}):
            negative_only_sel = True

    has_max_u = "maxUnavailable" in spec and spec.get("maxUnavailable") is not None
    has_min_a = "minAvailable" in spec and spec.get("minAvailable") is not None
    if has_max_u and has_min_a:
        ev.add(
            "INVALID_PDB_SPEC",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"PodDisruptionBudget {ev.ref.name!r} specifies both minAvailable and maxUnavailable",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
        return
    p_max = _parse_int_or_pct(spec.get("maxUnavailable")) if has_max_u else None
    p_min = _parse_int_or_pct(spec.get("minAvailable")) if has_min_a else None
    if (has_max_u and p_max is None) or (has_min_a and p_min is None):
        ev.add(
            "MALFORMED_FIELD_TYPE",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"PodDisruptionBudget {ev.ref.name!r} has invalid minAvailable/maxUnavailable value",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
        return

    blocks_drains = (
        (p_max is not None and p_max[1] == 0.0)
        or (p_min is not None and p_min[0] == "pct" and p_min[1] >= 100.0)
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
        ev.add(
            "PDB_ADDED_OFFLINE",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"adds PodDisruptionBudget {ev.ref.name!r} (offline classifier cannot verify live workload readiness or overlapping PDBs)",
            revert=REVERT_MODERATE,
            field_path="spec",
        )


def _is_internal_lb_annotations(annos: dict[str, Any]) -> bool:
    low_map = {str(k).lower(): str(v).strip().lower() for k, v in annos.items()}
    if low_map.get("networking.gke.io/load-balancer-type") == "internal":
        return True
    if low_map.get("cloud.google.com/load-balancer-type") == "internal":
        return True
    if low_map.get("service.beta.kubernetes.io/aws-load-balancer-scheme") == "internal":
        return True
    if low_map.get("service.beta.kubernetes.io/aws-load-balancer-internal") == "true":
        return True
    if low_map.get("service.beta.kubernetes.io/azure-load-balancer-internal") == "true":
        return True
    return False


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
            if b_type == "LoadBalancer":
                type_disrupt = DISRUPT_DESTRUCTIVE
            elif svc_type == "ExternalName" or b_type == "ExternalName":
                type_disrupt = DISRUPT_DISRUPTIVE
            else:
                type_disrupt = DISRUPT_NONE
            ev.add(
                "PUBLIC_EXPOSURE_SERVICE",
                DIM_SECURITY,
                RISK_HIGH,
                f"changes Service {ev.ref.name!r} type from {b_type} to {svc_type}",
                blast=BLAST_HIGH,
                disrupt=type_disrupt,
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
    if svc_type == "LoadBalancer" and _is_internal_lb_annotations(_meta_dict(before_obj, "annotations")) and not _is_internal_lb_annotations(_meta_dict(after_obj, "annotations")):
        ev.add(
            "INTERNAL_LOADBALANCER_EXPOSED",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"removes internal load-balancer restriction on Service {ev.ref.name!r} (recreates as public LoadBalancer)",
            blast=BLAST_CRITICAL,
            disrupt=DISRUPT_DESTRUCTIVE,
            field_path="metadata.annotations",
            security=True,
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
    for tp_key in ("externalTrafficPolicy", "internalTrafficPolicy"):
        if b_spec.get(tp_key) != a_spec.get(tp_key):
            ev.add(
                "SERVICE_TRAFFIC_POLICY_CHANGED",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"changes Service {ev.ref.name!r} {tp_key}",
                blast=BLAST_MEDIUM,
                disrupt=DISRUPT_DISRUPTIVE,
                field_path=f"spec.{tp_key}",
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
    elif a_lb_ranges and ({"0.0.0.0/0", "::/0"} & {str(x).strip() for x in a_lb_ranges}) and a_lb_ranges_raw != b_lb_ranges:
        ev.add(
            "PUBLIC_EXPOSURE_SERVICE",
            DIM_SECURITY,
            RISK_HIGH,
            f"opens Service {ev.ref.name!r} loadBalancerSourceRanges to public CIDR",
            blast=BLAST_HIGH,
            field_path="spec.loadBalancerSourceRanges",
            security=True,
        )
    elif svc_type == "LoadBalancer" and a_lb_ranges_raw != b_lb_ranges:
        b_lb_list = b_lb_ranges if isinstance(b_lb_ranges, list) else []
        narrowed_lb = any(item not in a_lb_ranges for item in b_lb_list)
        ev.add(
            "PUBLIC_EXPOSURE_SERVICE",
            DIM_SECURITY,
            RISK_HIGH,
            f"modifies LoadBalancer Service {ev.ref.name!r} loadBalancerSourceRanges",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if narrowed_lb else DISRUPT_NONE,
            field_path="spec.loadBalancerSourceRanges",
            security=True,
        )
    handled_svc_prefixes: set[tuple[str, ...]] = {
        ("metadata",),
        ("spec", "type"),
        ("spec", "externalName"),
        ("spec", "clusterIP"),
        ("spec", "clusterIPs"),
        ("spec", "selector"),
        ("spec", "ports"),
        ("spec", "externalTrafficPolicy"),
        ("spec", "internalTrafficPolicy"),
        ("spec", "loadBalancerSourceRanges"),
    }
    for d in diffs:
        if d.tokens and not any(d.tokens[: len(p)] == p for p in handled_svc_prefixes):
            ev.add(
                "UNALLOWLISTED_FIELD_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"modifies Service field {d.dotted!r}",
                revert=REVERT_MODERATE,
                field_path=d.dotted,
            )


def _check_hpa(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any], diffs: list[FieldDiff]) -> None:
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    if "scaleTargetRef" in a_spec and a_spec.get("scaleTargetRef") is not None:
        _as_dict(ev, a_spec.get("scaleTargetRef"), "spec.scaleTargetRef")

    # Normalize omitted/null minReplicas to Kubernetes default `1`; require maxReplicas >= 1
    a_min_raw = 1 if a_spec.get("minReplicas") is None else a_spec.get("minReplicas")
    a_max_present = "maxReplicas" in a_spec and a_spec.get("maxReplicas") is not None
    a_max_raw = a_spec.get("maxReplicas")
    a_min_valid = _is_strict_int(a_min_raw) and a_min_raw >= 0
    a_max_valid = a_max_present and _is_strict_int(a_max_raw) and a_max_raw >= 1

    if action == ACTION_ADD:
        if not a_min_valid or not a_max_valid or a_min_raw > a_max_raw:
            ev.add(
                "INVALID_HPA_REPLICAS",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"sets invalid HPA {ev.ref.name!r} minReplicas={a_min_raw!r} / maxReplicas={a_max_raw!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec",
            )
        elif a_min_raw == 0:
            ev.add(
                "HPA_SCALED_TO_ZERO",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"creates HPA {ev.ref.name!r} with minReplicas=0",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.minReplicas",
            )
        elif a_max_raw > MAX_LOW_REPLICA_TOTAL or a_min_raw > MAX_LOW_REPLICA_TOTAL:
            ev.add(
                "HPA_LARGE_INCREASE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"creates HPA {ev.ref.name!r} with high replica ceiling ({a_max_raw} > {MAX_LOW_REPLICA_TOTAL})",
                blast=BLAST_MEDIUM,
                field_path="spec.maxReplicas",
            )
        ev.add("NEW_HPA", DIM_BLAST_RADIUS, RISK_MEDIUM, f"creates new HorizontalPodAutoscaler {ev.ref.name!r}")
        return

    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    if b_spec.get("scaleTargetRef") != a_spec.get("scaleTargetRef"):
        ev.add(
            "HPA_TARGET_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"changes HPA {ev.ref.name!r} scaleTargetRef",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.scaleTargetRef",
        )

    b_min_raw = 1 if b_spec.get("minReplicas") is None else b_spec.get("minReplicas")
    b_max_raw = 1 if b_spec.get("maxReplicas") is None else b_spec.get("maxReplicas")
    b_min_valid = _is_strict_int(b_min_raw) and b_min_raw >= 0
    b_max_valid = _is_strict_int(b_max_raw) and b_max_raw >= 1

    if not a_min_valid or not a_max_valid or a_min_raw > a_max_raw:
        ev.add(
            "INVALID_HPA_REPLICAS",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sets invalid HPA {ev.ref.name!r} minReplicas={a_min_raw!r} / maxReplicas={a_max_raw!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    elif a_min_raw == 0 and b_min_valid and b_min_raw > 0:
        ev.add(
            "HPA_SCALED_TO_ZERO",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"lowers HPA {ev.ref.name!r} minReplicas from {b_min_raw} to 0",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.minReplicas",
        )
    elif (
        (b_min_valid and a_max_raw < b_min_raw)
        or (b_min_valid and a_min_raw < b_min_raw and ((b_min_raw - a_min_raw) / max(b_min_raw, 1) > 0.5 or (b_min_raw >= 2 and a_min_raw == 1)))
        or (b_max_valid and a_max_raw < b_max_raw and (b_max_raw - a_max_raw) / max(b_max_raw, 1) > 0.5)
    ):
        ev.add(
            "HPA_CAPACITY_REDUCED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sharply reduces HPA {ev.ref.name!r} replica bounds ({b_min_raw}..{b_max_raw} -> {a_min_raw}..{a_max_raw})",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    elif (b_min_valid and a_min_raw < b_min_raw) or (b_max_valid and a_max_raw < b_max_raw):
        ev.add(
            "HPA_BOUNDS_LOWERED",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"lowers HPA {ev.ref.name!r} replica bounds",
            revert=REVERT_MODERATE,
            field_path="spec",
        )
    elif (b_min_valid and a_min_raw > b_min_raw) or (b_max_valid and a_max_raw > b_max_raw):
        min_delta = a_min_raw - (b_min_raw if b_min_valid else 1)
        min_factor = a_min_raw / max(b_min_raw if b_min_valid else 1, 1)
        if (
            a_max_raw <= MAX_LOW_REPLICA_TOTAL
            and a_min_raw <= MAX_LOW_REPLICA_TOTAL
            and min_delta <= MAX_LOW_REPLICA_DELTA
            and min_factor <= MAX_LOW_REPLICA_FACTOR
        ):
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
                f"raises HPA {ev.ref.name!r} replica bounds steeply ({b_min_raw}..{b_max_raw} -> {a_min_raw}..{a_max_raw})",
                blast=BLAST_MEDIUM,
                revert=REVERT_MODERATE,
                field_path="spec",
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
                revert=REVERT_MODERATE,
                field_path=d.dotted,
            )


def _check_keda_scaled_object(
    ev: ResourceEval,
    action: str,
    before_obj: dict[str, Any] | None,
    after_obj: dict[str, Any],
    diffs: list[FieldDiff],
) -> None:
    """Evaluate KEDA ScaledObject (where omitted minReplicaCount defaults to 0 and maxReplicaCount to 100)."""
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    if "scaleTargetRef" in a_spec and a_spec.get("scaleTargetRef") is not None:
        _as_dict(ev, a_spec.get("scaleTargetRef"), "spec.scaleTargetRef")

    a_triggers = _as_list(ev, a_spec.get("triggers"), "spec.triggers") if "triggers" in a_spec else []
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    b_triggers = b_spec.get("triggers") if isinstance(b_spec.get("triggers"), list) else []
    b_auth_refs = [
        t.get("authenticationRef")
        for t in b_triggers
        if isinstance(t, dict) and t.get("authenticationRef") is not None
    ]
    a_auth_refs = [
        t.get("authenticationRef")
        for t in a_triggers
        if isinstance(t, dict) and t.get("authenticationRef") is not None
    ]
    if (action == ACTION_ADD and a_auth_refs) or (action == ACTION_MODIFY and a_auth_refs != b_auth_refs):
        ev.add(
            "KEDA_TRIGGER_AUTH_CHANGED",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} KEDA ScaledObject {ev.ref.name!r} trigger authenticationRef",
            revert=REVERT_MODERATE,
            field_path="spec.triggers",
            security=True,
        )

    a_min_raw = 0 if a_spec.get("minReplicaCount") is None else a_spec.get("minReplicaCount")
    a_max_raw = 100 if a_spec.get("maxReplicaCount") is None else a_spec.get("maxReplicaCount")
    a_min_valid = _is_strict_int(a_min_raw) and a_min_raw >= 0
    a_max_valid = _is_strict_int(a_max_raw) and a_max_raw >= 1
    a_idle = a_spec.get("idleReplicaCount")
    a_paused = a_spec.get("pausedReplicaCount")

    if action == ACTION_ADD:
        if not a_min_valid or not a_max_valid or a_min_raw > a_max_raw:
            ev.add(
                "INVALID_HPA_REPLICAS",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"sets invalid KEDA ScaledObject {ev.ref.name!r} minReplicaCount={a_min_raw!r} / maxReplicaCount={a_max_raw!r}",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec",
            )
        elif a_min_raw == 0 or (_is_strict_int(a_idle) and a_idle == 0) or a_paused is not None:
            ev.add(
                "HPA_SCALED_TO_ZERO",
                DIM_DISRUPTIVENESS,
                RISK_HIGH,
                f"creates KEDA ScaledObject {ev.ref.name!r} capable of scaling to zero or paused replicas",
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="spec.minReplicaCount",
            )
        elif a_max_raw > MAX_LOW_REPLICA_TOTAL or a_min_raw > MAX_LOW_REPLICA_TOTAL:
            ev.add(
                "HPA_LARGE_INCREASE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"creates KEDA ScaledObject {ev.ref.name!r} with high replica ceiling ({a_max_raw} > {MAX_LOW_REPLICA_TOTAL})",
                blast=BLAST_MEDIUM,
                field_path="spec.maxReplicaCount",
            )
        ev.add(
            "NEW_KEDA_SCALED_OBJECT",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"creates new KEDA ScaledObject {ev.ref.name!r}",
            revert=REVERT_MODERATE,
        )
        return

    if b_spec.get("scaleTargetRef") != a_spec.get("scaleTargetRef"):
        ev.add(
            "HPA_TARGET_CHANGED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"changes KEDA ScaledObject {ev.ref.name!r} scaleTargetRef",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.scaleTargetRef",
        )
    if b_spec.get("idleReplicaCount") != a_idle or b_spec.get("pausedReplicaCount") != a_paused:
        ev.add(
            "HPA_SCALED_TO_ZERO",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"modifies KEDA ScaledObject {ev.ref.name!r} idleReplicaCount or pausedReplicaCount",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )

    b_min_raw = 0 if b_spec.get("minReplicaCount") is None else b_spec.get("minReplicaCount")
    b_max_raw = 100 if b_spec.get("maxReplicaCount") is None else b_spec.get("maxReplicaCount")
    b_min_valid = _is_strict_int(b_min_raw) and b_min_raw >= 0
    b_max_valid = _is_strict_int(b_max_raw) and b_max_raw >= 1

    if not a_min_valid or not a_max_valid or a_min_raw > a_max_raw:
        ev.add(
            "INVALID_HPA_REPLICAS",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sets invalid KEDA ScaledObject {ev.ref.name!r} minReplicaCount={a_min_raw!r} / maxReplicaCount={a_max_raw!r}",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    elif a_min_raw == 0 and (b_min_raw != 0 or a_max_raw != b_max_raw):
        ev.add(
            "HPA_SCALED_TO_ZERO",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sets KEDA ScaledObject {ev.ref.name!r} minReplicaCount=0",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec.minReplicaCount",
        )
    elif (
        (b_min_valid and a_max_raw < b_min_raw)
        or (b_min_valid and a_min_raw < b_min_raw and ((b_min_raw - a_min_raw) / max(b_min_raw, 1) > 0.5 or (b_min_raw >= 2 and a_min_raw <= 1)))
        or (b_max_valid and a_max_raw < b_max_raw and (b_max_raw - a_max_raw) / max(b_max_raw, 1) > 0.5)
    ):
        ev.add(
            "HPA_CAPACITY_REDUCED",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"sharply reduces KEDA ScaledObject {ev.ref.name!r} replica bounds ({b_min_raw}..{b_max_raw} -> {a_min_raw}..{a_max_raw})",
            disrupt=DISRUPT_DISRUPTIVE,
            field_path="spec",
        )
    elif (b_min_valid and a_min_raw < b_min_raw) or (b_max_valid and a_max_raw < b_max_raw):
        ev.add(
            "HPA_BOUNDS_LOWERED",
            DIM_BLAST_RADIUS,
            RISK_MEDIUM,
            f"lowers KEDA ScaledObject {ev.ref.name!r} replica bounds",
            revert=REVERT_MODERATE,
            field_path="spec",
        )
    elif (b_min_valid and a_min_raw > b_min_raw) or (b_max_valid and a_max_raw > b_max_raw):
        base_min = max(b_min_raw if b_min_valid else 1, 1)
        min_delta = a_min_raw - (b_min_raw if b_min_valid else 1)
        min_factor = a_min_raw / base_min
        if (
            1 <= a_min_raw <= MAX_LOW_REPLICA_TOTAL
            and a_max_raw <= MAX_LOW_REPLICA_TOTAL
            and min_delta <= MAX_LOW_REPLICA_DELTA
            and min_factor <= MAX_LOW_REPLICA_FACTOR
        ):
            ev.add(
                "HPA_BOUNDS_BUMPED",
                DIM_BLAST_RADIUS,
                RISK_LOW,
                f"modestly increases KEDA ScaledObject {ev.ref.name!r} replica bounds within safe ceiling",
                field_path="spec",
            )
        else:
            ev.add(
                "HPA_LARGE_INCREASE",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"raises KEDA ScaledObject {ev.ref.name!r} replica bounds steeply ({b_min_raw}..{b_max_raw} -> {a_min_raw}..{a_max_raw})",
                blast=BLAST_MEDIUM,
                revert=REVERT_MODERATE,
                field_path="spec",
            )
    for d in diffs:
        if d.tokens and d.tokens[0] != "metadata" and not (
            d.tokens[0] == "spec"
            and len(d.tokens) >= 2
            and d.tokens[1] in ("minReplicaCount", "maxReplicaCount", "scaleTargetRef", "idleReplicaCount", "pausedReplicaCount")
        ):
            ev.add(
                "HPA_POLICY_MODIFIED",
                DIM_BLAST_RADIUS,
                RISK_MEDIUM,
                f"modifies KEDA ScaledObject field {d.dotted!r}",
                revert=REVERT_MODERATE,
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
    pre_pvc_reasons = len(ev.reasons)
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    for imm in (
        "storageClassName",
        "accessModes",
        "volumeMode",
        "volumeName",
        "selector",
        "dataSource",
        "dataSourceRef",
    ):
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
    elif b_sz is not None and a_req.get("storage") is None:
        ev.add(
            "PVC_STORAGE_SHRUNK",
            DIM_DISRUPTIVENESS,
            RISK_HIGH,
            f"removes PVC {ev.ref.name!r} storage request",
            blast=BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
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
    if len(ev.reasons) == pre_pvc_reasons and (b_spec != a_spec or not ev.reasons):
        ev.add(
            "PVC_MODIFIED",
            DIM_REVERTABILITY,
            RISK_MEDIUM,
            f"modifies PersistentVolumeClaim {ev.ref.name!r}",
            revert=REVERT_MODERATE,
        )


def _kyverno_rules_weakened(b_spec: dict[str, Any], a_spec: dict[str, Any]) -> bool:
    """Detect modern Kyverno per-rule validate.failureAction / verifyImages / failurePolicy weakening."""
    if str(b_spec.get("failurePolicy") or "Fail").lower() == "fail" and str(a_spec.get("failurePolicy") or "Fail").lower() == "ignore":
        return True
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
        if br.get("verifyImages") and ar.get("verifyImages") != br.get("verifyImages"):
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

    weakened = False
    if str(b_spec.get("validationFailureAction") or "").lower() == "enforce" and str(spec.get("validationFailureAction") or "").lower() != "enforce":
        weakened = True
    if str(b_spec.get("enforcementAction") or "deny").lower() == "deny" and str(spec.get("enforcementAction") or "deny").lower() in ("dryrun", "warn"):
        weakened = True
    if action == ACTION_MODIFY and _kyverno_rules_weakened(b_spec, spec):
        weakened = True

    enforces = (
        str(spec.get("validationFailureAction") or "").lower() == "enforce"
        or any(
            isinstance(r, dict)
            and (
                (isinstance(r.get("validate"), dict) and str(r["validate"].get("failureAction") or "").lower() == "enforce")
                or bool(r.get("mutate"))
                or bool(r.get("verifyImages"))
            )
            for r in (spec.get("rules") if isinstance(spec.get("rules"), list) else [])
        )
        or (ev.ref.group == "constraints.gatekeeper.sh" and str(spec.get("enforcementAction") or "deny").lower() == "deny")
    )
    sev = RISK_CRITICAL if (weakened or is_cluster) else RISK_HIGH
    ev.add(
        "POLICY_ENGINE_WEAKENED" if weakened else "POLICY_ENGINE_CHANGE",
        DIM_SECURITY,
        sev,
        f"{'weakens' if weakened else _action_verb(action)} admission policy {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL if is_cluster else BLAST_HIGH,
        disrupt=DISRUPT_DISRUPTIVE if (action == ACTION_MODIFY or enforces) else DISRUPT_NONE,
        security=True,
    )


def _check_istio_security(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate Istio PeerAuthentication, RequestAuthentication, AuthorizationPolicy."""
    mesh_wide = ev.ref.namespace in ("", "istio-system")
    spec = after_obj.get("spec") if isinstance(after_obj.get("spec"), dict) else {}
    mtls = spec.get("mtls") if isinstance(spec.get("mtls"), dict) else {}
    mtls_mode = str(mtls.get("mode") or "").upper()
    loosened_mtls = ev.ref.kind == "PeerAuthentication" and mtls_mode in ("DISABLE", "PERMISSIVE")
    authz_action = str(spec.get("action") or "ALLOW").upper()
    broad_allow_or_deny_all = ev.ref.kind == "AuthorizationPolicy" and authz_action in ("ALLOW", "DENY") and not spec.get("selector")
    is_disruptive = action == ACTION_MODIFY
    if ev.ref.kind == "PeerAuthentication" and mtls_mode == "STRICT":
        is_disruptive = True
    elif ev.ref.kind == "AuthorizationPolicy" and authz_action != "AUDIT":
        # Adding DENY, CUSTOM, or ALLOW (including empty rules deny-all) drops unmatched traffic
        if authz_action in ("DENY", "CUSTOM") or not spec.get("rules") or spec.get("selector") or spec.get("targetRef"):
            is_disruptive = True
    sev = RISK_CRITICAL if (mesh_wide and (loosened_mtls or broad_allow_or_deny_all or action == ACTION_MODIFY)) else RISK_HIGH
    ev.add(
        "MESH_SECURITY_POLICY_CHANGE",
        DIM_SECURITY,
        sev,
        f"{_action_verb(action)} Istio {ev.ref.kind} {ev.ref.name!r} in {ev.ref.namespace or 'cluster'}",
        blast=BLAST_CRITICAL if mesh_wide else BLAST_HIGH,
        disrupt=DISRUPT_DISRUPTIVE if is_disruptive else DISRUPT_NONE,
        security=True,
    )


def _check_gitops_reconciler(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate ArgoCD, Flux, and Config Sync reconcilers."""
    kind = ev.ref.kind
    spec = _as_dict(ev, after_obj.get("spec"), "spec")
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}

    prune_or_force = False
    if "prune" in spec and _is_truthy_or_nonbool(spec.get("prune")) and b_spec.get("prune") is not True:
        prune_or_force = True
    if "force" in spec and _is_truthy_or_nonbool(spec.get("force")) and b_spec.get("force") is not True:
        prune_or_force = True
    sync_pol = spec.get("syncPolicy")
    if isinstance(sync_pol, dict) and isinstance(sync_pol.get("automated"), dict) and "prune" in sync_pol["automated"]:
        if _is_truthy_or_nonbool(sync_pol["automated"].get("prune")):
            b_sp = b_spec.get("syncPolicy") if isinstance(b_spec.get("syncPolicy"), dict) else {}
            if not (isinstance(b_sp.get("automated"), dict) and b_sp["automated"].get("prune") is True):
                prune_or_force = True

    root_reconciler = kind in ("RootSync", "ApplicationSet", "AppProject", "ConfigManagement")
    source_or_dest_changed = action == ACTION_MODIFY and any(
        spec.get(k) != b_spec.get(k)
        for k in (
            "source",
            "sources",
            "sourceRef",
            "path",
            "chart",
            "chartRef",
            "git",
            "url",
            "oci",
            "helm",
            "bucketName",
            "endpoint",
            "destination",
            "targetNamespace",
            "storageNamespace",
            "project",
            "serviceAccountName",
            "kubeConfig",
        )
    )
    impersonation_set = action == ACTION_ADD and bool(spec.get("kubeConfig"))
    suspended = "suspend" in spec and _is_truthy_or_nonbool(spec.get("suspend")) and b_spec.get("suspend") is not True

    sev = RISK_CRITICAL if (root_reconciler or prune_or_force or source_or_dest_changed or impersonation_set) else RISK_HIGH
    disrupt = (
        DISRUPT_DESTRUCTIVE
        if (prune_or_force or source_or_dest_changed)
        else (DISRUPT_DISRUPTIVE if suspended else DISRUPT_NONE)
    )
    ev.add(
        "GITOPS_RECONCILER_CHANGE",
        DIM_BLAST_RADIUS,
        sev,
        f"{_action_verb(action)} GitOps reconciler {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL if root_reconciler else BLAST_HIGH,
        revert=REVERT_DIFFICULT,
        disrupt=disrupt,
        security=True,
    )


def _is_link_local_or_loopback_ip(ip_str: str) -> bool:
    s = ip_str.strip().lower()
    return (
        s.startswith(("169.254.", "127.", "0."))
        or s in ("0.0.0.0", "::1", "::", "localhost", "metadata.google.internal")
        or s.startswith("fe80:")
    )


def _check_endpoints_or_slice(ev: ResourceEval, action: str, obj: dict[str, Any]) -> None:
    """Evaluate manual v1/Endpoints and discovery.k8s.io/EndpointSlice resources."""
    ips: list[str] = []
    if ev.ref.kind == "Endpoints":
        for sub in _as_list(ev, obj.get("subsets"), "subsets"):
            if isinstance(sub, dict):
                for addr in _as_list(ev, sub.get("addresses"), "subsets.addresses"):
                    if isinstance(addr, dict) and isinstance(addr.get("ip"), str):
                        ips.append(addr["ip"])
    else:
        for ep in _as_list(ev, obj.get("endpoints"), "endpoints"):
            if isinstance(ep, dict):
                for ip in _as_list(ev, ep.get("addresses"), "endpoints.addresses"):
                    if isinstance(ip, str):
                        ips.append(ip)
    crit = (
        ev.ref.name == "kubernetes"
        or ev.ref.namespace in SYSTEM_NAMESPACES
        or any(_is_link_local_or_loopback_ip(ip) for ip in ips)
    )
    ev.add(
        "CRITICAL_ENDPOINT_HIJACK_OR_SSRF" if crit else "MANUAL_ENDPOINTS_CHANGE",
        DIM_SECURITY,
        RISK_CRITICAL if crit else RISK_HIGH,
        f"{_action_verb(action)} {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL if crit else BLAST_HIGH,
        disrupt=DISRUPT_DISRUPTIVE if (action == ACTION_MODIFY or crit) else DISRUPT_NONE,
        security=True,
    )


def _check_control_plane_extension(ev: ResourceEval, action: str, obj: dict[str, Any]) -> None:
    """Evaluate admission webhooks, CEL Validating/MutatingAdmissionPolicies, and APIService."""
    kind = ev.ref.kind
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    enforces_or_blocks = action == ACTION_MODIFY
    if kind in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
        webhooks = _as_list(ev, obj.get("webhooks"), "webhooks")
        if any(isinstance(w, dict) and str(w.get("failurePolicy") or "Fail") != "Ignore" for w in webhooks):
            enforces_or_blocks = True
    elif kind == "ValidatingAdmissionPolicyBinding":
        v_actions = spec.get("validationActions")
        if isinstance(v_actions, list) and any(str(x) == "Deny" for x in v_actions):
            enforces_or_blocks = True
        elif spec.get("policyName") and v_actions is None:
            enforces_or_blocks = True
    elif kind in ("MutatingAdmissionPolicy", "MutatingAdmissionPolicyBinding"):
        if spec:
            enforces_or_blocks = True

    ev.add(
        "CRITICAL_CONTROL_PLANE_EXTENSION",
        DIM_SECURITY,
        RISK_CRITICAL,
        f"{_action_verb(action)} cluster-wide API/admission extension {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_CRITICAL,
        revert=REVERT_MODERATE,
        disrupt=DISRUPT_DISRUPTIVE if enforces_or_blocks else DISRUPT_NONE,
        security=True,
    )


def _check_gke_networking(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate GKE Gateway/Ingress/MCS/Multi-Networking CRDs (`networking.gke.io`, `net.gke.io`, `BackendConfig`)."""
    kind = ev.ref.kind
    a_spec = _as_dict(ev, after_obj.get("spec"), "spec")
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}

    if kind in ("GKENetworkParamSet", "Network"):
        ev.add(
            "GKE_CLUSTER_NETWORK_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{_action_verb(action)} cluster-scoped GKE network resource {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    if kind in ("BackendConfig", "GCPBackendPolicy"):
        if action == ACTION_MODIFY:
            if b_spec.get("securityPolicy") and not a_spec.get("securityPolicy"):
                ev.add(
                    "CLOUD_ARMOR_POLICY_REMOVED",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"removes Cloud Armor securityPolicy from {kind} {ev.ref.name!r}",
                    blast=BLAST_CRITICAL,
                    disrupt=DISRUPT_NONE,
                    field_path="spec.securityPolicy",
                    security=True,
                )
            b_iap = b_spec.get("iap") if isinstance(b_spec.get("iap"), dict) else {}
            a_iap = a_spec.get("iap") if isinstance(a_spec.get("iap"), dict) else {}
            if b_iap.get("enabled") is True and a_iap.get("enabled") is not True:
                ev.add(
                    "IAP_AUTH_DISABLED",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"disables Identity-Aware Proxy (IAP) on {kind} {ev.ref.name!r}",
                    blast=BLAST_CRITICAL,
                    field_path="spec.iap.enabled",
                    security=True,
                )
        ev.add(
            "GKE_BACKEND_POLICY_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} GKE {kind} {ev.ref.name!r}",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    if kind == "FrontendConfig":
        if action == ACTION_MODIFY:
            b_redir = b_spec.get("redirectToHttps") if isinstance(b_spec.get("redirectToHttps"), dict) else {}
            a_redir = a_spec.get("redirectToHttps") if isinstance(a_spec.get("redirectToHttps"), dict) else {}
            if (b_redir.get("enabled") is True and a_redir.get("enabled") is not True) or (b_spec.get("sslPolicy") and not a_spec.get("sslPolicy")):
                ev.add(
                    "TLS_HARDENING_WEAKENED",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"weakens HTTPS redirect or sslPolicy on GKE FrontendConfig {ev.ref.name!r}",
                    blast=BLAST_HIGH,
                    security=True,
                )
        ev.add(
            "GKE_FRONTEND_CONFIG_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} GKE FrontendConfig {ev.ref.name!r}",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    if kind == "ManagedCertificate":
        ev.add(
            "GKE_MANAGED_CERTIFICATE_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} GKE ManagedCertificate {ev.ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    if kind == "HealthCheckPolicy":
        ev.add(
            "GKE_HEALTH_CHECK_POLICY_CHANGE",
            DIM_DISRUPTIVENESS if action == ACTION_MODIFY else DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{_action_verb(action)} GKE HealthCheckPolicy {ev.ref.name!r}",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return

    ev.add(
        "GKE_NETWORKING_POLICY_CHANGE",
        DIM_SECURITY,
        RISK_HIGH,
        f"{_action_verb(action)} GKE networking resource {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_HIGH,
        disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        security=True,
    )


def _check_monitoring_resource(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate GKE Managed Prometheus (`monitoring.googleapis.com`) and Prometheus Operator (`monitoring.coreos.com`)."""
    kind = ev.ref.kind
    is_cluster = is_cluster_scoped_kind(ev.ref.group, kind)
    spec = _as_dict(ev, after_obj.get("spec"), "spec")
    b_spec = before_obj.get("spec") if isinstance(before_obj, dict) and isinstance(before_obj.get("spec"), dict) else {}

    if is_cluster or kind in ("OperatorConfig", "Prometheus", "Alertmanager", "ThanosRuler"):
        ev.add(
            "CLUSTER_MONITORING_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{_action_verb(action)} monitoring infrastructure/cluster resource {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_MODERATE if kind in ("OperatorConfig", "Prometheus", "Alertmanager", "ThanosRuler") else REVERT_EASY,
            disrupt=DISRUPT_DISRUPTIVE if (action == ACTION_MODIFY and kind in ("OperatorConfig", "Prometheus", "Alertmanager")) else DISRUPT_NONE,
            security=(kind in ("OperatorConfig", "Prometheus", "Alertmanager")),
        )
        return

    # Check secret references inside scrape endpoints
    endpoints = spec.get("endpoints") if isinstance(spec.get("endpoints"), list) else (
        spec.get("podMetricsEndpoints") if isinstance(spec.get("podMetricsEndpoints"), list) else []
    )
    b_endpoints = b_spec.get("endpoints") if isinstance(b_spec.get("endpoints"), list) else (
        b_spec.get("podMetricsEndpoints") if isinstance(b_spec.get("podMetricsEndpoints"), list) else []
    )
    for ep in endpoints:
        if isinstance(ep, dict) and any(ep.get(auth_k) for auth_k in ("basicAuth", "authorization", "oauth2", "bearerTokenSecret", "tls", "tlsConfig")):
            if action == ACTION_ADD or ep not in b_endpoints:
                ev.add(
                    "MONITORING_SECRET_REF",
                    DIM_SECURITY,
                    RISK_HIGH,
                    f"{ev.ref.gvk_label()} {ev.ref.name!r} configures endpoint auth/TLS credentials",
                    blast=BLAST_MEDIUM,
                    security=True,
                )
                break

    if kind in ("Rules", "PrometheusRule", "AlertmanagerConfig") and action == ACTION_MODIFY:
        ev.add(
            "ALERTING_RULES_MODIFIED",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"modifies alerting/recording {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_MEDIUM,
        )
        return

    ev.add(
        "POD_MONITORING_CHANGE",
        DIM_BLAST_RADIUS,
        RISK_MEDIUM,
        f"{_action_verb(action)} {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_LOW,
        revert=REVERT_MODERATE,
    )


def _check_storage_resource(ev: ResourceEval, action: str, before_obj: dict[str, Any] | None, after_obj: dict[str, Any]) -> None:
    """Evaluate PV, CSIDriver, CSINode, VolumeAttachment, VolumeSnapshot, VolumeSnapshotContent, VolumeSnapshotClass."""
    kind = ev.ref.kind
    spec = _as_dict(ev, after_obj.get("spec"), "spec")
    is_cluster = is_cluster_scoped_kind(ev.ref.group, kind)

    if kind == "PersistentVolume":
        hp = spec.get("hostPath")
        loc = spec.get("local")
        raw_path = str(hp.get("path") or "") if isinstance(hp, dict) else (str(loc.get("path") or "") if isinstance(loc, dict) else "")
        if isinstance(hp, dict) or isinstance(loc, dict):
            crit = _is_critical_host_path(raw_path)
            ev.add(
                "PV_HOST_PATH_MOUNT",
                DIM_SECURITY,
                RISK_CRITICAL if crit else RISK_HIGH,
                f"PersistentVolume {ev.ref.name!r} exposes node path {raw_path!r}",
                blast=BLAST_CRITICAL if crit else BLAST_HIGH,
                revert=REVERT_DIFFICULT,
                security=True,
            )
            return

    if kind == "CSIDriver":
        has_tok = bool(spec.get("tokenRequests"))
        ev.add(
            "CSI_DRIVER_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL if has_tok else RISK_HIGH,
            f"{_action_verb(action)} CSIDriver {ev.ref.name!r}" + (" with kubelet tokenRequests" if has_tok else ""),
            blast=BLAST_CRITICAL if has_tok else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    if kind in ("VolumeAttachment", "CSINode"):
        ev.add(
            "NODE_STORAGE_ATTACHMENT_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} node-level storage attachment {ev.ref.gvk_label()} {ev.ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return

    ev.add(
        "CLUSTER_STORAGE_CHANGE",
        DIM_REVERTABILITY,
        RISK_HIGH,
        f"{_action_verb(action)} storage resource {ev.ref.gvk_label()} {ev.ref.name!r}",
        blast=BLAST_HIGH if is_cluster else BLAST_MEDIUM,
        revert=REVERT_DIFFICULT,
        security=is_cluster,
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

    # Determine production environment from object labels, PR Namespace labels, namespace name, or path
    obj_labels = _meta_dict(primary.obj, "labels")
    prior_labels = _meta_dict(before.obj, "labels") if before is not None else {}
    ns_lbl = (ns_labels or {}).get(ref.namespace, {})
    is_prod = (
        _is_production_labels(obj_labels)
        or _is_production_labels(prior_labels)
        or _is_production_labels(ns_lbl)
        or _is_production_namespace(ref.namespace)
        or (gk == ("", "Namespace") and _is_production_namespace(ref.name))
        or _is_production_path(file_path)
    )

    # Fail-closed finalizer: unmodeled changes NEVER silently pass as medium/easy
    def _finalize() -> ResourceEval:
        if not ev.reasons:
            ev.add(
                "UNMODELED_RESOURCE_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_UNKNOWN,
                f"unmodeled {action} on {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_UNKNOWN,
                revert=REVERT_UNKNOWN,
                disrupt=DISRUPT_UNKNOWN,
                supported=False,
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

    # 1. Self-modification of kube-agents CRDs (including `persona.kube-agents.x-k8s.io`) -> always critical
    if is_kube_agents_group(ref.group):
        ev.add(
            "KUBE_AGENTS_SELF_MODIFICATION",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{_action_verb(action)} kube-agents control-plane CR {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DESTRUCTIVE if action == ACTION_DELETE else (DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE),
            security=True,
        )
        return _finalize()

    # Universal plaintext credential scan BEFORE early returns so unknown CRDs,
    # Kustomize patches, or namespace-less manifests can never mask leaked private keys or tokens.
    if action != ACTION_DELETE:
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

    # 0. Kustomize strategic-merge `$patch` directives (e.g. `$patch: delete`)
    if action != ACTION_DELETE:
        patches = _find_kustomize_patches(primary.obj)
        if patches:
            delete_patches = [p for p, v in patches if str(v).strip().lower() == "delete"]
            if delete_patches:
                crit = is_prod or is_cluster_scoped or gk in DATA_STATEFUL_DELETE_KINDS or in_system_ns
                ev.add(
                    "KUSTOMIZE_DELETE_PATCH",
                    DIM_DISRUPTIVENESS,
                    RISK_CRITICAL if crit else RISK_HIGH,
                    f"manifest contains Kustomize `$patch: delete` directive targeting {ref.gvk_label()} {ref.name!r}",
                    blast=BLAST_CRITICAL if crit else BLAST_HIGH,
                    revert=REVERT_IRREVERSIBLE if gk in DATA_STATEFUL_DELETE_KINDS else REVERT_DIFFICULT,
                    disrupt=DISRUPT_DESTRUCTIVE,
                    field_path=delete_patches[0],
                    supported=False,
                )
                if "$patch" in delete_patches and not (
                    set(primary.obj.keys()) - {"apiVersion", "kind", "metadata", "$patch"}
                ):
                    return _finalize()
            else:
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
                f"{_action_verb(action)} unsupported/custom resource {ref.gvk_label()} {ref.name!r} in system namespace {ref.namespace!r}",
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
        return _finalize()

    # 3. Missing namespace on a known namespaced resource -> record `unknown` floor and
    # CONTINUE evaluating security/posture rules so omitting `metadata.namespace` on a
    # privileged/hostPath Pod or RBAC RoleBinding can never downgrade CRITICAL -> UNKNOWN.
    if not is_cluster_scoped and not ref.namespace and ref.kind != "TokenRequest":
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
        if action == ACTION_DELETE:
            # Still run delete classification below so stateful/secret deletes stay CRITICAL/IRREVERSIBLE
            pass

    # 3b. Validate API version for standard Kubernetes API groups & version changes
    allowed_versions = _KNOWN_GROUP_VERSIONS.get(ref.group)
    if action != ACTION_DELETE and allowed_versions is not None and ref.version not in allowed_versions:
        ev.add(
            "UNRECOGNIZED_API_VERSION",
            DIM_BLAST_RADIUS,
            RISK_UNKNOWN,
            f"unrecognized apiVersion '{ref.group + '/' if ref.group else ''}{ref.version}' for {ref.kind}",
            blast=BLAST_UNKNOWN,
            revert=REVERT_UNKNOWN,
            disrupt=DISRUPT_UNKNOWN,
            field_path="apiVersion",
            supported=False,
        )

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
        if in_system_ns or gk == ("", "Node"):
            ev.add(
                "CRITICAL_SYSTEM_NAMESPACE_DELETE",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL,
                f"deletes {ref.gvk_label()} {ref.name!r}" + (f" in system namespace {ref.namespace!r}" if in_system_ns else ""),
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
        if (
            gk in CONTROL_PLANE_EXTENSION_KINDS
            or gk in FLOWCONTROL_KINDS
            or gk == ("audit.k8s.io", "Policy")
            or ref.group in ("kyverno.io", "templates.gatekeeper.sh", "constraints.gatekeeper.sh")
        ):
            ev.add(
                "CRITICAL_CONTROL_PLANE_DELETE",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"deletes admission/policy/apiserver control {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DESTRUCTIVE,
                security=True,
            )
            return _finalize()
        if (
            gk == ("", "Secret")
            or ref.kind in ("SealedSecret", "ExternalSecret")
            or gk in (("external-secrets.io", "SecretStore"), ("external-secrets.io", "ClusterSecretStore"))
            or ref.group == "cert-manager.io"
        ):
            ev.add(
                "SECRET_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if (is_prod or is_cluster_scoped) else RISK_HIGH,
                f"deletes {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_CRITICAL if is_cluster_scoped else BLAST_MEDIUM,
                revert=REVERT_IRREVERSIBLE if gk == ("", "Secret") or ref.kind in ("SealedSecret", "ExternalSecret") else REVERT_DIFFICULT,
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
        if is_cloud_infra_group(ref.group) or ref.group.endswith(".cnpg.io"):
            ev.add(
                "CLOUD_INFRA_DELETED",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL if is_prod else RISK_HIGH,
                f"deletes cloud/database infrastructure resource {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_HIGH,
                revert=REVERT_IRREVERSIBLE,
                disrupt=DISRUPT_DESTRUCTIVE,
            )
            return _finalize()
        if gk == ("", "Service"):
            svc_spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
            if str(svc_spec.get("type") or "ClusterIP") == "LoadBalancer":
                ev.add(
                    "LOADBALANCER_SERVICE_DELETED",
                    DIM_DISRUPTIVENESS,
                    RISK_CRITICAL if is_prod else RISK_HIGH,
                    f"deletes LoadBalancer Service {ref.name!r} (releases external cloud load-balancer IP)",
                    blast=BLAST_HIGH,
                    revert=REVERT_DIFFICULT,
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
            disrupt=DISRUPT_DISRUPTIVE,
        )
        return _finalize()

    # Compute diffs up front on MODIFY so metadata tampering, apiVersion changes,
    # and system metadata edits are checked universally across EVERY resource kind.
    diffs = diff_objects(before.sanitized, after.sanitized) if (action == ACTION_MODIFY and before and after) else []

    if action == ACTION_MODIFY and before is not None:
        if before.ref.version != primary.ref.version or before.obj.get("apiVersion") != primary.obj.get("apiVersion"):
            ev.add(
                "API_VERSION_CHANGED",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"changes apiVersion on {ref.gvk_label()} {ref.name!r} ({before.obj.get('apiVersion')!r} -> {primary.obj.get('apiVersion')!r})",
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE,
                field_path="apiVersion",
            )
        _check_metadata_edits(
            ev,
            before.obj,
            primary.obj,
            diffs,
            allow_low_metadata=(gk in LOW_RISK_EDITABLE_KINDS),
        )
    elif action == ACTION_ADD:
        _check_metadata_add(ev, primary.obj)

    # 5. System namespace create/modify
    if in_system_ns:
        ev.add(
            "SYSTEM_NAMESPACE_TARGET",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} {ref.gvk_label()} {ref.name!r} inside system namespace {ref.namespace!r}",
            blast=BLAST_HIGH,
            security=True,
        )

    # 5b. Cluster Node objects (cordoning, tainting, node config) -> critical
    if gk == ("", "Node"):
        ev.add(
            "NODE_RESOURCE_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_CRITICAL,
            f"{_action_verb(action)} cluster Node {ref.name!r} (affects node scheduling, taints, and workloads)",
            blast=BLAST_CRITICAL,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    # 6. Control-plane admission webhooks, CEL Validating/MutatingAdmissionPolicies, APIService, FlowControl, Audit
    if gk in CONTROL_PLANE_EXTENSION_KINDS:
        _check_control_plane_extension(ev, action, primary.obj)
        return _finalize()

    if gk in FLOWCONTROL_KINDS:
        ev.add(
            "APISERVER_FLOWCONTROL_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{_action_verb(action)} kube-apiserver traffic priority/fairness {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    if gk == ("audit.k8s.io", "Policy"):
        ev.add(
            "AUDIT_POLICY_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{_action_verb(action)} Kubernetes audit Policy {ref.name!r}",
            blast=BLAST_CRITICAL,
            revert=REVERT_MODERATE,
            security=True,
        )
        return _finalize()

    # 6b. Certificates, Trust Bundles, TokenRequest & Auth Reviews
    if gk in CERT_AND_AUTH_KINDS:
        if gk == ("certificates.k8s.io", "CertificateSigningRequest"):
            ev.add(
                "CSR_RESOURCE_CHANGE",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"{_action_verb(action)} CertificateSigningRequest {ref.name!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_IRREVERSIBLE,
                security=True,
            )
        elif gk == ("certificates.k8s.io", "ClusterTrustBundle"):
            ev.add(
                "CLUSTER_TRUST_BUNDLE_CHANGE",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"{_action_verb(action)} ClusterTrustBundle {ref.name!r} (cluster root trust anchor)",
                blast=BLAST_CRITICAL,
                revert=REVERT_DIFFICULT,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
                security=True,
            )
        elif ref.kind == "TokenRequest":
            ev.add(
                "TOKEN_REQUEST_MANIFEST",
                DIM_SECURITY,
                RISK_CRITICAL,
                f"{_action_verb(action)} ServiceAccount TokenRequest {ref.name!r}",
                blast=BLAST_HIGH,
                revert=REVERT_IRREVERSIBLE,
                security=True,
            )
        else:
            ev.add(
                "AUTH_REVIEW_MANIFEST",
                DIM_SECURITY,
                RISK_HIGH,
                f"{_action_verb(action)} auth review manifest {ref.gvk_label()} {ref.name!r}",
                blast=BLAST_MEDIUM,
                security=True,
            )
        return _finalize()

    # 6c. Coordination Leases (`kube-node-lease` / `kube-system` vs app leader election)
    if gk in COORDINATION_KINDS:
        if in_system_ns:
            ev.add(
                "SYSTEM_LEASE_CHANGE",
                DIM_DISRUPTIVENESS,
                RISK_CRITICAL,
                f"{_action_verb(action)} control-plane or node heartbeat {ref.kind} {ref.name!r} in {ref.namespace!r}",
                blast=BLAST_CRITICAL,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE,
                security=True,
            )
        else:
            ev.add(
                "LEASE_RESOURCE_CHANGE",
                DIM_DISRUPTIVENESS if action == ACTION_MODIFY else DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{_action_verb(action)} leader-election {ref.kind} {ref.name!r}",
                blast=BLAST_MEDIUM,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            )
        return _finalize()

    # 7. CRDs (create / modify)
    if gk == ("apiextensions.k8s.io", "CustomResourceDefinition"):
        ev.add(
            "CRD_SCHEMA_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{_action_verb(action)} CustomResourceDefinition {ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
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
        role_ref_changed = action == ACTION_MODIFY and before is not None and before.obj.get("roleRef") != primary.obj.get("roleRef")
        rbac_narrowed = False
        if action == ACTION_MODIFY and before is not None and not role_ref_changed:
            if ref.kind in ("RoleBinding", "ClusterRoleBinding"):
                b_subjs = before.obj.get("subjects") if isinstance(before.obj.get("subjects"), list) else []
                a_subjs = primary.obj.get("subjects") if isinstance(primary.obj.get("subjects"), list) else []
                if any(s not in a_subjs for s in b_subjs):
                    rbac_narrowed = True
            else:
                b_rules = before.obj.get("rules") if isinstance(before.obj.get("rules"), list) else []
                a_rules = primary.obj.get("rules") if isinstance(primary.obj.get("rules"), list) else []
                if any(r not in a_rules for r in b_rules):
                    rbac_narrowed = True
        ev.add(
            "RBAC_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} RBAC {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_MODERATE if role_ref_changed else REVERT_EASY,
            disrupt=DISRUPT_DESTRUCTIVE if role_ref_changed else (DISRUPT_DISRUPTIVE if rbac_narrowed else DISRUPT_NONE),
            security=True,
        )
        _check_rbac_superuser(ev, primary.obj)
        return _finalize()

    if gk == ("", "ServiceAccount"):
        annos = _meta_dict(primary.obj, "annotations")
        b_annos = _meta_dict(before.obj if before else None, "annotations")
        wi_keys = ("iam.gke.io/gcp-service-account", "eks.amazonaws.com/role-arn", "azure.workload.identity/client-id")
        wi_changed = any(annos.get(wk) != b_annos.get(wk) and (annos.get(wk) or b_annos.get(wk)) for wk in wi_keys)
        ev.add(
            "WORKLOAD_IDENTITY_BINDING_CHANGE" if wi_changed else "IDENTITY_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL if (wi_changed and in_system_ns) else RISK_HIGH,
            f"{_action_verb(action)} ServiceAccount {ref.name!r}" + (" cloud workload-identity federation" if wi_changed else ""),
            blast=BLAST_HIGH if wi_changed else BLAST_MEDIUM,
            revert=REVERT_MODERATE if wi_changed else REVERT_EASY,
            disrupt=DISRUPT_DISRUPTIVE if (action == ACTION_MODIFY and wi_changed) else DISRUPT_NONE,
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
            f"{_action_verb(action)} {ref.gvk_label()} {ref.name!r} (secret backend trust boundary)",
            blast=BLAST_CRITICAL if is_cluster_store else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    if gk == ("", "Secret") or ref.kind in ("SealedSecret", "ExternalSecret"):
        is_imm_edit = action == ACTION_MODIFY and before is not None and before.obj.get("immutable") is True
        ev.add(
            "SECRET_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} {ref.gvk_label()} {ref.name!r}",
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

    # 13. Cloud Infrastructure CRDs (KCC / GKE Cluster / Crossplane / Upbound / ACK / ASO) & CNPG DBs
    if is_cloud_infra_group(ref.group) or ref.group.endswith(".cnpg.io"):
        ev.add(
            "CLOUD_INFRA_CHANGE",
            DIM_REVERTABILITY,
            RISK_HIGH,
            f"{_action_verb(action)} cloud/database infrastructure resource {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL if ref.kind in ("ContainerCluster", "Cluster", "Project", "ComputeNetwork") else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            security=True,
        )
        if "iam" in ref.group.lower() or "iam" in ref.kind.lower():
            _check_cloud_iam(ev, primary.obj)
        return _finalize()

    # 13b. AI/ML Distributed Workloads & Batch Scheduling (KubeRay, JobSet, LeaderWorkerSet, Kueue)
    if gk in AI_ML_WORKLOAD_KINDS:
        def _scan_nested_pod_templates(node: Any, depth: int = 0) -> None:
            if depth > 12:
                return
            if isinstance(node, dict):
                tmpl = node.get("template")
                if isinstance(tmpl, dict):
                    ps = tmpl.get("spec")
                    if isinstance(ps, dict) and any(k in ps for k in ("containers", "initContainers", "ephemeralContainers", "volumes", "securityContext", "hostNetwork", "hostPID", "hostIPC")):
                        _check_pod_spec_posture(ev, ps, require_containers=False)
                for v in node.values():
                    _scan_nested_pod_templates(v, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    _scan_nested_pod_templates(item, depth + 1)

        _scan_nested_pod_templates(primary.obj.get("spec"))
        is_kueue_cluster = ref.group == "kueue.x-k8s.io" and is_cluster_scoped
        ev.add(
            "AI_ML_WORKLOAD_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH if (is_kueue_cluster or action == ACTION_MODIFY) else RISK_MEDIUM,
            f"{_action_verb(action)} distributed AI/ML or batch scheduling resource {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_HIGH if is_kueue_cluster else BLAST_MEDIUM,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()

    # 14. Istio Mesh Resources
    if ref.group.endswith(".istio.io"):
        if ref.kind in ISTIO_PROXY_KINDS:
            mesh_wide = ref.namespace in ("", "istio-system")
            ev.add(
                "ISTIO_PROXY_EXTENSION",
                DIM_SECURITY,
                RISK_CRITICAL if mesh_wide else RISK_HIGH,
                f"{_action_verb(action)} Istio data-plane proxy extension {ref.kind} {ref.name!r}",
                blast=BLAST_CRITICAL if mesh_wide else BLAST_HIGH,
                disrupt=DISRUPT_DISRUPTIVE,
                security=True,
            )
            return _finalize()
        if ref.kind in ISTIO_SECURITY_KINDS:
            _check_istio_security(ev, action, before.obj if before else None, primary.obj)
            return _finalize()
        spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
        if ref.kind == "ServiceEntry":
            hosts = [str(h).lower() for h in (spec.get("hosts") if isinstance(spec.get("hosts"), list) else [])]
            addrs = [str(a) for a in (spec.get("addresses") if isinstance(spec.get("addresses"), list) else [])]
            hijack = any(h.endswith(".svc.cluster.local") or h == "kubernetes.default" for h in hosts) or any(
                _is_link_local_or_loopback_ip(a) for a in addrs
            )
            if hijack:
                ev.add(
                    "MESH_TRAFFIC_HIJACK",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"ServiceEntry {ref.name!r} intercepts internal cluster DNS or link-local/metadata IP",
                    blast=BLAST_CRITICAL,
                    disrupt=DISRUPT_DISRUPTIVE,
                    security=True,
                )
                return _finalize()
        if ref.kind == "DestinationRule" and action == ACTION_MODIFY and before is not None:
            b_sp = before.obj.get("spec") if isinstance(before.obj.get("spec"), dict) else {}
            b_tp = b_sp.get("trafficPolicy") if isinstance(b_sp.get("trafficPolicy"), dict) else {}
            a_tp = spec.get("trafficPolicy") if isinstance(spec.get("trafficPolicy"), dict) else {}
            b_tls = str((b_tp.get("tls") or {}).get("mode") or "").upper() if isinstance(b_tp.get("tls"), dict) else ""
            a_tls = str((a_tp.get("tls") or {}).get("mode") or "").upper() if isinstance(a_tp.get("tls"), dict) else ""
            if b_tls in ("ISTIO_MUTUAL", "MUTUAL", "SIMPLE") and a_tls in ("DISABLE", ""):
                ev.add(
                    "MESH_SECURITY_POLICY_CHANGE",
                    DIM_SECURITY,
                    RISK_CRITICAL,
                    f"DestinationRule {ref.name!r} disables upstream TLS ({b_tls} -> {a_tls or 'NONE'})",
                    blast=BLAST_HIGH,
                    disrupt=DISRUPT_DISRUPTIVE,
                    security=True,
                )
                return _finalize()
        ev.add(
            "ISTIO_TRAFFIC_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{_action_verb(action)} Istio mesh resource {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if ref.namespace == "istio-system" else BLAST_MEDIUM,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()

    # 15. Gateway API Resources (`gateway.networking.k8s.io`)
    if ref.group == "gateway.networking.k8s.io":
        if ref.kind in ("ReferenceGrant", "ReferencePolicy"):
            ev.add(
                "CROSS_NAMESPACE_REFERENCE_GRANT",
                DIM_SECURITY,
                RISK_HIGH,
                f"{_action_verb(action)} Gateway API {ref.kind} {ref.name!r} (cross-namespace access)",
                blast=BLAST_HIGH,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
                security=True,
            )
        elif ref.kind in ("Gateway", "GatewayClass"):
            ev.add(
                "GATEWAY_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{_action_verb(action)} Gateway API {ref.kind} {ref.name!r}",
                blast=BLAST_CRITICAL if ref.kind == "GatewayClass" else BLAST_HIGH,
                revert=REVERT_MODERATE,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
                security=True,
            )
        elif ref.kind == "BackendTLSPolicy":
            ev.add(
                "BACKEND_TLS_POLICY_CHANGE",
                DIM_SECURITY,
                RISK_HIGH,
                f"{_action_verb(action)} Gateway API BackendTLSPolicy {ref.name!r}",
                blast=BLAST_HIGH,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
                security=True,
            )
        elif ref.kind == "BackendLBPolicy":
            ev.add(
                "BACKEND_LB_POLICY_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{_action_verb(action)} Gateway API BackendLBPolicy {ref.name!r}",
                blast=BLAST_MEDIUM,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            )
        else:
            ev.add(
                "TRAFFIC_ROUTE_CHANGE",
                DIM_BLAST_RADIUS,
                RISK_HIGH,
                f"{_action_verb(action)} Gateway API route {ref.kind} {ref.name!r}",
                blast=BLAST_MEDIUM,
                disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            )
        return _finalize()

    # 15b. GKE Networking, Gateway Policies & Compute/Autoscaling
    if gk in GKE_NETWORKING_KINDS:
        _check_gke_networking(ev, action, before.obj if before else None, primary.obj)
        return _finalize()

    if gk in GKE_COMPUTE_AUTOSCALING_KINDS:
        spec = _as_dict(ev, primary.obj.get("spec"), "spec")
        is_zero_quota = False
        if ref.kind == "CapacityQuota":
            lims = spec.get("limits") if isinstance(spec.get("limits"), dict) else {}
            res_lims = lims.get("resources") if isinstance(lims.get("resources"), dict) else {}
            is_zero_quota = any(parse_cpu_millicores(v) == 0.0 for k, v in res_lims.items() if k == "cpu")
        ev.add(
            "GKE_COMPUTE_OR_CAPACITY_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_HIGH,
            f"{_action_verb(action)} GKE compute/autoscaling {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if (action == ACTION_MODIFY or is_zero_quota) else DISRUPT_NONE,
        )
        return _finalize()

    # 15c. Observability & Monitoring (`monitoring.googleapis.com`, `monitoring.coreos.com`)
    if gk in MONITORING_KINDS:
        _check_monitoring_resource(ev, action, before.obj if before else None, primary.obj)
        return _finalize()

    # 16. Ingress / IngressClass / NetworkPolicy / ServiceCIDR / IPAddress / Endpoints / EndpointSlice
    if gk in (("networking.k8s.io", "ServiceCIDR"), ("networking.k8s.io", "IPAddress")):
        b_spec = before.obj.get("spec") if (action == ACTION_MODIFY and before and isinstance(before.obj.get("spec"), dict)) else {}
        a_spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
        cidrs_changed = action == ACTION_MODIFY and b_spec.get("cidrs") != a_spec.get("cidrs")
        ev.add(
            "CLUSTER_IPAM_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL,
            f"{_action_verb(action)} cluster IPAM resource {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_CRITICAL if ref.kind == "ServiceCIDR" else BLAST_HIGH,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DESTRUCTIVE if cidrs_changed else (DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE),
            security=True,
        )
        return _finalize()

    if gk in (("", "Endpoints"), ("discovery.k8s.io", "EndpointSlice")):
        _check_endpoints_or_slice(ev, action, primary.obj)
        return _finalize()

    if gk == ("networking.k8s.io", "Ingress") or gk == ("networking.k8s.io", "IngressClass"):
        annos = _meta_dict(primary.obj, "annotations")
        b_annos = _meta_dict(before.obj if before else None, "annotations")
        has_snippet = any(any(sn in str(k).lower() for sn in _INGRESS_SNIPPET_KEYS) for k in annos)
        auth_removed = action == ACTION_MODIFY and any(
            k in b_annos and k not in annos
            for k in (
                "nginx.ingress.kubernetes.io/auth-url",
                "nginx.ingress.kubernetes.io/auth-type",
                "nginx.ingress.kubernetes.io/auth-tls-verify-client",
            )
        )
        imm_controller = (
            ref.kind == "IngressClass"
            and action == ACTION_MODIFY
            and before is not None
            and (before.obj.get("spec") or {}).get("controller") != (primary.obj.get("spec") or {}).get("controller")
        )
        ev.add(
            "INGRESS_SNIPPET_INJECTION" if has_snippet else ("INGRESS_AUTH_REMOVED" if auth_removed else "INGRESS_CHANGE"),
            DIM_SECURITY if (has_snippet or auth_removed) else DIM_BLAST_RADIUS,
            RISK_CRITICAL if (has_snippet or auth_removed) else RISK_HIGH,
            f"{_action_verb(action)} {ref.kind} {ref.name!r}" + (" with raw proxy snippet annotation" if has_snippet else ""),
            blast=BLAST_CRITICAL if (has_snippet or auth_removed) else BLAST_HIGH,
            disrupt=DISRUPT_DESTRUCTIVE if imm_controller else (DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE),
            security=True,
        )
        return _finalize()

    if gk == ("networking.k8s.io", "NetworkPolicy"):
        ev.add(
            "NETWORK_POLICY_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} NetworkPolicy {ref.name!r} (immediately isolates pods and can block or expose traffic)",
            blast=BLAST_HIGH,
            disrupt=DISRUPT_DISRUPTIVE,
            security=True,
        )
        return _finalize()

    # 17. Storage & DRA (PVC, PV, StorageClass, CSIDriver, CSINode, VolumeAttachment, VolumeSnapshot*, resource.k8s.io)
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
            f"{_action_verb(action)} StorageClass {ref.name!r}",
            blast=BLAST_CRITICAL if is_default else BLAST_HIGH,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()
    if gk in (
        ("", "PersistentVolume"),
        ("storage.k8s.io", "CSIDriver"),
        ("storage.k8s.io", "CSINode"),
        ("storage.k8s.io", "VolumeAttachment"),
        ("snapshot.storage.k8s.io", "VolumeSnapshot"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotContent"),
        ("snapshot.storage.k8s.io", "VolumeSnapshotClass"),
    ):
        _check_storage_resource(ev, action, before.obj if before else None, primary.obj)
        return _finalize()
    if gk in DRA_KINDS:
        is_slice = ref.kind == "ResourceSlice"
        ev.add(
            "DRA_CLUSTER_RESOURCE_CHANGE" if is_cluster_scoped else "DRA_RESOURCE_CLAIM_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL if is_slice else RISK_HIGH,
            f"{_action_verb(action)} Dynamic Resource Allocation {ref.gvk_label()} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()

    # 18. PriorityClass / RuntimeClass / ResourceQuota / LimitRange / CertManager
    if gk == ("scheduling.k8s.io", "PriorityClass"):
        val = primary.obj.get("value")
        is_glob = primary.obj.get("globalDefault") is True or (_is_strict_int(val) and val >= 1_000_000_000)
        imm_pc = action == ACTION_MODIFY and before is not None and (
            before.obj.get("value") != val or before.obj.get("preemptionPolicy") != primary.obj.get("preemptionPolicy")
        )
        ev.add(
            "PRIORITY_CLASS_CHANGE",
            DIM_BLAST_RADIUS,
            RISK_CRITICAL if is_glob else RISK_HIGH,
            f"{_action_verb(action)} PriorityClass {ref.name!r}",
            blast=BLAST_CRITICAL if is_glob else BLAST_HIGH,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE if imm_pc else DISRUPT_NONE,
        )
        return _finalize()
    if gk == ("node.k8s.io", "RuntimeClass"):
        imm_rc = action == ACTION_MODIFY and before is not None and before.obj.get("handler") != primary.obj.get("handler")
        ev.add(
            "RUNTIME_CLASS_CHANGE",
            DIM_SECURITY,
            RISK_HIGH,
            f"{_action_verb(action)} RuntimeClass {ref.name!r}",
            blast=BLAST_HIGH,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DESTRUCTIVE if imm_rc else (DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE),
            security=True,
        )
        return _finalize()
    if gk in (("", "ResourceQuota"), ("", "LimitRange")):
        spec = primary.obj.get("spec") if isinstance(primary.obj.get("spec"), dict) else {}
        if ref.kind == "ResourceQuota":
            hard = spec.get("hard") if isinstance(spec.get("hard"), dict) else {}
            zero_quota = any(_is_zero_quota_value(v) for v in hard.values())
        else:
            lim_items = spec.get("limits") if isinstance(spec.get("limits"), list) else []
            zero_quota = any(
                _is_zero_quota_value(v)
                for item in lim_items
                if isinstance(item, dict)
                for bound_key in ("max", "default")
                for v in (item.get(bound_key).values() if isinstance(item.get(bound_key), dict) else ())
            )
        ev.add(
            "QUOTA_OR_LIMIT_CHANGE",
            DIM_DISRUPTIVENESS if zero_quota else DIM_BLAST_RADIUS,
            RISK_HIGH if (zero_quota or action == ACTION_MODIFY) else RISK_MEDIUM,
            f"{_action_verb(action)} {ref.kind} {ref.name!r}",
            blast=BLAST_MEDIUM,
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if (zero_quota or action == ACTION_MODIFY) else DISRUPT_NONE,
        )
        return _finalize()
    if ref.group == "cert-manager.io":
        ev.add(
            "CERT_MANAGER_CHANGE",
            DIM_SECURITY,
            RISK_HIGH if is_cluster_scoped else RISK_MEDIUM,
            f"{_action_verb(action)} cert-manager {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
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
            f"{_action_verb(action)} VerticalPodAutoscaler {ref.name!r} with updateMode={mode!r}",
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if active else DISRUPT_NONE,
        )
        return _finalize()

    # 20b. KEDA TriggerAuthentication / ClusterTriggerAuthentication / ScaledJob
    if gk in (("keda.sh", "TriggerAuthentication"), ("keda.sh", "ClusterTriggerAuthentication")):
        ev.add(
            "KEDA_AUTH_CHANGE",
            DIM_SECURITY,
            RISK_CRITICAL if is_cluster_scoped else RISK_HIGH,
            f"{_action_verb(action)} KEDA {ref.kind} {ref.name!r}",
            blast=BLAST_HIGH if is_cluster_scoped else BLAST_MEDIUM,
            revert=REVERT_DIFFICULT,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
            security=True,
        )
        return _finalize()
    if gk == ("keda.sh", "ScaledJob"):
        ev.add(
            "KEDA_SCALED_JOB_CHANGE",
            DIM_DISRUPTIVENESS if action == ACTION_MODIFY else DIM_BLAST_RADIUS,
            RISK_HIGH if action == ACTION_MODIFY else RISK_MEDIUM,
            f"{_action_verb(action)} KEDA ScaledJob {ref.name!r}",
            revert=REVERT_MODERATE,
            disrupt=DISRUPT_DISRUPTIVE if action == ACTION_MODIFY else DISRUPT_NONE,
        )
        return _finalize()

    # 21. Remaining modify-capable kinds (PodTemplate, ConfigMap, Service, HPA, KEDA ScaledObject, Workloads)
    if gk == ("", "PodTemplate"):
        _check_pod_template(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("", "ConfigMap"):
        _check_configmap(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("", "Service"):
        _check_service(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("autoscaling", "HorizontalPodAutoscaler"):
        _check_hpa(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk == ("keda.sh", "ScaledObject"):
        _check_keda_scaled_object(ev, action, before.obj if before else None, primary.obj, diffs)
    elif gk in WORKLOAD_KINDS:
        if action == ACTION_ADD:
            _check_workload_add(ev, primary.obj)
        else:
            assert before is not None
            _check_workload_modify(ev, before.obj, primary.obj, diffs)

    return _finalize()
