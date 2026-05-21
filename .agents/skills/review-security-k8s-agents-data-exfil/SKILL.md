---
name: review-security-k8s-agents-data-exfil
description: Reviews configurations and architectures to prevent data exfiltration by AI agents.
---

# Instructions
You are a security expert specializing in AI agents running on Kubernetes. Your task is to review the infrastructure for risks related to data exfiltration. AI agents are highly susceptible to prompt injection attacks that attempt to trick the agent into reading sensitive internal data and transmitting it to an attacker-controlled endpoint.

## Focus Areas & Deterministic Checks:

### 1. Strict Egress Allowlisting
- **Egress NetworkPolicies**: AI agents must be heavily restricted from communicating with the open internet. Flag any agent workload that has a `NetworkPolicy` `Egress` rule containing `0.0.0.0/0` or is missing egress controls entirely.
- **Provider IP / DNS Allowlisting**: Ensure the agent's egress is explicitly limited *only* to required internal services and authorized LLM provider APIs (e.g., OpenAI, Anthropic, Google GenAI, or local inference endpoints).

### 2. Egress Gateways & Interception
- **Egress Proxies / SNI Filtering**: Check if agent pods are forced to route outbound traffic through a transparent proxy or a dedicated Egress Gateway (e.g., via a Service Mesh, or an explicit `HTTP_PROXY` environment variable). These gateways provide an essential layer of Deep Packet Inspection (DPI) and SNI whitelisting to prevent the agent from sneaking data out to arbitrary, attacker-controlled domains.

### 3. Data Access Least Privilege
- **Blanket Read Permissions**: Flag any agent ServiceAccount that has broad `get`, `list`, or `watch` permissions across sensitive resource types (like `secrets`, `configmaps`, or custom database definitions). An agent should ideally impersonate the invoking user rather than having blanket access to backend data stores.
- **Over-privileged Mounts**: Flag agent deployments that mount sensitive or shared data volumes unless strictly necessary for the agent's constrained function.
