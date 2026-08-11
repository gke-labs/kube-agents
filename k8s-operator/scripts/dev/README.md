# Development Scripts

This directory contains development and infrastructure setup scripts for the `k8s-operator`.

## `setup-gcp-github-wif.sh`

### Purpose

The `setup-gcp-github-wif.sh` script automates the creation and configuration of Google Cloud Platform (GCP) Workload Identity Federation (WIF). It sets up secure, keyless authentication so that your GitHub Actions pipelines can deploy and manage resources in your GCP project (like pushing Docker images and deploying to GKE) without requiring long-lived service account keys.

### What it does

1. **Enables Required APIs**: Ensures fundamental APIs (`iamcredentials`, `cloudresourcemanager`, `container`, `storage`, `pubsub`) are enabled on your GCP project.
2. **Creates a Service Account**: Provisions a dedicated GCP Service Account for GitHub Actions to impersonate.
3. **Assigns IAM Roles**: Grants necessary permissions (`roles/cloudkms.admin`, `roles/container.admin`, etc.) to the new service account. When run with `--admin`, grants extended lifecycle administration roles (`roles/iam.serviceAccountAdmin`, `roles/resourcemanager.projectIamAdmin`, `roles/pubsub.admin`) required by `provision.sh` / `teardown.sh`.
4. **Configures WIF**: Creates a Workload Identity Pool and an OIDC Provider linked to GitHub (`https://token.actions.githubusercontent.com`).
5. **Secures Access**: Configures attribute mapping and conditions so that _only_ your specific GitHub Repository is authorized to authenticate via this pool.
6. **Grants WIF Principal Role**: Grants the `roles/serviceusage.serviceUsageConsumer` role to the WIF principal to allow it to consume service quota.
7. **Outputs Variables**: Generates the exact variables you need to configure in GitHub.

### Usage

Before running the script, you must have the Google Cloud CLI (`gcloud`) installed and authenticated. You must also set three required environment variables:

- `PROJECT_ID`: Your target Google Cloud Project ID.
- `SA_NAME`: The desired name for the new Service Account (e.g., `github-actions-deploy-sa`).
- `GITHUB_REPO`: Your GitHub repository in `owner/repo` format (must be exact to allow access).

#### Options

- `--admin`: Grants extended IAM roles (`roles/iam.serviceAccountAdmin`, `roles/resourcemanager.projectIamAdmin`, `roles/pubsub.admin`) for full autonomous `provision.sh` / `teardown.sh` lifecycle operations.
- `ADMIN=true`: Environment variable alternative to `--admin`.

> **Note:** IAM role grants made with `--admin` are additive and remain assigned to the service account in your GCP project until manually revoked.

#### Example Execution

```bash
export PROJECT_ID="my-gcp-project-id"
export SA_NAME="github-actions-sa"
export GITHUB_REPO="your-github-username/your-repo-name"

cd k8s-operator/scripts/dev/
chmod +x setup-gcp-github-wif.sh

# Standard roles for CI deployment
./setup-gcp-github-wif.sh

# Or with full lifecycle admin roles for autonomous provision/teardown cycles:
./setup-gcp-github-wif.sh --admin
```

When the script finishes, it will print three variables (`GCP_PROJECT_ID`, `GCP_SERVICE_ACCOUNT`, and `GCP_WORKLOAD_IDENTITY_PROVIDER`). To complete the setup, copy those three values and add them to your GitHub Repository > Settings > Environments as Environment Variables.
