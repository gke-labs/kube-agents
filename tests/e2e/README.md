# Google Chat Agent End-to-End (E2E) Test Suite

This directory contains the automated E2E test suite for verifying the **Hermes Platform Agent** integration with Google Chat.

## 📌 Architecture & Design Concept

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              pytest runner                              │
└───────┬─────────────────────────────────┬───────────────────────▲───────┘
        │ 1. Post prompt message          │ 2. Publish Chat Event │
        │    (Service Account WIF)        │    with Thread ID     │ 5. Polls & asserts
        │                                 │    (Service Account)  │    response (OTA User)
        ▼                                 ▼                       │
┌──────────────────────┐      ┌──────────────────────┐            │
│  Google Chat API     │      │    Pub/Sub Topic:    │            │
│(spaces.messages.create)     │ platform-agent-events│            │
└───────┬──────────────┘      └───────────┬──────────┘            │
        │                                 │ 3. Pull Event         │
        │ 1b. Returns Thread ID           ▼                       │
        │                     ┌──────────────────────┐            │
        │                     │ Hermes Agent (GKE)   │            │
        │                     └───────────┬──────────┘            │
        │                                 │ 4. Post Reply         │
        ▼                                 ▼                       │
┌─────────────────────────────────────────────────────────────────┴───────┐
│                            Google Chat Space                            │
└─────────────────────────────────────────────────────────────────────────┘
```

### 1. Hybrid Auth Model & Google Chat API Restrictions

- **Service Account (WIF Keyless Authentication)**:
  Used to post the initial thread message (`spaces.messages.create`) and trigger Pub/Sub (`pubsub.projects.topics.publish`).
- **Owned Test Account (OTA User Credentials)**:
  Used to poll and read space messages (`spaces.messages.list`).
  _Why OTA User Credentials?_ By Google Chat API security policy, Service Accounts using `chat.bot` or `chat.messages.readonly` are **strictly forbidden** from calling `spaces.messages.list` (returns HTTP 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT`). Using a dedicated OTA (Owned Test Account) user credential enables 100% automated message verification in CI without using personal developer accounts.

### 2. Why Hybrid Pub/Sub Triggering is Required

- **Google Chat API Event Suppression**: Google Chat API does not generate Pub/Sub interaction events for messages posted programmatically via `spaces.messages.create` to prevent infinite bot loops.
- **Hybrid Test Flow**:
  1. **Step 1**: Test runner posts a prompt via Service Account WIF to establish a real Google Chat Space Thread ID (`spaces/{SPACE_ID}/threads/{THREAD_ID}`).
  2. **Step 2**: Test runner constructs a valid Google Chat event payload referencing the real Thread ID and authorized test identity (`TEST_USER_EMAIL`), publishing it directly to Pub/Sub topic `platform-agent-chat-events`.
  3. **Step 3**: **Hermes Agent** in GKE receives the Pub/Sub event, computes a well-known, predictable answer that can be validated deterministically, and posts the reply into the real space thread.
  4. **Step 4**: Test runner polls the thread via `poll_chat_service` using the OTA User credentials (`chat.messages.readonly`) and asserts the expected response.

---

## 🛠️ Complete GCP Project & CI Setup Checklist

To configure a new or existing GCP project for running this E2E test suite in CI/CD, complete the following 4 setup sections:

### 1. Enable Required GCP APIs

Ensure the following APIs are enabled in your target GCP project:

```bash
gcloud services enable \
  chat.googleapis.com \
  pubsub.googleapis.com \
  iamcredentials.googleapis.com \
  --project="<GCP_PROJECT_ID>"
```

### 2. Provision CI Service Account & Workload Identity Federation (WIF)

The test runner requires a CI Service Account (`github-actions-e2e@<GCP_PROJECT_ID>.iam.gserviceaccount.com`) with keyless Workload Identity Federation (WIF) access.

Run the automated IAM provisioning script:

```bash
./tests/e2e/scripts/provision_ci_iam.sh \
  --gcp_project "<GCP_PROJECT_ID>" \
  --git_project "<GITHUB_OWNER>/<REPOSITORY_NAME>"
```

_What this script provisions:_

