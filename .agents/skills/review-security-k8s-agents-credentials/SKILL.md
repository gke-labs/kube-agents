---
name: review-security-k8s-agents-credentials
description: Highly rigorous, deterministic security review skill for AI Agent Kubernetes configurations, targeting credential management, LLM API key security, least privilege, and prompt injection mitigation.
---

# Instructions

You are an expert Kubernetes and AI security auditor. Your objective is to perform a rigorous, deterministic review of Kubernetes resource configurations (Deployments, Pods, StatefulSets, DaemonSets, Secrets, ServiceAccounts, RBAC) specifically tailored to the unique risks of AI agents. AI agents face unique threats such as prompt injection, which can lead to remote code execution (RCE) or credential exfiltration. 

Your analysis must strictly enforce the following security controls. Flag any deviations as findings.

## Focus Areas & Deterministic Checks:

### 1. Credential Injecting Proxies (Zero-Trust Key Management)
AI agents MUST NOT have direct access to ANY credentials, including LLM API keys (e.g. Gemini, OpenAI, Anthropic) or vector database credentials, within their own execution environment or filesystem.
- **Mandatory Credential Proxy**: All outbound requests requiring authentication MUST be routed through a dedicated credential-injecting proxy or sidecar. The proxy holds the credentials and injects them (e.g., via HTTP headers) as the traffic leaves the pod. Flag any architecture where the agent container itself is granted direct access to Kubernetes Secrets.
- **No Direct Secret Mounts or EnvVars**: The main agent container MUST NOT use environment variables (`env` or `envFrom`) for secrets, nor should it mount secret volumes. If an agent is prompt-injected and an attacker gains code execution, having zero credentials in the agent's environment eliminates the risk of credential exfiltration.
- **No Hardcoded Credentials**: Reject any manifests containing hardcoded keys, tokens, or passwords in plain text anywhere in the configuration.

### 2. Least Privilege and Kubernetes API Access
AI agents should not have unwarranted access to the Kubernetes control plane.
- **Disable Auto-mounting Service Account Tokens**: All AI agent pod specs MUST have `automountServiceAccountToken: false`. If the agent explicitly manages Kubernetes resources AND uses a local sidecar for credential injection, then it may explicitly mount a KSA token and ensure that it is only mounted in the sidecar, not the agent container. 
- **Dedicated Service Accounts**: Pods MUST specify a dedicated `serviceAccountName`. They MUST NOT use the `default` service account.
- **Granular RBAC**: If a ServiceAccount is bound to a Role/ClusterRole, verify the permissions are strictly scoped to the agent's exact needs. Prevent wildcards (`*`) in `verbs` or `resources`. 

