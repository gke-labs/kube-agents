---
name: review-security-k8s-namespaces
description: Reviews Kubernetes namespace configurations and structural boundaries for workload isolation and security issues.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes namespace configurations and resources to ensure proper structural boundaries, workload isolation, and defense-in-depth security.

## Focus Areas:

### 1. Structural Isolation & Boundary Evaluation
Evaluate if there is a "decent amount of isolation between workloads via use of namespaces" by checking the following:
- **Workload Density & Logical Grouping**: Flag if a vast majority (e.g., >80-90%) of workloads are dumped into a single namespace (like `default` or `prod`). Expect a micro-segmentation strategy delineated by application, team, tenant, or environment.
- **Environment & Tenant Mixing**: Flag if naming conventions or labels suggest that different trust levels (e.g., 'dev', 'staging', 'prod') or different tenants share the same namespace.

### 2. Abuse & Evasion Detection
- **"Default" and System Namespace Abuse**: Flag custom, non-system workloads deployed in default or system namespaces (`default`, `kube-system`, `kube-public`, `kube-node-lease`).
- **Rogue Namespace Names**: Flag namespaces with names attempting to impersonate system components (e.g., `kube-admin`, `system-core`, `k8s-infra`) to hide unauthorized workloads.
- **Security Policy Bypass**: Flag labels or annotations applied to non-infrastructure namespaces that bypass cluster-wide security policies (e.g., OPA Gatekeeper exemptions or `pod-security.kubernetes.io/enforce=privileged`).
- **Resource Quota "Evasion"**: Evaluate if the applied `ResourceQuotas` or `LimitRanges` are absurdly high, rendering them useless against noisy-neighbor attacks or resource exhaustion.
- **Namespace Finalizer Abuse**: Check for unrecognized or suspicious `finalizers` on the namespace object, which attackers can use for persistence or DoS during cleanup.

### 3. Cross-Namespace Risks & Hygiene
- **Cross-Namespace References**: Flag any configurations attempting to illegitimately reference resources across namespace boundaries. Check `Gateway` API configs (e.g., `Gateway`, `HTTPRoute` missing `ReferenceGrant`), `Ingress` TLS secret references, `ExternalName` services pointing to internal namespaces, and custom operators.
- **Dangling or Orphaned Namespaces**: Detect namespaces with no active pods or deployments that still contain active `Secrets`, `ServiceAccounts`, or `RoleBindings`, which can be leveraged for lateral movement.
