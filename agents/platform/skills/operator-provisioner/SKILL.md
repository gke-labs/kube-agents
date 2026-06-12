---
name: operator-provisioner
description: Dynamically provision and deploy specialized Operator Agents as Kubernetes Pods in GKE at runtime.
---

# operator-provisioner - GKE Operator Agent Provisioning

This skill equips the GKE-hosted Platform Agent to dynamically provision and deploy specialized Operator Agents as Kubernetes Pods in GKE at runtime.

## When to Use

- **Operator Agent Provisioning**: Triggered when a new GKE cluster is registered, and needs a dedicated cluster-scoped operator agent.

## Execution Instructions

Follow these steps to generate and apply GKE manifests to deploy an Operator Agent:

### Step 1: Gather Parameters

Retrieve the following variables from the user command or workspace metadata:

- `CLUSTER_NAME`: The target GKE cluster name (e.g., `mercury-01`).
- `CLUSTER_LOCATION`: The GKE cluster region/zone (e.g., `us-central1`).
- `GIT_REPO`: The target platform repository URL (e.g., `git@github.com:jayantid/kube-agents-mock-platform.git`).
- `REPO`: The container registry repository path (e.g., `us-central1-docker.pkg.dev/jayantid-gke-dev/kube-agents`).

### Step 1.5: Validate Parameters

Before proceeding to Step 2, you **must** verify that all required parameters listed above are fully resolved. If any of the variables (`CLUSTER_NAME`, `CLUSTER_LOCATION`, `GIT_REPO`, `REPO`) are empty, missing, or unresolved, you **must stop execution immediately** and output a clear query in the chat asking the user to provide the missing values. You are strictly forbidden from writing or committing any file containing unresolved placeholders (like `<CLUSTER_NAME>`).

### Step 2: Read and Parameterize the Manifest Templates

1. Read the base manifest template files:
   - Deployment: `/opt/data/templates/operator/deployment.yaml` (absolute path in your container workspace).
   - Service: `/opt/data/templates/operator/service.yaml` (absolute path in your container workspace).
2. Compute the unique agent name:
   - `OPERATOR_AGENT_NAME="operator-agent-${CLUSTER_NAME}-${CLUSTER_LOCATION}"`
3. Replace all placeholder strings in memory for both files:
   - Replace `<OPERATOR_AGENT_NAME>` with the computed `OPERATOR_AGENT_NAME`.
   - Replace `<CLUSTER_NAME>` with the target cluster name.
   - Replace `<CLUSTER_LOCATION>` with the cluster region/zone.
   - Replace `<GIT_REPO>` with the target Git repository URL.
   - Replace `<REPO>` with the EXACT registry path provided by the user (do not modify, sanitize, or guess the registry name).
   - Replace `<AGENT_BROWSER_ARGS>` with the value of the `AGENT_BROWSER_ARGS` environment variable if present in the environment, otherwise replace it with an empty string.
4. Concatenate the resolved Deployment and Service manifests (separated by `---`) and save the combined content to a temporary file in your workspace:
   - Path: `temp-operator-deployment-<cluster_name>-<cluster_location>.yaml`

### Step 3: Commit Manifests to Git

Since the GKE cluster is read-only and all mutations must happen via GitOps CI/CD:

1. Navigate to your writeable workspace directory:
   ```bash
   cd /opt/data
   ```
2. Clone the target repository `GIT_REPO` (which you gathered in Step 1) into a folder named `operator-repo`.
   - Note: You must navigate inside the `/opt/data/operator-repo` directory to perform Git operations.
3. Navigate into the cloned repository and create a new branch:
   ```bash
   cd operator-repo
   git checkout -b "feat/provision-operator-<cluster_name>-<cluster_location>"
   ```
4. Copy the parameterized manifest file `temp-operator-deployment-<cluster_name>-<cluster_location>.yaml` into the repository's configuration directory with a unique name:
   ```bash
   mkdir -p k8s
   cp "../temp-operator-deployment-<cluster_name>-<cluster_location>.yaml" "k8s/operator-agent-<cluster_name>-<cluster_location>.yaml"
   ```
5. Add and commit the manifest:
   ```bash
   git add "k8s/operator-agent-<cluster_name>-<cluster_location>.yaml"
   git commit -m "feat(deploy): provision operator agent for cluster <cluster_name> in <cluster_location>"
   ```
6. Push the branch to the remote repository on GitHub:
   ```bash
   git push origin "feat/provision-operator-<cluster_name>-<cluster_location>"
   ```

### Step 4: Create GitHub Pull Request

Use the GitHub CLI (`gh`) to open a Draft Pull Request against the repository:

```bash
gh pr create \
  --title "feat(deploy): provision operator agent for <cluster_name> in <cluster_location>" \
  --body "This Pull Request provisions a new Operator Agent in GKE namespace \`agent-system\` to manage GKE cluster \`<cluster_name>\` in location \`<cluster_location>\` for this platform repository. Upon merge, the CI/CD pipeline will automatically deploy the agent Pod." \
  --draft
```

### Step 5: Clean Up Local Workspace

1. Remove the temporary manifest file to clean up your workspace:
   ```bash
   rm "temp-operator-deployment-<cluster_name>-<cluster_location>.yaml"
   ```
2. Delete the cloned `operator-repo` folder.

### Step 6: Inform User of PR Creation

Reply to the user in chat providing the Pull Request URL and instructions:

> _"I have successfully created a Draft Pull Request to provision the Operator Agent in GKE namespace `agent-system` to manage GKE cluster `<CLUSTER_NAME>` in `<CLUSTER_LOCATION>`. Once the PR is merged, the GKE CI/CD pipeline will automatically deploy the agent._
>
> _**Next Steps**: You can merge the Pull Request directly to trigger automated GitOps deployment._
>
> _PR URL: <PR_URL>"_
