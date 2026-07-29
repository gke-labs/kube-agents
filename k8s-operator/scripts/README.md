# Provisioning & Teardown Scripts Reference

This directory contains the automation scripts for provisioning and tearing down the GCP and GKE infrastructure required by the `kube-agents` platform agent and operator.

> **This page is the canonical description of what each script does.** `INSTALL.md`, the operator
> README, and the documentation site all link here rather than restating the steps. If you change a
> script's behaviour, update it here — and nowhere else.

## Architecture & Configuration Flow

All scripts are modular and idempotent. They share a single configuration state stored in a local `vars.sh` file (which is git-ignored).

When any script is run:

1. It checks if `vars.sh` exists.
2. If any required variables are missing, the script prompts the user for them, exports them, and appends them to `vars.sh`.
3. If they are already defined in `vars.sh`, the script sources them and runs non-interactively.

> [!NOTE]
> Because the provisioning scripts persist configuration state in `vars.sh`, running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit `vars.sh` or perform a teardown first.

---

## File Directory

### Orchestration Scripts

- **[provision.sh](provision.sh)**: Master script that coordinates the sequential execution of all core provisioning steps.
- **[teardown.sh](teardown.sh)**: Master script that coordinates the teardown steps in reverse order (conditionally including auxiliary scripts).

### Pipeline steps

Generated from each script's own comment banner.

<!-- BEGIN GENERATED: provisioning-steps -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

### Provisioning steps

| # | Script | What it does |
| :-: | ------ | ------------ |
| 1 | [`provision_01_gcp_cluster.sh`](provision_01_gcp_cluster.sh) | **GCP APIs & GKE Cluster Initialization** — Idempotent setup script that enables the GCP APIs and bootstraps the bare GKE cluster. The target namespace is created later, by the operator deploy in step 03. |
| 2 | [`provision_02_gvisor_nodepool.sh`](provision_02_gvisor_nodepool.sh) | **Optional Dedicated gVisor Node Pool Initialization** — Idempotent script to bootstrap a dedicated GKE Sandbox (gVisor) node pool on an existing GKE Standard cluster. Can be run independently for migration. |
| 3 | [`provision_03_gcp_gke_operator.sh`](provision_03_gcp_gke_operator.sh) | **Deploy Kubernetes Operator (CRDs & Controller Manager)** — Idempotent script that installs the CRDs and deploys the operator to the cluster. |
| 4 | [`provision_04_gcp_iam.sh`](provision_04_gcp_iam.sh) | **Controller & Agent GCP Workload Identity & GCP IAM Permissions** — Idempotent script for granting GKE cluster management and Workload Identity permissions to the Operator Controller Manager and Agent GSAs. |
| 5 | [`provision_05_gcp_gchat.sh`](provision_05_gcp_gchat.sh) | **Google Chat & Pub/Sub Setup** — Configures the Google Chat backend: Pub/Sub routing, the Agent's Service Account, and grants the Service Account permission to read incoming chat messages. Also enables the Workspace Add-ons and Chat APIs and provisions their service identities — without the Chat API identity, Google Chat fails silently. |
| 6 | [`provision_06_slack.sh`](provision_06_slack.sh) | **Slack Integration Setup** — Configures Slack bot tokens, app tokens, and home channel settings. |
| 7 | [`provision_07_gcp_k8s_secrets.sh`](provision_07_gcp_k8s_secrets.sh) | **GKE Kubernetes Secrets Setup** — Idempotent setup script to configure local Kubernetes secrets directly. |
| 8 | [`provision_08_deploy_platform_agent.sh`](provision_08_deploy_platform_agent.sh) | **Deploy PlatformAgent Custom Resource Manifest** — Idempotent script that connects to GKE, renders the platform-agent.yaml template, and deploys it to the cluster. |
| 9 | [`provision_09_deploy_litellm.sh`](provision_09_deploy_litellm.sh) | **Deploy LiteLLM Gateway** — Idempotent script that connects to GKE and deploys the LiteLLM Gateway. |
| 10 | [`provision_10_deploy_github_minter.sh`](provision_10_deploy_github_minter.sh) | **Deploy GitHub Token Minter** — Idempotent script that deploys the GitHub Token Minter. Runs only when GITHUB_ORG, GITHUB_REPO, and GITHUB_APP_ID are all set; skipped otherwise. |
| 11 | [`provision_11_deploy_inference_replay.sh`](provision_11_deploy_inference_replay.sh) | **Deploy Inference Replay Proxy (optional)** — Idempotent script that deploys the Inference Replay proxy in front of the LiteLLM gateway. Skipped unless INFERENCE_REPLAY_ENABLED=true. The proxy intercepts the `litellm` Service so agents need no configuration changes. With REPLAY_MODE=off (default) it is a pure pass-through; flip the `inference-replay-config` ConfigMap to `on` to start recording/replaying. |

