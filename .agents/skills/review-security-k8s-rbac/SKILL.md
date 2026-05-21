---
name: review-security-k8s-rbac
description: Reviews Kubernetes RBAC configurations for security issues based on GKE best practices and advanced attack vectors.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes Role-Based Access Control (RBAC) configurations for security vulnerabilities, strictly adhering to GKE best practices and anticipating advanced attack vectors.

## Review Process & Deterministic Order
You MUST conduct your review in the following deterministic order, starting from the most global and severe issues and moving towards more localized resource configurations.

### 1. Global, Group, & Default Configuration Review
- **Default Groups & Users**: Flag any non-default bindings targeting `system:anonymous`, `system:unauthenticated`, `system:authenticated`, or `system:masters`.
- **Broad Service Account Groups**: Flag bindings targeting `system:serviceaccounts` or `system:serviceaccounts:<namespace>`.
- **Cluster Admin**: Flag any bindings of the `cluster-admin` role to unauthenticated, anonymous, or overly broad user groups.
- **System Roles**: Flag any roles granting `delete` or `update` on `system:` prefixed RoleBindings and ClusterRoleBindings.
- **Missing Subject Namespaces**: Flag `ClusterRoleBindings` where a subject of kind `ServiceAccount` is missing the `namespace` field.

### 2. Privilege Escalation & Core Security Bypass Review
- **RBAC & CSR Modification**: Flag any roles granting `bind`, `escalate`, `create`, `update`, or `patch` on `rbac.authorization.k8s.io` resources. Flag `create` on `certificatesigningrequests` or `update` on `certificatesigningrequests/approval`.
- **Webhook & CRD Manipulation**: Flag `create`, `update`, or `patch` on `mutatingwebhookconfigurations`, `validatingwebhookconfigurations`, or `customresourcedefinitions`.
- **Auth Probing**: Flag `create` on `tokenreviews` or `subjectaccessreviews`.
- **Security Controls Bypass**: Flag roles granting `update` or `patch` on `namespaces` (PSA bypass) and any modification access to `networkpolicies`.

### 3. Role & Binding Design Review
- **Wildcards & Mass Deletion**: Flag any use of the `*` wildcard in `apiGroups`, `resources`, or `verbs`. Flag the use of the `deletecollection` verb for DoS risks.
- **Privilege Escalation Verbs**: Check for `escalate`, `bind`, and `impersonate` verbs in general.
- **Over-scoped Bindings & Namespace Isolation**: Flag when access control relies primarily on cluster-scoped access (`ClusterRoleBindings`) instead of adhering to the Principle of Least Privilege via namespace-local `RoleBindings`. A `RoleBinding` should be used wherever possible to maintain true namespace isolation. Also flag when a `Role` grants access to disjoint sets of verbs/resources that should be split into multiple rules.
- **Self-Modification**: Flag any role that allows a Pod to self-modify (e.g., update its own ServiceAccount or rolebindings).

### 4. Sensitive Resources & Subresources Review
- **Sensitive Resources**: Flag any roles granting access to highly sensitive resources such as `secrets`, `pods/exec`, `pods/portforward`, `pods/attach`, or `nodes/proxy`. Ensure these are strictly scoped (e.g., using `resourceNames`).
- **Secret Harvesting**: Explicitly flag `list` or `watch` verbs on `secrets` as they enable mass exfiltration.
- **Ephemeral Containers**: Flag `update` or `patch` on `pods/ephemeralcontainers`.
- **Status Modification**: Flag non-system roles granting access to `/status` subresources (like `pods/status`).
