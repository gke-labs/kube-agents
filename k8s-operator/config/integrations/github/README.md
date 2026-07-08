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

### Setting up the GitHub App

1. Navigate to your GitHub Organization (or personal settings) -> **Developer Settings** -> **GitHub Apps** -> **New GitHub App**.
2. Assign a name and configure the required repository permissions (e.g., `Contents: Read & write`, `Pull requests: Read & write`).
3. Once created, note the **App ID**.
4. Scroll down and click **Generate a private key**. This will download a `.pem` file to your local machine.
5. Navigate to the target repository the agent is intended to manage, go to **Settings** -> **GitHub Apps**, and install the newly created App.

### Provisioning Configuration Variables

To deploy the agent with GitHub integration, the `vars.sh` file (used by the `provision.sh` script) must be populated with the details of your GitHub App.

- `GITHUB_APP_ID`: The unique numeric ID of the GitHub App (found in the App's General Settings).
- `GITHUB_ORG`: The name of the GitHub organization or user account where the repository is hosted.
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
