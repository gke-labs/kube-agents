---
name: inter-agent-communication
description: Natively executes direct, secure, and token-authorized synchronous agent API calls to target GKE Cluster Operator Agents and Development Team Agents over GKE NetworkPolicies, utilizing stable K8s Service FQDNs and the secure state registry.
---

# Inter-Agent Communication Skill (Secure Synchronous RPC)

This skill enables direct, secure, and token-authorized network communication between the Platform Agent and target operational agents (`operator` or `devteam`) running inside your GKE fleet.

Network communication with proper GKE Network Policies is the primary way to communicate. Because the Platform Agent may run on a **different cluster** than the operational agents, all network communication is routed over stable **Kubernetes Service Fully Qualified Domain Names (FQDNs)** (like GKE Multi-Cluster Services or private DNS names), completely avoiding ephemeral raw IP addresses.

To prevent unauthorized access across namespaces, every cross-agent HTTP call is fully encrypted (optional HTTPS support) and authorized using short-lived or unique **Bearer Tokens** generated dynamically during provisioning.

---

## The Dynamic FQDN & Token State Registry

To resolve the stable network location and security token of each agent dynamically across namespaces and clusters, you maintain an active registry file under your home directory:

*   **File Path:** `/opt/data/operator_agents.jsonl` (resolves dynamically to `HERMES_HOME/operator_agents.jsonl` in the container).
*   **Entry Schema with FQDN and Token:**
    ```json
    {"agent_id": "operator-mercury-05-us-central1", "cluster_name": "mercury-05", "location": "us-central1", "project_id": "agentic-harness-demo", "created_at": "2026-05-27T13:19:30Z", "status": "active", "endpoint": "operator-mercury-05-us-central1.agent-system.svc.clusterset.local:8642", "api_key": "4cf239...a11"}
    ```
    *The `endpoint` field stores the stable K8s Service FQDN. The `api_key` field stores the secure, unique random bearer token generated specifically for the target agent, which is automatically passed in the `Authorization` headers.*

---

## Core Behavior

### 1. Resolve and Call the Target Agent (Operator or DevTeam)
When you need to delegate a task, ask a question, or trigger an action inside a managed GKE cluster or developer namespace:

1.  **Identify the Target Agent:** e.g., `operator-mercury-05-us-central1` or `devteam-mercury-05-us-central1-payments`.
2.  **Execute the Secure Call:** Run the generic Python client script using your terminal tool, passing the target agent ID and your query:
    ```bash
    ./scripts/agent_call.py <target_agent_id> "<your_query_here>"
    ```
3.  **Process the Response:** The script will:
    *   Check your state registry to resolve the target's stable `endpoint` FQDN and its unique `api_key` bearer token.
    *   If the endpoint is not in the registry, it **automatically falls back** to the standard GKE Multi-Cluster Services (MCS) FQDN:
        `{agent_id}.agent-system.svc.clusterset.local:8642`
    *   Execute a secure HTTP POST call to the target agent's completions API on port `8642`, dynamically injecting the `"Authorization": "Bearer <api_key>"` header.
    *   Block and wait for the target agent to complete its reasoning loop.
    *   Print the final response text directly to `stdout`.
4.  **Read the Output:** Your LLM reads the stdout response immediately, maintaining perfect turn continuity.