### Teardown steps

| # | Script | What it does |
| :-: | ------ | ------------ |
| 1 | [`teardown_01_gcp_cluster.sh`](teardown_01_gcp_cluster.sh) | **Teardown GKE Cluster & Local State** — Idempotent script to clean up the GKE Standard Cluster and local state files. |
| 2 | [`teardown_02_gvisor_nodepool.sh`](teardown_02_gvisor_nodepool.sh) | **Optional Teardown of Dedicated gVisor Node Pool** — Idempotent script to clean up the dedicated GKE Sandbox (gVisor) node pool and RuntimeClass. Can be run independently to test disabling gVisor. |
| 3 | [`teardown_03_gcp_gke_operator.sh`](teardown_03_gcp_gke_operator.sh) | **Teardown Kubernetes Operator (CRDs & Controller Manager)** — Idempotent script to clean up the deployed operator and CRDs. |
| 4 | [`teardown_04_gcp_iam.sh`](teardown_04_gcp_iam.sh) | **Teardown Controller & Agent GCP Workload Identity & GCP IAM** — Idempotent script to remove cluster management and Workload Identity bindings from the Controller manager and all Agent GSAs, and delete the GSAs. |
| 5 | [`teardown_05_gcp_gchat.sh`](teardown_05_gcp_gchat.sh) | **Teardown Google Chat & Pub/Sub Setup** — Idempotent script to clean up GChat Pub/Sub Topic/Subscription and the Bot GSA. |
| 6 | [`teardown_06_slack.sh`](teardown_06_slack.sh) | **Teardown Slack Integration Setup** — Idempotent script to clean up Slack integration state and tokens. |
| 7 | [`teardown_07_gcp_k8s_secrets.sh`](teardown_07_gcp_k8s_secrets.sh) | **Teardown GKE Secrets** — Idempotent script to clean up Kubernetes secrets. |
| 8 | [`teardown_08_deploy_platform_agent.sh`](teardown_08_deploy_platform_agent.sh) | **Teardown PlatformAgent Custom Resource** — Idempotent script to clean up the applied PlatformAgent Custom Resource (CR) and delete the local generated manifest file. |
| 9 | [`teardown_09_deploy_litellm.sh`](teardown_09_deploy_litellm.sh) | **Teardown LiteLLM Gateway** — Idempotent script to undeploy the LiteLLM gateway. |
| 10 | [`teardown_10_deploy_github_minter.sh`](teardown_10_deploy_github_minter.sh) | **Teardown GitHub Token Minter** — Idempotent script to clean up the GitHub Token Minter. |
| 11 | [`teardown_11_deploy_inference_replay.sh`](teardown_11_deploy_inference_replay.sh) | **Teardown Inference Replay Proxy** — Idempotent script to undeploy the Inference Replay proxy and restore the original LiteLLM Service. Safe to run even when the proxy was never deployed. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: provisioning-steps -->

### Auxiliary & Development Scripts

- **[common.sh](common.sh)**: Shared utility functions, color output, logging, prompt helpers, and state management.
- **[platform-agent.yaml.template](platform-agent.yaml.template)**: Manifest template used by `provision_08_deploy_platform_agent.sh` to render the `PlatformAgent` Custom Resource.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)**: Helper script that prints Google Chat integration post-provisioning instructions.
- **[print_instructions_slack.sh](print_instructions_slack.sh)**: Helper script that prints Slack integration post-provisioning instructions.
- **[dev/dev_rebuild_agent.sh](dev/dev_rebuild_agent.sh)**: Fast local development utility that builds, pushes, and redeploys agent container images.

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
