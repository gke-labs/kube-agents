---
name: review-security-k8s-network
description: Reviews Kubernetes network configurations (NetworkPolicies, Services, Ingress) for isolation and exposure risks.
---
# Task
Review Kubernetes network configurations (`NetworkPolicies`, `Services`, `Ingresses`, Service Mesh) for vulnerabilities.

# Checks
## 1. Network Isolation
- **Default-Deny**: Verify every namespace has a "default-deny" `NetworkPolicy` for *both* Ingress and Egress.
- **Egress Neglect**: Flag workloads lacking Egress restrictions (enables data exfiltration/C2).
- **Overly Permissive Rules**: Flag broad CIDRs (`0.0.0.0/0`, `::/0`) or empty `podSelector`/`namespaceSelector`.

## 2. Service Exposure
- **Accidental Public Exposure**: Flag `LoadBalancer` services lacking internal annotations if meant to be internal.
- **NodePort Usage**: Flag `NodePort` usage (bypasses standard protections).
- **Sensitive Ports**: Flag exposure of admin/sensitive ports (22, 3389, 2379, 10250).

## 3. Traffic Routing
- **Ingress TLS**: Require `tls` block on Ingresses. Flag missing HTTP->HTTPS redirects.
- **Endpoint Hijacking**: Flag manual `Endpoints`/`EndpointSlice` resources (risk of malicious external redirect).
- **HostNetwork Bypass**: Flag `hostNetwork: true` (bypasses pod network policies).

## 4. Service Mesh
- **mTLS Enforcement**: Require `STRICT` mutual TLS via `PeerAuthentication`.
- **Authorization Bypass**: Ensure `AuthorizationPolicies` follow default-deny and don't conflict with legacy policies.
