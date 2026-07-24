---
title: Provisioning scripts
description: The modular sub-scripts that make up `./provision.sh` and their teardown counterparts.
sidebar:
  order: 3
---

The provisioner in [`k8s-operator/scripts/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/scripts) is composed of one orchestrator (`provision.sh`) and a set of idempotent step scripts (plus their teardown mirrors and an optional gVisor step). This page catalogs each step; the [quick start](/kube-agents/install/quickstart-gke/) shows the operator's-eye view.

Shared state — cluster name, region, project ID, model provider, GitOps repo — lives in `k8s-operator/scripts/vars.sh` (git-ignored). Each script sources `common.sh`, which loads that state and provides the shared helpers (prompting, retries, step runner); missing values prompt the user and get appended to `vars.sh`.

## Orchestrators

- **[`provision.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/provision.sh)** — runs the numbered steps in order (skipping opt-in steps unless enabled).
- **[`teardown.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/teardown.sh)** — runs the steps in reverse.

Both accept `--dry-run` to print planned actions without applying them.

## Provisioning steps

### 01. GKE cluster

`provision_01_gcp_cluster.sh` — Enables the required GCP APIs (`container.googleapis.com`), provisions a GKE Standard cluster with Workload Identity, and fetches `kubectl` credentials. The `kubeagents-system` namespace is created later by the operator deploy in step 03.

### 02. gVisor node pool (opt-in)

`provision_02_gvisor_nodepool.sh` — Only runs if `ENABLE_GVISOR=true`. Provisions a dedicated GKE Sandbox (gVisor) node pool (`gvisor-pool` by default, overridable via `GVISOR_POOL_NAME`) for sandboxed skill execution.

### 03. Operator CRDs + controller

`provision_03_gcp_gke_operator.sh` — Ensures cert-manager is present (auto-installing it, with leader election disabled on Autopilot, if the `certificates.cert-manager.io` CRD is missing), then installs the `PlatformAgent` CRD (`make install`) and deploys the operator controller manager (`make deploy`) into the cluster.

### 04. IAM + Workload Identity

`provision_04_gcp_iam.sh` — Creates the Platform Agent GSA (and, when GitHub integration is configured, the GitHub Token Minter GSA), binds their Kubernetes SAs via Workload Identity, and grants the Platform Agent the roles matching its `PLATFORM_AGENT_PERMISSION_SET` (`read-only`, `gke-admin`, or `custom`).

### 05. Google Chat Pub/Sub (opt-in)

`provision_05_gcp_gchat.sh` — Only runs if `GOOGLE_CHAT_ENABLED=true`. Enables the Chat/Pub/Sub APIs, provisions the Workspace Add-ons service identity, creates the Pub/Sub topic and subscription that the Google Chat app publishes events into, and grants the Platform Agent GSA subscriber access. Prints setup instructions for the Chat API console.

### 06. Slack (opt-in)

`provision_06_slack.sh` — Only configures Slack if `SLACK_ENABLED=true`. Collects bot token(s), app token, allowed users, and home channel, and saves them to `vars.sh`. The tokens are synced into the `platform-agent-secrets` Secret later, by step 07.

### 07. LLM API key Secret

`provision_07_gcp_k8s_secrets.sh` — Prompts for the model provider (`gemini` / `anthropic` / `chatgpt` / `openai`) and its API key, generates a random `API_SERVER_KEY`, and creates the `platform-agent-secrets` Secret (also carrying the Slack tokens from step 06) in the target namespace.

### 08. PlatformAgent CR

`provision_08_deploy_platform_agent.sh` — Renders `platform-agent.yaml` from a template (via `envsubst`), then `kubectl apply`s the `PlatformAgent` CR to trigger the operator's reconciliation.

### 09. LiteLLM Gateway

`provision_09_deploy_litellm.sh` — Deploys the LiteLLM Deployment + Service. The `PlatformAgent` config references this Service (`litellm`) as its Completions API endpoint.

### 10. Minty (GitHub Token Minter)

`provision_10_deploy_github_minter.sh` — Only runs when GitHub integration is configured (`GITHUB_ORG`, `GITHUB_REPO`, and `GITHUB_APP_ID` set). Enables the Cloud KMS API, sets up a KMS keyring + key for token signing (importing the GitHub App PEM via the Minty CLI when a path is provided), then deploys Minty (`make deploy-github`). See the [Token minter](/kube-agents/deploy/token-minter/) deploy page for details.

### 11. Inference replay (opt-in)

`provision_11_deploy_inference_replay.sh` — Only runs if `INFERENCE_REPLAY_ENABLED=true`. Deploys the [inference-replay proxy](/kube-agents/concepts/inference-gateway/#inference-replay) with a PVC for the cache and re-points the `litellm` Service to route through the proxy.

## Teardown steps

Mirror the provisioning steps in reverse. Full table on [Uninstall](/kube-agents/install/uninstall/).

## Utilities

- **[`update_cluster_name.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/update_cluster_name.sh)** — Patches the target GKE cluster name into the deployed `platform-agent` `PlatformAgent` spec (`spec.harness.clusterName`), triggering the operator to reconcile.

## Development helpers (`dev/`)

- **[`dev/dev_rebuild_agent.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/dev_rebuild_agent.sh)** — Fast local iteration on the Platform Agent workspace image.
- **[`dev/setup-gcp-github-wif.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/setup-gcp-github-wif.sh)** — Sets up GCP Workload Identity Federation (pool + OIDC provider + service account) so GitHub Actions can deploy to the project keylessly. Requires `PROJECT_ID`, `SA_NAME`, and `GITHUB_REPO` env vars.
- **[`dev/teardown_dev_01_gcp_artifact_registry.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/teardown_dev_01_gcp_artifact_registry.sh)** — Deletes the dev-only Artifact Registry created by `dev_rebuild_agent.sh`.

## Common gotchas

- **cert-manager.** Step 03 auto-installs cert-manager (v1.14.4) if the `certificates.cert-manager.io` CRD isn't present, so you normally don't need to install it yourself. All steps are idempotent, so you can safely re-run.
- **`vars.sh` collision.** If you rerun the provisioner against a different project without wiping `vars.sh`, you'll target the previous project. Delete `vars.sh` to reset.
- **Autopilot leader election.** On GKE Autopilot, step 03 installs cert-manager with leader election disabled automatically (kube-system restrictions) — see [Prerequisites](/kube-agents/install/prerequisites/#gke-autopilot-install).
