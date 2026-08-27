#!/bin/bash
set -euo pipefail

POOL_NAME="github-pool"
PROVIDER_NAME="github-deploy-provider"

# 1. Parse optional CLI flags and environment variables
IS_ADMIN="${ADMIN:-false}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --admin)
      IS_ADMIN=true
      shift
      ;;
    -*)
      echo "Error: Unknown option '$1'." >&2
      exit 1
      ;;
    *)
      echo "Error: Unexpected argument '$1'." >&2
      exit 1
      ;;
  esac
done

# 2. Check for required environment variables
MISSING_VAR=false
if [ -z "${PROJECT_ID:-}" ]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")
  if [ -z "$PROJECT_ID" ]; then
    echo "Error: PROJECT_ID environment variable is not set and no default project found in gcloud config."
    MISSING_VAR=true
  fi
fi
if [ -z "${SA_NAME:-}" ]; then
  echo "Error: SA_NAME environment variable is not set."
  MISSING_VAR=true
fi
if [ -z "${GITHUB_REPO:-}" ]; then
  echo "Error: GITHUB_REPO environment variable is not set."
  MISSING_VAR=true
fi

if [ "$MISSING_VAR" = true ]; then
  echo ""
  echo "Please set the required variables before running this script. For example:"
  echo 'export PROJECT_ID="your-gcp-project-id"'
  echo 'export SA_NAME="github-actions-sa"'
  echo 'export GITHUB_REPO="your-github-username/your-repo-name"'
  echo 'Optionally pass --admin to grant full autonomous E2E lifecycle roles.'
  exit 1
fi

# 3. Prompt user for confirmation
echo "The script will perform the setup for GitHub Actions WIF in the following project: $PROJECT_ID"
if [ "$IS_ADMIN" = true ]; then
  echo "Mode: ADMIN (Includes extended IAM roles for full install/uninstall cycles)"
else
  echo "Mode: STANDARD (Base roles only. Use --admin for autonomous E2E pipeline support)"
fi
echo ""

read -r -p "Do you want to proceed? (y/N): " confirm || confirm="n"
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo "Setup aborted by user."
  exit 1
fi

echo ""
echo "Starting GCP Setup..."

# Enable necessary services (optional but helpful)
echo "Ensuring required APIs are enabled..."
gcloud services enable iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  container.googleapis.com \
  storage.googleapis.com \
  pubsub.googleapis.com \
  gkebackup.googleapis.com \
  logging.googleapis.com \
  artifactregistry.googleapis.com \
  --project="${PROJECT_ID}"

# 3. Create Service Account
echo "Creating Service Account: ${SA_NAME}..."
gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="GitHub Actions Service Account" || echo "Service account may already exist, continuing..."

# 4. Grant necessary permissions
echo "Granting necessary roles to the Service Account..."
# Base roles required for standard CI deployments:
ROLES=(
  # Cloud KMS keyring and CryptoKey management for GKE CMEK database encryption, Backup for GKE, and GitHub Token Minter key provisioning
  "roles/cloudkms.admin"

  # Full GKE cluster lifecycle management (cluster provisioning, node pools, and gVisor sandbox configuration)
  "roles/container.admin"

  # Read-only Compute Engine and VPC inspection for GKE node-pool instance groups, machine types, reservations, and quota advice
  "roles/compute.viewer"

  # Enabling and consuming required Google Cloud APIs across the platform
  "roles/serviceusage.serviceUsageAdmin"
  "roles/serviceusage.serviceUsageConsumer"
)

# Extended roles required for full autonomous E2E lifecycle (install.sh + uninstall.sh):
if [ "$IS_ADMIN" = true ]; then
  echo "Admin mode selected. Adding extended lifecycle administration roles..."
  ROLES+=(
    # The kube-agents-iam module creates and deletes the agent's service accounts
    "roles/iam.serviceAccountAdmin"

    # The kube-agents-iam module binds and unbinds project IAM policies
    "roles/resourcemanager.projectIamAdmin"

    # The chat-pubsub module manages the Google Chat Pub/Sub topic and subscription
    "roles/pubsub.admin"

    # The gke-backup-plan module manages Backup for GKE plans
    "roles/gkebackup.admin"

    # Terraform remote state management (creating and managing gs://<project>-kube-agents-tfstate)
    "roles/storage.admin"

    # Cloud Logging sink creation and filter routing for event and autoscaler log ingestion into Pub/Sub
    "roles/logging.configWriter"

    # Artifact Registry repository creation and image management for agent plugin extensions
    "roles/artifactregistry.admin"
  )
fi

for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$role" >/dev/null
done

# 5. Create WIF Pool & Provider
echo "Creating Workload Identity Pool..."
gcloud iam workload-identity-pools create "${POOL_NAME}" \
  --location="global" \
  --project="${PROJECT_ID}" || echo "Pool may already exist, continuing..."

echo "Configuring Workload Identity Provider..."
if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
  --location="global" \
  --workload-identity-pool="${POOL_NAME}" \
  --project="${PROJECT_ID}" &>/dev/null; then
  echo "Creating new Provider..."
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
    --location="global" \
    --workload-identity-pool="${POOL_NAME}" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'" \
    --project="${PROJECT_ID}"
else
  echo "Provider already exists, updating attribute condition..."
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER_NAME}" \
    --location="global" \
    --workload-identity-pool="${POOL_NAME}" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}'" \
    --project="${PROJECT_ID}"
fi

PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")

# 6. Bind the service account to the WIF provider
echo "Binding Service Account to WIF Provider..."
gcloud iam service-accounts add-iam-policy-binding "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${GITHUB_REPO}" \
  --project="${PROJECT_ID}" >/dev/null

# 7. Grant the WIF principal Service Usage Consumer role on the project
echo "Granting Service Usage Consumer role to the WIF Principal..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --role="roles/serviceusage.serviceUsageConsumer" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${GITHUB_REPO}" \
  >/dev/null

echo ""
echo "✅ GCP Setup Completed Successfully!"
echo ""
echo "Please add the following values to your GitHub 'dev' Environment Variables:"
echo "GCP_PROJECT_ID: ${PROJECT_ID}"
echo "GCP_SERVICE_ACCOUNT: ${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "GCP_WORKLOAD_IDENTITY_PROVIDER: projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/providers/${PROVIDER_NAME}"
