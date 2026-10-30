---
title: Token minter (Minty)
description: The in-cluster broker that mints short-lived GitHub App installation tokens without any long-lived secret on disk.
sidebar:
  order: 3
---

:::note
Only an install whose GitOps repository is on GitHub uses Minty. A GitLab or Gitea install has no GitHub App, no token minter and no Cloud KMS key: the credential broker holds the forge's access token in a Kubernetes Secret. See [GitLab as the GitOps forge](/kube-agents/install/gitlab/) and [Gitea as the GitOps forge](/kube-agents/install/gitea/).
:::

Minty is the GitHub Token Minter — an in-cluster service that mints short-lived (1-hour) repository-scoped GitHub App installation tokens on demand for the Platform Agent's `submit-suggestion`, `fleet-audit`, and `github-issue-resolver` skills, and read-only tokens for the credential broker's own clones of the repositories registered as context. The GitHub App's private key never leaves GCP KMS.

GCP half (minter GSA, Workload Identity binding, import-only KMS signing key): [`terraform/modules/github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter).
Kubernetes half (Deployment, Service, NetworkPolicy, KSA, rule ConfigMap, `github-app-credentials` Secret): the chart's `githubMinter.*` values; the dev copy is `make -C k8s-operator deploy-github`.
Overlay reference (the kustomize manifests behind that dev copy, and Minty's GSA-token limitations): [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md).

## How it works

1. **Request.** The agent calls Minty via HTTP, specifying the target org and repo. The request is authenticated with the agent's Google Service Account OIDC token (via Workload Identity).
2. **Verification.** Minty checks the request against local rules ([`configmap.yaml.template`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)). It extracts the `email` claim from the OIDC token and verifies against `assertion.email`.
3. **KMS signing.** Minty asks GCP KMS to sign a JWT with the GitHub App's private key. The raw key material never touches Minty.
4. **Token exchange.** Minty exchanges the signed JWT with GitHub for a 1-hour repository-scoped installation access token.
5. **Delivery.** Minty returns the token to the credential broker, which spends it on the agent's behalf for `git push`, PR-open, and issue operations — the Platform Agent publishes audit findings as GitHub issues and reads `/remediate` comments on them, and `github-issue-resolver` triages the rest. The agent asks for the operation, never for the token.

## The GitOps repo must be owned by an organization

Minty resolves the installation with `GET /orgs/{org}/installation` ([`pkg/server/source/github.go`](https://github.com/abcxyz/github-token-minter/blob/main/pkg/server/source/github.go), `app.InstallationForOrg`). GitHub serves personal accounts from `/users/{user}/installation` instead, and Minty has no fallback to it, so a repo owned by a personal account cannot be used — every mint fails with `errors retrieving GitHub installation: … 404` no matter how the App is configured.

Create the repo under an organization, or transfer an existing one into it. A free organization is enough. Note that GitHub shares one namespace across users and organizations, so you cannot create an organization whose name matches your own username.

## Single-organization scoping boundary

Minty's rule ConfigMap is mounted in-container at `/etc/minty/<GITHUB_ORG>`. A single PlatformAgent instance and its associated Minty deployment manage multiple repositories within the primary GitHub Organization where the GitHub App is installed. Additional repositories registered under `managed_repos` in the `gitops-state` ConfigMap must belong to this primary organization, and so must the repositories registered under its `context_repos` key.

## Read-only tokens for context repositories

A repository registered under `context_repos` — a Terraform repository an audit reads for declared intent — gets a read-only grant, not the write one. The operator renders a policy per same-organization context repository carrying the `platform-agent-read-scope` scope alone, which grants `contents: read` and nothing else. The credential broker requests a token from that scope for its own content-mode clone of the repository, presents it to that one `git` process, and installs it nowhere: the agent sandbox never holds it, and the broker's write gate still refuses a `commit` or `push` to a context repository. A repository registered under both keys is managed and keeps its write policy.

The GitHub App must be installed on each context repository, as it must on each managed one; a private repository the App is not installed on fails to clone as before. A cross-organization entry is skipped with an operator log line. A rule ConfigMap whose `default.yaml` predates the read scope renders no context policies — upgrade the chart or the kustomize template to get it.

## GitHub Enterprise Server

When the install's GitHub forge is at a GitHub Enterprise Server host, Minty mints on that instance instead of on github.com. The chart does this from the forge declaration in `platformAgent.integration.forges`; there is no separate `githubMinter.*` value for it. An install serves one GitHub host, github.com or one GitHub Enterprise Server, so one Minty serves it.

- **API URL.** The chart sets `SOURCE_SYSTEM_API_BASE_URL=https://<host>/api/v3` on the Minty container. Minty then calls the instance's REST API, where it calls `api.github.com` on github.com. A github.com install does not get the variable.
- **Private CA.** When the GitHub forge names `caBundleRef`, the chart mounts that key of the Secret into the Minty pod at `/etc/kube-agents/minter-ca/ca.crt` and sets `SSL_CERT_DIR=/etc/kube-agents/minter-ca`. Minty is a Go program that uses the default HTTP transport, so it reads the system roots: the CA bundle in its image, and each certificate file in the directories that `SSL_CERT_DIR` names. Minty trusts the configured CA beside the image's bundle, for all of its outbound calls (the instance, and the Google APIs it calls). The CA is one that you trust and install. It is the same Secret that the credential broker reads, in the release namespace. The mount is optional: until the Secret exists, Minty starts, and its calls to the instance fail on TLS. Minty reads `SSL_CERT_DIR` once, when it starts, so after you create or rotate the Secret, restart it: `kubectl rollout restart deployment/github-token-minter -n kubeagents-system`. The credential broker reads the CA on each call and needs no restart; the difference is intended, because the broker reads the CA file on each call, while Go loads its system roots once per process.
- **Egress.** Minty's NetworkPolicy (`githubMinter.networkPolicy`, on by default) allows TCP port 443 to any address, private ranges included. So Minty reaches an instance on a private network on port 443, if the cluster's network can route to it. An instance on another port is not supported. A firewall or a VPC route outside the cluster is yours to open.
- **GitHub App.** Create the App on the instance, not on github.com: an App and its installation belong to one GitHub host. Give it the same repository permissions as on github.com (`Contents: Read & write`, `Pull requests: Read & write`, `Issues: Read & write`), and install it on the organization, the GitOps repository and each context repository. The repository must be owned by an organization, for the reason above.
- **Key import.** `minty tools import-pk` imports the App's private key into the Cloud KMS key, and it calls only Cloud KMS, so the import is the same for an App on a GitHub Enterprise Server. `install.sh` runs it from `--github-pem-path`, as on github.com, into a key that has no `ENABLED` version yet. On the Terraform + Helm method, import the key before the apply, because the apply waits for the minter, which is not Ready without it. With the composition's default names and the minty version `install.sh` pins:

  ```bash
  git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git ~/minty
  (cd ~/minty && go run ./cmd/minty tools import-pk -project-id=PROJECT_ID -location=REGION \
    -key-ring=github-token-minter-keyring -key=github-token-minter-key -private-key=@/path/to/app.pem)
  ```

  The key ring and the import-only key must exist first; the `gcloud kms` commands that create them are in INSTALL.md. On a new key, the import makes key version `1`, the version the chart uses (`githubMinter.kms.keyVersion`, default `"1"`). When the key already exists (a switch from github.com, or a reinstall in the same project), skip the create commands, read the new version with `gcloud kms keys versions list --key=github-token-minter-key --keyring=github-token-minter-keyring --location=REGION --project=PROJECT_ID --filter=state=ENABLED --format='value(name)'` (the last path segment of the newest version is `N`), and set `githubMinter = { kms = { keyVersion = "N" } }` in `extra_helm_values`. With `install.sh`, give a new key name with `--kms-key` instead, so that the import makes version `1` of a new key.

- **Readiness.** Minty's readiness probe asks its own `/version` route, and minty `v2.7.1` calls the instance only when it mints. So the pod becomes Ready without the CA Secret or a route to the instance; a mint then fails until both are in place.
- **Extra repositories.** Register another repository on the instance by its full URL, `https://<host>/owner/name`, with `"type": "github"`. A bare `owner/name` or a github.com URL is skipped on a GitHub Enterprise Server install, by the operator's policy sync and by the agent, so it gets no policy.

- **Install.** `install.sh --gitops-host=<host>` installs it, with `--gitops-ca-file` for a private CA; `upgrade.sh` and `uninstall.sh` read the forge from `install.env`. After the apply, `install.sh` writes the CA into the Secret `github-forge-ca` and restarts the minter, which reads its CA when it starts. The Terraform + Helm method sets `gitops_host` and `github_ca_secret_name` in `terraform.tfvars`. See [GitHub Enterprise Server in INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#github-enterprise-server).

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
4. **App ID:** The numeric App ID from the GitHub App settings page (**General**, "App ID", a number such as `1234567`), not the App's name or slug. The installer refuses anything else.
5. **Private Key (`.pem`):** Generated and downloaded from the GitHub App settings page (needed for initial Cloud KMS import).
6. **Host Requirements:** `go` 1.21+ on the host machine running the import — `minty tools import-pk` builds the Minty CLI, which asks for Go 1.24 and relies on 1.21 onwards fetching that toolchain on demand — and `gcloud` authenticated with Cloud KMS admin permissions on your GCP project.

### Path 1: Automated Import via `install.sh`

During initial installation, `install.sh` can create the Cloud KMS keyring/key and import the GitHub App private key automatically using the Minty CLI:

```bash
./install.sh --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
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
     --gcp-project-id="YOUR_GCP_PROJECT_ID" \
     --gke-cluster-name="platform-agent-host" \
     --gcp-region="us-central1" \
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
  --overrides='{"spec":{"serviceAccountName":"kubeagents-platform-agent"}}' \
  --labels="kubeagents.x-k8s.io/has-credential-proxy=true" \
  -- sh
```

The `kubeagents.x-k8s.io/has-credential-proxy=true` label is required: Minty's `NetworkPolicy` only accepts ingress from pods carrying it, the label the operator puts on the credential proxy pod, Minty's one caller.

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
