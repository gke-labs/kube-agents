---
name: dev-team-provisioner
description: Dynamically provision and deploy specialized Dev Team Agents as Kubernetes Pods in GKE at runtime.
---

# dev-team-provisioner - GKE Dev Team Agent Provisioning

This skill equips the GKE-hosted Platform Agent to dynamically provision and deploy specialized Dev Team Agents using Custom Resources managed by the GKE Operator.

## When to Use

- **DevTeam Agent Provisioning**: Triggered when a new namespace or application is registered, and needs a dedicated namespace-scoped agent.

## Execution Instructions

Follow these steps to generate and apply GKE Custom Resource manifests to deploy a DevTeam Agent:

### Step 1: Gather Parameters

Retrieve the following variables from the user command or workspace metadata:

- `NAMESPACE`: The target namespace (e.g., `payments`).
- `CLUSTER_NAME`: The target GKE cluster name (e.g., `mercury-01`).
- `CLUSTER_LOCATION`: The GKE cluster region/zone (e.g., `us-central1`).
- `GIT_REPO`: The target application repository URL (e.g., `git@github.com:jayantid/kube-agents-mock-payments.git`).
- `REPO`: The container registry repository path (e.g., `us-central1-docker.pkg.dev/jayantid-gke-dev/kube-agents`).
- `PROJECT_ID`: The GCP Project ID (e.g., `jayantid-gke-dev`).

### Step 1.5: Validate Parameters

Before proceeding to Step 2, you **must** verify that all required parameters listed above are fully resolved. If any of the variables (`NAMESPACE`, `CLUSTER_NAME`, `CLUSTER_LOCATION`, `GIT_REPO`, `REPO`, `PROJECT_ID`) are empty, missing, or unresolved, you **must stop execution immediately** and output a clear query in the chat asking the user to provide the missing values. You are strictly forbidden from writing or committing any file containing unresolved placeholders (like `<CLUSTER_NAME>`).

### Step 2: Read and Parameterize the Custom Resource Template

1. Read the custom resource template file:
   - Path: `/opt/defaults/templates/devteam/devteamagent.yaml` (absolute path in your container workspace).
2. Replace all placeholder strings in memory:
   - Replace all instances of `${TARGET_NAMESPACE}` with the actual target namespace.
   - Replace `${TARGET_CLUSTER_NAME}` with the target cluster name.
   - Replace `${TARGET_CLUSTER_LOCATION}` with the cluster region/zone.
   - Replace `${GIT_REPO}` with the target Git repository URL.
   - Replace `${DEVTEAM_AGENT_IMAGE}` with the exact container image path constructed as `${REPO}/devteam-agent`.
   - Replace `${PROJECT_ID}` with the GCP Project ID.
3. Save the resolved manifest content to a temporary file in your workspace:
   - Path: `temp-devteam-agent-<namespace>.yaml`

### Step 3: Apply the Custom Resource Manifest Directly

Apply the parameterized Custom Resource manifest directly to the GKE cluster:

1. Apply the manifest file:
   ```bash
   kubectl apply -f "temp-devteam-agent-${TARGET_NAMESPACE}.yaml"
   ```

### Step 4: Clean Up Local Workspace

1. Remove the temporary manifest file to clean up your workspace:
   ```bash
   rm "temp-devteam-agent-${TARGET_NAMESPACE}.yaml"
   ```

### Step 5: Inform User of Provisioning

Reply to the user in chat confirming that the agent has been provisioned:

> _"I have successfully provisioned the DevTeamAgent custom resource for namespace `<NAMESPACE>` under the hood. The GKE Operator is deploying the agent Pod, and it will be ready shortly to receive delegated tasks."_
