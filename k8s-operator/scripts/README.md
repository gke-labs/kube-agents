# Provisioning & Teardown Scripts Reference

This directory contains the automation scripts for provisioning and tearing down the GCP and GKE infrastructure required by the `kube-agents` platform agent and operator.

## Architecture & Configuration Flow

All scripts are modular and idempotent. They share a single configuration state stored in a local [vars.sh](vars.sh) file (which is git-ignored).

When any script is run:

1. It checks if [vars.sh](vars.sh) exists.
2. If any required variables are missing, the script prompts the user for them, exports them, and appends them to [vars.sh](vars.sh).
3. If they are already defined in [vars.sh](vars.sh), the script sources them and runs non-interactively.

> [!NOTE]
> Because the provisioning scripts persist configuration state in [vars.sh](vars.sh), running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit [vars.sh](vars.sh) or perform a teardown first.

---

## File Directory

### Orchestration Scripts

- **[provision.sh](provision.sh)**: Master script that coordinates the sequential execution of all core provisioning steps.
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (conditionally including auxiliary scripts).

#### Provisioning Steps

1. **[provision_01_gcp_cluster.sh](provision_01_gcp_cluster.sh)**
   - Sets up initial project configs.
   - Enables GKE Service API (`container.googleapis.com`).
   - Provisions a GKE Standard Cluster with Workload Identity enabled.
   - Points `kubectl` credentials to the new cluster and creates the target namespace.
2. **[provision_02_gvisor_nodepool.sh](provision_02_gvisor_nodepool.sh)**
   - Provisions a dedicated GKE Sandbox (gVisor) node pool (defaults to `gvisor-pool`, configurable via `GVISOR_POOL_NAME`). Executed automatically if `ENABLE_GVISOR=true`.
3. **[provision_03_gcp_gke_operator.sh](provision_03_gcp_gke_operator.sh)**
   - Installs `cert-manager` (`v1.14.4`) if not present (including leader-election compatibility patching for GKE Autopilot clusters).
   - Installs Custom Resource Definitions (CRDs) for `PlatformAgent`.
   - Deploys the Operator controller manager into the GKE cluster.
4. **[provision_04_gcp_iam.sh](provision_04_gcp_iam.sh)**
   - Enables GCP Service APIs (`container.googleapis.com` and `cloudresourcemanager.googleapis.com`).
   - Pre-provisions GCP Service Accounts (GSAs) for the Platform Agent and conditionally for the GitHub Token Minter.
   - Configures Workload Identity policy bindings mapping the Kubernetes SAs to the GCP GSAs.
   - Grants GKE cluster management and monitoring permissions to the Platform Agent GSA based on the selected permission set (`read-only`, `gke-admin`, or `custom`, default: `gke-admin`).
   - Configures Workload Identity policy bindings and annotations for the GitHub Token Minter GSA/KSA if GitHub integration is configured.
5. **[provision_05_gcp_gchat.sh](provision_05_gcp_gchat.sh)**
   - Enables GCP Service APIs (`pubsub.googleapis.com` and `chat.googleapis.com`).
   - Sets up the Pub/Sub Topic and Subscription for Google Chat events (skipped if `GOOGLE_CHAT_ENABLED=false`).
   - Configures IAM policy bindings allowing the Platform Agent GSA to read incoming messages from the Pub/Sub subscription.
   - Note: Access can be restricted to specific users by configuring `GOOGLE_CHAT_ALLOWED_USERS`.
6. **[provision_06_slack.sh](provision_06_slack.sh)**
   - Configures Slack integration parameters, bot tokens, app tokens, and home channel settings (skipped if `SLACK_ENABLED=false`).
   - **Note:** You must create a Slack App and obtain tokens before running this. [See the Slack App Setup Guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack).
   - Note: Access can be restricted to specific users by configuring `SLACK_ALLOWED_USERS`.
