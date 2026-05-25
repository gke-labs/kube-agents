---
name: review-security-k8s-agents-data-exfil
description: Reviews Kubernetes configurations to prevent data exfiltration by AI agents.
---
# Task
Review infrastructure for AI agent data exfiltration risks (e.g. via prompt injection).

# Checks
## 1. Strict Egress Allowlisting
- **NetworkPolicies**: Require restrictive egress. Flag missing egress controls or `0.0.0.0/0`.
- **Allowlisting**: Limit egress strictly to necessary internal services and authorized LLM APIs.

## 2. Egress Gateways
- **Interception**: Require outbound traffic routing through transparent proxies/Egress Gateways (e.g., Service Mesh, `HTTP_PROXY`) for Deep Packet Inspection and SNI allowlisting.

## 3. Data Access
- **Blanket Permissions**: Flag broad `get`/`list`/`watch` RBAC on sensitive resources (`secrets`, `configmaps`).
- **Over-privileged Mounts**: Flag broad or shared sensitive volume mounts.
