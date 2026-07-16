#!/usr/bin/env bash
# ==============================================================================
# 🤖 E2E Test CI Provisioning: GCP Service Account & WIF Setup
# ==============================================================================
# Provisions the GitHub Actions E2E test Service Account and configures
# least-privilege IAM permissions and Workload Identity Federation (WIF)
# bindings for CI execution on autopush or developer GCP projects.
# ==============================================================================

set -euo pipefail

# Check Prerequisites
if ! command -v gcloud &>/dev/null; then
  echo "[ERROR] gcloud CLI is required but not installed." >&2
  exit 1
fi

PROJECT_ID=""
GITHUB_REPO=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gcp_project=*)
      PROJECT_ID="${1#*=}"
      shift
      ;;
    --gcp_project)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --git_project=*)
      GITHUB_REPO="${1#*=}"
      shift
      ;;
    --git_project)
      GITHUB_REPO="${2:-}"
      shift 2
      ;;
    *)
      echo "[ERROR] Unknown argument: $1" >&2
      echo "Usage: ./tests/e2e/provision_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>" >&2
      exit 1
      ;;
  esac
done

if [ -z "$PROJECT_ID" ] || [ -z "$GITHUB_REPO" ]; then
  echo "[ERROR] Both mandatory flags --gcp_project and --git_project are required." >&2
  echo "Usage: ./tests/e2e/provision_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>" >&2
  echo "Example: ./tests/e2e/provision_ci_iam.sh --gcp_project kube-agents-autopush --git_project gke-labs/kube-agents" >&2
  exit 1
fi

CHAT_TOPIC_NAME="platform-agent-chat-events"
E2E_SA_NAME="github-actions-e2e"
E2E_SA_EMAIL="${E2E_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "===> Provisioning E2E CI Infrastructure on GCP Project: ${PROJECT_ID} for GitHub Repo: ${GITHUB_REPO}"

# Step 1: Enable required APIs
echo "[INFO] Enabling IAM Credentials, Resource Manager, and IAM APIs..."
gcloud services enable \
    iamcredentials.googleapis.com \
    cloudresourcemanager.googleapis.com \
    iam.googleapis.com \
    --project="$PROJECT_ID"

# Step 2: Create E2E Service Account if missing
if ! gcloud iam service-accounts describe "$E2E_SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "[INFO] Creating Service Account $E2E_SA_EMAIL..."
  gcloud iam service-accounts create "$E2E_SA_NAME" \
      --project="$PROJECT_ID" \
      --display-name="GitHub Actions E2E Runner Service Account"
else
  echo "[INFO] Service Account $E2E_SA_EMAIL already exists."
fi

# Step 3: Grant Least-Privilege IAM Permissions
echo "[INFO] Granting roles/pubsub.publisher on topic $CHAT_TOPIC_NAME..."
gcloud pubsub topics add-iam-policy-binding "$CHAT_TOPIC_NAME" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${E2E_SA_EMAIL}" \
    --role="roles/pubsub.publisher"

echo "[INFO] Granting roles/chat.admin on project $PROJECT_ID..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${E2E_SA_EMAIL}" \
    --role="roles/chat.admin"

# Step 4: Configure Workload Identity Federation
echo "[INFO] Configuring Workload Identity Federation Pool..."
WIF_POOL_NAME="github-actions-pool"
WIF_PROVIDER_NAME="github-provider"

# If pool or provider was soft-deleted, attempt to undelete first
gcloud iam workload-identity-pools undelete "$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1 || true

if ! gcloud iam workload-identity-pools describe "$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$WIF_POOL_NAME" \
      --project="${PROJECT_ID}" \
      --location="global" \
      --display-name="GitHub Actions Pool"
fi

WIF_POOL_ID=$(gcloud iam workload-identity-pools describe "$WIF_POOL_NAME" \
    --project="${PROJECT_ID}" \
    --location="global" \
    --format="value(name)")

gcloud iam workload-identity-pools providers undelete "$WIF_PROVIDER_NAME" --workload-identity-pool="$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1 || true

if ! gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER_NAME" --workload-identity-pool="$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER_NAME" \
      --project="${PROJECT_ID}" \
      --location="global" \
      --workload-identity-pool="$WIF_POOL_NAME" \
      --display-name="GitHub Actions OIDC Provider" \
      --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
      --attribute-condition="assertion.repository != ''" \
      --issuer-uri="https://token.actions.githubusercontent.com"
fi

echo "[INFO] Binding GitHub repository $GITHUB_REPO to $E2E_SA_EMAIL..."
gcloud iam service-accounts add-iam-policy-binding "${E2E_SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPO}"

gcloud iam service-accounts add-iam-policy-binding "${E2E_SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --role="roles/iam.serviceAccountTokenCreator" \
    --member="principalSet://iam.googleapis.com/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPO}"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${E2E_SA_EMAIL}" \
    --role="roles/iam.serviceAccountTokenCreator"

echo "[SUCCESS] E2E CI Service Account and WIF Provider successfully provisioned!"
echo "[INFO] WIF Provider Resource Name:"
echo "${WIF_POOL_ID}/providers/${WIF_PROVIDER_NAME}"
