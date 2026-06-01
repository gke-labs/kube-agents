---
name: dev-team-provisioner
description: Dynamically provision and deploy specialized Dev Team Agents as Kubernetes Pods in GKE at runtime.
---

# dev-team-provisioner - GKE Dev Team Agent Provisioning

This skill equips the GKE-hosted Platform Agent to dynamically provision and deploy specialized Dev Team Agents as Kubernetes Pods in GKE at runtime.

## When to Use

- **DevTeam Agent Provisioning**: Triggered when a new namespace or application is registered, and needs a dedicated namespace-scoped agent.

## Execution Instructions

Follow these steps to generate and apply GKE manifests to deploy a DevTeam Agent:

### Step 1: Gather Parameters

Retrieve the following variables from the user command or workspace metadata:

- `NAMESPACE`: The target namespace (e.g., `payments`).
- `CLUSTER_NAME`: The target GKE cluster name (e.g., `mercury-01`).
- `CLUSTER_LOCATION`: The GKE cluster region/zone (e.g., `us-central1`).
- `GIT_REPO`: The target application repository URL (e.g., `git@github.com:jayantid/kube-agents-mock-payments.git`).
- `GITHUB_TOKEN`: The GitHub Personal Access Token with push access to the target repository.
- `REPO`: The container registry repository path (e.g., `us-central1-docker.pkg.dev/jayantid-gke-dev/kube-agents`).

### Step 1.5: Validate Parameters

Before proceeding to Step 2, you **must** verify that all required parameters listed above are fully resolved. If any of the variables (`NAMESPACE`, `CLUSTER_NAME`, `CLUSTER_LOCATION`, `GIT_REPO`, `GITHUB_TOKEN`, `REPO`) are empty, missing, or unresolved, you **must stop execution immediately** and output a clear query in the chat asking the user to provide the missing values. You are strictly forbidden from writing or committing any file containing unresolved placeholders (like `<CLUSTER_NAME>`).

### Step 1.6: Generate API Server Key

Auto-generate a secure Bearer token for inter-agent authentication. Do **not** ask the user for this value.

```bash
API_SERVER_KEY=$(openssl rand -hex 32)
```

### Step 2: Read and Parameterize the Manifest Template

1. Read the base manifest template file:
   - Path: `/opt/data/templates/devteam/deployment.yaml` (absolute path in your container workspace).
2. Replace all placeholder strings in memory:
   - Replace all instances of `<NAMESPACE>` with the actual namespace.
   - Replace `<CLUSTER_NAME>` with the target cluster name.
   - Replace `<CLUSTER_LOCATION>` with the cluster region/zone.
   - Replace `<GIT_REPO>` with the target Git repository URL.
   - Replace `<REPO>` with the EXACT registry path provided by the user (do not modify, sanitize, or guess the registry name).
   - Replace `<API_SERVER_KEY>` with the generated Bearer token.
3. Save the resolved manifest content to a temporary file in your workspace:
   - Path: `temp-devteam-deployment-<namespace>.yaml`

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
4. Copy the parameterized manifest file `temp-devteam-deployment-<namespace>.yaml` into the repository's configuration directory:
   ```bash
   mkdir -p k8s
   cp "../temp-devteam-deployment-<namespace>.yaml" "k8s/devteam-agent.yaml"
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
  --body "This Pull Request provisions a new Dev Team Agent in GKE namespace \`agent-system\` to manage GKE namespace \`<namespace>\` for this application repository. Upon merge, the CI/CD pipeline will automatically deploy the agent Pod." \
  --draft
```

### Step 5: Clean Up Local Workspace

1. Remove the temporary manifest file to clean up your workspace:
   ```bash
   rm "temp-devteam-deployment-<namespace>.yaml"
   ```
2. Delete the cloned `app-repo` folder.

### Step 6: Inform User of PR Creation

Reply to the user in chat providing the Pull Request URL and instructions:

> _"I have successfully created a Draft Pull Request to provision the Dev Team Agent in GKE namespace `agent-system` to manage GKE namespace `<NAMESPACE>`. Once the PR is merged, the GKE CI/CD pipeline will automatically deploy the agent._
>
> _**Next Steps**: You can merge the Pull Request directly. The deployment manifest uses a `<GITHUB_TOKEN>` placeholder to secure your credentials. On first startup, the Dev Team Agent will automatically detect the placeholder and prompt you inside the chat session to securely paste your GitHub token._
>
> _PR URL: <PR_URL>"_
