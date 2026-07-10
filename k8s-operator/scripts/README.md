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

- **[provision.sh](provision.sh)**: Master script that coordinates the execution of all core provisioning steps (01 to 09).
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (09 down to 01, conditionally including auxiliary scripts).

### Provisioning Steps

1. **[provision_01_gcp_cluster.sh](provision_01_gcp_cluster.sh)**
   - Sets up initial project configs.
   - Enables GKE/GCP Service APIs (`container.googleapis.com` and `cloudresourcemanager.googleapis.com`).
   - Provisions a GKE Standard Cluster with Workload Identity enabled.
   - Points `kubectl` credentials to the new cluster and creates the target namespace.
     1a. **[provision_01a_gvisor_nodepool.sh](provision_01a_gvisor_nodepool.sh)** (Optional)
   - Provisions a dedicated GKE Sandbox (gVisor) node pool (`gvisor-pool`). Executed automatically if `ENABLE_GVISOR=true`.
2. **[provision_02_gcp_gke_operator.sh](provision_02_gcp_gke_operator.sh)**
   - Installs Custom Resource Definitions (CRDs) for `PlatformAgent`.
   - Installs Custom Resource Definitions (CRDs) for `PlatformAgent`.
   - Deploys the Operator controller manager into the GKE cluster.
3. **[provision_03_gcp_iam.sh](provision_03_gcp_iam.sh)**
   - Pre-provisions GCP Service Accounts (GSAs) for the Controller and Platform Agent.
   - Pre-provisions GCP Service Accounts (GSAs) for the Controller and Platform Agent.
   - Configures Workload Identity policy bindings mapping the Kubernetes SAs to the GCP GSAs.
   - Grants GKE admin permissions to the Controller GSA, and GKE permissions to the Agent GSAs.
   - Annotates the Controller KSA in GKE and restarts the controller manager deployment to apply Workload Identity instantly.
4. **[provision_04_gcp_gchat.sh](provision_04_gcp_gchat.sh)**
   - Sets up the Pub/Sub Topic and Subscription for Google Chat events.
5. **[provision_05_slack.sh](provision_05_slack.sh)**
   - Configures Slack integration parameters, bot tokens, and home channel settings.
6. **[provision_06_gcp_k8s_secrets.sh](provision_06_gcp_k8s_secrets.sh)**
   - Prompts for/reads the `MODEL_PROVIDER` and corresponding `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
   - Creates the Kubernetes Secret (`platform-agent-secrets`) directly in the target GKE namespace.
7. **[provision_07_deploy_platform_agent.sh](provision_07_deploy_platform_agent.sh)**
   - Uses `envsubst` to render `platform-agent.yaml` from its template.
   - Applies the resulting `PlatformAgent` Custom Resource (CR) to deploy the platform agent instance.
8. **[provision_08_deploy_litellm.sh](provision_08_deploy_litellm.sh)**
   - Deploys the LiteLLM Gateway to the GKE cluster.
9. **[provision_09_deploy_github_minter.sh](provision_09_deploy_github_minter.sh)**
   - Sets up Google Cloud KMS keyrings and keys for token signing.
   - Deploys the GitHub Token Minter into the cluster.

### Auxiliary & Development Scripts (`dev/`)

- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: Fast local development utility that builds, pushes, and redeploys agent container images.

### Teardown Steps

- **[teardown_09_deploy_github_minter.sh](teardown_09_deploy_github_minter.sh)**: Cleans up the GitHub Token Minter deployment, GSAs, and KMS resources.
- **[teardown_08_deploy_litellm.sh](teardown_08_deploy_litellm.sh)**: Undeploys the LiteLLM Gateway from the cluster.
- **[teardown_07_deploy_platform_agent.sh](teardown_07_deploy_platform_agent.sh)**: Safely deletes the `PlatformAgent` Custom Resource and cleans up local manifests.
- **[teardown_06_gcp_k8s_secrets.sh](teardown_06_gcp_k8s_secrets.sh)**: Deletes the Kubernetes secrets in GKE.
- **[teardown_05_slack.sh](teardown_05_slack.sh)**: Resets Slack integration configuration state and tokens.
- **[teardown_04_gcp_gchat.sh](teardown_04_gcp_gchat.sh)**: Deletes the Google Chat Pub/Sub topic and subscription.
- **[teardown_03_gcp_iam.sh](teardown_03_gcp_iam.sh)**: Removes all GCP IAM policy bindings, Workload Identity mappings, and deletes the GSAs for the Controller and Agents.
- **[teardown_02_gcp_gke_operator.sh](teardown_02_gcp_gke_operator.sh)**: Removes the Operator manager deployment and unregisters CRDs.
- **[teardown_01a_gvisor_nodepool.sh](teardown_01a_gvisor_nodepool.sh)**: Optional standalone script to delete the dedicated gVisor node pool without destroying the cluster.
- **[dev/teardown_dev_01_gcp_artifact_registry.sh](dev/teardown_dev_01_gcp_artifact_registry.sh)**: Conditionally executed by master teardown if local dev artifact registry was created.
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
