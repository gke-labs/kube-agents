---
name: review-security-k8s-network
description: Reviews Kubernetes network configurations, access controls, and routing boundaries for security vulnerabilities.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes network configurations, including NetworkPolicies, Services, Ingresses, and Service Mesh setups, for security vulnerabilities.

## Focus Areas & Deterministic Checks:

### 1. Network Isolation & Micro-segmentation
- **Default-Deny Enforcement**: Verify that every namespace implements a "default-deny" `NetworkPolicy` for *both* Ingress and Egress traffic.
- **Egress Neglect**: Specifically flag workloads that lack Egress restrictions. Unrestricted egress allows compromised pods to exfiltrate data, perform lateral movement, or establish C2 beaconing.
- **Overly Permissive Rules**: Flag `NetworkPolicy` rules containing broad CIDR blocks (e.g., `0.0.0.0/0` or `::/0`) or empty `podSelector`/`namespaceSelector` blocks unless strictly justified.

### 2. Service & Exposure Security
- **Accidental Public Exposure**: Check `Service` definitions of type `LoadBalancer`. Ensure they have the appropriate cloud-provider annotations (e.g., `networking.gke.io/load-balancer-type: "Internal"`) if they are meant to be internal-only.
- **NodePort Usage**: Flag the use of `NodePort` services. These open ports across all nodes in the cluster and bypass standard network protections.
- **Sensitive Port Exposure**: Flag any services exposing known administrative or sensitive ports (e.g., 22, 3389, 2379 for etcd, 10250 for kubelet) to broader networks.

### 3. Traffic Routing & Ingress Hygiene
- **Ingress TLS**: Evaluate `Ingress` configurations to ensure TLS is enforced (`tls` block is present and properly configured). Flag instances where HTTP is not automatically redirected to HTTPS.
- **Endpoint/EndpointSlice Hijacking**: Check for manually created `Endpoints` or `EndpointSlice` resources. Attackers can use these to secretly redirect intra-cluster traffic to external malicious IPs.
- **HostNetwork Bypass**: Verify that `NetworkPolicies` are not bypassed by workloads using `hostNetwork: true` (which operate in the node's network namespace and ignore pod-level network policies).

### 4. Service Mesh (If Applicable)
- **mTLS Enforcement**: If a service mesh (like Istio or Linkerd) is detected, verify that `PeerAuthentication` or equivalent resources enforce `STRICT` mutual TLS (mTLS) for all inter-service communication.
- **Authorization Bypass**: Review mesh `AuthorizationPolicies` to ensure they follow a default-deny posture and are not inadvertently conflicting with permissive legacy `NetworkPolicies`.
