# Kube-Agents Frequently Asked Questions (FAQ)

This FAQ addresses common operational, security, and safety questions about deploying and running Kube-Agents on Google Kubernetes Engine (GKE).

---

## Deployment & Software

### Q: What is installed on my GKE cluster?

A: When you deploy Kube-Agents, the following components are installed (mostly isolated within the `kubeagents-system` namespace):

- **Kube-Agents Operator**: A Go-based controller that manages agent resources.
- **Platform Agent Pod**: The active AI runtime container.
- **LiteLLM Gateway**: An open-source model proxy and logging gateway.
- **GitHub Token Minter (Minty)**: A secure service for minting temporary GitHub tokens.
- **Fluent Bit Sidecar**: For forwarding logs to Cloud Logging.
- _Prerequisite_: **cert-manager** is installed (typically in the `cert-manager` namespace) to manage TLS certificates for webhooks.

### Q: Can I run multiple independent agents on the same GKE cluster?

A: **Yes.** Because Kube-Agents is built using the Operator pattern, you can define multiple `PlatformAgent` Custom Resources in different namespaces. Each agent runs in its own pod with namespace-scoped configurations, and can be bound to different Google Service Accounts (GSAs) for workload isolation.

For a detailed list and node sizing recommendations, see the [Architecture & Sizing Guide](architecture_and_sizing.md).

---

## Security & Change Control

### Q: Can Kube-Agents make changes to my cluster or workloads without my approval?

A: **No, not by default.** Kube-Agents enforces strict change control through several layers:

1.  **Read-Only Kubernetes RBAC**: The Kubernetes Service Account bound to the agent pod only has the standard `view` and a custom `explorer` role. It has **no write permissions** (no `create`, `update`, `patch`, or `delete` verbs) for cluster resources. It cannot modify deployments, services, or namespaces directly.
2.  **GitOps Workflow (Secure Write Path)**: If you want the agent to suggest changes (like scaling a deployment or fixing a config drift), it does so by opening a Pull Request (PR) in your Git repository. A human must review and merge this PR before a GitOps controller (like ArgoCD) applies the change to the cluster.
3.  **Strict Read-Only Mode**: You can configure the system's Google Service Account (GSA) with read-only GCP IAM roles (e.g., `container.viewer`). In this configuration, the agent has zero write capabilities on both GKE and GCP.

### Q: Are my Kubernetes Secrets (like database passwords or API keys) exposed to the agent?

A: **No.** The default Kubernetes RBAC assigned to the agent (the `view` ClusterRole and custom `explorer` role) explicitly **excludes access to Secrets**. The agent cannot read, list, or modify Secrets in your cluster. If the agent needs to configure a resource that requires a secret (e.g., a database connection), it will instruct the operator to reference an existing secret name rather than reading the secret content itself.

### Q: Does Kube-Agents send my cluster data to external LLM providers?

A: **Only data directly relevant to your queries.** When you interact with the agent or when it runs a diagnostic task, it retrieves relevant cluster state (like pod statuses, events, or resource configurations) using its read-only tools. This retrieved context is sent to the configured LLM API (e.g., Google AI Studio/Gemini API) to generate a response. Bulk cluster data is never exported.

For details on configuring this, see the [Security & IAM Guide](security_and_iam.md).

---

## Safety Guardrails & Runaway Prevention

### Q: What prevents the agent from spiraling out of control (e.g., infinite loops, massive token usage)?

A: Kube-Agents has several built-in guardrails to prevent "runaway" behavior:

1.  **Human-in-the-Loop (Approval Gates)**: Any action that changes state (e.g., creating a Git commit, triggering a webhook) requires explicit confirmation. The agent will pause and present an approval card to the operator (via Google Chat or Slack) before executing.
2.  **Max Execution Steps**: The agent gateway limits the number of reasoning steps per turn. If the agent gets stuck in a loop, the harness terminates the run after a predefined limit.
3.  **Token Limits**: Conversations have strict context window limits. If the agent loops, it will quickly consume its token budget and halt, preventing runaway API costs.
4.  **Compute Resource Limits**: The agent pod is configured with GKE resource limits (CPU/Memory quotas). It cannot consume more resources than allocated, preventing it from impacting other workloads on the cluster.
5.  **Rate Limiting**: LiteLLM can be configured to enforce rate limits (requests per minute/tokens per minute) on the underlying Gemini API to prevent spike costs.

---

## Operations & Troubleshooting

### Q: How do I debug the agent if it gets stuck or stops responding?

A: You can troubleshoot the agent using standard Kubernetes diagnostics:

1.  **Check Pod Logs**: View the logs of the agent container:
    ```bash
    kubectl logs -n kubeagents-system deployment/platform-agent-gateway -c platform-agent
    ```
2.  **Check LiteLLM Logs**: If you suspect API connection or model routing issues:
    ```bash
    kubectl logs -n kubeagents-system deployment/litellm
    ```
3.  **Restart the Agent**: Force a reload by restarting the deployment:
    ```bash
    kubectl rollout restart deployment/platform-agent-gateway -n kubeagents-system
    ```
