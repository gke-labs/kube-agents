# GitHub Token Minter (Minty) Integration

This directory contains the configuration and deployment manifests for integrating the **GitHub Token Minter (Minty)** broker into the cluster. This integration allows agents to securely request short-lived GitHub access tokens without storing long-lived, static credentials, enabling them to safely perform write operations on the Kubernetes infrastructure via GitOps.

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

This holds regardless of App ID, key, or installation state, so it is worth ruling out first: a 404 here means the org lookup, whereas a bad or mismatched key returns 401. The tooling checks this for you. `install.sh` validates the answer at the prompt and re-asks, so a bad value is settled before any GCP resource exists; `provision_04_gcp_iam.sh` and `provision_10_deploy_github_minter.sh` re-check and refuse to continue if `GITHUB_ORG` is a user account or does not exist, which also covers a `vars.sh` edited by hand. All three only warn when the lookup itself is inconclusive — an unreachable or rate-limited api.github.com must not block a provision that is otherwise fine. Set `SKIP_GITHUB_ORG_CHECK=true` to bypass the check everywhere if it is ever wrong about your account; the Minter's own behaviour is unchanged by it.

Create the GitOps repository under an organization, or transfer an existing repository into one — a free organization suffices. GitHub shares a single namespace between users and organizations, so an organization cannot take the same name as your personal account.

### Setting up the GitHub App

1. Navigate to your GitHub Organization (or personal settings) -> **Developer Settings** -> **GitHub Apps** -> **New GitHub App**.
2. Assign a name and configure the required repository permissions (e.g., `Contents: Read & write`, `Pull requests: Read & write`, `Issues: Read & write`).
3. Once created, note the **App ID**.
4. Scroll down and click **Generate a private key**. This will download a `.pem` file to your local machine.
5. Navigate to the target repository the agent is intended to manage, go to **Settings** -> **GitHub Apps**, and install the newly created App.

The App may be owned by the organization or by a personal account, but an App created under a personal account defaults to "Only on this account" and cannot be installed onto an organization in that state. Either create it under the organization, where it remains private to that organization, or open the personal App's **Advanced** settings and **Make public** — which makes it installable elsewhere, not accessible to anyone without an explicit installation.

### Provisioning Configuration Variables

To deploy the agent with GitHub integration, the `vars.sh` file (used by the `provision.sh` script) must be populated with the details of your GitHub App.

- `GITHUB_APP_ID`: The unique numeric ID of the GitHub App (found in the App's General Settings).
- `GITHUB_ORG`: The name of the GitHub organization hosting the repository. This must be an organization, not a user account — see [The Target Repository Must Be Organization-Owned](#the-target-repository-must-be-organization-owned).
- `GITHUB_REPO`: The name of the target repository the agent will manage.
- `GITHUB_PEM_PATH`: The absolute local file path to the downloaded `.pem` private key file. If provided, the provisioning script will automatically use the Minty CLI to import it into Google Cloud KMS. If omitted, the deployment will proceed but Minty will fail readiness probes until a key is manually imported.

## Minty Limitations & GSA Tokens

Minty was originally designed for integration with GitHub Actions, which inherently provides OIDC tokens containing a specific `"repository"` claim. Deploying Minty in GKE introduces specific constraints regarding this validation model:

- **KSA Tokens are Unsupported:** Native Kubernetes Service Account (KSA) tokens do not support the injection of arbitrary custom claims such as `"repository"`. Consequently, Minty's default validation engine will reject KSA tokens due to the missing claim.
- **GSA Tokens (The Solution):** To resolve this, Workload Identity is utilized to provide Google Service Account (GSA) OIDC tokens. Minty implements a specific exemption for tokens where the issuer is `https://accounts.google.com`. When processing a Google-issued token, Minty bypasses the `"repository"` claim requirement. Instead, it validates the caller's identity via the `assertion.email` rule and derives the target repository directly from the JSON POST payload.

## Cryptographic Key Import via Minty CLI

During provisioning, the scripts clone the `github-token-minter` repository to leverage its included CLI tool (`minty tools import-pk`) for uploading the GitHub `.pem` file to Google Cloud KMS.

This approach is required due to the cryptographic wrapping prerequisites of the Google Cloud KMS API. Uploading an asymmetric private key natively via the Google Cloud CLI (`gcloud kms keys versions import`) strictly requires that the target key be explicitly converted from PKCS#1 into an unencrypted PKCS#8 format, and necessitates the provisioning of a separate KMS "Import Job" to facilitate secure RSA-OAEP wrapping.

The Minty CLI abstracts this complex cryptographic workflow. It automatically provisions the KMS Import Job, securely reformats the PKCS#1 string into PKCS#8 in-memory, performs the RSA-OAEP wrapping, and uploads the payload securely to KMS, ensuring a robust and standardized key import process.

### Importing Without the Minty CLI

**Skip this unless the automatic import failed.** With a working Go toolchain on the provisioning host the CLI does all of the above for you and there is nothing to do here.

It is worth knowing the recovery path exists, though, because the script's own advice is circular when Go is the missing piece: it warns `Go is required to run the Minty CLI tool` or `Failed to import GitHub Private Key to KMS. You must import it manually` — and the manual instructions it prints are another `go run ./cmd/minty` invocation. Either way it continues, leaving the KMS key with no enabled version, so the Minter deploys and then never passes its readiness probe.

`gcloud` does the same import in four commands. Run them, then re-run `provision_10_deploy_github_minter.sh`:

```bash
# 1. PKCS#1 (what GitHub downloads) to unencrypted PKCS#8 DER (what KMS accepts).
openssl pkcs8 -topk8 -nocrypt -inform PEM -in "${GITHUB_PEM_PATH}" \
    -outform DER -out /tmp/gh-app-key.p8.der

# 2. An Import Job supplies the RSA-OAEP wrapping key. Its protection level must
#    match the target key's (the provisioner creates a SOFTWARE key).
gcloud kms import-jobs create gh-app-key-import \
    --location="${KMS_LOCATION}" --keyring="${KMS_KEYRING}" \
    --import-method=rsa-oaep-4096-sha256-aes-256 --protection-level=software \
    --project="${PROJECT_ID}"

# 3. Wait for it to reach ACTIVE — key generation takes a few seconds.
gcloud kms import-jobs describe gh-app-key-import \
    --location="${KMS_LOCATION}" --keyring="${KMS_KEYRING}" \
    --project="${PROJECT_ID}" --format="value(state)"

# 4. gcloud wraps the key locally and uploads it. CLOUDSDK_PYTHON_SITEPACKAGES=1
#    is required: without it gcloud reports "Cannot load the Pyca cryptography
#    library" even when its own interpreter has the module installed.
CLOUDSDK_PYTHON_SITEPACKAGES=1 gcloud kms keys versions import \
    --import-job=gh-app-key-import \
    --location="${KMS_LOCATION}" --keyring="${KMS_KEYRING}" \
    --key="${KMS_KEY}" --algorithm=rsa-sign-pkcs1-2048-sha256 \
    --target-key-file=/tmp/gh-app-key.p8.der --project="${PROJECT_ID}"

rm -f /tmp/gh-app-key.p8.der
```

The version reports `PENDING_IMPORT` briefly before becoming `ENABLED`. Delete the DER file afterwards — unlike the KMS copy, it is raw key material on local disk. Re-running the provisioner then resolves the active version and skips the Minty CLI, because it only attempts an import when no enabled version exists.

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
    "repositories": ["YOUR_GITHUB_REPO"],
    "scope": "platform-agent-scope"
  }'
```

If successful, Minty will return a JSON payload containing the short-lived GitHub access token.
