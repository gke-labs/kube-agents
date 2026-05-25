# platform-agent-provisioner - GKE Subagent Provisioning

This skill equips the Platform Agent to dynamically provision and deploy specialized child agents (`devteam` and `operator`) as Kubernetes Pods in GKE at runtime.

## When to Use
- **DevTeam Agent Provisioning**: Triggered when a new namespace or application is registered, and needs a dedicated namespace-scoped agent.
- **Operator Agent Provisioning**: Triggered when a new GKE cluster is added to the management scope.

## Execution Instructions

Follow these steps to generate and apply GKE manifests to deploy a DevTeam Agent:

### Step 1: Gather Parameters
Retrieve the following variables from the user command or workspace metadata:
- `NAMESPACE`: The target namespace (e.g., `payments`).
- `GSA_EMAIL`: The Google Service Account email associated with the team (for GKE Workload Identity binding).
- `GITHUB_TOKEN`: The GitHub Personal Access Token with push access to the target repository.
- `REPO`: The container registry repository path (e.g., `us-central1-docker.pkg.dev/jayantid-gke-dev/kube-agents`).

### Step 2: Read and Parameterize the Manifest Template
1. Read the base manifest template file:
   - Path: `agents/devteam/deployment.yaml` (located in the repository root).
2. Replace all placeholder strings in memory:
   - Replace all instances of `<NAMESPACE>` with the actual namespace.
   - Replace `<GSA_EMAIL>` with the target GSA email.
   - Replace `<GITHUB_TOKEN>` with the GitHub PAT.
   - Replace `<REPO>` with the registry path.
3. Save the resolved manifest content to a temporary file in your workspace:
   - Path: `temp-devteam-deployment-<namespace>.yaml`

### Step 3: Apply Manifests to GKE
Run the following shell commands to apply the resources to the GKE cluster:
```bash
# Create namespace if it does not exist
kubectl create namespace "<NAMESPACE>" --dry-run=client -o yaml | kubectl apply -f -

# Apply the parameterized manifest bundle (SA, Secret, PVC, Deployment)
kubectl apply -f "temp-devteam-deployment-<namespace>.yaml"
```

### Step 4: Verify Rollout Status
Monitor the deployment status to ensure the agent pod starts successfully:
```bash
kubectl rollout status deployment/devteam-agent -n "<NAMESPACE>" --timeout=2m
```
If the rollout fails or times out, fetch the container logs to diagnose startup issues:
```bash
kubectl logs -l app=devteam-agent -n "<NAMESPACE>" --tail=50
```

### Step 5: Clean Up
Remove the temporary manifest file containing the GitHub Token to prevent credential leaks on disk:
```bash
rm "temp-devteam-deployment-<namespace>.yaml"
```

### Step 6: Confirm Provisioning
Reply to the user in chat confirming the successful deployment:
> *"I have successfully provisioned the Dev Team Agent in namespace `<NAMESPACE>` on GKE. The agent is now running and will automatically reconcile the Git repository."*
