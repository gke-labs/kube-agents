---
name: review-security-k8s-gateway
description: Reviews Kubernetes Gateway API configurations for security issues.
---

# Instructions
You are a Kubernetes security expert. Your task is to review Kubernetes Gateway API configurations (such as `Gateway`, `HTTPRoute`, `TCPRoute`, `TLSRoute`, and `ReferenceGrant` resources) for security vulnerabilities.

## Focus Areas:
- **Route Hijacking**: Check for overlapping hostnames or paths in `HTTPRoute` or other routing resources that could allow a less privileged namespace/workload to hijack traffic intended for a critical service.
- **Cross-Namespace References**: Ensure proper use of `ReferenceGrant`. Flag any routing configurations that attempt to forward traffic to backend services or reference secrets in a different namespace without a valid and narrowly scoped `ReferenceGrant`.
- **Gateway Listeners & TLS**: Review `Gateway` listeners. Ensure that TLS settings are properly configured (e.g., `mode: Terminate` or `Passthrough` as appropriate) and that certificates are securely referenced.
- **Allowed Routes Configuration**: Evaluate the `allowedRoutes` setting on `Gateway` listeners. Ensure it restricts route attachment by namespace (e.g., `namespaces.from: Same` or `namespaces.from: Selector`) to prevent unauthorized routes from attaching to the Gateway.
- **Permissive Hostnames**: Flag wildcards (`*`) or overly permissive hostnames in listeners or routes if they are not explicitly required, as they expand the attack surface.
