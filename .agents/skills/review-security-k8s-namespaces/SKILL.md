---
name: review-security-k8s-namespaces
description: Reviews Kubernetes namespace configurations for workload isolation, multi-tenancy, and boundary defense.
---
# Task
Review Kubernetes namespace configs/resources for structural boundaries, workload isolation, and defense-in-depth security.

# Checks
## 1. Structural Isolation
- **Workload Density**: Flag if most workloads (>80-90%) are dumped into a single namespace (`default`, `prod`). Expect micro-segmentation.
- **Tenant Mixing**: Flag mixing of different trust levels (dev/staging/prod) or tenants in the same namespace.

## 2. Abuse & Evasion
- **System Namespace Abuse**: Flag custom workloads in `default`, `kube-system`, `kube-public`, `kube-node-lease`.
- **Rogue Namespaces**: Flag names impersonating system components (e.g., `kube-admin`, `k8s-infra`).
- **Policy Bypass**: Flag labels/annotations on non-system namespaces that bypass cluster policies (e.g., OPA exemptions, `pod-security.kubernetes.io/enforce=privileged`).
- **Quota Evasion**: Flag absurdly high `ResourceQuotas`/`LimitRanges`.
- **Finalizer Abuse**: Flag suspicious `finalizers` on namespaces.

## 3. Cross-Namespace Risks
- **Cross-References**: Flag illegitimate cross-namespace resource references (e.g., `Gateway` lacking `ReferenceGrant`, `ExternalName` to internal namespaces).
- **Dangling Namespaces**: Flag active `Secrets`, `ServiceAccounts`, or `RoleBindings` in namespaces with no active pods.
