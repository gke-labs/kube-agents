---
name: platform-agent-provisioner
description: Dynamically provision and configure specialized child agents (devteam and operator) at runtime.
---

# platform-agent-provisioner - Dynamic Subagent Provisioning

This skill equips the Platform Agent to dynamically provision and configure specialized child agents (`devteam` and `operator`) at runtime.

## When to Use

- **Operator Agent Provisioning**: Triggered when a new GKE cluster is added to the setup.
- **DevTeam Agent Provisioning**: Triggered when a new application is deployed to a namespace.

## Execution Instructions

Follow these steps to recursively copy, register, and update configuration rules:

### Step 1: Determine Target Workspace and Unique ID

1. Determine the scope details and form the unique agent ID (`<agent-name>`):
   - **For Cluster Operator**: `operator-<cluster_name>-<location>`
     _(Example: `operator-payment-prod-us-central1`)_
   - **For DevTeam**: `devteam-<cluster_name>-<location>-<namespace>`
     _(Example: `devteam-payment-prod-us-central1-payment-staging`)_
2. Locate source templates:
   - For operator: `templates/operator`
   - For devteam: `templates/devteam`
3. Target workspace directory:
   - `../<agent-name>` (relative to your platform agent workspace directory).

### Step 2: Copy Template Workspace Recursively

Copy the entire pre-packaged template directory to create a new standalone child agent workspace:

```bash
mkdir -p "../<agent-name>"
cp -a "templates/<agent-type>/." "../<agent-name>/"
```

_(Replace `<agent-type>` with `devteam` or `operator`)_

### Step 2b: Inject Scope Configuration File

Write the active target GKE scope details to a new `USER.md` file at the root of the dynamically provisioned child agent workspace:

- **For operator**:

  ```bash
  cat << 'EOF' > "../<agent-name>/USER.md"
  # GKE Scope Configuration
  - **Cluster Name:** <cluster_name>
  - **Cluster Location:** <location>
  EOF
  ```

- **For devteam**:
  ```bash
  cat << 'EOF' > "../<agent-name>/USER.md"
  # GKE Scope Configuration
  - **Cluster Name:** <cluster_name>
  - **Cluster Location:** <location>
  - **Namespace:** <namespace>
  EOF
  ```

_(Replace `<cluster_name>`, `<location>`, and `<namespace>` with your active target GKE scope parameters)_

### Step 3: Register Subagent in Agent Harness

Instruct the underlying AI agent harness to register the new subagent `<agent-name>` using the workspace located at `../<agent-name>`. Ensure the harness loads the identity from `IDENTITY.md` and core instructions from `SOUL.md` within that workspace.

### Step 4: Update Semantic Routing and Delegation

Instruct the AI agent harness to update its routing configuration to allow the Platform Agent to delegate tasks to the newly created `<agent-name>` subagent.

### Step 5: Configure Heartbeat Schedule

Instruct the AI agent harness to configure a recurring schedule (cron) for the new subagent:

- **Schedule**: Every 1 minute (`1m` or `* * * * *`)
- **Action**: Send a message to the subagent to execute its heartbeat routine.
- **Message Content**:
  ```text
  [Scheduled Heartbeat]
  Read HEARTBEAT.md and execute due checks.
  Update memory/heartbeat-state.json with fresh timestamps/results.
  If healthy and no anomalies, respond exactly NO_REPLY; otherwise return concise blockers.
  ```

### Step 6: Confirm Provisioning

Inform the user that the subagent has been successfully provisioned and registered in the harness.

> _"I have successfully dynamically provisioned <agent-name> inside your workspace and registered it with the agent harness. It is now active and ready for delegation."_
