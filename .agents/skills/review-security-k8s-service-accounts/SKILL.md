---
name: review-security-k8s-service-accounts
description: Reviews Kubernetes ServiceAccount configurations for identity management and least privilege boundaries.
---
# Task
Review Kubernetes ServiceAccount resources/configurations to ensure least privilege and strict identity boundaries.

# Checks
## 1. Identity Boundaries & Defaults
- **Default Usage**: Flag RBAC bindings assigned to the `default` service account.
- **Identity Sprawl**: Flag custom ServiceAccounts shared across distinct applications. Enforce 1:1 app-to-account mapping.
- **Token Automounting**: Require `automountServiceAccountToken: false` on ServiceAccounts.

## 2. Cloud IAM Bridges
- **Workload Identity**: Flag ServiceAccounts shared across workloads mapped to a single cloud identity (e.g., GCP `iam.gke.io/gcp-service-account`).

## 3. Legacy & Over-provisioning
- **Long-lived Tokens**: Flag explicit `Secret` objects of type `kubernetes.io/service-account-token`. Require ephemeral `TokenRequest` API.
- **Image Pull Secrets**: Ensure `imagePullSecrets` don't grant broad access to corporate registries.
- **Orphaned Identities**: Flag ServiceAccounts with RBAC bindings but no active workloads.
