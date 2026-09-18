# GitHub Token Minter (Minty) Integration

This directory contains the configuration and deployment manifests for integrating the **GitHub Token Minter (Minty)** broker into the cluster. This integration allows agents to securely request short-lived GitHub access tokens without storing long-lived, static credentials, enabling them to safely perform write operations on the Kubernetes infrastructure via GitOps.

- **Upstream Project & Guide:** [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter)
- **Operational Model:** **Ahead-Of-Time (AOT)**. Cloud KMS keyrings, asymmetric signing keys, and the GitHub App private key import are pre-provisioned once upfront as durable cloud infrastructure. Because Cloud KMS keys cannot be destroyed or deleted in GCP, once Version 1 is `ENABLED`, it persists permanently across cluster teardowns and redeployments.

## How It All Works

Minty acts as a secure broker between Google Cloud IAM (Workload Identity) and GitHub. When an agent requires access to a GitHub repository, the following flow occurs:

1. **The Request:** The agent initiates an HTTP request to the Minty service, specifying the target organization and repository. The request is authenticated using the agent's Google Service Account (GSA) OIDC token to cryptographically prove its identity.
2. **The Verification:** Minty evaluates the request against its local rules (provided by `configmap.yaml`). It extracts the `"email"` claim from the OIDC token and verifies it against the `assertion.email` rule. If the agent's email is authorized for the requested repository, the rule evaluates to true.
3. **The Exchange (KMS Signing):** Upon successful authorization, Minty interfaces with Google Cloud Key Management Service (KMS). Minty holds a reference to the GitHub App's private key stored securely in KMS. The private key is never exported or exposed to Minty. Instead, Minty constructs an authentication payload and invokes the KMS API to cryptographically sign it using secure hardware.
4. **The Token Generation:** Armed with the KMS-signed JWT, Minty authenticates with the GitHub API on behalf of the configured GitHub App. GitHub verifies the signature and returns a short-lived installation access token scoped to the target repository.
5. **The Delivery:** Minty returns this short-lived GitHub access token to the agent, which can then utilize it to perform write operations on the Kubernetes infrastructure via GitOps (e.g., by pushing configuration changes or managing Pull Requests).

## The GitHub App

Minty itself does not natively possess access to any GitHub repositories. The **GitHub App** serves as the machine identity within GitHub that holds the necessary permissions.

By installing the GitHub App into a target repository, explicit authorization is granted to that machine identity. Minty's role is strictly to ensure that only authorized internal workloads are permitted to generate tokens on behalf of the App.

### The Target Repository Must Be Organization-Owned

