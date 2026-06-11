---
name: gke-multi-tenancy
description: Guidance on implementing multi-tenancy and governance in Google Kubernetes Engine (GKE) clusters.
---

# GKE Multi-tenancy and Governance

This skill provides guidance on implementing multi-tenancy and governance in Google Kubernetes Engine (GKE) clusters.

## Overview

Multi-tenancy allows you to share a single GKE cluster among multiple teams or applications securely. Governance ensures that policies and resource limits are enforced.

## Workflows

### 1. Create Namespaces for Isolation

Namespaces provide a scope for names and are the primary unit of isolation in Kubernetes.

**Steps:**

1. Create a namespace for each tenant.

**Example Namespace Manifest:**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: tenant-a
  labels:
    team: alpha
```

### 2. Configure RBAC for Least Privilege

Role-Based Access Control (RBAC) allows you to control who has access to what resources within a namespace.

**Steps:**

1. Define a `Role` with specific permissions.
2. Bind the `Role` to a user or group using a `RoleBinding`.

**Example Role and RoleBinding Manifest:**

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: tenant-a
  name: pod-reader
rules:
  - apiGroups: [""] # "" indicates the core API group
    resources: ["pods"]
    verbs: ["get", "watch", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: read-pods
  namespace: tenant-a
subjects:
  - kind: User
    name: user@example.com # Name is case sensitive
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: Role
  name: pod-reader
  apiGroup: rbac.authorization.k8s.io
```

### 3. Enforce Resource Quotas

Resource quotas prevent a single tenant from consuming all resources in the cluster.

**Example ResourceQuota Manifest:**

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: tenant-a-quota
  namespace: tenant-a
spec:
  hard:
    requests.cpu: "2"
    requests.memory: 4Gi
    limits.cpu: "4"
    limits.memory: 8Gi
```

## Best Practices

1. **Namespace Per Tenant**: Always use separate namespaces for different teams or applications.
2. **Least Privilege RBAC**: Grant only the permissions necessary for users and service accounts to do their jobs.
3. **Enforce Quotas**: Use Resource Quotas to ensure fair sharing of cluster resources.
4. **Network Policies**: Combine namespaces with Network Policies (see [gke-workload-security](../gke-workload-security/SKILL.md)) to restrict cross-tenant traffic.
