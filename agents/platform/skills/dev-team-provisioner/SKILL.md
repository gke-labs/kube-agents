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

Before proceeding to Step 2, you **must** verify that all required parameters listed above are fully resolved. If any of the variables (`NAMESPACE`, `CLUSTER_NAME`, `CLUSTER_LOCATION`, `GIT_REPO`, `REPO`, `PROJECT_ID`) are empty, missing, or unresolved, you **must stop execution immediately** and output a clear query in the chat asking the user to provide the missing values. You are strictly forbidden from writing or committing any file containing unresolved placeholders (like `${TARGET_CLUSTER_NAME}`).

### Step 2: Read and Parameterize the Custom Resource Template

1. Read the custom resource template file:
   - Path: `/opt/data/templates/devteam/devteamagent.yaml` (absolute path in your container workspace).
2. Replace all placeholder strings in memory:
   - Replace all instances of `${TARGET_NAMESPACE}` with the actual target namespace.
   - Replace `${TARGET_CLUSTER_NAME}` with the target cluster name.
   - Replace `${TARGET_CLUSTER_LOCATION}` with the cluster region/zone.
   - Replace `${GIT_REPO}` with the target Git repository URL.
   - Replace `${DEVTEAM_AGENT_IMAGE}` with the exact container image path constructed as `${REPO}/devteam-agent`.
   - Replace `${PROJECT_ID}` with the GCP Project ID.
3. Save the resolved manifest content to a temporary file in your workspace:
   - Path: `temp-devteam-agent-<namespace>.yaml`

### Step 3: Commit Manifests to Git

Since the GKE cluster is read-only and all mutations must happen via GitOps CI/CD:

1. Navigate to your writeable workspace directory:
   ```bash
   cd /opt/data
   ```
2. Clone the target application repository `GIT_REPO` (which you gathered in Step 1) into a folder named `app-repo`.
   - Note: You must navigate inside the `/opt/data/app-repo` directory to perform Git operations.
3. Navigate into the cloned repository and create a new branch:
   ```bash
   cd app-repo
   git checkout -b "feat/provision-devteam-<namespace>"
   ```
4. Copy the parameterized Custom Resource manifest file `temp-devteam-agent-<namespace>.yaml` into the repository's configuration directory:
   ```bash
   mkdir -p k8s
   cp "../temp-devteam-agent-<namespace>.yaml" "k8s/devteam-agent.yaml"
   ```
5. Add and commit the manifest:
   ```bash
   git add "k8s/devteam-agent.yaml"
   git commit -m "feat(deploy): provision devteam agent for namespace <namespace>"
   ```
6. Push the branch to the remote repository on GitHub:
   ```bash
   git push origin "feat/provision-devteam-<namespace>"
   ```

### Step 4: Create GitHub Pull Request

Use the GitHub CLI (`gh`) to open a Draft Pull Request against the application repository:

```bash
gh pr create \
  --title "feat(deploy): provision devteam agent for <namespace>" \
  --body "This Pull Request registers a new DevTeamAgent Custom Resource in GKE namespace \`kubeagents-system\` to manage GKE namespace \`<namespace>\` for this application repository. Upon merge, the GKE Operator will automatically deploy the agent Pod and configure its workspace." \
  --draft
```

### Step 5: Clean Up Local Workspace

1. Remove the temporary manifest file to clean up your workspace:
   ```bash
   rm "temp-devteam-agent-<namespace>.yaml"
   ```
2. Delete the cloned `app-repo` folder.

### Step 6: Inform User of PR Creation

Reply to the user in chat providing the Pull Request URL and instructions:

> _"I have successfully created a Draft Pull Request to provision the DevTeamAgent custom resource. Once the PR is merged, the GKE Operator will automatically spin up the Dev Team Agent Pod to manage GKE namespace `<NAMESPACE>`._
>
> _**Next Steps**: You can merge the Pull Request directly. On first startup, the Dev Team Agent will automatically prompt you inside the chat session to securely paste your GitHub token to authenticate its git reconciliation loop._
>
> _PR URL: <PR_URL>"_
