---
title: Token minter (Minty)
description: The in-cluster broker that mints short-lived GitHub App installation tokens without any long-lived secret on disk.
sidebar:
  order: 3
---

Minty is the GitHub Token Minter — an in-cluster service that mints short-lived (1-hour) repository-scoped GitHub App installation tokens on demand for the Platform Agent's `submit-suggestion`, `fleet-audit`, and `github-issue-resolver` skills, and read-only tokens for the credential broker's own clones of the repositories registered as context. The GitHub App's private key never leaves GCP KMS.

GCP half (minter GSA, Workload Identity binding, import-only KMS signing key): [`terraform/modules/github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter).
Kubernetes half (Deployment, Service, NetworkPolicy, KSA, rule ConfigMap, `github-app-credentials` Secret): the chart's `githubMinter.*` values; the dev copy is `make -C k8s-operator deploy-github`.
Overlay reference (the kustomize manifests behind that dev copy, and Minty's GSA-token limitations): [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md).

## How it works

1. **Request.** The agent calls Minty via HTTP, specifying the target org and repo. The request is authenticated with the agent's Google Service Account OIDC token (via Workload Identity).
2. **Verification.** Minty checks the request against local rules ([`configmap.yaml.template`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)). It extracts the `email` claim from the OIDC token and verifies against `assertion.email`.
3. **KMS signing.** Minty asks GCP KMS to sign a JWT with the GitHub App's private key. The raw key material never touches Minty.
4. **Token exchange.** Minty exchanges the signed JWT with GitHub for a 1-hour repository-scoped installation access token.
5. **Delivery.** Minty returns the token to the agent, which uses it for `git push`, PR-open, and issue operations — the Platform Agent publishes audit findings as GitHub issues and reads `/remediate` comments on them, and `github-issue-resolver` triages the rest.

## The GitOps repo must be owned by an organization

Minty resolves the installation with `GET /orgs/{org}/installation` ([`pkg/server/source/github.go`](https://github.com/abcxyz/github-token-minter/blob/main/pkg/server/source/github.go), `app.InstallationForOrg`). GitHub serves personal accounts from `/users/{user}/installation` instead, and Minty has no fallback to it, so a repo owned by a personal account cannot be used — every mint fails with `errors retrieving GitHub installation: … 404` no matter how the App is configured.

Create the repo under an organization, or transfer an existing one into it. A free organization is enough. Note that GitHub shares one namespace across users and organizations, so you cannot create an organization whose name matches your own username.

## Single-organization scoping boundary

Minty's rule ConfigMap is mounted in-container at `/etc/minty/<GITHUB_ORG>`. A single PlatformAgent instance and its associated Minty deployment manage multiple repositories within the primary GitHub Organization where the GitHub App is installed. Additional repositories registered under `managed_repos` in the `gitops-state` ConfigMap must belong to this primary organization, and so must the repositories registered under its `context_repos` key.

## Read-only tokens for context repositories

A repository registered under `context_repos` — a Terraform repository an audit reads for declared intent — gets a read-only grant, not the write one. The operator renders a policy per same-organization context repository carrying the `platform-agent-read-scope` scope alone, which grants `contents: read` and nothing else. The credential broker requests a token from that scope for its own content-mode clone of the repository, presents it to that one `git` process, and installs it nowhere: the agent sandbox never holds it, and the broker's write gate still refuses a `commit` or `push` to a context repository. A repository registered under both keys is managed and keeps its write policy.

The GitHub App must be installed on each context repository, as it must on each managed one; a private repository the App is not installed on fails to clone as before. A cross-organization entry is skipped with an operator log line. A rule ConfigMap whose `default.yaml` predates the read scope renders no context policies — upgrade the chart or the kustomize template to get it.

## Setup and Key Provisioning

Minty requires a GitHub App private key imported into Google Cloud KMS as an asymmetric signing key (`rsa-sign-pkcs1-2048-sha256` or `rsa-sign-pkcs1-4096-sha512`). You can either let the `kube-agents` installer import the key automatically during initial setup, or pre-provision it Ahead-Of-Time (AOT).

### Prerequisites Checklist

Before enabling the token minter, ensure you have:

1. **GitHub Organization:** A GitHub repo **owned by an organization** (or transferred into one). Minty queries `/orgs/{org}/installation`, so personal accounts fail token minting with HTTP 404. A free organization is sufficient.
2. **GitOps Repository:** The repository the agent opens pull requests against (e.g. `gke-fleet-iac`).
3. **GitHub App:**
   - Created in GitHub (`Settings -> Developer settings -> GitHub Apps`).
   - Repository permissions: `Contents: Read & write`, `Pull requests: Read & write`, `Issues: Read & write`.
   - Installed onto the target organization, the GitOps repository, and every repository registered under `context_repos`.
   - If created under a personal user account, "Where can this GitHub App be installed?" must be set to "Any account (Public)".
4. **App ID:** The numeric App ID from the GitHub App settings page.
5. **Private Key (`.pem`):** Generated and downloaded from the GitHub App settings page (needed for initial Cloud KMS import).
6. **Host Requirements:** `go` 1.21+ on the host machine running the import — `minty tools import-pk` builds the Minty CLI, which asks for Go 1.24 and relies on 1.21 onwards fetching that toolchain on demand — and `gcloud` authenticated with Cloud KMS admin permissions on your GCP project.

### Path 1: Automated Import via `install.sh`

During initial installation, `install.sh` can create the Cloud KMS keyring/key and import the GitHub App private key automatically using the Minty CLI:

```bash
./install.sh --non-interactive \
  --project-id="YOUR_GCP_PROJECT_ID" \
  --cluster-name="platform-agent-host" \
  --region="us-central1" \
  --gitops-org="YOUR_GITHUB_ORG" \
  --gitops-repo="YOUR_GITOPS_REPO" \
  --github-app-id="YOUR_GITHUB_APP_ID" \
  --github-pem-path="/path/to/app-private-key.pem"
```

In interactive mode, `install.sh` prompts for the path to the `.pem` file if the Cloud KMS key does not yet hold an `ENABLED` version.

> [!TIP]
> **Delete the `.pem` after import:** Once `install.sh` successfully imports the key into Cloud KMS, delete the local `.pem` file. Cloud KMS keys cannot be destroyed or deleted in GCP. Subsequent runs, re-installations, and upgrades automatically detect the existing `ENABLED` key version and skip the `.pem` import step.

### Path 2: Ahead-Of-Time (AOT) Pre-Provisioned Key (CI/CD & Production)

For automated CI/CD pipelines, release automation, or production environments where runners do not handle raw private keys, pre-provision the Cloud KMS key upfront following the official [GitHub Token Minter documentation](https://github.com/abcxyz/github-token-minter) and the [Google Cloud KMS key import guide](https://cloud.google.com/kms/docs/importing-a-key):

1. **Pre-provision Cloud KMS Key & Import Private Key:**
   Follow the upstream [GitHub Token Minter guide](https://github.com/abcxyz/github-token-minter) and [Google Cloud KMS documentation](https://cloud.google.com/kms/docs/importing-a-key) to configure your Cloud KMS keyring and key, and import your GitHub App's private key. By default, `kube-agents` expects the key ring `github-token-minter-keyring` and key `github-token-minter-key` in your cluster's region (or custom names passed via `--kms-keyring` and `--kms-key`).

2. **Deploy `kube-agents` without `.pem`:**
   Once the key holds an `ENABLED` version in Cloud KMS, invoke `install.sh` without `--github-pem-path`:
   ```bash
   ./install.sh --non-interactive \
     --project-id="YOUR_GCP_PROJECT_ID" \
     --cluster-name="platform-agent-host" \
     --region="us-central1" \
     --gitops-org="YOUR_GITHUB_ORG" \
     --gitops-repo="YOUR_GITOPS_REPO" \
     --github-app-id="YOUR_GITHUB_APP_ID"
   ```

### Install variables

The deployment requires the following variables in `install.env` or via CLI flags:

- `GITHUB_APP_ID` (`--github-app-id`) — numeric App ID.
- `GITOPS_ORG` (`--gitops-org`) — the organization hosting the GitOps repository.
- `GITOPS_REPO` (`--gitops-repo`) — GitOps repository name (default `gke-fleet-iac`).
- `GITHUB_PEM_PATH` (`--github-pem-path`) — path to `.pem` (Path 1 only; omitted when key is pre-provisioned).
- `KMS_KEYRING` (`--kms-keyring`) — Cloud KMS keyring name (default `github-token-minter-keyring`).
- `KMS_KEY` (`--kms-key`) — Cloud KMS key name (default `github-token-minter-key`).

  `GITOPS_ORG` and `GITOPS_REPO` were previously named `GITHUB_ORG` / `GITHUB_REPO`, which still work for one release with a
  deprecation warning.

## Why KMS instead of a Kubernetes Secret

- **No raw key material on disk.** KMS holds the key; Minty never sees it.
- **Auditable.** Every sign operation logs to Cloud Audit Logs.
- **Rotatable without touching the cluster's key material.** Import a new key version to KMS; nothing on the node ever held the old one. Rotation is not free of a redeploy, though — the Deployment names one `cryptoKeyVersions/<n>`, not the key, so a new version also needs `githubMinter.kms.keyVersion` bumped and the chart re-applied.

In the Ahead-Of-Time (AOT) model, the private key is imported into Cloud KMS upfront following the upstream documentation ([`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter)). Because the key material lives permanently in Cloud KMS, `kube-agents` manifests and CI pipelines never handle or stage private keys.

