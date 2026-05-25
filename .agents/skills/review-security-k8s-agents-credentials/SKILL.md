---
name: review-security-k8s-agents-credentials
description: Reviews AI agent Kubernetes configurations for credentials exposed to agents.
---
# Task
Review K8s configurations tailored to unique AI agent risks (prompt injection, RCE, credential exfiltration).

# Checks
## 1. Zero-Trust Key Management
- **Credential Proxy**: All authenticated outbound requests MUST route through a dedicated injecting proxy/sidecar. Flag agent containers with direct access to Secrets.
- **No Direct Mounts/EnvVars**: Main agent container MUST NOT use `env`, `envFrom`, or volume mounts for secrets.
- **No Hardcoded Credentials**: Flag any plaintext keys/passwords in manifests.

## 2. Least Privilege
- **No Auto-mounting**: Require `automountServiceAccountToken: false`. If API access is required, mount token ONLY in the sidecar proxy.
- **Dedicated Accounts**: Require dedicated `serviceAccountName`. Flag `default`.
- **Granular RBAC**: Ensure strict scoping. Flag wildcards (`*`) in `verbs` or `resources`.
