---
title: Provisioning scripts
description: The modular sub-scripts that make up `./provision.sh` and their teardown counterparts.
sidebar:
  order: 3
---

The provisioner in [`k8s-operator/scripts/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/scripts) is composed of one orchestrator (`provision.sh`) and a set of idempotent step scripts (plus their teardown mirrors and an optional gVisor step). This page catalogs each step; the [quick start](/kube-agents/install/quickstart-gke/) shows the operator's-eye view.

Shared state — cluster name, region, project ID, model provider, GitOps repo — lives in `k8s-operator/scripts/vars.sh` (git-ignored). Each script sources it; missing values prompt the user and get appended to `vars.sh`.

## Orchestrators

- **[`provision.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/provision.sh)** — runs the numbered steps in order (skipping opt-in steps unless enabled).
- **[`teardown.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/teardown.sh)** — runs the steps in reverse.

Both accept `--dry-run` to print planned actions without applying them.

## Provisioning steps

### 01. GKE cluster

`provision_01_gcp_cluster.sh` — Enables the required GCP APIs, provisions a GKE Standard cluster with Workload Identity, sets `kubectl` credentials, and creates the target namespace (`kubeagents-system`).

### 02. gVisor node pool (opt-in)

`provision_02_gvisor_nodepool.sh` — Only runs if `ENABLE_GVISOR=true`. Provisions a dedicated GKE Sandbox (gVisor) node pool (`gvisor-pool` by default, overridable via `GVISOR_POOL_NAME`) for sandboxed skill execution.

### 03. Operator CRDs + controller

`provision_03_gcp_gke_operator.sh` — Installs the `PlatformAgent` CRD and deploys the operator controller manager into the cluster.

### 04. IAM + Workload Identity

`provision_04_gcp_iam.sh` — Creates GSAs for the controller and Platform Agent, binds Kubernetes SAs to them via Workload Identity, and grants the appropriate GKE permissions (`read-only`, `gke-admin`, or `custom`).

### 05. Google Chat Pub/Sub

`provision_05_gcp_gchat.sh` — Creates the Pub/Sub topic and subscription that the Google Chat app publishes events into. Prints the topic name for you to configure in the Chat API console.

### 06. Slack (opt-in)

`provision_06_slack.sh` — Only configures Slack if `SLACK_ENABLED=true`. Collects bot token, app token, allowed users, and home channel, and stores them as Kubernetes secrets.

### 07. LLM API key Secret

`provision_07_gcp_k8s_secrets.sh` — Prompts for the model provider (`gemini` / `anthropic` / `openai`) and API key, and creates the `platform-agent-secrets` Secret in the target namespace.

### 08. PlatformAgent CR

`provision_08_deploy_platform_agent.sh` — Renders `platform-agent.yaml` from a template (via `envsubst`), then `kubectl apply`s the `PlatformAgent` CR to trigger the operator's reconciliation.

### 09. LiteLLM Gateway

`provision_09_deploy_litellm.sh` — Deploys the LiteLLM Deployment + Service. The `PlatformAgent` config references this Service (`litellm`) as its Completions API endpoint.

### 10. Minty (GitHub Token Minter)

`provision_10_deploy_github_minter.sh` — Sets up a GCP KMS keyring + key for token signing, then deploys Minty. See the [Token minter](/kube-agents/deploy/token-minter/) deploy page for details.

### 11. Inference replay (opt-in)

`provision_11_deploy_inference_replay.sh` — Only runs if `INFERENCE_REPLAY_ENABLED=true`. Deploys the [inference-replay proxy](/kube-agents/concepts/inference-gateway/#inference-replay) with a PVC for the cache and re-points the `litellm` Service to route through the proxy.

## Teardown steps

Mirror the provisioning steps in reverse. Full table on [Uninstall](/kube-agents/install/uninstall/).

## Development helpers (`dev/`)

- **[`dev/dev_rebuild_agent.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/dev_rebuild_agent.sh)** — Fast local iteration on the Platform Agent workspace image.
- **[`dev/teardown_dev_01_gcp_artifact_registry.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/teardown_dev_01_gcp_artifact_registry.sh)** — Deletes the dev-only Artifact Registry created by `dev_rebuild_agent.sh`.

## Common gotchas

- **cert-manager missing.** Step 02 will fail if cert-manager isn't installed. Install it once per cluster; the provisioner is idempotent so you can re-run.
- **`vars.sh` collision.** If you rerun the provisioner against a different project without wiping `vars.sh`, you'll target the previous project. Delete `vars.sh` to reset.
- **Autopilot leader election.** cert-manager on Autopilot needs leader election disabled — see [Prerequisites](/kube-agents/install/prerequisites/#gke-autopilot-install).
