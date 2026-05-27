---
name: operator-agent-manager
description: Dynamically provisions and de-provisions GKE-native Cluster Operator Agents, managing their GKE infrastructure and maintaining the active registry in operator_agents.jsonl.
---

# GKE Operator Agent Lifecycle Manager Skill

This skill guides the Platform Agent through the complete lifecycle of GKE-native Cluster Operator Agents (`operator-<cluster>-<location>`). It covers adding new operator agents (creating GKE infrastructure via declarative custom resources), deleting existing agents (de-provisioning GKE infrastructure), and maintaining a persistent database of all active operator agents.

---

## Persistent State Registry (`operator_agents.jsonl`)

All active operator agents currently managed by this platform control plane are recorded in a **JSON Lines (JSONL)** database file stored securely on the persistent volume.

*   **File Path:** `/opt/data/operator_agents.jsonl` (resolves dynamically to `HERMES_HOME/operator_agents.jsonl` in the container).
*   **Entry Schema:**
    ```json
    {"agent_id": "operator-mercury-05-us-central1", "cluster_name": "mercury-05", "location": "us-central1", "project_id": "agentic-harness-demo", "created_at": "2026-05-27T13:19:30Z", "status": "active"}
    ```

To inspect, audit, or list the operator agents you currently manage, use your file-reading tools to read and parse this file.

---

## Core Behavior

### 1. Add a New Operator Agent
When a user requests the provisioning or addition of a new GKE Cluster Operator Agent:
1.  **Gather Parameters:** Ask for `cluster_name` and `location` (region).
2.  **Resolve Project ID:** Check your local `USER.md` or `gcloud` config to find the active GCP project. If it cannot be resolved automatically, ask the user.
3.  **Execute Provisioning Script:** Run the custom Python script:
    ```bash
    ./scripts/operator_agent_provision.py <cluster_name> <location> [project_id]
    ```
    *This script automatically scans GCP for IP conflicts, allocates a non-overlapping `/28` master CIDR range, applies the GKE cluster custom resource manifest, and registers the new agent inside `operator_agents.jsonl`.*
4.  **Monitor Status:** Monitor the GKE cluster creation progress by running:
    ```bash
    kubectl get containercluster <cluster_name> -n agent-system -o json
    ```
    Wait for the condition `type: Ready` to reach `status: "True"` (takes 5-8 minutes). Once ready, inform the user that the operator agent infrastructure is active.

### 2. Delete an Operator Agent
When a user requests the de-provisioning or deletion of an existing GKE Cluster Operator Agent:
1.  **Identify Target:** Gather the `cluster_name` and `location` of the agent to remove.
2.  **Execute De-provisioning Script:** Run the custom Python script:
    ```bash
    ./scripts/operator_agent_deprovision.py <cluster_name> <location>
    ```
    *This script deletes the GKE cluster custom resource manifest from GKE (triggering Google Cloud to safely tear down the cluster in the background) and automatically purges the agent entry from `operator_agents.jsonl`.*
3.  **Notify User:** Confirm to the user that the operator agent has been deleted from the harness and the GKE infrastructure is being safely torn down.

### 3. List Managed Agents
When a user asks for a list or status of active operator agents:
1.  Use your file-reading tools to read `/opt/data/operator_agents.jsonl`.
2.  Parse the JSON lines and present a clean, formatted summary table to the user showing the active `agent_id`, `cluster_name`, `location`, and `created_at` timestamp.
