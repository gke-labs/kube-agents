---
title: Prerequisites
description: What you need in place before running the kube-agents installer.
---

The shipping install path targets GKE. You'll need one working GCP project plus the standard command-line tools, and cert-manager installed on the cluster so the operator's admission webhooks come up cleanly.

## Local tooling

- **Google Cloud SDK** (`gcloud`) — **576.0.0 or newer**, [install](https://cloud.google.com/sdk/docs/install), authenticated: `gcloud auth login && gcloud auth application-default login`. The installer sets Managed OpenTelemetry with `--managed-otel-scope`, which reached GA in gcloud 576.0.0 (2026-07-14); on an older SDK the flag exists only on the alpha and beta tracks. The installer checks this before it touches any cloud resource — `gcloud components update` if it complains.
- **Terraform** — the install engine is the [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) composition; `./install.sh` pre-flights `terraform` and offers to install it from HashiCorp's tap (Homebrew) or apt repository. It is equally the _teardown_ engine, and `./uninstall.sh` installs nothing on your behalf — with an install to tear down and no terraform, it refuses with exit 1, so a machine that has only ever run the installer's auto-install cannot tear the install down. (Against a target with no Terraform state there is nothing to destroy, so it exits 3 without needing terraform at all.)
- **`kubectl`** — [install](https://kubernetes.io/docs/tasks/tools/). The installer points it at the GKE cluster it creates.
- **Docker or Podman** — required by the operator dev workflow (`make docker-build`) if you rebuild images locally. Not required for a stock install.
- **Bash** — the installer scripts are bash (including the `/bin/bash` macOS ships).
- **`jq`, `gh`, `helm`, `git`** — the rest of the CLI set `./install.sh` pre-flights up front and offers to install when missing.
- **`envsubst`** — only for the development Kustomize path (`make -C k8s-operator deploy-*`); usually shipped with `gettext`.

## GCP project

- A GCP project you can enable APIs on and where you can create GKE clusters, Pub/Sub topics, KMS keyrings, and IAM service accounts.
- Billing enabled on that project.
- The `Editor` or `Owner` role for the user running `./install.sh` (or a scoped set covering the resources above).

The installer will enable APIs and create all resources itself; you don't need to pre-provision the cluster.

**No extra firewall rule is needed on private clusters.** The operator's webhook server listens on
`10250`, one of the two ports GKE's automatic control-plane-to-node rule already permits — see
[Admission webhooks](/kube-agents/operator/#admission-webhooks). A cluster that hardens `10250`
beyond the GKE default (scoping it to node IPs, say) still needs a rule for the webhook, or a move to
a port it does allow — which is a Kustomize patch across the `--webhook-port` flag, the manager
`containerPort`, and the Service `targetPort` together, not a single flag. Changing one of the three
leaves the API server dialing a port nothing is listening on; see
[Serving on a different port](/kube-agents/operator/#serving-on-a-different-port).

## cert-manager on the target cluster

The operator's admission webhooks need TLS certificates managed by [cert-manager](https://cert-manager.io) (v1.13.0+).

**You usually do not need to install this yourself.** The Terraform composition `terraform/examples/full-install` installs cert-manager as its own `helm_release`, pinned in its `cert_manager_version` variable, including the leader-election relocation Autopilot needs. On an existing cluster that already runs cert-manager, set `enable_cert_manager = false` — the composition does not detect an existing install and the apply fails on the existing CRDs. `./install.sh` probes for a `cert-manager` Deployment on the existing-cluster path and writes that variable for you. (An existing cert-manager installed under a different namespace or release name is not detected.)

Install it by hand only if you are:

- deploying into an existing cluster without the installer or Terraform ([Manual install](/kube-agents/install/manual/)), or
- pinning a specific cert-manager version.

The Helm chart on its own is the one path that never installs cert-manager: a chart that shipped a `Certificate` into a cluster without the CRDs would fail at apply time for everyone. It therefore leaves the operator's admission webhooks off (`operator.webhooks.enabled=false`) until you install cert-manager and turn them on. See the [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md).

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
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.1/cert-manager.yaml
```

On Autopilot you'll additionally need to patch the deployments to append `--leader-elect=false`. Because argument indices vary by cert-manager version, verify the arg list before patching — a positional JSON patch (`/args/1`) will silently corrupt an unexpected version.

## Chat platform

- **Google Chat** (default): a GCP project with the Chat API enabled and a Chat app configured to publish events to Pub/Sub. The composition's [`chat-pubsub` module](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/chat-pubsub) creates the topic and subscription (`enable_google_chat = true`, or the installer's `--enable-google-chat`); you configure the Chat app itself in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com).
- **Slack** (opt-in): a Slack workspace where you can install a bot app and generate bot + app tokens. Follow the [Hermes Slack setup guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). Slack is configured only if you enable it in the installer's chat menu (or set `enable_slack = true` in `terraform.tfvars`).

## LLM credentials

Pick one at least:

- `GEMINI_API_KEY` (recommended default; get one at [aistudio.google.com](https://aistudio.google.com)).
- `ANTHROPIC_API_KEY`.
- `OPENAI_API_KEY`.

Or route one of these keys through a self-hosted LiteLLM gateway — see [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) for a Gemini API-key template.

## GitOps repo (for `submit-suggestion`)

The declarative workflow needs a GitHub repo to file PRs against.

- A GitHub repo **owned by an organization**. Minty looks the installation up under `/orgs/{org}/`, so a repo owned by a personal account cannot be used — see [token minter](/kube-agents/deploy/token-minter/). A free organization is enough.
- A GitHub App with `contents:write`, `pull_requests:write`, and `issues:write` permissions, installed on that repo. The App itself may be owned by the organization or by your personal account.
- The App's private key wrapped in a GCP KMS key — the [`github-minter` Terraform module](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter) creates the keyring and an import-only signing key, and `./install.sh` imports the downloaded `.pem` into it via the Minty CLI (a one-shot step, so the key material never enters Terraform state).

See [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) for the full Minty setup.

## Ready to install

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./install.sh` end-to-end.
- [Manual install](/kube-agents/install/manual/) — step-by-step, no wrapper script.