## GSA-only auth

Native Kubernetes SA tokens don't carry the `repository` claim Minty's default validator expects, so Minty routes through **Google Service Account (GSA)** tokens instead. When the token issuer is `https://accounts.google.com`, Minty bypasses the `repository` claim check and validates on `assertion.email`, deriving the target repo from the POST body.

That's why the install pre-provisions GSAs and Workload Identity bindings (the [`kube-agents-iam`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/kube-agents-iam) and [`github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter) modules) — Minty won't accept KSA tokens.

## Deployment details

Names and values baked into the deployment templates ([`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)):

- **Kubernetes Service / Deployment:** `github-token-minter` (namespace `kubeagents-system`), listening on port `8080` with a `/version` health endpoint.
- **Image:** substituted from `GITHUB_MINTER_IMAGE`, run as `/minty server run`. The upstream reference and pin live in `images.json`; see the [Docker images](docker-images.md) inventory.
- **Kubernetes SA:** `kubeagents-github-minter`, Workload-Identity-bound to GSA `kubeagents-github-minter-gsa` (which holds `roles/cloudkms.signerVerifier` on the KMS key).
- **Scopes:** the ConfigMap rule exposes two. `platform-agent-scope` grants `contents: write`, `pull_requests: write`, and `issues: write`, and is what the agent's managed repositories ride. `platform-agent-read-scope` grants `contents: read` alone, and is what the credential broker requests for its clone of a context repository. A request names one in its `scope` field.
- The App ID is injected from the `github-app-credentials` Secret, and the KMS key reference (`projects/.../cryptoKeyVersions/<n>`) points at the configured key version (the chart's `githubMinter.kms.keyVersion`), which must be ENABLED — i.e. imported — before the Deployment passes readiness.

## Manual testing

```bash
kubectl run debug-box --rm -it \
  --image=curlimages/curl \
  --namespace=kubeagents-system \
  --serviceaccount=kubeagents-platform-agent \
  --labels="app=platform-agent" \
  -- sh
```

The `app=platform-agent` label is required: Minty's `NetworkPolicy` only accepts ingress from pods carrying it.

From inside the pod (the OIDC `audience` must match the Minty service URL, and the token is passed in the `X-OIDC-Token` header — not `Authorization`):

```sh
AUDIENCE="http://github-token-minter.kubeagents-system.svc.cluster.local:8080"
OIDC_TOKEN=$(curl -s -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=${AUDIENCE}&format=full")

curl -i -X POST http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token \
  -H "Content-Type: application/json" \
  -H "X-OIDC-Token: $OIDC_TOKEN" \
  -d '{"org_name":"<org>","repositories":["<repo>"],"scope":"platform-agent-scope"}'
```

A 200 response whose body is the short-lived, repository-scoped GitHub installation token means the pipeline works end-to-end. Put `platform-agent-read-scope` in `scope` to check the read-only grant: the token returned can clone the repository and nothing more.

## Where to go next

- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `submit-suggestion` skill that uses Minty.
- [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) — the kustomize overlay's reference notes, including Minty's GSA-token limitations.