- Service Account: `github-actions-e2e@<GCP_PROJECT_ID>.iam.gserviceaccount.com`
- IAM Roles: `roles/pubsub.publisher` on topic `platform-agent-chat-events` + `roles/chat.admin`
- WIF Pool & Provider: `github-actions-pool` / `github-provider` bound to your GitHub repository.

_(To teardown CI IAM resources when no longer needed, run `./tests/e2e/scripts/teardown_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>`)._

### 3. Setup Owned Test Account (OTA) & Google Chat Space

1. **Create OTA Account**: Create or configure a dedicated test account (e.g. `kube-agents-e2e-verifier@gmail.com`).
2. **Activate Google Chat**: Open Chrome Incognito, log in as the test account, and navigate to `https://chat.google.com` to accept initial setup.
3. **Add OTA to Space**: Open your target Google Chat Space (`CHAT_SPACE_ID`), ensure _external members are allowed_, and add your OTA email as a member.

### 4. Setup OAuth Consent Screen & Client ID in GCP Console

1. **Configure OAuth Consent Screen**:
   - Go to **[https://console.cloud.google.com/apis/credentials/consent?project=<GCP_PROJECT_ID>](https://console.cloud.google.com/apis/credentials/consent)**
   - Select **External** User Type (or **Internal** if using a Google Workspace organization).
   - Enter App Name (`E2E Chat Verifier`) and Support/Developer Email (e.g. your email address).
   - In **Test Users**, click **+ ADD USERS** and add your OTA email (`kube-agents-e2e-verifier@gmail.com`).
2. **Publish the OAuth App (Mandatory to prevent 7-day token expiration)**:
   - Under **Publishing Status**, click **PUBLISH APP** and confirm publication (moves status from **Testing** to **In Production**).
   - > ⚠️ **CRITICAL NOTE**: By default, GCP OAuth applications in **Testing** status force all issued refresh tokens to expire after **7 days**, which causes CI/CD runs to fail with `invalid_grant: Token has been expired or revoked`. Publishing the application removes this 7-day limit, allowing the OTA refresh token to live indefinitely and self-renew during CI runs.
3. **Create OAuth Client ID**:
   - Go to **[https://console.cloud.google.com/apis/credentials?project=<GCP_PROJECT_ID>](https://console.cloud.google.com/apis/credentials)**
   - Click **+ CREATE CREDENTIALS** ➔ **OAuth client ID**.
   - Select Application type: **Desktop app**.
   - Name: `e2e-chat-verifier`.
   - Copy the generated **Client ID** and **Client Secret**.

### 5. Generate Refresh Token & Configure GitHub Secrets

1. Run the standalone token generator:
   ```bash
   CLIENT_ID="<your_client_id>" CLIENT_SECRET="<your_client_secret>" python3 tests/e2e/scripts/generate_token.py
   ```
2. Open the printed authorization URL in Chrome Incognito (logged into your OTA test account).
3. Click **Allow** (if prompted with an unverified app warning, click **Advanced ➔ Go to E2E Chat Verifier (unsafe) ➔ Allow**).
4. When Chrome redirects to `http://localhost:8080/?code=...` (`ERR_CONNECTION_REFUSED`), copy the **entire URL from your browser address bar** and paste it into the terminal prompt.
5. The generator writes its output to a `0600` file under your temp directory and prints the path. It does not print the credentials themselves, so they do not linger in terminal scrollback. Open that file to read the values.
6. Save the 4 credentials as **GitHub Repository Secrets** (**Settings ➔ Secrets and variables ➔ Actions**):
   - `E2E_CHAT_CLIENT_ID` — from the credentials file
   - `E2E_CHAT_CLIENT_SECRET` — the Client Secret from step 4.3; the generator never echoes it back
   - `E2E_CHAT_REFRESH_TOKEN` — from the credentials file
   - `E2E_CHAT_SPACE_ID`
7. Delete the credentials file (`rm <printed path>`). The refresh token does not expire once the OAuth app is published, so a stray copy on disk is a standing liability.

---

## 💻 Local Setup & Execution Guide

### Step 1: Install Dependencies & Set Up Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r tests/e2e/requirements.txt
```

### Step 2: Authenticate GCP ADC for Local Execution

> **Note on Local Runs**: When running locally without `E2E_CHAT_*` environment variables, the test runner automatically falls back to your personal `gcloud` ADC credentials. You can run the test locally against any GCP project and target Google Chat Space without requiring OTA user credentials!

```bash
source k8s-operator/scripts/vars.sh
export CHAT_SPACE_ID="spaces/XXXXXXXXX"

gcloud auth application-default set-quota-project "$PROJECT_ID"
gcloud auth application-default login --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/chat.messages.create,https://www.googleapis.com/auth/chat.messages.readonly,https://www.googleapis.com/auth/pubsub"
```

### Step 3: Run pytest

```bash
pytest tests/e2e/gchat_agent_test.py -v -s
```

---

## 🚀 Operator AgentPlugins E2E Test Suite (`tests/e2e/operator/agentplugins_e2e_test.py`)

This test suite performs a 17-step end-to-end verification of the `AgentPlugin` CRD, OCI image volume mounting, profile targeting and linking, config allowlisting and scoping, execution-limit tuning, failsafes, and status condition handling on a live Kubernetes/GKE cluster.

> [!WARNING]
> **This suite takes over the target namespace. Run it on a test cluster, not one you care about.**
>
> It builds a clean environment for itself, by design:
>
> - **Every existing `AgentPlugin` in the namespace is destroyed.** Step 12 deletes the
>   `AgentPlugin` CRD to prove `PlatformAgent` reconciliation survives without it, and deleting a
>   CRD cascades to every custom resource of that kind. The CRD is restored; **your plugin CRs are
>   not.** Anything installed there is gone when the run finishes — and if that included a
>   platform adapter, alert ingress goes with it.
> - **The operator Deployment is repointed** at an image the suite builds from your working tree.
> - **The `PlatformAgent` Deployment is rolled repeatedly**, step 9 temporarily annotates the
>   `PlatformAgent` to disable image volumes, and step 17 deletes the agent pod to re-run its
>   startup.
> - **`spec.harness.tuning` is overwritten and then removed.** Steps 14 and 16 set it to
>   exercise the tuning lifecycle and clear it again afterwards, including in their failure
>   paths — so any tuning the namespace already had is gone when the run finishes. Other
>   `PlatformAgent` spec fields are left alone.
>
> If the namespace has plugins you want back afterwards, snapshot and re-apply them yourself:
>
> ```bash
> kubectl get agentplugins -n "$NAMESPACE" -o yaml > /tmp/plugins-snapshot.yaml
> # ...run the suite...
> kubectl apply -f /tmp/plugins-snapshot.yaml
> ```

### Prerequisites:

- Active `kubectl` context pointing to a test cluster (e.g. GKE).
- Write access to a container registry (e.g. `gcr.io/$PROJECT_ID`).
- An image builder: either a running Docker daemon (default) or `crane` plus a Go
  toolchain (`IMAGE_BUILDER=crane`) for hosts without one.

### Environment variables:

| Variable          | Required | Purpose                                                                              |
| ----------------- | -------- | ------------------------------------------------------------------------------------ |
| `KUBE_CONTEXT`    | Yes      | `kubectl` context of the target cluster.                                             |
| `NAMESPACE`       | Yes      | Namespace holding the operator and `PlatformAgent`.                                  |
| `REGISTRY`        | Yes      | Registry prefix for the operator and plugin images.                                  |
| `IMAGE_BUILDER`   | No       | `docker` (default) or `crane`. See below.                                            |
| `CRANE_BIN`       | No       | Path to the `crane` binary. Only used by `IMAGE_BUILDER=crane`. Defaults to `crane`. |
| `TARGET_PLATFORM` | No       | Platform for `crane` builds. Defaults to `linux/amd64`, matching GKE nodes.          |

### Choosing an image builder

| Builder  | Requires                | Notes                                                 |
| -------- | ----------------------- | ----------------------------------------------------- |
| `docker` | A running Docker daemon | Default. Builds the real Dockerfiles.                 |
| `crane`  | `crane` + Go toolchain  | No daemon required. Does **not** use the Dockerfiles. |

`crane` is the fallback for hosts without a Docker daemon. It assembles images directly:
the operator is cross-compiled with `go build` and layered onto the same
`gcr.io/distroless/static:nonroot` base with the same entrypoint/user/workdir as
`k8s-operator/Dockerfile`, and the plugin image is `alpine:3.19` plus the plugin files.
Because it bypasses the Dockerfiles, a `crane` run does not validate them — use `docker`
when the image build itself is what you need to exercise.

Install `crane` with:

```bash
GOBIN="$HOME/.local/bin" go install github.com/google/go-containerregistry/cmd/crane@latest
```

`crane` authenticates through `~/.docker/config.json`. For `gcr.io` that means a
credential helper entry, which `gcloud auth configure-docker gcr.io` adds.

### Execution:

```bash
PYTHONIOENCODING=utf-8 \
LANG=C.UTF-8 \
LC_ALL=C.UTF-8 \
KUBE_CONTEXT="gke_my-project_europe-west1_ka-dev-mgmt" \
NAMESPACE="kubeagents-system" \
REGISTRY="gcr.io/my-project" \
python3 tests/e2e/operator/agentplugins_e2e_test.py
```

On a host without a Docker daemon, add `IMAGE_BUILDER=crane`.

### 17-Step Verification Workflow:

1. **Rebuild & Deploy Operator**: Compiles `k8s-operator` binary, builds the container image, pushes to registry, applies CRDs, and deploys `kubeagents-controller-manager`.
2. **Verify Operator Version**: Confirms controller manager pod image tag matches the newly pushed build.
3. **Build & Push OCI Plugin Image**: Packages example plugin assets into an OCI container image with a unique build ID.
4. **Deploy AgentPlugin CR**: Deploys targeted `AgentPlugin` CR with `agentRef: "platform-agent"`, allowed `approvals` configuration subtree, disallowed config keys, and `imagePullPolicy: Always`.
5. **Verify Plugin Activation**: Asserts Hermes pod logs `[HERMES-PLUGIN-E2E]` (`available=True`), that an untargeted plugin's `approvals:` subtree merges through `profile-default.overlay.yaml` into the agent's own `config.yaml` rather than the pod-wide managed scope, that the managed scope itself reached the pod, that the agent's config stayed writable, rejection of unallowed config keys, and operator log `manifestsLog.Error("ignoring plugin config key outside allowed subtrees")`.
6. **Remove AgentPlugin CR**: Deletes the `AgentPlugin` CR and waits for rollout.
7. **Verify Log Silence**: Confirms unique plugin log output stops appearing in replacement pod logs.
8. **Verify ConfigMap Cleanup**: Confirms plugin entry is removed from `plugins.enabled` in `config.yaml`.
9. **ImageVolume Disable Safeguard**: Annotates `PlatformAgent` with `enable-image-volumes=false`. Verifies OCI volume attachment is skipped, `AgentPlugin.status.phase` updates to `Degraded`, condition `Ready` sets `Reason: ImageVolumeUnsupported`, and operator logs `skipping plugin OCI image volume mount`.
10. **Orphaned `agentRef` Reporting**: Creates an `AgentPlugin` whose `agentRef` names no `PlatformAgent`. Verifies `status.phase` becomes `Degraded` with `Reason: AgentNotFound`, and that the plugin still never reaches `plugins.enabled`.
11. **Image Pull Failure Reporting**: Creates an `AgentPlugin` referencing an image that does not exist, which blocks the agent pod from starting. Verifies `status.phase` becomes `Degraded` with `Reason: ImagePullFailed` and the failing reference in the message, then that the agent recovers once the plugin is removed.
12. **Missing CRD Decoupled Dependency Safeguard**: Temporarily deletes `AgentPlugin` CRD from cluster. Verifies `PlatformAgent` reconciliation succeeds without controller crashes, and verifies reflector error log. Restores CRD per-file **and restarts the operator** — re-applying the CRD alone does not revive the informer, which keeps retrying with growing backoff, so later steps would race that recovery. Runs late because deleting the CRD stops the operator watching `AgentPlugin` until it restarts.
13. **Duplicate Plugin Name Safeguard**: Creates an `AgentPlugin` named `sessionstore`, which normalizes onto the built-in `session_store`. Verifies `status.phase` becomes `Degraded` with condition `Reason: DuplicatePluginName` and that the operator logs the collision.
14. **Profile Targeting and Execution Tuning**: Creates an `AgentPlugin` with `spec.targetProfile`. Verifies the plugin is enabled in that profile's `profile-<name>.overlay.yaml` and **not** on the default profile, that the OCI volume mounts at `/opt/agent-plugins/<name>/<plugin>` with nothing mounted inside the PVC's `profiles/` tree, that the pod has the plugin linked into `profiles/<name>/plugins/<plugin>` with the profile itself still scaffolded (`profile.yaml` and `skills/` present), and that config subtrees are scoped correctly — `platforms` stays with the gateway (adapters are gateway singletons) while `approvals` follows the plugin. Then sets `spec.harness.tuning`, verifies the limits reach the default profile's overlay and both profile overlays — and that the board cap is not pinned pod-wide — removes it again, and verifies the overlays disappear, Hermes' own defaults return, and the board cap falls back to the one committed in `agents/chat/config.yaml`. Also asserts the CRD rejects `targetProfile: default`, and that withdrawing the plugin prunes the link and drops it from the profile's `plugins.enabled` — a stale link or a stale enablement entry each break the next start on their own.
15. **Unknown `targetProfile`**: Creates an `AgentPlugin` naming a profile that does not exist. Verifies the operator still emits the overlay, the image stages at `/opt/agent-plugins/<name>/<plugin>`, **no** directory appears at `profiles/<name>` on the PVC — one would suppress the scaffold of any profile later given that name — and the entrypoint warns that the overlay names a missing profile.
16. **Cluster-Profile Targeting**: Targets one existing `cluster-*` profile while `tuning.cluster` is set. Verifies both `profileclass-cluster.overlay.yaml` and `profile-<cluster>.overlay.yaml` are emitted and **both** merged into that profile's `config.yaml`, that the plugin is linked into it, and that the runtime `cluster_identity` stamp survives the merge. Then withdraws the plugin and verifies the class-wide tuning stays applied. Prefers a real cluster profile — briefly enabling the example plugin on that Cluster Agent — and stands up a throwaway `cluster-e2eprobe` profile (removed afterwards) when the deployment has none, rather than skipping: skipping would let this regression through on exactly the clean environments CI runs on.
17. **Stale Plugin Directory Self-Heal**: Replaces the plugin link with an empty directory — the shape every deployment upgrading from the in-PVC mount layout has on disk — restarts the pod, and verifies startup restores the link and that it resolves to the mounted plugin.

---

## 🔒 Credential Isolation E2E (`tests/e2e/operator/credential_isolation_e2e_test.py`)

Asserts, at runtime, the credential boundary between the sandbox and the credential broker. The operator's Go tests assert the _inputs_ — `shareProcessNamespace` left unset, split UIDs, broker-private volumes — and a rendered manifest cannot tell you what the kernel actually shows one container about another. This closes that gap and there is no way to close it without a cluster, so there is no CI job for it.

Unlike the AgentPlugins suite it is **read-only and safe to run against a cluster you care about**: no image build, no registry, no writes. It runs `id` and reads `/proc` and `/proc/self/mounts`.

### Prerequisites

- Active `kubectl` context with `exec` permission on the agent namespace.
- A running agent Pod in the **sidecar layout**, which is what the operator renders today. If the broker is ever in a Pod of its own the suite exits without running, rather than passing vacuously — the containers are then in different Pods and there is no shared process namespace to check.

### Environment variables

| Variable       | Required | Purpose                                    |
| -------------- | -------- | ------------------------------------------ |
| `KUBE_CONTEXT` | Yes      | `kubectl` context of the target cluster.   |
| `NAMESPACE`    | Yes      | Namespace holding the `PlatformAgent` Pod. |

### Execution

```bash
KUBE_CONTEXT="gke_my-project_europe-west1_ka-dev-mgmt" \
NAMESPACE="kubeagents-system" \
python3 tests/e2e/operator/credential_isolation_e2e_test.py
```

### What it checks

1. **PID 1 is each container's own entrypoint**, not the Pod infra (`pause`) process — the first observable consequence of `shareProcessNamespace: true`.
2. **The broker is actually running the credential proxy.** Checked _before_ the scan below, because a crash-looping broker and an invisible broker produce the same empty result.
3. **No broker process appears in the agent container's `/proc`** — the second observable consequence, asserted separately because a CRI quirk could mask either one alone. Paired with a positive control requiring the scan to have returned _some_ process, so an empty result cannot be mistaken for isolation.
4. **No `/proc/<pid>/environ` the agent can read carries the broker's environment.** The consequence stated directly rather than inferred, so it catches a credential-holding process this file does not know to look for by name. Also paired with a positive control: the identical command is run in the broker container, where a hit is guaranteed, and is required to find one. This is the only check whose external tool (`grep`) is exercised nowhere else, and an absence-of-output assertion cannot otherwise tell "nothing to find" from "nothing ran".
5. **The two containers run as different users** (10000 and 10001). Reading another process's environ needs ptrace-level access, which the kernel grants on a UID match; splitting the UIDs removes that even if the shared namespace ever returns.
6. **The agent mounts neither the broker's HOME nor its backend socket directory.** Asserted against the agent's mount table, not against whether the directory exists: an empty directory of the same name passes either way, and it is the mount that decides whether the bytes are shared. `kubectl` reads `$HOME/.kube/kuberc` with no flag at all and a kuberc can set `as`, so a writable broker HOME is caller-supplied impersonation through a file.

---

## 🤖 Running in GitHub Actions (CI)

Three workflows run this suite. [`.github/workflows/e2e-gchat-test.yml`](../../.github/workflows/e2e-gchat-test.yml) and [`.github/workflows/e2e-manual-runner.yml`](../../.github/workflows/e2e-manual-runner.yml) are triggered by hand via `workflow_dispatch` (or via GitHub CLI / Web UI). The third is [`.github/workflows/e2e-run.yml`](../../.github/workflows/e2e-run.yml), which runs nothing on its own — it is the reusable job that calls `scripts/release/execute_e2e_tests.sh`, and both pipelines delegate to it. [`.github/workflows/rc-release-pipeline.yml`](../../.github/workflows/rc-release-pipeline.yml) calls it unattended and `step-4-tag-validated` depends on the result, so a break here stops the release-candidate tag within three hours. That pipeline carries no schedule of its own: [`.github/workflows/rc-scheduler.yml`](../../.github/workflows/rc-scheduler.yml) runs three-hourly and dispatches it only when there is a new candidate, so a tick with nothing to do leaves no run behind to be mistaken for a passing one. [`.github/workflows/nightly-pipeline.yml`](../../.github/workflows/nightly-pipeline.yml) calls it too, gating the staging promotion — that one is `workflow_dispatch`-only for now, so it runs when somebody starts it.

Each caller splits its suites into a gate and tolerated coverage. `blocking_suite` fails the run; `optional_suites`, a comma-separated list run by `scripts/release/run_optional_e2e_suites.sh`, reports and does not. The split is evidence rather than preference: a suite gates a promotion once it has passed on a real cluster, and runs as optional coverage until then. The RC pipeline gates on `gchat` and tolerates `rc`; the nightly pipeline gates on `rc` — the subset the RC pipeline exercises every three hours — and tolerates `agent-plugin`, `gchat` and `stockout-full`, which between them cover every file the `nightly` suite adds. Per-suite filters such as `STOCKOUT_SCENARIOS` are declared in the suite's `env_vars` in `e2e_config.yaml`, not passed from the workflow, because one workflow input cannot carry a different value for each suite in a list.

### Triggering Workflow via GitHub CLI (`gh`):

```bash
gh workflow run .github/workflows/e2e-gchat-test.yml \
  -f gcp_project_id="kube-agents-autopush"
```

> **Note**: If `chat_space_id` is omitted in `gh workflow run`, the workflow automatically uses the `E2E_CHAT_SPACE_ID` GitHub Repository Secret.

### Authentication in CI:

1. **Service Account via WIF**: Authenticates `github-actions-e2e@kube-agents-autopush.iam.gserviceaccount.com` for keyless message creation and Pub/Sub publishing.
2. **OTA User Credentials & Target Space via Secrets**: Uses `E2E_CHAT_REFRESH_TOKEN`, `E2E_CHAT_CLIENT_ID`, `E2E_CHAT_CLIENT_SECRET`, and `E2E_CHAT_SPACE_ID` secrets for space resolution and response polling.
