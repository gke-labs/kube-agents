---
name: review-security-k8s-nodes
description: Reviews Kubernetes YAML manifests for node boundary violations and node-to-cluster escalation risks.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes YAML manifests specifically looking for issues that violate the node boundary. Your focus is exclusively on misconfigurations that would allow an attacker who has compromised a single node (or a pod on that node) to escalate their privileges *further* into the rest of the cluster.

## Focus Areas:

### 1. Node Credential & HostPath Abuse
- **Kubelet Credential Theft**: Scrutinize workloads that mount sensitive node directories via `hostPath` (e.g., `/etc/kubernetes`, `/var/lib/kubelet`, or node `kubeconfig` files). Be aware of the nuance: historically, daemons used these credentials because the NodeRestriction admission plugin and Node Authorization plugin limit access to the same node Kubelet is running on (unlike standard ServiceAccounts). While this can be a valid security pattern, flag it for review. Recommend investigating if modern Validating Admission Policies (VAPs) could be used instead to restrict standard ServiceAccount writes by enforcing the resource name to match the node name from the client identity.
- **Runtime Socket Mounts**: Flag `hostPath` mounts to `/var/run/docker.sock`, `/run/containerd/containerd.sock`, or `/run/crio/crio.sock`. Access to the container runtime socket allows a compromised pod to execute commands in any other pod on that node, enabling the theft of highly privileged service account tokens.

### 2. Node RBAC & NodeRestriction Bypass
- **Overprivileged Node Groups**: Flag any `RoleBinding` or `ClusterRoleBinding` in the YAML that assigns additional permissions to the `system:nodes` group or specific `system:node:<name>` users. Nodes should be strictly limited by the `NodeRestriction` admission controller; manually granting them extra RBAC breaks this boundary.
- **Node Impersonation**: Flag any RBAC roles granting the `impersonate` verb specifically targeting the `system:nodes` group or node users.
- **KSA Node Modification & Re-labeling**: Explicitly flag any roles bound to standard Kubernetes Service Accounts (KSAs) that grant permission to modify nodes in any way (e.g., `create`, `update`, `patch`, or `delete` verbs on `nodes`, `nodes/status`, or `nodes/proxy`). The primary risk here is that a compromised workload can leverage these permissions to re-label or re-taint the node (especially if node-restricted labels aren't strictly enforced), allowing the attacker to manipulate scheduling and attract highly sensitive pods to their compromised node.

### 3. Taint & Toleration Evasion (Lateral Pivot)
- **Malicious Scheduling**: Flag if untrusted or generic workloads are granted extremely broad `tolerations` (e.g., `operator: Exists` with no key). An attacker could exploit this to force an untrusted pod onto a dedicated, highly sensitive node (like a control-plane or secrets-management node) to attempt a local node breakout.
