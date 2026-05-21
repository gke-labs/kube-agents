---
name: review-security-k8s-rbac
description: Reviews Kubernetes RBAC configurations for permissions and privilege escalation risks.
---
# Task
Review Kubernetes RBAC configurations for vulnerabilities, prioritizing GKE best practices and advanced attack vectors.

# Checks
## 1. Global & Default Configs
- **Default Groups/Users**: Flag non-default bindings for `system:anonymous`, `system:unauthenticated`, `system:authenticated`, `system:masters`.
- **Broad Service Accounts**: Flag bindings for `system:serviceaccounts` or `system:serviceaccounts:<namespace>`.
- **Cluster Admin**: Flag `cluster-admin` bindings to unauthenticated/broad groups.
- **System Roles**: Flag `delete`/`update` on `system:` prefixed bindings.
- **Missing Namespaces**: Flag `ClusterRoleBindings` to `ServiceAccount` missing a `namespace`.

## 2. Privilege Escalation & Bypasses
- **RBAC & CSR**: Flag `bind`, `escalate`, `create`, `update`, `patch` on `rbac.authorization.k8s.io`. Flag `create` on `certificatesigningrequests` or `update` on `certificatesigningrequests/approval`.
- **Webhooks & CRDs**: Flag `create`, `update`, `patch` on `mutatingwebhookconfigurations`, `validatingwebhookconfigurations`, or `customresourcedefinitions`.
- **Auth Probing**: Flag `create` on `tokenreviews`/`subjectaccessreviews`.
- **Policy Bypass**: Flag `update`/`patch` on `namespaces` (PSA bypass) or modification access to `networkpolicies`.

## 3. Role & Binding Design
- **Wildcards**: Flag `*` in `apiGroups`, `resources`, `verbs`. Flag `deletecollection` (DoS risk).
- **Escalation Verbs**: Flag `escalate`, `bind`, `impersonate`.
- **Over-scoped**: Flag `ClusterRoleBindings` used where namespace-local `RoleBindings` suffice. Flag disjoint resource/verb sets in single roles.
- **Self-Modification**: Flag roles allowing pods to self-modify (e.g., updating own ServiceAccount/rolebindings).

## 4. Sensitive Resources
- **Highly Sensitive**: Flag access to `secrets`, `pods/exec`, `pods/portforward`, `pods/attach`, `nodes/proxy` (unless strictly scoped via `resourceNames`).
- **Secret Harvesting**: Flag `list` or `watch` on `secrets`.
- **Ephemeral/Status**: Flag `update`/`patch` on `pods/ephemeralcontainers` or `/status` subresources.

## 5. GKE RBAC Guide
- Refer to `resources/gke-rbac.md` for additional guidance. Identify if any of this guidance is both relevant and missing from your review and include those findings as well.
