---
title: Prerequisites
description: What you need in place before running the kube-agents provisioner.
---

The shipping install path targets GKE. You'll need one working GCP project plus the standard command-line tools, and cert-manager installed on the cluster so the operator's admission webhooks come up cleanly.

## Local tooling

- **Google Cloud SDK** (`gcloud`) — [install](https://cloud.google.com/sdk/docs/install), authenticated: `gcloud auth login && gcloud auth application-default login`.
- **`kubectl`** — [install](https://kubernetes.io/docs/tasks/tools/). The provisioner points it at the GKE cluster it creates.
- **Docker or Podman** — required by the operator dev workflow (`make docker-build`) if you rebuild images locally. Not required for a stock install.
- **Bash 4+** — the provisioning scripts are bash.
- **`envsubst`** — usually shipped with `gettext`.

## GCP project

- A GCP project you can enable APIs on and where you can create GKE clusters, Pub/Sub topics, KMS keyrings, and IAM service accounts.
- Billing enabled on that project.
- The `Editor` or `Owner` role for the user running `./provision.sh` (or a scoped set covering the resources above).

The provisioner will enable APIs and create all resources itself; you don't need to pre-provision the cluster.

## cert-manager on the target cluster

The operator's admission webhooks need TLS certificates managed by [cert-manager](https://cert-manager.io) (v1.13.0+). Install it once per cluster.

### Standard install (recommended)

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true
```

### GKE Autopilot install

Autopilot blocks leader-election Leases in `kube-system`. Disable leader election during install:

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true \
  --set controller.leaderElection.enabled=false \
  --set cainjector.leaderElection.enabled=false
```

### Manifest fallback

If Helm isn't available:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml
```

On Autopilot you'll additionally need to patch the deployments to append `--leader-elect=false`. Because argument indices vary by cert-manager version, verify the arg list before patching — a positional JSON patch (`/args/1`) will silently corrupt an unexpected version.

## Chat platform

- **Google Chat** (default): a GCP project with the Chat API enabled and a Chat app configured to publish events to Pub/Sub. `provision_05_gcp_gchat.sh` creates the topic and subscription; you configure the Chat app itself in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com).
- **Slack** (opt-in): a Slack workspace where you can install a bot app and generate bot + app tokens. Follow the [Hermes Slack setup guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). Slack is configured only if `SLACK_ENABLED=true` when you run the provisioner.

## LLM credentials

Pick one at least:

- `GEMINI_API_KEY` (recommended default; get one at [aistudio.google.com](https://aistudio.google.com)).
- `ANTHROPIC_API_KEY`.
- `OPENAI_API_KEY`.

Or route one of these keys through a self-hosted LiteLLM gateway — see [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) for a Gemini API-key template.

## GitOps repo (for `submit-suggestion`)

The declarative workflow needs a GitHub repo to file PRs against.

- A GitHub repo you own or can install a GitHub App on.
- A GitHub App with `contents:write` and `pull_requests:write` permissions, installed on that repo.
- The App's private key wrapped in a GCP KMS key — `provision_10_deploy_github_minter.sh` sets up the keyring and key, and you upload the private key material to it.

See [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) for the full Minty setup.

## Ready to install

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` end-to-end.
- [Manual install](/kube-agents/install/manual/) — step-by-step, no wrapper script.
