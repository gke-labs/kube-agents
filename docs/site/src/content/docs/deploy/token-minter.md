---
title: Token minter (Minty)
description: The in-cluster broker that mints short-lived GitHub App installation tokens without any long-lived secret on disk.
sidebar:
  order: 3
---

Minty is the GitHub Token Minter — an in-cluster service that mints short-lived (1-hour) GitHub App installation tokens on demand for the Platform Agent's `submit-suggestion`, `fleet-audit`, and `github-issue-resolver` skills. The GitHub App's private key never leaves GCP KMS.

GCP half (minter GSA, Workload Identity binding, import-only KMS signing key): [`terraform/modules/github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter).
Kubernetes half (Deployment, Service, NetworkPolicy, KSA, rule ConfigMap, `github-app-credentials` Secret): the chart's `githubMinter.*` values; the dev copy is `make -C k8s-operator deploy-github`.
Full README: [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md).

## How it works

1. **Request.** The agent calls Minty via HTTP, specifying the target org and repo. The request is authenticated with the agent's Google Service Account OIDC token (via Workload Identity).
2. **Verification.** Minty checks the request against local rules ([`configmap.yaml.template`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)). It extracts the `email` claim from the OIDC token and verifies against `assertion.email`.
3. **KMS signing.** Minty asks GCP KMS to sign a JWT with the GitHub App's private key. The raw key material never touches Minty.
4. **Token exchange.** Minty exchanges the signed JWT with GitHub for a 1-hour installation access token.
5. **Delivery.** Minty returns the token to the agent, which uses it for `git push`, PR-open, and issue operations — the Platform Agent publishes audit findings as GitHub issues and reads `/remediate` comments on them, and `github-issue-resolver` triages the rest.

## The GitOps repo must be owned by an organization

Minty resolves the installation with `GET /orgs/{org}/installation` ([`pkg/server/source/github.go`](https://github.com/abcxyz/github-token-minter/blob/main/pkg/server/source/github.go), `app.InstallationForOrg`). GitHub serves personal accounts from `/users/{user}/installation` instead, and Minty has no fallback to it, so a repo owned by a personal account cannot be used — every mint fails with `errors retrieving GitHub installation: … 404` no matter how the App is configured.

Create the repo under an organization, or transfer an existing one into it. A free organization is enough. Note that GitHub shares one namespace across users and organizations, so you cannot create an organization whose name matches your own username.

## Setup checklist

### GitHub App

1. Create a new GitHub App, owned either by the organization or by your personal account.
2. Assign permissions: `Contents: Read & write`, `Pull requests: Read & write`, `Issues: Read & write`.
3. Note the **App ID**.
4. Generate and download a **private key** (`.pem` file).
5. Install the App on the target GitOps repo.

A personal-account App is created as "Only on this account", which cannot install onto an organization. Either own the App from the organization (it stays private to it), or flip the personal one to "Any account" under **Advanced → Make public** first. Public here means installable by others, not that anyone gains access — an install is still explicit.

### Install variables

`./install.sh` collects these in its GitOps interview and saves them to `k8s-operator/scripts/vars.sh`:

- `GITHUB_APP_ID` — numeric App ID.
- `GITHUB_ORG` — the organization hosting the repo. A username will not work; see above.
- `GITHUB_REPO` — repo name.
- `GITHUB_PEM_PATH` — absolute path to the `.pem` file. If provided, the installer auto-imports it to KMS via the Minty CLI. If omitted, deployment proceeds but Minty fails readiness until the key is imported manually.

## Why KMS instead of a Kubernetes Secret

- **No raw key material on disk.** KMS holds the key; Minty never sees it.
- **Auditable.** Every sign operation logs to Cloud Audit Logs.
- **Rotatable without touching the cluster's key material.** Import a new key version to KMS; nothing on the node ever held the old one. Rotation is not free of a redeploy, though — the Deployment names one `cryptoKeyVersions/<n>`, not the key, so a new version also needs `githubMinter.kms.keyVersion` bumped and the chart re-applied.

The Minty CLI handles the KMS import — it deals with PKCS#1 to PKCS#8 conversion, provisions the KMS Import Job, and does RSA-OAEP wrapping automatically. The installer shallow-clones the Minty repo at `v2.7.1` and runs `go run ./cmd/minty tools import-pk` from the tree (requires `git` and `go` on the install host; the `go run <module>@v2.7.1` form does not resolve because upstream's go.mod lacks the `/v2` suffix its v2 tags require). The import is deliberately not a Terraform resource, so the PEM never enters Terraform state.

When that path is unavailable — no Go toolchain, or a host security agent that kills the compiler — `gcloud` can do the same import in four commands. The [integration README](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) has the recipe; re-running the installer afterwards skips the Go step entirely, because it only imports when the key has no enabled version.

## GSA-only auth

Native Kubernetes SA tokens don't carry the `repository` claim Minty's default validator expects, so Minty routes through **Google Service Account (GSA)** tokens instead. When the token issuer is `https://accounts.google.com`, Minty bypasses the `repository` claim check and validates on `assertion.email`, deriving the target repo from the POST body.

That's why the install pre-provisions GSAs and Workload Identity bindings (the [`kube-agents-iam`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/kube-agents-iam) and [`github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter) modules) — Minty won't accept KSA tokens.

## Deployment details

Names and values baked into the deployment templates ([`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)):

- **Kubernetes Service / Deployment:** `github-token-minter` (namespace `kubeagents-system`), listening on port `8080` with a `/version` health endpoint.
- **Image:** substituted from `GITHUB_MINTER_IMAGE`, run as `/minty server run`. The upstream reference and pin live in `images.json`; see the [Docker images](docker-images.md) inventory.
- **Kubernetes SA:** `kubeagents-github-minter`, Workload-Identity-bound to GSA `kubeagents-github-minter-gsa` (which holds `roles/cloudkms.signerVerifier` on the KMS key).
- **Scope:** the ConfigMap rule exposes a `platform-agent-scope` scope granting `contents: write`, `pull_requests: write`, and `issues: write`; requests must pass this in the `scope` field.
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

A 200 response whose body is the short-lived GitHub installation token means the pipeline works end-to-end.

## Where to go next

- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `submit-suggestion` skill that uses Minty.
- [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) — full Minty install detail.
