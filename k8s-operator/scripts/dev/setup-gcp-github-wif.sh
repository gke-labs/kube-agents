#!/bin/bash
set -e

# 1. Check for required environment variables
MISSING_VAR=false
if [ -z "$PROJECT_ID" ]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  if [ -z "$PROJECT_ID" ]; then
    echo "Error: PROJECT_ID environment variable is not set and no default project found in gcloud config."
    MISSING_VAR=true
  fi
fi
if [ -z "$SA_NAME" ]; then
  echo "Error: SA_NAME environment variable is not set."
  MISSING_VAR=true
fi
if [ -z "$GITHUB_REPO" ]; then
  echo "Error: GITHUB_REPO environment variable is not set."
  MISSING_VAR=true
fi

POOL_NAME="github-pool"
PROVIDER_NAME="github-deploy-provider"

if [ "$MISSING_VAR" = true ]; then
  echo ""
  echo "Please set the required variables before running this script. For example:"
  echo 'export PROJECT_ID="your-gcp-project-id"'
  echo 'export SA_NAME="github-actions-sa"'
  echo 'export GITHUB_REPO="your-github-username/your-repo-name"'
  exit 1
fi

# 2. Prompt user for confirmation
echo "The script will perform the setup for GitHub Actions WIF in the following project: $PROJECT_ID"
echo ""

read -p "Do you want to proceed? (y/N): " confirm
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
  --project="${PROJECT_ID}"

# 3. Create Service Account
echo "Creating Service Account: ${SA_NAME}..."
gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="GitHub Actions Service Account" || echo "Service account may already exist, continuing..."

# 4. Grant necessary permissions
echo "Granting necessary roles to the Service Account..."
for role in roles/cloudkms.admin roles/container.admin roles/serviceusage.serviceUsageAdmin roles/serviceusage.serviceUsageConsumer; do
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
