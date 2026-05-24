---
name: review-security-k8s-agents-firewall
description: Reviews Kubernetes network and firewall configurations specifically for AI agent execution sandboxes.
---
# Task
Review network policies and firewall configs for AI agent control loops and execution sandboxes.

# Checks
## 1. Egress Restrictions
- **Sandbox Network**: Enforce default-deny egress. Allowlist absolute minimum required IPs/services.
- **Internal APIs**: Block agent/sandbox access to internal cluster APIs, K8s services, and cloud metadata (e.g., `169.254.169.254`).
- **Exfiltration Vectors**: Flag broad egress (e.g., `0.0.0.0/0`) on agent pods.

## 2. Ingress & Invocation
- **Authorized Sources**: Restrict agent API ingress to trusted upstream services (auth gateways, orchestrators).
- **Bypass Prevention**: Flag LoadBalancer or NodePort exposure on main agent containers lacking strict ingress NetworkPolicies.