7. **[provision_07_gcp_k8s_secrets.sh](provision_07_gcp_k8s_secrets.sh)**
   - Prompts for/reads the `MODEL_PROVIDER` and corresponding `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.
   - Generates a secure random `API_SERVER_KEY` if not already set.
   - Creates the Kubernetes Secret (`platform-agent-secrets`) containing model API keys, the server key, and Slack tokens directly in the target GKE namespace.
   - Creates the Kubernetes Secret (`github-app-credentials`) if `GITHUB_APP_ID` is configured.
8. **[provision_08_deploy_platform_agent.sh](provision_08_deploy_platform_agent.sh)**
   - Uses `envsubst` to render `platform-agent.yaml` from its template.
   - Automatically enables the `gvisor` runtime class in the rendered manifest if `ENABLE_GVISOR=true`.
   - Applies the resulting `PlatformAgent` Custom Resource (CR) to deploy the platform agent instance.
9. **[provision_09_deploy_litellm.sh](provision_09_deploy_litellm.sh)**
   - Deploys the LiteLLM Gateway to the GKE cluster.
10. **[provision_10_deploy_github_minter.sh](provision_10_deploy_github_minter.sh)**
    - Enables Cloud KMS API (`cloudkms.googleapis.com`).
    - Sets up Google Cloud KMS keyrings and keys for token signing and grants signer/verifier roles to the Minter GSA.
    - Imports GitHub App private keys (`GITHUB_PEM_PATH`) into Cloud KMS when configured.
    - Deploys the GitHub Token Minter into the cluster.
11. **[provision_11_deploy_inference_replay.sh](provision_11_deploy_inference_replay.sh)**
    - Opt-in via `INFERENCE_REPLAY_ENABLED=true`; otherwise skipped.
    - Prompts for `REPLAY_IMAGE` (the proxy container image).
    - Deploys the Inference Replay proxy: PVC + ConfigMap (mode=off pass-through), Deployment, a `litellm-gateway` Service pointing at the original LiteLLM pods, and a replacement `litellm` Service routing traffic through the proxy. Toggle caching on at runtime via `kubectl patch configmap inference-replay-config -n <ns> --type merge -p '{"data":{"mode":"on"}}'`.

### Auxiliary & Development Scripts

- **[common.sh](common.sh)**: Shared utility functions, color output, logging, prompt helpers, and state management.
- **[platform-agent.yaml.template](platform-agent.yaml.template)**: Manifest template used by `provision_08_deploy_platform_agent.sh` to render the `PlatformAgent` Custom Resource.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)**: Helper script that prints Google Chat integration post-provisioning instructions.
- **[print_instructions_slack.sh](print_instructions_slack.sh)**: Helper script that prints Slack integration post-provisioning instructions.
- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: Fast local development utility that builds, pushes, and redeploys agent container images.

### Teardown Steps

- **[teardown_11_deploy_inference_replay.sh](teardown_11_deploy_inference_replay.sh)**: Always executed by master teardown; undeploys the proxy (including the cache PVC) if present and re-applies the LiteLLM Service manifest to restore the original selector. Idempotent no-op if the proxy was never deployed.
- **[teardown_10_deploy_github_minter.sh](teardown_10_deploy_github_minter.sh)**: Cleans up the GitHub Token Minter deployment and disables/schedules Cloud KMS key versions for destruction.
- **[teardown_09_deploy_litellm.sh](teardown_09_deploy_litellm.sh)**: Undeploys the LiteLLM Gateway from the cluster.
- **[teardown_08_deploy_platform_agent.sh](teardown_08_deploy_platform_agent.sh)**: Safely deletes the `PlatformAgent` Custom Resource and cleans up local manifests.
- **[teardown_07_gcp_k8s_secrets.sh](teardown_07_gcp_k8s_secrets.sh)**: Deletes the Kubernetes secrets in GKE.
- **[teardown_06_slack.sh](teardown_06_slack.sh)**: Resets Slack integration configuration state and tokens.
- **[teardown_05_gcp_gchat.sh](teardown_05_gcp_gchat.sh)**: Deletes the Google Chat Pub/Sub topic and subscription.
- **[teardown_04_gcp_iam.sh](teardown_04_gcp_iam.sh)**: Removes all GCP IAM policy bindings, Workload Identity mappings, and deletes the GSAs for the Platform Agent and GitHub Token Minter.
- **[teardown_03_gcp_gke_operator.sh](teardown_03_gcp_gke_operator.sh)**: Removes the Operator manager deployment and unregisters CRDs.
- **[teardown_02_gvisor_nodepool.sh](teardown_02_gvisor_nodepool.sh)**: Deletes the dedicated gVisor node pool without destroying the cluster.
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
./provision_04_gcp_iam.sh
```
