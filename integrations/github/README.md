# GitHub Token Broker (Minty)

The **GitHub Token Broker** is implemented using the open-source [abcxyz/github-token-minter](https://github.com/abcxyz/github-token-minter) (Minty) service. It acts as an authentication proxy for GKE agents, abstracting the master GitHub App private key. It ensures that client agents (such as `platform-agent`) only handle short-lived, repository-scoped installation tokens.

---

## Architecture Overview

```
[Agent Pod]
     │
     ▼ (OIDC Token via X-OIDC-Token Header)
[Minty Pod] ---> (Decrypts key/Signs JWT via GCP KMS) ---> [GitHub API]
```

Minty evaluates incoming request authorization against OIDC claims (e.g. Workload Identity) using a CEL (Common Expression Language) policy, then generates a GitHub installation token using the App's private key (stored securely in GCP KMS).

## Security Design (Zero-Exposure Private Key)

To enforce the principle of least privilege, the master GitHub App private key is **never exposed in plaintext to any pods** in the GKE `agent-system` namespace (including the Minty pod itself).

Instead, Minty delegates all cryptographic operations to GCP KMS:

1. **JWT Creation:** When authenticating with GitHub, Minty creates the JWT header and payload locally and calculates its SHA-256 hash.
2. **KMS Delegation:** Minty sends only the _hash_ to GCP KMS via the `AsymmetricSign` API.
3. **In-KMS Signing:** GCP KMS performs the RSA signing operation internally using the write-only key material and returns only the signature.
4. **No Plaintext Key:** At no point does Minty or any GKE pod pull or store the private key PEM file.

---

## REST API Contract

### Request Token

- **Endpoint:** `POST /token`
- **Headers:**
  - `Content-Type: application/json`
  - `X-OIDC-Token: <RAW_OIDC_JWT_TOKEN>` (Your GKE Service Account projected token)
- **JSON Body:**

```json
{
  "org_name": "your-github-org-or-user",
  "repositories": ["your-repo-name"],
  "scope": "platform-agent-scope"
}
```

### Response

Returns the raw installation token as a plain text string:

```
ghs_1234567890abcdefghijklmnopqrstuvwxyz
```

---

## Provisioning and Installation

### Step 0: GitHub App Credentials Setup

Minty requires the **GitHub App ID** to identify your application.

- **App ID:** This is stored in a Kubernetes secret named `github-app-credentials` under the `app-id` key.
- **Private Key:** Instead of using Kubernetes secrets, the private key PEM is stored and managed securely in **GCP KMS** (see Step 2).
- **Installation ID:** Minty **does not require** the installation ID to be configured. It dynamically resolves the correct installation ID from GitHub based on the target organization or user name requested by the client.

Create the `github-app-credentials` secret containing your App ID:

```bash
kubectl create secret generic github-app-credentials \
    --from-literal=app-id=YOUR_GITHUB_APP_ID \
    -n agent-system
```

### Step 1: Workload Identity Setup

Create the Service Accounts and bind them to allow Minty to authenticate with GCP KMS:

```bash
# 1. Create a GCP Service Account for the minter
gcloud iam service-accounts create github-token-minter-sa \
    --description="Service Account for the GitHub Token Minter" \
    --display-name="GitHub Token Minter SA"

# 2. Deploy the Kubernetes Service Account (KSA) (ensure you update placeholders in serviceaccount.yaml first)
kubectl apply -f serviceaccount.yaml

# 3. Allow GKE KSA to impersonate GCP SA
gcloud iam service-accounts add-iam-policy-binding github-token-minter-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:YOUR_PROJECT_ID.svc.id.goog[agent-system/github-token-minter-ksa]"

```

### Step 2: GCP KMS Setup and Key Import

Minty uses GCP KMS for asymmetric signing using the GitHub App private key.

1. **Enable the Cloud KMS API (if not already enabled):**

   ```bash
   gcloud services enable cloudkms.googleapis.com
   ```

2. **Create Keyring and Key:**

   ```bash
   # Create Keyring
   gcloud kms keyrings create github-token-minter-keyring --location=us-central1

   # Create Key (asymmetric signing, no initial version)
   gcloud kms keys create github-private-key \
       --location=us-central1 \
       --keyring=github-token-minter-keyring \
       --purpose=asymmetric-signing \
       --default-algorithm=rsa-sign-pkcs1-2048-sha256 \
       --import-only \
       --skip-initial-version-creation
   ```

3. **Grant Signer Permissions:**

   ```bash
   gcloud kms keys add-iam-policy-binding github-private-key \
       --location=us-central1 \
       --keyring=github-token-minter-keyring \
       --member="serviceAccount:github-token-minter-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
       --role="roles/cloudkms.signerVerifier"
   ```

4. **Import Private Key PEM:**
   Clone the `abcxyz/github-token-minter` repo and use the CLI to import the PEM file:
   ```bash
   go run ./cmd/minty tools import-pk \
       -project-id=YOUR_PROJECT_ID \
       -location=us-central1 \
       -key-ring=github-token-minter-keyring \
       -key=github-private-key \
       -private-key=@/path/to/github-app-private-key.pem
   ```
   _Note: This creates key version `1` (e.g. `.../cryptoKeys/github-private-key/cryptoKeyVersions/1`)._

### Step 3: Deploy Policy ConfigMap

Create `configmap.yaml` defining access rules for your repositories. Note that the key name in the ConfigMap data maps to the path `/etc/minty/{org}/{repo}.yaml` (using `subPath` mounts).

Example for `YOUR_GITHUB_ORG/YOUR_GITHUB_REPO` repository:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: github-token-minter-config
  namespace: agent-system
data:
  YOUR_GITHUB_ORG-YOUR_GITHUB_REPO.yaml: |
    version: 'minty.abcxyz.dev/v2'
    rule:
      # Allow GKE cluster OIDC issuer
      if: "assertion.iss == 'https://container.googleapis.com/v1/projects/YOUR_PROJECT_ID/locations/YOUR_REGION/clusters/YOUR_CLUSTER'"
    scope:
      platform-agent-scope:
        rule:
          if: "assertion.sub == 'system:serviceaccount:agent-system:platform-agent'"
        repositories:
          - 'YOUR_GITHUB_REPO'
        permissions:
          contents: 'write'
          pull_requests: 'write'
```

Apply it:

```bash
kubectl apply -f configmap.yaml
```

### Step 4: Deploy Minty Service

Apply the deployment manifest (`deployment.yaml`):

```bash
kubectl apply -f deployment.yaml
```
