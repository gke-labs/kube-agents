---
name: review-security-k8s-service-accounts
description: Reviews Kubernetes ServiceAccount configurations and identity management for security vulnerabilities.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes ServiceAccount resources and their related configurations to ensure least privilege and strict identity boundaries.

## Focus Areas & Deterministic Checks:

### 1. Identity Boundaries & Defaults
- **Default Service Account Usage**: Flag any RBAC bindings that assign permissions to the `default` service account in any namespace.
- **Identity Sharing (Sprawl)**: Check if a single custom ServiceAccount is shared across multiple distinct applications or deployments. Enforce a 1:1 mapping of application to ServiceAccount to maintain strict identity boundaries.
- **Token Automounting**: Ensure `automountServiceAccountToken: false` is explicitly set on ServiceAccounts. This enforces a secure-by-default posture, preventing every pod from having a token injected unless explicitly requested at the pod level.

### 2. Cloud IAM Bridges & External Identity
- **Workload Identity Bridges**: Cross-reference ServiceAccounts with cloud identity annotations (e.g., `iam.gke.io/gcp-service-account` for Google Cloud). If it's shared across many workloads even though the workloads are using separate KSAs, highlight this risk. 

### 3. Legacy Credentials & Over-provisioning
- **Long-lived Tokens**: Flag the existence of any explicitly created `Secret` objects of type `kubernetes.io/service-account-token`. These are legacy, long-lived, non-expiring credentials that should be replaced with the ephemeral `TokenRequest` API.
- **Image Pull Secrets**: Review `imagePullSecrets` attached to ServiceAccounts. Ensure they do not provide broad, unneeded access to corporate container registries that an attacker could abuse to steal proprietary images.
- **Orphaned Identities**: Flag ServiceAccounts that have RBAC bindings granting them permissions, but are not actually assigned to any active workloads in the namespace.