Minty resolves the App installation through `app.InstallationForOrg` ([`pkg/server/source/github.go`](https://github.com/abcxyz/github-token-minter/blob/main/pkg/server/source/github.go) in the upstream `github-token-minter` repository), which calls `GET /orgs/{org}/installation`. GitHub serves personal accounts from a different endpoint, `/users/{user}/installation`, and Minty implements no fallback to it. A repository owned by a personal account therefore fails every mint with:

```
errors retrieving GitHub installation: failed to get access token url for org <name>:
  ... Get "https://api.github.com/orgs/<name>/installation": retryable status code: 404
```

This holds regardless of App ID, key, or installation state, so it is worth ruling out first: a 404 here means the org lookup, whereas a bad or mismatched key returns 401. The tooling checks this for you: `install.sh` validates the answer at the prompt and re-asks, so a bad value is settled before any GCP resource exists, and re-checks before the apply, which also covers an `install.env` edited by hand. It only warns when the lookup itself is inconclusive — an unreachable or rate-limited api.github.com must not block an install that is otherwise fine. Set `SKIP_GITHUB_ORG_CHECK=true` to bypass the check if it is ever wrong about your account; the Minter's own behaviour is unchanged by it.

Create the GitOps repository under an organization, or transfer an existing repository into one — a free organization suffices. GitHub shares a single namespace between users and organizations, so an organization cannot take the same name as your personal account.

### Setting up the GitHub App (AOT)

1. Navigate to your GitHub Organization (or personal settings) -> **Developer Settings** -> **GitHub Apps** -> **New GitHub App** (see [upstream setup guide](https://github.com/abcxyz/github-token-minter#readme)).
2. Assign a name and configure the required repository permissions (e.g., `Contents: Read & write`, `Pull requests: Read & write`, `Issues: Read & write`).
3. Once created, note the numeric **App ID**.
4. Scroll down and click **Generate a private key**. This will download a `.pem` file to your local machine.
5. Navigate to the target repository the agent is intended to manage, go to **Settings** -> **GitHub Apps**, and install the newly created App.

The App may be owned by the organization or by a personal account, but an App created under a personal account defaults to "Only on this account" and cannot be installed onto an organization in that state. Either create it under the organization, where it remains private to that organization, or open the personal App's **Advanced** settings and **Make public** — which makes it installable elsewhere, not accessible to anyone without an explicit installation.

### Provisioning Configuration Variables

When configuring the installer or Terraform (`install.env` or `terraform.tfvars`), set the GitOps repository coordinates:

- `GITHUB_APP_ID`: The unique numeric ID of the GitHub App (found in the App's General Settings).
- `GITOPS_ORG`: The name of the GitHub organization hosting the GitOps repository.
- `GITOPS_REPO`: The name of the target repository the agent will manage.

When deploying manually via the Kustomize Makefile target (`make deploy-github`), export `GITHUB_ORG` and `GITHUB_REPO` directly in the environment and create the `github-app-credentials` Secret in the target namespace first. Both steps, with the commands, are in [`k8s-operator/README.md`](../../../README.md#deploying-github-integration), which documents this path end to end.

## Minty Limitations & GSA Tokens

Minty was originally designed for integration with GitHub Actions, which inherently provides OIDC tokens containing a specific `"repository"` claim. Deploying Minty in GKE introduces specific constraints regarding this validation model:

- **Single-Organization Boundary:** Minty's rule ConfigMap is mounted in-container at `/etc/minty/<GITHUB_ORG>`. A single PlatformAgent instance and its associated Minty deployment manage multiple repositories within the primary GitHub Organization where the GitHub App is installed. Additional repositories registered in the `gitops-state` ConfigMap must belong to this primary organization.
- **KSA Tokens are Unsupported:** Native Kubernetes Service Account (KSA) tokens do not support the injection of arbitrary custom claims such as `"repository"`. Consequently, Minty's default validation engine will reject KSA tokens due to the missing claim.
- **GSA Tokens (The Solution):** To resolve this, Workload Identity is utilized to provide Google Service Account (GSA) OIDC tokens. Minty implements a specific exemption for tokens where the issuer is `https://accounts.google.com`. When processing a Google-issued token, Minty bypasses the `"repository"` claim requirement. Instead, it validates the caller's identity via the `assertion.email` rule and derives the target repository directly from the JSON POST payload.

## Cloud KMS Key Provisioning & Key Import

The GitHub Token Minter requires an asymmetric signing key (`rsa-sign-pkcs1-2048-sha256` or `rsa-sign-pkcs1-4096-sha512`) in Google Cloud KMS with your GitHub App private key imported (Version 1 `ENABLED`).

- **Key Coordinates:** By default, `kube-agents` expects the key ring `github-token-minter-keyring` and crypto key `github-token-minter-key` in your cluster's Cloud KMS region (`KMS_LOCATION`).
- **Permanence:** Cloud KMS key rings and crypto keys cannot be destroyed or deleted in GCP. Once Version 1 is imported and `ENABLED`, it persists permanently across cluster teardowns (`uninstall.sh`) and re-installations.
- **Import Methods:**
  - **Option 1 (Automated via `install.sh`):** Pass `--github-pem-path="/path/to/app-private-key.pem"` during install. `install.sh` provisions the key and uses the Minty CLI to import it into Cloud KMS. Once imported, delete the local `.pem` file. Subsequent runs skip the import.
  - **Option 2 (Pre-provisioned AOT):** Follow the [upstream GitHub Token Minter guide](https://github.com/abcxyz/github-token-minter#readme) and [Google Cloud KMS documentation](https://cloud.google.com/kms/docs/importing-a-key) to provision the key and import the private key before installing (recommended for CI/CD and release automation). Deployments run without `.pem` or Go on the host.

## Manual Testing

To manually verify the Token Minter integration, you can execute a debug pod running in the same namespace as the agent.

1. Start an interactive debug pod containing `curl`:

```bash
kubectl run debug-box --rm -it \
  --image=curlimages/curl \
  --namespace=kubeagents-system \
  --labels="app=platform-agent" \
  --overrides='
  {
    "spec": {
      "serviceAccountName": "kubeagents-platform-agent"
    }
  }' -- sh
```

2. Once inside the pod, obtain the Google Service Account OIDC token using the metadata server. The `audience` parameter must reflect the URL of the Minty service.
3. Call the token minter using the retrieved token to request an installation access token.

```bash
# 1. Get the Google Service Account OIDC token
AUDIENCE="http://github-token-minter.kubeagents-system.svc.cluster.local:8080"
OIDC_TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=${AUDIENCE}&format=full")

# 2. Call the minter
curl -i -X POST http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token \
  -H "Content-Type: application/json" \
  -H "X-OIDC-Token: $OIDC_TOKEN" \
  -d '{
    "org_name": "YOUR_GITHUB_ORG",
    "repositories": ["YOUR_REPO"],
    "scope": "platform-agent-scope"
  }'
```

If successful, Minty will return a JSON payload containing the short-lived, repository-scoped GitHub access token.

The rule ConfigMap also exposes `platform-agent-read-scope`, which grants `contents: read` alone: the scope the credential broker requests for its clone of a repository registered under `context_repos`. The operator renders one such policy per context repository, from `default.yaml`.
