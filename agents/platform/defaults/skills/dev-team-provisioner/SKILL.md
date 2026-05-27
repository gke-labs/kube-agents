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

### Step 2: Read and Parameterize the Manifest Template

1. Read the base manifest template file:
   - Path: `templates/devteam/deployment.yaml` (located relative to your workspace directory).
2. Replace all placeholder strings in memory:
   - Replace all instances of `<NAMESPACE>` with the actual namespace.
   - Replace `<CLUSTER_NAME>` with the target cluster name.
   - Replace `<CLUSTER_LOCATION>` with the cluster region/zone.
   - Replace `<GIT_REPO>` with the target Git repository URL.
   - Replace `<GITHUB_TOKEN>` with the GitHub PAT.
   - Replace `<REPO>` with the registry path.
3. Save the resolved manifest content to a temporary file in your workspace:
   - Path: `temp-devteam-deployment-<namespace>.yaml`

### Step 3: Commit Manifests to Git

Since the GKE cluster is read-only and all mutations must happen via GitOps CI/CD:

1. Clone the repository that contains the GKE deployment manifests (e.g. `git@github.com:jayantid/kube-agents.git` or using the HTTPS authenticated clone URL) into a folder named `infrastructure-repo`.
   - Note: You must navigate inside the `infrastructure-repo` directory to perform Git operations.
2. Create and switch to a new branch:
   ```bash
   git checkout -b "feat/provision-devteam-<namespace>"
   ```
3. Create the directory for the target namespace:
   ```bash
   mkdir -p "namespaces/<namespace>"
   ```
4. Copy the parameterized manifest file `temp-devteam-deployment-<namespace>.yaml` into the repository:
   ```bash
   cp "../temp-devteam-deployment-<namespace>.yaml" "namespaces/<namespace>/devteam-agent.yaml"
   ```
5. Add and commit the manifest:
   ```bash
   git add "namespaces/<namespace>/devteam-agent.yaml"
   git commit -m "feat(deploy): provision devteam agent for namespace <namespace>"
   ```
6. Push the branch to your fork on GitHub:
   ```bash
   git push origin "feat/provision-devteam-<namespace>"
   ```

### Step 4: Create GitHub Pull Request

Use the GitHub CLI (`gh`) to open a Draft Pull Request against the upstream repository:

```bash
gh pr create \
  --title "feat(deploy): provision devteam agent for <namespace>" \
  --body "This Pull Request provisions a new Dev Team Agent in GKE namespace \`<namespace>\` as part of the GitOps deployment flow. Upon merge, the CI/CD pipeline will automatically deploy the resources." \
  --draft
```

### Step 5: Clean Up Local Workspace

1. Remove the temporary manifest file to clean up your workspace:
   ```bash
   rm "temp-devteam-deployment-<namespace>.yaml"
   ```
2. Delete the cloned `infrastructure-repo` folder.

### Step 6: Inform User of PR Creation

Reply to the user in chat providing the Pull Request URL and instructions:

> _"I have successfully created a Draft Pull Request to provision the Dev Team Agent in GKE namespace `<NAMESPACE>`. Once the PR is merged, the GKE CI/CD pipeline will automatically deploy the agent._
>
> _PR URL: <PR_URL>"_
