#!/usr/bin/env bash
# ==============================================================================
# 🤖 E2E Test CI Teardown: GCP Service Account & WIF Cleanup
# ==============================================================================
# Cleans up the GitHub Actions E2E test Service Account and Workload Identity
# Federation (WIF) resources provisioned for CI execution.
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
      echo "Usage: ./tests/e2e/teardown_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>" >&2
      exit 1
      ;;
  esac
done

if [ -z "$PROJECT_ID" ] || [ -z "$GITHUB_REPO" ]; then
  echo "[ERROR] Both mandatory flags --gcp_project and --git_project are required." >&2
  echo "Usage: ./tests/e2e/teardown_ci_iam.sh --gcp_project <GCP_PROJECT_ID> --git_project <GITHUB_OWNER/REPO>" >&2
  echo "Example: ./tests/e2e/teardown_ci_iam.sh --gcp_project kube-agents-autopush --git_project gke-labs/kube-agents" >&2
  exit 1
fi

CHAT_TOPIC_NAME="platform-agent-chat-events"
E2E_SA_NAME="github-actions-e2e"
E2E_SA_EMAIL="${E2E_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
WIF_POOL_NAME="github-actions-pool"
WIF_PROVIDER_NAME="github-provider"

echo "===> Tearing down E2E CI Infrastructure on GCP Project: ${PROJECT_ID} for GitHub Repo: ${GITHUB_REPO}"

# Step 1: Delete Workload Identity Provider
if gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER_NAME" --workload-identity-pool="$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "[INFO] Deleting Workload Identity Provider $WIF_PROVIDER_NAME..."
  gcloud iam workload-identity-pools providers delete "$WIF_PROVIDER_NAME" \
      --workload-identity-pool="$WIF_POOL_NAME" \
      --location="global" \
      --project="$PROJECT_ID" \
      --quiet || true
fi

# Step 2: Delete Workload Identity Pool
if gcloud iam workload-identity-pools describe "$WIF_POOL_NAME" --location="global" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "[INFO] Deleting Workload Identity Pool $WIF_POOL_NAME..."
  gcloud iam workload-identity-pools delete "$WIF_POOL_NAME" \
      --location="global" \
      --project="$PROJECT_ID" \
      --quiet || true
fi

# Step 3: Remove IAM Policy Bindings
echo "[INFO] Removing IAM policy bindings for $E2E_SA_EMAIL..."
gcloud pubsub topics remove-iam-policy-binding "$CHAT_TOPIC_NAME" \
    --project="$PROJECT_ID" \
    --member="serviceAccount:${E2E_SA_EMAIL}" \
    --role="roles/pubsub.publisher" >/dev/null 2>&1 || true

gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${E2E_SA_EMAIL}" \
    --role="roles/chat.admin" >/dev/null 2>&1 || true

# Step 4: Delete Service Account
if gcloud iam service-accounts describe "$E2E_SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "[INFO] Deleting Service Account $E2E_SA_EMAIL..."
  gcloud iam service-accounts delete "$E2E_SA_EMAIL" \
      --project="$PROJECT_ID" \
      --quiet || true
fi

echo "[SUCCESS] E2E CI Service Account and WIF resources successfully cleaned up!"
