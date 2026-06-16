# Provisioning & Teardown Scripts Reference

This directory contains the automation scripts for provisioning and tearing down the GCP and GKE infrastructure required by the `kube-agents` platform agent and operator.

## Architecture & Configuration Flow

All scripts are modular and idempotent. They share a single configuration state stored in a local [vars.sh](vars.sh) file (which is git-ignored).

When any script is run:

1. It checks if [vars.sh](vars.sh) exists.
2. If any required variables are missing, the script prompts the user for them, exports them, and appends them to [vars.sh](vars.sh).
3. If they are already defined in [vars.sh](vars.sh), the script sources them and runs non-interactively.

---

## File Directory

### Orchestration Scripts

- **[provision.sh](provision.sh)**: Master script that coordinates the execution of all provisioning steps (01 to 06).
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (06 down to 01).

### Provisioning Steps

1. **[provision_01_gcp_cluster.sh](provision_01_gcp_cluster.sh)**
   - Sets up initial project configs.
   - Enables GKE/GCP Service APIs (`container.googleapis.com` and `cloudresourcemanager.googleapis.com`).
   - Provisions a GKE Standard Cluster with Workload Identity enabled.
   - Points `kubectl` credentials to the new cluster and creates the target namespace.
2. **[provision_02_gcp_gke_operator.sh](provision_02_gcp_gke_operator.sh)**
   - Installs Custom Resource Definitions (CRDs) for `PlatformAgent`, `DevTeamAgent`, and `OperatorAgent`.
   - Deploys the Operator controller manager into the GKE cluster.
3. **[provision_03_gcp_iam.sh](provision_03_gcp_iam.sh)**
   - Pre-provisions GCP Service Accounts (GSAs) for the Controller, Platform Agent, Operator Agent, and DevTeam Agent.
   - Configures Workload Identity policy bindings mapping the Kubernetes SAs to the GCP GSAs.
   - Grants GKE admin permissions to the Controller GSA, and Vertex AI and GKE permissions to the Agent GSAs.
   - Annotates the Controller KSA in GKE and restarts the controller manager deployment to apply Workload Identity instantly.
4. **[provision_04_gcp_k8s_secrets.sh](provision_04_gcp_k8s_secrets.sh)**
   - Prompts for/reads the `GEMINI_API_KEY`, `HERMES_API_KEY`, and `GITHUB_KEY`.
   - Creates the Kubernetes Secret (`platform-agent-secrets`) directly in the target GKE namespace.
5. **[provision_05_gcp_gchat.sh](provision_05_gcp_gchat.sh)**
   - Sets up the Pub/Sub Topic and Subscription for Google Chat events.
6. **[provision_06_deploy_platform_agent.sh](provision_06_deploy_platform_agent.sh)**
   - Uses `envsubst` to render `platform-agent.yaml` from its template.
   - Applies the resulting `PlatformAgent` Custom Resource (CR) to deploy the platform agent instance.

### Teardown Steps

- **[teardown_06_deploy_platform_agent.sh](teardown_06_deploy_platform_agent.sh)**: Safely deletes the `PlatformAgent` Custom Resource and cleans up local manifests.
- **[teardown_05_gcp_gchat.sh](teardown_05_gcp_gchat.sh)**: Deletes the Google Chat Pub/Sub topic and subscription.
- **[teardown_04_gcp_k8s_secrets.sh](teardown_04_gcp_k8s_secrets.sh)**: Deletes the Kubernetes secrets in GKE.
- **[teardown_03_gcp_iam.sh](teardown_03_gcp_iam.sh)**: Removes all GCP IAM policy bindings, Workload Identity mappings, and deletes the GSAs for the Controller and Agents.
- **[teardown_02_gcp_gke_operator.sh](teardown_02_gcp_gke_operator.sh)**: Removes the Operator manager deployment and unregisters CRDs.
- **[teardown_01_gcp_cluster.sh](teardown_01_gcp_cluster.sh)**: Deletes the GKE Standard cluster and removes the local state file `vars.sh`.

---

## Direct Usage Examples

Normally, these scripts are run via the parent Makefile targets. However, they can also be run directly.

### Run Provision Pipeline

Execute the master script from this directory:

```bash
./provision.sh
```

To run a dry-run check (simulates commands without modifying cloud resources):

```bash
./provision.sh --dry-run
```

### Run Teardown Pipeline

Clean up the provisioned environment:

```bash
./teardown.sh
```

### Run Specific Step

For example, if you want to update IAM configurations:

```bash
./provision_03_gcp_iam.sh
```
