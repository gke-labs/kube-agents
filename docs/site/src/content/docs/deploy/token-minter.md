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
2. **Verification.** Minty checks the request against local rules ([`configmap.yaml`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github)). It extracts the `email` claim from the OIDC token and verifies against `assertion.email`.
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

Add to `k8s-operator/scripts/vars.sh` (or answer the prompts when `provision_10_*` runs):

- `GITHUB_APP_ID` — numeric App ID.
- `GITHUB_ORG` — org or user hosting the repo.
- `GITHUB_REPO` — repo name.
- `GITHUB_PEM_PATH` — absolute path to the `.pem` file. If provided, the provisioner auto-imports it to KMS via the Minty CLI. If omitted, deployment proceeds but Minty fails readiness until the key is imported manually.

## Why KMS instead of a Kubernetes Secret

- **No raw key material on disk.** KMS holds the key; Minty never sees it.
- **Auditable.** Every sign operation logs to Cloud Audit Logs.
- **Rotatable without redeploy.** Import a new key version to KMS; Minty picks it up.

The Minty CLI (`minty tools import-pk`) handles the KMS import — it deals with PKCS#1 to PKCS#8 conversion and RSA-OAEP wrapping automatically. Manual import via `gcloud kms keys versions import` would require you to do that yourself.

## GSA-only auth

Native Kubernetes SA tokens don't carry the `repository` claim Minty's default validator expects, so Minty routes through **Google Service Account (GSA)** tokens instead. When the token issuer is `https://accounts.google.com`, Minty bypasses the `repository` claim check and validates on `assertion.email`, deriving the target repo from the POST body.

That's why the provisioner (`provision_04_gcp_iam.sh`) pre-provisions GSAs and Workload Identity bindings — Minty won't accept KSA tokens.

## Manual testing

```bash
kubectl run debug-box --rm -it \
  --image=curlimages/curl \
  --namespace=kubeagents-system \
  --serviceaccount=kubeagents-platform-agent \
  -- sh
```

From inside the pod:

```sh
TOKEN=$(curl -s "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=minty" \
  -H "Metadata-Flavor: Google")
curl -X POST http://minty.kubeagents-system:8080/token \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"<org>","repo":"<repo>"}'
```

A 200 response with a `token` field means the pipeline works end-to-end.

## Where to go next

- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `submit-suggestion` skill that uses Minty.
- [`k8s-operator/config/integrations/github/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/github/README.md) — full Minty install detail.
