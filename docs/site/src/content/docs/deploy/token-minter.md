---
title: Token minter (Minty)
description: The in-cluster broker that mints short-lived GitHub App installation tokens without any long-lived secret on disk.
sidebar:
  order: 3
---

Minty is the GitHub Token Minter — an in-cluster service that mints short-lived (1-hour) GitHub App installation tokens on demand for the Platform Agent's `submit-suggestion` and `github-issue-resolver` skills. The GitHub App's private key never leaves GCP KMS.

Provisioner: [`provision_10_deploy_github_minter.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/provision_10_deploy_github_minter.sh).
Full README: [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md).

## How it works

1. **Request.** The agent calls Minty via HTTP, specifying the target org and repo. The request is authenticated with the agent's Google Service Account OIDC token (via Workload Identity).
2. **Verification.** Minty checks the request against local rules ([`configmap.yaml.template`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)). It extracts the `email` claim from the OIDC token and verifies against `assertion.email`.
3. **KMS signing.** Minty asks GCP KMS to sign a JWT with the GitHub App's private key. The raw key material never touches Minty.
4. **Token exchange.** Minty exchanges the signed JWT with GitHub for a 1-hour installation access token.
5. **Delivery.** Minty returns the token to the agent, which uses it for `git push` and PR-open operations.

## Setup checklist

### GitHub App

1. Create a new GitHub App in your organization (or personal account).
2. Assign permissions: `Contents: Read & write`, `Pull requests: Read & write`.
3. Note the **App ID**.
4. Generate and download a **private key** (`.pem` file).
5. Install the App on the target GitOps repo.

### Provisioning variables

Add to `k8s-operator/scripts/vars.sh` (or answer the prompts when `provision_04_gcp_iam.sh` runs):

- `GITHUB_APP_ID` — numeric App ID.
- `GITHUB_ORG` — org or user hosting the repo.
- `GITHUB_REPO` — repo name.
- `GITHUB_PEM_PATH` — absolute path to the `.pem` file. If provided, the provisioner auto-imports it to KMS via the Minty CLI. If omitted, deployment proceeds but Minty fails readiness until the key is imported manually.

## Why KMS instead of a Kubernetes Secret

- **No raw key material on disk.** KMS holds the key; Minty never sees it.
- **Auditable.** Every sign operation logs to Cloud Audit Logs.
- **Rotatable without redeploy.** Import a new key version to KMS; Minty picks it up.

The Minty CLI handles the KMS import — it deals with PKCS#1 to PKCS#8 conversion, provisions the KMS Import Job, and does RSA-OAEP wrapping automatically. The provisioner clones [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter) at tag `v2.7.1` and runs `go run ./cmd/minty tools import-pk` (requires `go` on the provisioning host). Manual import via `gcloud kms keys versions import` would require you to do all of that yourself.

## GSA-only auth

Native Kubernetes SA tokens don't carry the `repository` claim Minty's default validator expects, so Minty routes through **Google Service Account (GSA)** tokens instead. When the token issuer is `https://accounts.google.com`, Minty bypasses the `repository` claim check and validates on `assertion.email`, deriving the target repo from the POST body.

That's why the provisioner (`provision_04_gcp_iam.sh`) pre-provisions GSAs and Workload Identity bindings — Minty won't accept KSA tokens.

## Deployment details

Names and values baked into the deployment templates ([`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)):

- **Kubernetes Service / Deployment:** `github-token-minter` (namespace `kubeagents-system`), listening on port `8080` with a `/version` health endpoint.
- **Image:** `us-docker.pkg.dev/abcxyz-artifacts/docker-images/github-token-minter-server:v2.7.1-amd64`, run as `/minty server run`.
- **Kubernetes SA:** `kubeagents-github-minter`, Workload-Identity-bound to GSA `kubeagents-github-minter-gsa` (which holds `roles/cloudkms.signerVerifier` on the KMS key).
- **Scope:** the ConfigMap rule exposes a `platform-agent-scope` scope granting `contents: write` and `pull_requests: write`; requests must pass this in the `scope` field.
- The App ID is injected from the `github-app-credentials` Secret, and the KMS key reference (`projects/.../cryptoKeyVersions/<n>`) is resolved dynamically to the latest enabled version at provision time.

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
