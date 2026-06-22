---
name: dev-team-registrar
description: Register an already existing namespace-scoped GKE DevTeam Agent in the fleet state registry.
---

# dev-team-registrar - GKE Dev Team Agent Registration

This skill equips the GKE-hosted Platform Agent to natively register a pre-existing or manually provisioned DevTeam Agent workspace configuration inside the fleet state registry.

## When to Use

- **DevTeam Agent Registration**: Triggered when a tenant namespace/workspace is already managed by a pre-existing or manually deployed DevTeam Agent, and the Platform Agent needs to register it to enable cross-cluster agent-to-agent (A2A) communication.

## Execution Instructions

Follow these steps to register an existing DevTeam Agent:

### Step 1: Gather Parameters

Retrieve the following variables from the user command or conversation context:

- `CLUSTER_NAME`: The name of the GKE cluster where the team workspace resides (e.g., `mercury-01`).
- `CLUSTER_LOCATION`: The GKE cluster region/zone (e.g., `us-central1`).
- `NAMESPACE`: The isolated tenant namespace assigned to the development team (e.g., `devteam-billing`).
- `PROJECT_ID`: Optional GCP Project ID. If omitted, it resolves automatically from the environment.

### Step 1.5: Validate Parameters

Verify that `CLUSTER_NAME`, `CLUSTER_LOCATION`, and `NAMESPACE` are fully resolved. If any of these parameters are missing, ask the user to provide them in the chat.

### Step 2: Invoke the Registration Tool

Call the `register_devteam` tool with the gathered parameters:

```json
{
  "cluster_name": "<CLUSTER_NAME>",
  "location": "<CLUSTER_LOCATION>",
  "namespace": "<NAMESPACE>",
  "project_id": "<PROJECT_ID>"
}
```

This single tool call will automatically add the DevTeam Agent's stable endpoint to the fleet state registry.

### Step 3: Inform the User

Confirm to the user that the DevTeam Agent has been successfully registered in the fleet state registry and is now active for cross-cluster communication.
