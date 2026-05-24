---
name: review-security-k8s-nodes
description: Reviews Kubernetes manifests for node boundary violations and risks of node-to-cluster privilege escalation.
---
# Task
Review K8s manifests for node boundary violations allowing compromised nodes/pods to escalate privileges cluster-wide.

# Checks
## 1. Credential & HostPath Abuse
- **Kubelet Credential Theft**: Flag `hostPath` mounts to sensitive node directories (`/etc/kubernetes`, `/var/lib/kubelet`, `kubeconfig`). Recommend VAPs.
- **Runtime Socket Mounts**: Flag `hostPath` mounts to container runtime sockets (e.g., `/var/run/docker.sock`, `containerd.sock`). Enables full node takeover.

## 2. RBAC & NodeRestriction Bypass
- **Overprivileged Node Groups**: Flag extra RBAC bindings for `system:nodes` or `system:node:<name>` bypassing `NodeRestriction`.
- **Node Impersonation**: Flag roles granting `impersonate` on `system:nodes`.
- **Node Modification**: Flag KSA roles granting node modification (`create`, `update`, `patch`, `delete` on `nodes`, `nodes/status`, etc.). Allows malicious relabeling/scheduling.

## 3. Lateral Pivot via Scheduling
- **Malicious Scheduling**: Flag broad `tolerations` (e.g., `operator: Exists` with no key) on untrusted workloads allowing them to schedule on sensitive nodes (e.g. control-plane).
