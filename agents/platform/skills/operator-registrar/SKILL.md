---
name: operator-registrar
description: Register an already existing cluster-scoped GKE Operator Agent in the fleet state registry.
---

# operator-registrar - GKE Operator Agent Registration

This skill equips the GKE-hosted Platform Agent to natively register an already existing or manually provisioned GKE Operator Agent inside the fleet state registry.

## When to Use

- **Operator Agent Registration**: Triggered when a GKE cluster is already managed by a pre-existing or manually deployed Operator Agent, and the Platform Agent needs to register it in the fleet state to enable cross-cluster agent-to-agent (A2A) communication.

## Execution Instructions

Follow these steps to register an existing Operator Agent:

### Step 1: Gather Parameters

Retrieve the following variables from the user command or conversation context:

- `CLUSTER_NAME`: The name of the GKE cluster managed by the operator (e.g., `mercury-01`).
- `CLUSTER_LOCATION`: The GKE cluster region/zone (e.g., `us-central1`).
- `PROJECT_ID`: Optional GCP Project ID. If omitted, it resolves automatically from the environment.

### Step 1.5: Validate Parameters

Verify that `CLUSTER_NAME` and `CLUSTER_LOCATION` are fully resolved. If any of these parameters are missing, ask the user to provide them in the chat.

### Step 2: Invoke the Registration Tool

Call the `register_operator` tool with the gathered parameters:

```json
{
  "cluster_name": "<CLUSTER_NAME>",
  "location": "<CLUSTER_LOCATION>",
  "project_id": "<PROJECT_ID>"
}
```

This single tool call will automatically add the Operator Agent's stable endpoint to the fleet state registry.

### Step 3: Inform the User

Confirm to the user that the Operator Agent has been successfully registered in the fleet state registry and is now active for cross-cluster communication.
