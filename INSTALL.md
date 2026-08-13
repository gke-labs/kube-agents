# Kubernetes Agentic Harness Installation & Setup Guide

This comprehensive, step-by-step guide explains how to install, configure, deploy, and verify the **Kubernetes Agentic Harness (`kube-agents`)** across different environments—from automated Google Cloud Platform (GCP) / GKE deployments to local development clusters and third-party multi-agent orchestrators.

> **What this file is.** A self-contained, executable procedure — runnable from a fresh clone with no
> network access to the documentation site, by a human or an AI agent. It deliberately carries the
> commands and nothing else.
>
> For the explanatory material — why each component exists, architecture, troubleshooting in depth,
> and the concept guides — see **<https://gke-labs.github.io/kube-agents/>**. For what each
> provisioning script does, see
> [`k8s-operator/scripts/README.md`](k8s-operator/scripts/README.md).

---

## Table of Contents

1. [Architecture & Overview](#architecture--overview)
2. [Prerequisites & Tooling Matrix](#prerequisites--tooling-matrix)
3. [Method 0: Zero-Friction One-Liner Installation (Fastest)](#method-0-zero-friction-one-liner-installation-fastest)
4. [Method 1: Automated GCP & GKE Provisioning (Recommended)](#method-1-automated-gcp--gke-provisioning-recommended)
   - [Modular Pipeline Stages](#modular-pipeline-stages)
   - [Step-by-Step Execution](#step-by-step-execution)
5. [Method 2: Manual Kubernetes Cluster Deployment](#method-2-manual-kubernetes-cluster-deployment)
   - [Step 1: Install cert-manager](#step-1-install-cert-manager)
   - [Step 2: Create API Key & Access Secrets](#step-2-create-api-key--access-secrets)
   - [Step 3: Build & Push the Operator Image](#step-3-build--push-the-operator-image)
   - [Step 4: Deploy the Operator & CRDs](#step-4-deploy-the-operator--crds)
   - [Step 5: Deploy Integrations (LiteLLM & GitHub)](#step-5-deploy-integrations-litellm--github)
   - [Step 6: Apply Custom Resources](#step-6-apply-custom-resources)
6. [Method 3: Local Development & Fast Iteration](#method-3-local-development--fast-iteration)
7. [Method 4: Declarative IaC Install (Terraform + Helm)](#method-4-declarative-iac-install-terraform--helm)
8. [Teardown & Cleanup](#teardown--cleanup)
9. [Troubleshooting & Common FAQ](#troubleshooting--common-faq)

---

## Method 0: Zero-Friction One-Liner Installation (Fastest)

Run the interactive one-liner installer directly in **Google Cloud Shell** or any authenticated bash terminal:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/install.sh | bash
```

When prompted for the image/source revision, enter a SemVer release tag or the full 40-character
commit SHA behind a validated RC tag. The installer rejects mutable refs such as `latest` and `main`
so the provisioning scripts and container images stay on the same revision. On the one-liner above
there is no local checkout yet, so a value is required; running `./install.sh` from a kube-agents
clone instead offers that clone's `HEAD` as the default — which only works if a container image was
published for that commit (CI builds one per `main` commit and per release tag).

_(Alternatively via GitHub raw URL: `curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash`)_

### What `install.sh` Automatically Handles:

- **`gcloud` Authentication**: Checks login state and launches auth flows if needed.
- **GCP Project & Region Selection**: Auto-detects the active project and prompts for confirmation; you can type a project ID that the discovered list does not show.
- **Provisioning Sources**: Puts the provisioning scripts on disk (this checkout, or a clone at the requested revision) and verifies they match the image ref _before_ the interview starts.
- **GKE Cluster Setup**: Provisions the supported GKE Standard topology or connects to an existing cluster.
- **Chat Integrations**: Configures Google Chat and/or Slack when selected.
- **AI Model Credentials**: Prompts for Gemini, OpenAI, or Anthropic credentials, or selects Vertex AI (no key — Workload Identity).
- **Long-Term Memory**: Asks whether the agents should remember anything between conversations, and if so which store (`--memory=file|hindsight|off`, default `file`). The default is **on**, and it is the store this repository shipped before the searchable one existed, so an upgrade that says nothing about memory keeps what it already has: per-user Markdown inside the pod (`multiuser_memory`), no extra services, suited to **small or personal** deployments — but the whole store is loaded into the model's context every turn, so it stops scaling past a few pages. Pick `hindsight` for **enterprise** deployments — ranked recall that stays affordable as the store grows, at the cost of an API server and a Postgres database in the cluster; it selects the `kube_agents_memory` provider. Pick `off` to retain nothing and run no database. The measurements behind that split, and how to change it later, are in [`docs/designs/memory.md`](docs/designs/memory.md).
- **Automated Pipeline Execution**: Writes `k8s-operator/scripts/vars.sh` and launches `make gcp-provision`.

The installer performs no GCP operation of its own — it configures and then delegates to the
pipeline in [`k8s-operator/scripts/`](k8s-operator/scripts/README.md), which is the canonical
description of what gets created. It sources `k8s-operator/scripts/common.sh`, so its defaults
(region, cluster name, model provider, registry prefix) and its accepted values are the ones the
pipeline uses; see [Shared defaults live in `common.sh`](k8s-operator/scripts/README.md#shared-defaults-live-in-commonsh).

Two behaviours worth knowing before the first run:

- **The image/source ref defaults to the checkout's `HEAD`** and must be a SemVer release tag or a
  full 40-character commit SHA. Provisioning refuses to start from a dirty or mismatched checkout so
  the scripts and the container image stay on one revision; pass `--allow-unverified-source` to
  override that while iterating on the installer itself.
- **The agent's GCP IAM permission set defaults to `read-only`**, matching the provisioner. It
  controls cloud-plane writes only — Kubernetes RBAC is read-only in every set, and the GitOps
  pull-request path works in every set. See the site's
  [security and IAM reference](docs/site/src/content/docs/reference/security-and-iam.md).

### Non-Interactive & AI Agent Execution Mode

AI Agent harnesses and automated CI scripts can execute `install.sh` without interactive prompts:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/install.sh | bash -s -- \
  --non-interactive \
  --project-id="my-gcp-project" \
  --cluster-name="platform-agent-host" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

To run pre-flight checks and output configuration state (`vars.sh` and `/tmp/kube-agents-install-report.json`) without creating cloud resources:

```bash
./install.sh --dry-run --non-interactive \
  --project-id="my-gcp-project" \
  --image-tag="<SEMVER_TAG_OR_FULL_COMMIT_SHA>"
```

---

## Architecture & Overview

The Kubernetes Agentic Harness manages Kubernetes operations via an autonomous **Platform Agent (`platform`)** acting as the master custodian and architect.

- **Agent Configuration (`agents/platform`)**: Contains the system prompt and persona identity (`SOUL.md`), workspace instructions (`AGENTS.md`), runtime configuration (`config.yaml`), operational playbooks (`governance/`) that the scheduled governance jobs point at, their schedules (`cron/jobs.json`), and reusable skills (`skills/`).
- **Kubernetes Operator (`k8s-operator`)**: A Kubebuilder-powered Go operator that manages Custom Resource Definitions (`PlatformAgent`) and reconciles cluster lifecycle state.
- **Integrations**: Supports LiteLLM Gateway for LLM provider routing (Gemini, Vertex AI, OpenAI, Anthropic) and enterprise messaging bridges (Google Chat, Slack).

---

## Prerequisites & Tooling Matrix

Before beginning installation, ensure your environment meets the following requirements:

| CLI Tool / Utility              | Required Version                                | Verification Command       | Description                                                                                           |
| :------------------------------ | :---------------------------------------------- | :------------------------- | :---------------------------------------------------------------------------------------------------- |
| **Go**                          | `1.25+`                                         | `go version`               | Required for building operator binaries and running tests.                                            |
| **Docker / Podman**             | `20.10+`                                        | `docker --version`         | Required to build container images for the operator.                                                  |
| **kubectl**                     | `1.28+`                                         | `kubectl version --client` | Communicates with your target Kubernetes or GKE cluster.                                              |
| **Kubernetes Cluster**          | `1.28+` (`1.35+` for `AgentPlugin` OCI volumes) | `kubectl version`          | Target Kubernetes or GKE cluster (`AgentPlugin` OCI volumes require K8s 1.35+ `ImageVolume` gate).    |
| **Google Cloud SDK (`gcloud`)** | `576.0.0+`                                      | `gcloud version`           | GKE cluster access, IAM, and Artifact Registry. `576.0.0` is where `--managed-otel-scope` reached GA. |
| **Helm**                        | `3.10+`                                         | `helm version`             | Used for installing cluster dependencies like `cert-manager`.                                         |
| **gettext (`envsubst`)**        | Standard                                        | `envsubst --version`       | Used by Makefile deployment targets for template substitution.                                        |

---

## Method 1: Automated GCP & GKE Provisioning (Recommended)

For full end-to-end setups on Google Cloud Platform (GCP) with GKE Standard, Workload Identity, Pub/Sub, LiteLLM, GitHub Token Minter, and Inference Replay Proxy, use the automated provisioning pipeline in `k8s-operator/`.

### Modular Pipeline Stages

The automated installer executes a sequence of numbered, idempotent stages, from GKE cluster
creation through to the optional inference-replay proxy. Each stage has its own `make` target and can be
re-run on its own.

- **What each stage does:** [`k8s-operator/scripts/README.md`](k8s-operator/scripts/README.md)
- **The current target list:** `cd k8s-operator && make help`

Stage 03 installs `cert-manager` automatically if it is not already present, so you do **not** need
to install it yourself on this path. (You do for [Method 2](#method-2-manual-kubernetes-cluster-deployment).)

### Step-by-Step Execution

#### Step 1: Authenticate with Google Cloud

Authenticate your `gcloud` CLI and set Application Default Credentials:

```bash
gcloud auth login
gcloud auth application-default login
```

#### Step 2: Execute Provisioning

Navigate to the `k8s-operator` directory and launch the provisioning pipeline:

```bash
cd k8s-operator
make gcp-provision
```

- On the first run, the script prompts for configuration inputs (GCP Project ID, region, cluster name, model provider, API key, etc.) and saves them locally in `scripts/vars.sh`.
- Subsequent invocations reuse `scripts/vars.sh` for non-interactive idempotency.

- **Private Container Registry**: If your GKE clusters cannot pull from `ghcr.io`, mirror the `kube-agents` container images into your private registry (e.g. Artifact Registry `us-docker.pkg.dev/my-project/kube-agents`) and set `REGISTRY_PREFIX="us-docker.pkg.dev/my-project/kube-agents"` in `scripts/vars.sh` or pass `--registry-prefix="us-docker.pkg.dev/my-project/kube-agents"` to `install.sh`. See the [Docker images guide](docs/site/src/content/docs/deploy/docker-images.md) for the full image list.

> [!NOTE]
> Because the provisioning scripts persist configuration state in `scripts/vars.sh`, running the script again will reuse the same options selected on the first run. If you want to change configuration variables, manually edit `scripts/vars.sh` or perform a teardown first.

- **Dry-run check**: To preview actions without modifying cloud infrastructure:
  ```bash
  make gcp-provision ARGS="--dry-run"
  ```

#### Security & CMEK Encryption

The automated installer includes local state hardening and Cloud KMS (CMEK) etcd database encryption:

- **Local State Security**: Configuration state saved in `k8s-operator/scripts/vars.sh` is protected with strict file permissions (`umask 077`, `chmod 600`).
- **GKE Database Encryption (CMEK)**: GKE etcd database encryption is automatically configured using Cloud KMS (`GKE_DB_KMS_KEYRING` / `GKE_DB_KMS_KEY`).
- **`ALLOW_UNENCRYPTED_SECRETS`**: Set `ALLOW_UNENCRYPTED_SECRETS=true` before provisioning if deploying to existing unencrypted clusters or testing environments without CMEK:
  ```bash
  export ALLOW_UNENCRYPTED_SECRETS=true
  make gcp-provision
  ```
- **`PERSIST_SECRETS_ON_DISK`**: By default (`PERSIST_SECRETS_ON_DISK=true`), credentials (API keys, Slack tokens) are saved to `vars.sh`. Set `PERSIST_SECRETS_ON_DISK=false` to prevent writing sensitive credentials to disk.

> [!TIP]
> Each stage can also be run on its own (e.g. `make gcp-provision-01-cluster`). Run
> `cd k8s-operator && make help` for the complete, always-current list of provisioning and teardown
> targets.

#### Step 3: Verify Running Components

Verify that the operator, LiteLLM gateway, and custom resources are healthy:

```bash
kubectl get deployments -n kubeagents-system
kubectl get pods -n kubeagents-system
kubectl get platformagents --all-namespaces
```

#### Step 4: ChatGPT OAuth Authentication (If Applicable)

If you chose `chatgpt` as your `MODEL_PROVIDER`, follow the printed OAuth Device Flow instructions or check the LiteLLM gateway logs:

```bash
kubectl logs -n kubeagents-system deployment/litellm -f
```

#### Step 5: Enable Google Chat & Slack Integrations (Manual Required Steps)

If you enabled Google Chat (`GOOGLE_CHAT_ENABLED=true`) or Slack (`SLACK_ENABLED=true`) during provisioning, perform the following required manual steps after `make gcp-provision` completes:

##### 1. Google Chat Configuration (`GOOGLE_CHAT_ENABLED=true`)

1. **Configure the Google Chat API endpoint in GCP Console**:
   - Open the Google Chat API configuration page: `https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=<PROJECT_ID>`
   - Set the **App name** to `GKE Platform Agent Bot`.
   - Optionally set an **Avatar URL** pointing at an image you host.
   - Under **Connection settings**, select **Cloud Pub/Sub** and enter the Cloud Pub/Sub topic created during provisioning:
     ```text
     projects/<PROJECT_ID>/topics/<CHAT_TOPIC_NAME>
     ```
   - Under **Visibility**, select **Specific people and groups in your domain** and enter your email address (`ALLOWED_USERS`).
2. **Send a Test Direct Message**:
   - Send a DM to the bot in Google Chat with the message `"Hi Platform Agent"`.
3. **Approve Pairing Code (Optional / First-time setup)**:
   - If pairing mode is enabled, approve the pairing code displayed in the gateway logs:
     ```bash
     kubectl exec -it deploy/platform-agent-gateway -n kubeagents-system -- hermes pairing approve google_chat <PAIRING_CODE>
     ```
   - Re-display these instructions at any time from the `k8s-operator` directory:
     ```bash
     ./scripts/print_instructions_gchat.sh
     ```

##### 2. Slack Configuration (`SLACK_ENABLED=true`)

1. **Verify Slack App Settings**:
   - Ensure **Socket Mode** is enabled in your Slack App console.
   - Verify that your Bot Token (`SLACK_BOT_TOKEN`) has the required scopes: `app_mentions:read`, `channels:history`, `chat:write`, `channels:read`, `groups:read`, `im:read`, `mpim:read`, `files:write`.
   - `files:write` is the one that is easy to miss, because omitting it looks like nothing is wrong. A card whose answer is text is delivered normally; a card that produces a **file** has its upload rejected with `missing_scope`, which the artifact delivery path catches and logs as a warning. The user is told the task completed and never sees the artifact. Add the scope and reinstall the app.
2. **Test Bot Connection**:
   - Invite the bot to a channel or send a direct message: `"Hi Platform Agent"`.
3. **Approve Pairing Code (Optional / First-time setup)**:
   - If pairing mode is enabled, approve the pairing code displayed in the gateway logs:
     ```bash
     kubectl exec -it deploy/platform-agent-gateway -n kubeagents-system -- hermes pairing approve slack <PAIRING_CODE>
     ```
4. **Register the Native Slash Commands (Optional)**:
   - Slack routes a leading-slash message to the app's slash handler only if that slash is registered on the app. Generate the manifest:
     ```bash
     kubectl exec deploy/platform-agent-gateway -n kubeagents-system -- hermes slack manifest
     ```
   - Paste the JSON into the Slack App Console (**Features → App Manifest → Edit**), save, and reinstall when Slack prompts. That manifest replaces the whole app definition — to keep an app you have already configured, add `--slashes-only` and merge the printed array into the existing `features.slash_commands`.
   - This adds Slack's autocomplete, not the behaviour: a typed `/hermes <subcommand>` works either way, because the Chat Agent's `legacy_slash_commands` plugin unwraps it before the gateway resolves the command.
5. **Set the Home Channel (if you left `SLACK_HOME_CHANNEL` empty)**:
   - Scheduled audits have nowhere to post until one is set. From the Slack channel you want, run `/sethome` (or `/hermes sethome`). It takes effect immediately and persists across restarts.

- Re-display these instructions at any time from the `k8s-operator` directory:
  ```bash
  ./scripts/print_instructions_slack.sh
  ```

---

## Method 2: Manual Kubernetes Cluster Deployment

If you are installing into an existing Kubernetes or GKE cluster without using the automated GCP provisioning pipeline, follow these steps.

### Step 1: Install cert-manager

The Kubernetes Operator requires `cert-manager` (version `1.13.0+`) to generate and rotate admission webhook TLS certificates.

> Only needed on this manual path. [Method 1](#method-1-automated-gcp--gke-provisioning-recommended) installs `cert-manager` for you in stage 03.

- **Standard Kubernetes / GKE Standard Cluster (via Helm)**:

  ```bash
  helm repo add jetstack https://charts.jetstack.io
  helm repo update
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --set installCRDs=true
  ```

- **GKE Autopilot Cluster (Leader Election Workaround)**:
  GKE Autopilot restricts coordination Leases in `kube-system`. Disable leader election during install:
  ```bash
  helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --set installCRDs=true \
    --set controller.leaderElection.enabled=false \
    --set cainjector.leaderElection.enabled=false
  ```

### Step 2: Create API Key & Access Secrets

Create the `kubeagents-system` namespace and add your model provider credentials:

```bash
kubectl create namespace kubeagents-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic platform-agent-secrets \
  --namespace kubeagents-system \
  --from-literal=GEMINI_API_KEY="your-gemini-api-key" \
  --from-literal=API_SERVER_KEY="your-api-server-key" \
  --from-literal=ANTHROPIC_API_KEY="your-anthropic-api-key" \
  --from-literal=OPENAI_API_KEY="your-openai-api-key" \
  --from-literal=SESSION_KV_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=SESSION_KV_SALT="$(openssl rand -hex 32)"
```

The last two are generated, not chosen: `SESSION_KV_API_KEY` is the bearer token
for the pod-local Session KV server, and `SESSION_KV_SALT` is the HMAC salt that
pseudonymises chat identities before they are written to disk. Keep the salt:
rotating it re-anonymises every user, severing their past sessions from their
future ones.

Both are optional in the sense that the pod still starts without them, but
`SESSION_KV_API_KEY` is not optional in practice: the in-pod `k8s-event-watcher`
authenticates with it, treats an empty value as fatal, and exits on every start
— so **no cluster events are watched at all**, in a container that stays Ready
and a CR whose `.status` says nothing. The Session KV server also answers `503`
to every request (losing chat-thread resolution and incident lookup), and
identity pseudonyms stop being stable across pod restarts. If you are upgrading
an installation that predates these keys, `upgrade.sh` adds them to the existing
Secret before it rolls the agent; a Helm or Terraform install supplies them
itself. To add them by hand:

```bash
kubectl patch secret platform-agent-secrets -n kubeagents-system --type=merge \
  -p "{\"stringData\":{\"SESSION_KV_API_KEY\":\"$(openssl rand -hex 32)\",\"SESSION_KV_SALT\":\"$(openssl rand -hex 32)\"}}"
kubectl rollout restart deployment/platform-agent-gateway -n kubeagents-system
```

Vertex AI needs no entry here: `MODEL_PROVIDER=vertex` authenticates with Workload Identity
(see [Inference gateway](docs/site/src/content/docs/concepts/inference-gateway.md#vertex-ai-and-model-garden)).

### Step 3: Build & Push the Operator Image

Set your registry destination and build the container image:

```bash
cd k8s-operator

export IMG=us-central1-docker.pkg.dev/<YOUR_PROJECT>/<YOUR_REPO>/kube-agents-operator:latest

make docker-build IMG=$IMG
make docker-push IMG=$IMG
```

### Step 4: Deploy the Operator & CRDs

Install the Custom Resource Definitions (CRDs) and deploy the controller manager deployment:

```bash
make install
make deploy IMG=$IMG
```

Verify controller readiness:

```bash
kubectl rollout status deployment -n kubeagents-system
```

### Step 5: Deploy Integrations (LiteLLM & GitHub)

To optionally deploy the LiteLLM Gateway or GitHub Token Minter:

`GITHUB_ORG` must be a GitHub **organization**. The Token Minter looks App installations up at `/orgs/{org}/installation`, which does not exist for personal accounts, so a user-owned GitOps repo deploys cleanly and then fails every token request with a 404. This manual path skips the provisioning scripts' preflight check — see [`k8s-operator/config/integrations/github/README.md`](k8s-operator/config/integrations/github/README.md).

```bash
# Deploy LiteLLM Gateway
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash
# For MODEL_PROVIDER=vertex_ai also export PROJECT_ID, LITELLM_KSA_NAME,
# LITELLM_GSA_NAME, VERTEX_PROJECT_ID, and VERTEX_LOCATION — the vertex overlay
# renders the gateway's Workload Identity ServiceAccount from them.
make deploy-litellm

# Deploy GitHub Integration (requires pre-configured github-app-credentials secret and env vars)
export PROJECT_ID="your-gcp-project-id"
export REGION="your-gcp-region"
export CLUSTER_NAME="your-gke-cluster-name"
export KMS_LOCATION="your-kms-region" # a region; Cloud KMS has no zonal locations
export KMS_KEYRING="your-kms-keyring"
export KMS_KEY="your-kms-key"
export KMS_KEY_VERSION="your-kms-key-version"
export GITHUB_ORG="your-github-org"
export GITHUB_REPO="your-github-repo"
export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
make deploy-github
```

### Step 6: Apply Custom Resources

Submit a sample `PlatformAgent` Custom Resource to activate cluster governance (run inside `k8s-operator/`):

```bash
kubectl apply -f examples/platformagent.yaml
kubectl get platformagents -A
```

---

## Method 3: Local Development & Fast Iteration

For developer testing on a workstation against a local cluster (e.g., Kind) or remote GKE cluster without building container images:

1. **Set your active Kubernetes context**:
   ```bash
   kubectl config current-context
   ```
2. **Install CRDs**:
   ```bash
   cd k8s-operator
   make install
   ```
3. **Run the controller locally with webhooks disabled**:
   ```bash
   ENABLE_WEBHOOKS=false make run
   ```
4. **Fast Remote Rebuild & Update**:
   To rebuild and push an updated container image and trigger immediate deployment rollout in GKE:
   ```bash
   make dev-rebuild-agent ARGS="platform"
   ```

## Method 4: Declarative IaC Install (Terraform + Helm)

The declarative counterpart of Method 1: a single `terraform apply` provisions the GKE
Autopilot cluster, the agent's GCP identity (Workload Identity, IAM roles), optionally the
Google Chat backend and the GitHub minter's KMS resources, and installs the
[`charts/kube-agents`](charts/kube-agents/README.md) Helm chart on top. Use it when the
install should live in version-controlled IaC (GitOps, CI-driven environments) instead of
the interactive pipeline.

- **Canonical guide (self-contained):** [`terraform/examples/full-install/README.md`](terraform/examples/full-install/README.md)
- Pick **one** path per project — Method 1 and Method 4 create equivalent GCP resources (same IAM, Pub/Sub, and identifiers; the Terraform module provisions an Autopilot cluster where the scripts provision Standard).
- The manual Chat/Slack registrations in
  [Step 5 of Method 1](#step-5-enable-google-chat--slack-integrations-manual-required-steps)
  apply to this method too.
- Until the first `vX.Y.Z` release tag exists, keep the default `image_tag = "latest"`
  (see the guide's image-tag note).

## Teardown & Cleanup

To safely remove provisioned resources:

### Automated Uninstallation

To remove the resources created for one configured `kube-agents` installation:

```bash
./uninstall.sh --non-interactive \
  --project-id="<PROJECT_ID>" \
  --cluster-name="<CLUSTER_NAME>" \
  --region="<REGION>"
```

### Automated Cloud Teardown

To clean up all GCP/GKE cluster resources, IAM bindings, secrets, and subscriptions provisioned by `make gcp-provision`:

```bash
cd k8s-operator
make gcp-teardown
```

Teardown mirrors provisioning in reverse, and each step has its own `make gcp-teardown-NN-*` target.
Run `make help` for the list, and see
[`k8s-operator/scripts/README.md`](k8s-operator/scripts/README.md) for what each one removes.

### Manual Local Uninstall

To uninstall the operator controller and CRDs manually:

```bash
cd k8s-operator
make undeploy
make uninstall
```

---

## Troubleshooting & Common FAQ

### 1. Workload Identity Authorization Errors (`403 Permission Denied`)

- Ensure the GKE Kubernetes Service Account (`kubeagents-system/kubeagents-platform-agent`) is correctly annotated with the GCP Service Account email (`iam.gke.io/gcp-service-account`).
- Verify IAM bindings using:
  ```bash
  gcloud iam service-accounts get-iam-policy <GSA_EMAIL>
  ```

### 2. Admission Webhook Errors (`x509: certificate signed by unknown authority`)

- Confirm `cert-manager` pods are running in the `cert-manager` namespace:
  ```bash
  kubectl get pods -n cert-manager
  ```
- If running the controller locally via `make run`, ensure `ENABLE_WEBHOOKS=false` is explicitly set to bypass webhooks.

### 3. GKE Autopilot Pod Pending on Lease Resources

- Check if your deployment is stuck waiting for leader election Leases in `kube-system`. Disable leader election arguments `--leader-elect=false` when deploying controllers to GKE Autopilot clusters.

### 4. Agent Pod Crashlooping, or CLIs Reporting `credential proxy unavailable`

- The `platform-agent` Pod runs four containers, and `gcloud`/`kubectl` inside the sandbox are wrappers around the credential sidecar, so a failed sidecar looks like broken tooling rather than a failed container. Read the sidecar's log first:
  ```bash
  kubectl logs -n kubeagents-system deploy/platform-agent-gateway -c envoy-credential-proxy
  ```
- For the symptoms, what they mean, and how to check the Pod's identity from outside the sandbox, see the [credential isolation troubleshooting section](docs/site/src/content/docs/reference/credential-isolation.md#troubleshooting).
