# Kubernetes Agentic Harness Installation & Setup Guide

This comprehensive, step-by-step guide explains how to install, configure, deploy, and verify the **Kubernetes Agentic Harness (`kube-agents`)** across different environments—from automated Google Cloud Platform (GCP) / GKE deployments to local development clusters and third-party multi-agent orchestrators.

> **What this file is.** A self-contained, executable procedure — runnable from a fresh clone with no
> network access to the documentation site, by a human or an AI agent. It deliberately carries the
> commands and nothing else.
>
> For the explanatory material — why each component exists, architecture, troubleshooting in depth,
> and the concept guides — see **<https://gke-labs.github.io/kube-agents/>**. For the shared
> installer defaults and the `install.env` configuration model, see
> [`scripts/installer/README.md`](scripts/installer/README.md).

---

## Table of Contents

1. [Architecture & Overview](#architecture--overview)
2. [Prerequisites & Tooling Matrix](#prerequisites--tooling-matrix)
3. [Method 0: Zero-Friction One-Liner Installation (Fastest)](#method-0-zero-friction-one-liner-installation-fastest)
4. [Method 1: The Install Engine — Terraform + Helm](#method-1-the-install-engine--terraform--helm)
   - [Step-by-Step Execution](#step-by-step-execution)
5. [The Shell Sandbox](#the-shell-sandbox)
6. [Method 2: Manual Kubernetes Cluster Deployment](#method-2-manual-kubernetes-cluster-deployment)
   - [Step 1: Install cert-manager](#step-1-install-cert-manager)
   - [Step 2: Create API Key & Access Secrets](#step-2-create-api-key--access-secrets)
   - [Step 3: Build & Push the Operator Image](#step-3-build--push-the-operator-image)
   - [Step 4: Deploy the Operator & CRDs](#step-4-deploy-the-operator--crds)
   - [Step 5: Deploy Integrations (LiteLLM & GitHub)](#step-5-deploy-integrations-litellm--github)
   - [Step 6: Apply Custom Resources](#step-6-apply-custom-resources)
7. [Method 3: Local Development & Fast Iteration](#method-3-local-development--fast-iteration)
8. [Teardown & Cleanup](#teardown--cleanup)
9. [Troubleshooting & Common FAQ](#troubleshooting--common-faq)

---

## Method 0: Zero-Friction One-Liner Installation (Fastest)

Run the interactive one-liner installer directly in **Google Cloud Shell** or any authenticated bash terminal:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/install.sh | bash
```

_To pin to a specific official release, substitute `<RELEASE_VERSION>` with the desired version tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases):_

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash
```

When running the release-pinned installer (`<RELEASE_VERSION>/install.sh`), the baked release version is offered as the default, so pressing Enter accepts it; `--non-interactive` uses it without prompting at all. When running the generic installer (`gke-labs.github.io/kube-agents/install.sh`), the interactive prompt asks you to enter the target SemVer release tag or full 40-character commit SHA (from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases)), and `--non-interactive` requires `--image-tag`. The installer rejects mutable refs such as `latest` and `main` to ensure install sources and container images stay strictly aligned.

### What `install.sh` Automatically Handles:

- **`gcloud` Authentication**: Checks login state and launches auth flows if needed.
- **GCP Project & Region Selection**: Auto-detects the active project and prompts for confirmation; you can type a project ID that the discovered list does not show.
- **Install Sources**: Puts the Terraform configuration and chart on disk (this checkout, or a clone at the requested revision) and verifies they match the image ref _before_ the interview starts.
- **GKE Cluster Setup**: Provisions an Autopilot or Standard cluster (`--cluster-mode`, Autopilot by default) or connects to an existing one. Autopilot is regional, so a zonal `--region` with no explicit `--cluster-mode` builds Standard instead of failing; asking for `--cluster-mode=autopilot` at a zone is still an error.
- **Chat Integrations**: Configures Google Chat and/or Slack when selected.
- **AI Model Credentials**: Prompts for Gemini, OpenAI, or Anthropic credentials, or selects Vertex AI (no key — Workload Identity).
- **Long-Term Memory**: Asks whether the agents should remember anything between conversations, and if so which store (`--memory=file|hindsight|off`, default `file`). The default is **on**, and it is the store this repository shipped before the searchable one existed, so an upgrade that says nothing about memory keeps what it already has: per-user Markdown inside the pod (`multiuser_memory`), no extra services, suited to **small or personal** deployments — but the whole store is loaded into the model's context every turn, so it stops scaling past a few pages. Pick `hindsight` for **enterprise** deployments — ranked recall that stays affordable as the store grows, at the cost of an API server and a Postgres database in the cluster; it selects the `kube_agents_memory` provider. Pick `off` to retain nothing and run no database. The measurements behind that split, and how to change it later, are in [`docs/designs/memory.md`](docs/designs/memory.md).
- **Automated Engine Execution**: Generates `terraform/examples/full-install/terraform.tfvars` from the loaded configuration, records that configuration in `install.env` when a first install has none, and launches `lifecycle.sh apply` — a single `terraform apply` that provisions every GCP resource and installs the Helm chart, with the Terraform state kept in a versioned GCS bucket (`<project>-kube-agents-tfstate`).

The installer's engine is [Method 1](#method-1-the-install-engine--terraform--helm): the
[`terraform/examples/full-install`](terraform/examples/full-install/README.md) composition, which is
the canonical description of what gets created. Three things stay outside Terraform, run by the
installer itself: CMEK database encryption on a **pre-existing** cluster (a `gcloud` pre-step), the
managed-OTel collection scope (no Terraform field exists), and the GitHub App private-key import
into KMS (the PEM must not enter Terraform state). The installer sources
`scripts/installer/installer_common.sh`, which reads `install.defaults.env`, so its defaults
(region, cluster name, model provider, registry prefix) and its accepted values live in exactly
one place; see
[Shared defaults live in `installer_common.sh`](scripts/installer/README.md#shared-defaults-live-in-installer_commonsh).

Three behaviours worth knowing before the first run:

- **The image/source ref defaults to the checkout's `HEAD`** and must be a SemVer release tag or a
  full 40-character commit SHA. Provisioning refuses to start from a dirty or mismatched checkout so
  the scripts and the container image stay on one revision; pass `--allow-unverified-source` to
  override that while iterating on the installer itself.
- **The agent's GCP IAM permission set defaults to `read-only`**, matching the provisioner. It
  controls cloud-plane writes only — Kubernetes RBAC is read-only in every set, and the GitOps
  pull-request path works in every set. See the site's
  [security and IAM reference](docs/site/src/content/docs/reference/security-and-iam.md).
- **The agent runs sandboxed under gVisor**, because it executes model-authored commands and an
  unsandboxed pod shares the node kernel with everything else on the node. Autopilot, the shape a
  fresh install creates, ships the RuntimeClass and needs no node pool, from GKE `1.27.4-gke.800`
  on — so the sandbox costs nothing there. On a Standard cluster it provisions a `gvisor-pool`
  node pool of one `e2-standard-4` per zone. Pass `--gvisor=false` to run on the standard
  container runtime.

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

To run pre-flight checks and output configuration state (`terraform.tfvars` and
`/tmp/kube-agents-install-report.json`) without creating cloud resources — the dry run also
validates the Terraform configuration, and previews the full resource plan when Application
Default Credentials are available:

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

| CLI Tool / Utility              | Required Version                                | Verification Command       | Description                                                                                                 |
| :------------------------------ | :---------------------------------------------- | :------------------------- | :---------------------------------------------------------------------------------------------------------- |
| **Go**                          | `1.27+`                                         | `go version`               | Required for building operator binaries and running tests.                                                  |
| **Docker / Podman**             | `20.10+`                                        | `docker --version`         | Required to build container images for the operator.                                                        |
| **kubectl**                     | `1.28+`                                         | `kubectl version --client` | Communicates with your target Kubernetes or GKE cluster.                                                    |
| **Kubernetes Cluster**          | `1.29+` (`1.35+` for `AgentPlugin` OCI volumes) | `kubectl version`          | Target Kubernetes or GKE cluster (`AgentPlugin` OCI volumes require K8s 1.35+ `ImageVolume` gate).          |
| **Google Cloud SDK (`gcloud`)** | `576.0.0+`                                      | `gcloud version`           | GKE cluster access, IAM, and Artifact Registry. `576.0.0` is where `--managed-otel-scope` reached GA.       |
| **Terraform**                   | `~> 1.5`                                        | `terraform version`        | The install engine. `install.sh` offers to install it (Homebrew tap / HashiCorp apt repo) when missing.     |
| **Helm**                        | `3.10+`                                         | `helm version`             | `upgrade.sh`'s fast paths and the manual chart install; the engine itself uses the Terraform Helm provider. |
| **gettext (`envsubst`)**        | Standard                                        | `envsubst --version`       | Used by the kustomize deployment targets (Method 2) for template substitution.                              |
| **`jq`**                        | `1.6+`                                          | `jq --version`             | Reads `images.json`; the kustomize deploy targets resolve image references from it.                         |

---

## Method 1: The Install Engine — Terraform + Helm

This is the engine [Method 0](#method-0-zero-friction-one-liner-installation-fastest) drives, usable
directly when the install should live in version-controlled IaC (GitOps, CI-driven environments)
instead of an interview. One `terraform apply` of the
[`terraform/examples/full-install`](terraform/examples/full-install/README.md) composition
provisions every GCP resource — the GKE cluster (`cluster_mode = "autopilot"` or `"standard"`, or
`create_cluster = false` for a cluster somebody else made), the agent's identity and IAM, optionally
the Google Chat backend, the GitHub minter's KMS resources, and a Backup for GKE plan — and installs
the [`charts/kube-agents`](charts/kube-agents/README.md) Helm chart on top, which owns every
Kubernetes resource (operator, PlatformAgent CR, LiteLLM gateway, and the optional Hindsight store
and GitHub minter workloads).

- **Canonical guide (self-contained):** [`terraform/examples/full-install/README.md`](terraform/examples/full-install/README.md)
- Drive it through [`lifecycle.sh`](terraform/examples/full-install/lifecycle.sh) rather than bare
  `terraform` commands: `apply` adopts the Cloud KMS resources GCP refuses to delete and any Pub/Sub
  topic or subscription that already exists, `destroy` handles the four teardown asymmetries a bare
  `terraform destroy` trips over, and `plan` reports what an apply would change while creating
  nothing.
- The composition installs `cert-manager` automatically (`enable_cert_manager`, default true), so
  you do **not** need to install it yourself on this path. (You do for
  [Method 2](#method-2-manual-kubernetes-cluster-deployment).)
- The manual Chat/Slack registrations in
  [Step 4 of this method](#step-4-enable-google-chat--slack-integrations-manual-required-steps)
  apply however the engine is driven.
- Until the first `X.Y.Z` release tag exists, keep the default `image_tag = "latest"`
  (see the guide's image-tag note).

### Step-by-Step Execution

#### Step 1: Authenticate with Google Cloud

Authenticate your `gcloud` CLI and set Application Default Credentials:

```bash
gcloud auth login
gcloud auth application-default login
```

#### Step 2: Apply the Composition

The interactive way is `./install.sh` from the repository root (Method 0), which writes the
`terraform.tfvars` for you. Hand-driven:

```bash
cd terraform/examples/full-install
cp terraform.tfvars.example terraform.tfvars   # then edit it
KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply
```

- `KUBE_AGENTS_STATE_BUCKET=auto` keeps the Terraform state in a versioned GCS bucket
  (`<project>-kube-agents-tfstate`, prefix `kube-agents/<cluster>`), created on first use. Omit it
  for local state — fine for a hand-driven evaluation, wrong for anything `uninstall.sh` or
  `upgrade.sh` should later find. The state contains every secret the install was given; the
  bucket's IAM is its protection.
- `install.sh` re-runs are idempotent: it loads `install.env` from the first run, regenerates
  `terraform.tfvars` from it, and `terraform apply` reconciles whatever changed. Flags you omit
  keep the value the file records rather than reverting to a default, so bumping `--image-tag`
  alone changes only the image tag. To change configuration, edit `install.env` (copy
  `install.env.example` and `chmod 600` it if the first install has not written one yet — the
  example is tracked world-readable and the file it becomes holds your API keys) and re-run, use
  `./install.sh --menu` (Save & Apply re-applies through the same engine), or edit your
  hand-written tfvars and re-apply.

- **Private Container Registry**: If your GKE clusters may only pull from an approved registry, see
  [Private container registry](#private-container-registry) below for the full recipe. Mirroring
  only the `kube-agents` images is not enough on its own: `image_registry` (or `install.sh`'s
  `--registry-prefix`) covers the four images this project builds, and LiteLLM, fluent-bit, the
  GitHub token minter and Hindsight need `third_party_image_registry` (or
  `--third-party-registry-prefix`) as well; cert-manager is separate (see the composition README).

- **Dry-run check**: To preview actions without modifying cloud infrastructure:
  ```bash
  ./install.sh --dry-run -y --project-id=<PROJECT> --image-tag=<TAG>   # validate + terraform plan
  # or, hand-driven, plain:  terraform plan
  ```

#### Security & CMEK Encryption

The automated installer includes local state hardening and Cloud KMS (CMEK) etcd database encryption:

- **Local State Security**: The `install.env` configuration — and the `terraform.tfvars` generated from it — is protected with strict file permissions (`umask 077`, `chmod 600`). An `install.env` the installer wrote is 0600 from the start; one you created by copying `install.env.example` is whatever your umask made it, so `chmod 600` it yourself. `install.sh` tightens a group- or world-readable one when it loads it and prints what it did. The Terraform **state** additionally holds every secret in plaintext; it lives in the versioned GCS state bucket, whose IAM is its protection.
- **GKE Database Encryption (CMEK)**: GKE etcd database encryption is configured automatically using Cloud KMS (`kms_keyring_name` / `kms_key_name`, default `platform-agent-keyring` / `k8s-secret-encryption-key`). On a **pre-existing** cluster Terraform cannot enable it, so `install.sh` does that as a `gcloud` pre-step before the apply.
- **`ALLOW_UNENCRYPTED_SECRETS`**: Set `ALLOW_UNENCRYPTED_SECRETS=true` before running `install.sh` against an existing unencrypted cluster to skip that CMEK pre-step (testing environments only).
- **`PERSIST_SECRETS_ON_DISK`**: By default (`PERSIST_SECRETS_ON_DISK=true`), credentials (API keys, Slack tokens) are saved to `install.env`. Set `PERSIST_SECRETS_ON_DISK=false` to keep them out of every file the installer writes; they travel to Terraform as `TF_VAR_*` and later runs recover them from the live `platform-agent-secrets` Secret.

#### Private container registry

If your clusters may only pull from an approved registry, copy every image the install needs
there first, then export both registry prefixes before provisioning:

```bash
# Exported before the mirror step so the mirror and the install use one tag.
export IMAGE_TAG=v0.1.0

make mirror-images MIRROR_PREFIX=registry.example.com/kube-agents

./install.sh -y --image-tag=v0.1.0 \
  --registry-prefix=registry.example.com/kube-agents \
  --third-party-registry-prefix=registry.example.com/kube-agents
```

`make mirror-images` reads `images.json` at the repository root — the inventory of every image
an install pulls — and copies each one, keeping the trailing image name only.

`IMAGE_TAG` is not optional here. The mirror holds only the tag it was told to copy — `latest`
if it was told nothing — while the install asks for whatever `--image-tag` says. Set the two to
different values and the four first-party images name a reference the mirror was never given:
the apply reports success and the pods sit in ImagePullBackOff, after the cluster and
cert-manager already exist. Export it once, as above, so the mirror and the install cannot
disagree.

The two flags are separate because the images fall into two groups. `--registry-prefix`
(`image_registry` in tfvars) replaces `ghcr.io/gke-labs/kube-agents` for the images this project
builds — the operator, the agent, and the credential proxy. `--third-party-registry-prefix`
(`third_party_image_registry`) covers the ones it does not: the LiteLLM gateway, the fluent-bit
sidecar, the GitHub token minter, and Hindsight. **Neither implies the other** on the installer
flags, so an install that mirrors everything passes both, as above. (The chart's own
`global.thirdPartyImageRegistry` differs here, deliberately: it defaults to
`global.imageRegistry`.) cert-manager is a separate Helm release the values never reach — see
the composition README's mirrored-registry section for its recipe.

See the [Docker images guide](docs/site/src/content/docs/deploy/docker-images.md) for the
inventory, the mirror script's options, the Helm and Terraform equivalents, and how to rebuild
from mirrored base images rather than copying.

#### Step 3: Verify Running Components

Verify that the operator, LiteLLM gateway, and custom resources are healthy:

```bash
kubectl get deployments -n kubeagents-system
kubectl get pods -n kubeagents-system
kubectl get platformagents --all-namespaces
```

#### Step 4: Enable Google Chat & Slack Integrations (Manual Required Steps)

If you enabled Google Chat or Slack during the install, perform the following required manual steps after the apply completes:

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
   - Re-display these instructions at any time from the repository root:
     ```bash
     ./scripts/installer/print_instructions_gchat.sh
     ```

##### 2. Slack Configuration (`SLACK_ENABLED=true`)

1. **Verify Slack App Settings**:
   - Ensure **Socket Mode** is enabled in your Slack App console.
   - Verify that your Bot Token (`SLACK_BOT_TOKEN`) has the required scopes: `app_mentions:read`, `channels:history`, `chat:write`, `channels:read`, `groups:read`, `im:read`, `mpim:read`, `files:write`, `reactions:write`.
   - `files:write` is the one that is easy to miss, because omitting it looks like nothing is wrong. A card whose answer is text is delivered normally; a card that produces a **file** has its upload rejected with `missing_scope`, which the artifact delivery path catches and logs as a warning. The user is told the task completed and never sees the artifact. Add the scope and reinstall the app.
   - `reactions:write` fails more quietly still. The agent puts 👀 on a message when it picks the work up and swaps it for ✅ or ❌ when the turn ends; without the scope Slack rejects each of those with `missing_scope`, the adapter logs it at debug and carries on, and the answer still arrives. The only symptom is that no reaction ever appears. Add the scope and reinstall.
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
   - This adds Slack's autocomplete, not the behaviour: a typed `/hermes <subcommand>` works either way, because the Planning Agent's `legacy_slash_commands` plugin unwraps it before the gateway resolves the command.
5. **Set the Home Channel (if you left `SLACK_HOME_CHANNEL` empty)**:
   - Scheduled audits have nowhere to post until one is set. From the Slack channel you want, run `/sethome` (or `/hermes sethome`). It takes effect immediately and persists across restarts.

- Re-display these instructions at any time from the repository root:
  ```bash
  ./scripts/installer/print_instructions_slack.sh
  ```

---

## The Shell Sandbox

Every command the model writes runs in `platform-agent-shell-0`, a pod of its own that holds no
credentials and is reached over SSH. It is not optional and there is nothing to turn on: the
operator refuses `shellSandbox.enabled: false` with `Degraded`/`ShellSandboxCannotBeDisabled`, and
the chart fails the render rather than installing something that would sit `Degraded`. The SSH
keypair it needs is minted for you by `install.sh`, by the Terraform composition, and by
`upgrade.sh` on an install that predates it, so there is no key ceremony.

The one thing left to choose is the container runtime. `gvisor` puts a user-space kernel under the
sandbox pod and needs a GKE Sandbox node pool; on Standard clusters `enable_gvisor_node_pool = true`
builds one, and Autopilot ships the `gvisor` RuntimeClass with no pool to manage. Leave it empty to
run on the node's standard runtime.

Set it on a new install by adding this to `terraform.tfvars` before the first apply
([Method 1](#method-1-the-install-engine--terraform--helm), Step 2):

```hcl
cluster_mode            = "standard" # omit both lines on Autopilot, which
enable_gvisor_node_pool = true       # provides the gvisor RuntimeClass natively

extra_helm_values = {
  platformAgent = { harness = { experimental = { shellSandbox = {
    runtimeClassName = "gvisor"
  } } } }
}
```

Or on an existing one:

```bash
helm upgrade kube-agents ./charts/kube-agents \
  --namespace kubeagents-system --reuse-values \
  --set platformAgent.harness.experimental.shellSandbox.runtimeClassName=gvisor \
  --wait --timeout 10m
```

Confirm it took: `kubectl get pod platform-agent-shell-0 -n kubeagents-system -o jsonpath='{.spec.runtimeClassName}'`.

On an install that came from `install.sh`, put the same value in `extra_helm_values` in
`terraform/examples/full-install/terraform.tfvars` as well. The next `upgrade.sh` or `install.sh`
re-run regenerates that file from `install.env`, which does not record this setting, and reverts a
value set only on the Helm release.

Installing the chart directly with `helm install` gives you a sandbox the agent cannot log into: the
chart cannot generate an `authorized_keys`-form public key, so `platform-agent-shell-authorized-keys`
renders empty. Supply the keypair yourself in `platformAgent.credentials.data`
(`SANDBOX_SSH_PRIVATE_KEY`, `SANDBOX_SSH_PUBLIC_KEY`) or use Method 1.

---

## Method 2: Manual Kubernetes Cluster Deployment

If you are installing into an existing Kubernetes or GKE cluster without using the automated GCP provisioning pipeline, follow these steps.

### Step 1: Install cert-manager

The Kubernetes Operator requires `cert-manager` (version `1.13.0+`) to generate and rotate admission webhook TLS certificates.

> Only needed on this manual path. [Method 1](#method-1-the-install-engine--terraform--helm) installs `cert-manager` for you.

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

# An immutable tag: `make deploy` refuses a floating tag such as `latest`, because it
# lets a pod reschedule upgrade the controller past the RBAC applied with it.
# Set ALLOW_MUTABLE_IMG=1 only for a cluster you will discard.
export IMG=us-central1-docker.pkg.dev/<YOUR_PROJECT>/<YOUR_REPO>/kube-agents-operator:$(git rev-parse HEAD)

make docker-build IMG=$IMG
make docker-push IMG=$IMG
```

### Step 4: Deploy the Operator & CRDs

Install the Custom Resource Definitions (CRDs) and deploy the controller manager deployment:

```bash
make install
make deploy IMG=$IMG
```

Then apply the agent-RBAC admission policies. `make deploy` does **not** include them — they are
deliberately outside the kustomize overlay, because its `namePrefix` would rewrite each policy's
name without rewriting the `spec.policyName` its binding refers to, leaving both bindings pointing
at nothing and the policies silently inert:

```bash
# Kubernetes 1.30+ only (ValidatingAdmissionPolicy v1). Skip on older clusters -- unlike
# the chart, which checks the version itself, this apply will fail there.
kubectl apply -f config/admission/agent-rbac-policy.yaml
```

[Method 1](#method-1-the-install-engine--terraform--helm) gets these from the chart and needs no
such step. Skipping it here leaves agent RBAC without its admission backstop. Read that file's
header for what the policies do and do not enforce — notably, they cannot check the rules of a role
that a binding merely _references_.

If the agent images are mirrored into a private registry, tell the operator where to find them.
These two are the images it resolves at reconcile time rather than reading from a manifest — the
agent image for a `PlatformAgent` that omits `spec.deployment.image`, and the logging sidecar it
injects into every agent pod — so nothing else sets them:

```bash
kubectl set env deployment/kubeagents-controller-manager -n kubeagents-system \
  PLATFORM_AGENT_IMAGE=registry.example.com/kube-agents/platform-agent:latest \
  FLUENT_BIT_IMAGE=registry.example.com/kube-agents/fluent-bit:5.1.1
```

The Helm chart does this for you when a registry prefix
is in effect; the commands above are for a hand-rolled `make deploy`. The credential-proxy
sidecar needs no variable — the operator derives it from the agent image. See the
[Docker images guide](docs/site/src/content/docs/deploy/docker-images.md) for all override env
vars and their precedence.

Verify controller readiness:

```bash
kubectl rollout status deployment -n kubeagents-system
```

### Step 5: Deploy Integrations (LiteLLM & GitHub)

To optionally deploy the LiteLLM Gateway or GitHub Token Minter:

`GITHUB_ORG` must be a GitHub **organization**. The Token Minter looks App installations up at `/orgs/{org}/installation`, which does not exist for personal accounts, so a user-owned GitOps repo deploys cleanly and then fails every token request with a 404. This manual path skips the installer's preflight check — see [`k8s-operator/config/integrations/github/README.md`](k8s-operator/config/integrations/github/README.md).

`GITHUB_ORG`/`GITHUB_REPO` here, not the `GITOPS_ORG`/`GITOPS_REPO` the installer takes: this is the hand-driven `make deploy-github` path, whose envsubst allowlist in `k8s-operator/Makefile` passes the `GITHUB_*` names. The rename is scoped to the installer's own inputs.

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

`uninstall.sh` above delegates to `lifecycle.sh destroy`, which is also usable directly from a
checkout:

```bash
cd terraform/examples/full-install
KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh destroy
```

`destroy` handles the four asymmetries a bare `terraform destroy` trips over: it deletes the
PlatformAgent CR up front (force-clearing a wedged finalizer), purges the backups a BackupPlan
still owns, clears the cluster's deletion protection, and forgets the undeletable Cloud KMS
resources from state so their key versions are never scheduled for destruction — the next
`lifecycle.sh apply` adopts them back automatically.

With **no Terraform state** (none in the GCS bucket, none locally), `uninstall.sh` says so and
stops with exit **3**, touching nothing. Either nothing is installed against those coordinates,
or the install was made by a release that predates this engine — re-run it with
`--source-ref=<that release>` so the matching teardown runs.

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

- `gcloud`/`kubectl` in the shell sandbox are wrappers around the credential proxy, which runs in a Pod of its own, so a failed proxy looks like broken tooling rather than a failed container. Read the proxy's log first:
  ```bash
  kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy
  ```
- For the symptoms, what they mean, and how to check the Pod's identity from outside the sandbox, see the [credential isolation troubleshooting section](docs/site/src/content/docs/reference/credential-isolation.md#troubleshooting).
