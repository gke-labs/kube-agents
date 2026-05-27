---
name: inter-agent-communication
description: Natively executes direct, synchronous agent API calls to target GKE Cluster Operator Agents and Development Team Agents over GKE NetworkPolicies, utilizing stable K8s Service FQDNs.
---

# Inter-Agent Communication Skill (Generic Synchronous RPC)

This skill enables direct, synchronous network communication between the Platform Agent and target operational agents (`operator` or `devteam`) running inside your GKE fleet.

To enforce strict security boundaries, **agents do not share any persistent volumes.** Network communication with proper GKE Network Policies is the only way to communicate. Because the Platform Agent may run on a **different cluster** than the operational agents, all network communication is routed over stable **Kubernetes Service Fully Qualified Domain Names (FQDNs)** (like GKE Multi-Cluster Services or private DNS names), completely avoiding ephemeral raw IP addresses that are prone to change during pod rescheduling.

---

## The Dynamic FQDN State Registry

To resolve the stable network location of each agent dynamically across namespaces and clusters, you maintain an active registry file under your home directory:

*   **File Path:** `/opt/data/operator_agents.jsonl` (resolves dynamically to `HERMES_HOME/operator_agents.jsonl` in the container).
*   **Entry Schema with Stable FQDN:**
    ```json
    {"agent_id": "operator-mercury-05-us-central1", "cluster_name": "mercury-05", "location": "us-central1", "project_id": "agentic-harness-demo", "created_at": "2026-05-27T13:19:30Z", "status": "active", "endpoint": "operator-mercury-05-us-central1.agent-system.svc.clusterset.local:8642"}
    ```
    *The `endpoint` field stores the stable, cross-cluster GKE Multi-Cluster Services (MCS) FQDN rather than a raw IP address.*

---

## Core Behavior

### 1. Resolve and Call the Target Agent (Operator or DevTeam)
When you need to delegate a task, ask a question, or trigger an action inside a managed GKE cluster or developer namespace:

1.  **Identify the Target Agent:** e.g., `operator-mercury-05-us-central1` or `devteam-mercury-05-us-central1-payments`.
2.  **Execute the Synchronous Call:** Run the generic Python client script using your terminal tool, passing the target agent ID and your query:
    ```bash
    ./scripts/agent_call.py <target_agent_id> "<your_query_here>"
    ```
3.  **Process the Response:** The script will:
    *   Check your state registry to see if a custom FQDN is registered for the agent.
    *   If not found, **automatically fall back** to the standard GKE Multi-Cluster Services (MCS) cross-cluster FQDN:
        `{agent_id}.agent-system.svc.clusterset.local:8642`
    *   Make a direct, secure HTTP POST call to the target agent's OpenAI-compatible completions API on port `8642`.
    *   Block and wait for the target agent to complete its reasoning loop.
    *   Print the final response text directly to `stdout`.
4.  **Read the Output:** Your LLM reads the stdout response immediately, allowing you to continue your turn.
