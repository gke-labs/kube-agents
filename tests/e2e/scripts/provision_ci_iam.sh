#!/usr/bin/env bash
# ==============================================================================
# 🤖 E2E Test CI Provisioning: GCP Service Account & WIF Setup
# ==============================================================================
# Provisions the GitHub Actions E2E test Service Account and configures
# least-privilege IAM permissions and Workload Identity Federation (WIF)
# bindings for CI execution on autopush or developer GCP projects.
# ==============================================================================

set -euo pipefail

readonly GITHUB_REPO_PATTERN='^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9._-]*[A-Za-z0-9_-][A-Za-z0-9._-]*$'
readonly WIF_LOCATION="global"
readonly WIF_POOL_NAME="github-actions-pool"
readonly WIF_POOL_DISPLAY_NAME="GitHub Actions Pool"
readonly WIF_PROVIDER_NAME="github-provider"
readonly WIF_PROVIDER_DISPLAY_NAME="GitHub Actions OIDC Provider"
readonly WIF_ATTRIBUTE_MAPPING="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository"
readonly WIF_OIDC_ISSUER_URI="https://token.actions.githubusercontent.com"
readonly WIF_LEGACY_INSECURE_CONDITION="assertion.repository != ''"
readonly WIF_STATE_ACTIVE="ACTIVE"
readonly WIF_STATE_DELETED="DELETED"
readonly GCLOUD_NOT_FOUND_MARKER="NOT_FOUND"
readonly GCLOUD_SERVICE_DISABLED_MARKER="SERVICE_DISABLED"
readonly GCLOUD_API_UNUSED_MARKER="API has not been used in project"

is_valid_github_repo() {
  # Evaluate ASCII ranges under the C locale so non-ASCII letters (e.g. 'é' under en_US.UTF-8) do not match [A-Za-z].
  local LC_ALL=C
  [[ "${1:-}" =~ $GITHUB_REPO_PATTERN ]]
}

ensure_active_provider_condition_is_updatable() {
  local existing_condition="$1"
  if [[ -n "${existing_condition}" && "${existing_condition}" != "${WIF_ATTRIBUTE_CONDITION}" && "${existing_condition}" != "${WIF_LEGACY_INSECURE_CONDITION}" ]]; then
    echo "[ERROR] Existing WIF provider ${WIF_PROVIDER_NAME} already has attribute condition: ${existing_condition}" >&2
    echo "[ERROR] Refusing to overwrite a non-legacy provider condition for ${GITHUB_REPO}. Update it explicitly with 'gcloud iam workload-identity-pools providers update-oidc' if replacing it is intended." >&2
    exit 1
  fi
}

describe_wif_pool() {
  local describe_out describe_err
  WIF_POOL_STATE=""
  WIF_POOL_ID=""
  if describe_out=$(gcloud iam workload-identity-pools describe "$WIF_POOL_NAME" \
      --location="${WIF_LOCATION}" \
      --project="$PROJECT_ID" \
      --quiet \
      --format="value(state,name)" 2>"${ERR_FILE}"); then
    IFS=$'\t' read -r WIF_POOL_STATE WIF_POOL_ID <<< "${describe_out}"
    return 0
  fi
  describe_err=$(<"${ERR_FILE}")
  if [[ "${describe_err}" == *"${GCLOUD_NOT_FOUND_MARKER}"* ]]; then
    return 0
  fi
  echo "[ERROR] Failed to inspect WIF pool ${WIF_POOL_NAME}:" >&2
  echo "${describe_err}" >&2
  exit 1
}

describe_wif_provider() {
  local allow_disabled_api="$1"
  local describe_out describe_err
  WIF_PROVIDER_STATE=""
  WIF_PROVIDER_CONDITION=""
  if describe_out=$(gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER_NAME" \
      --workload-identity-pool="$WIF_POOL_NAME" \
      --location="${WIF_LOCATION}" \
      --project="$PROJECT_ID" \
      --quiet \
      --format='value[separator="\n"](state,attributeCondition)' 2>"${ERR_FILE}"); then
    if [[ "${describe_out}" == *$'\n'* ]]; then
      WIF_PROVIDER_STATE="${describe_out%%$'\n'*}"
      WIF_PROVIDER_CONDITION="${describe_out#*$'\n'}"
    else
      WIF_PROVIDER_STATE="${describe_out}"
      WIF_PROVIDER_CONDITION=""
    fi
    return 0
  fi
  describe_err=$(<"${ERR_FILE}")
  if [[ "${describe_err}" == *"${GCLOUD_NOT_FOUND_MARKER}"* ]]; then
    return 0
  fi
  if [[ "${allow_disabled_api}" == "true" ]] && [[ "${describe_err}" == *"${GCLOUD_SERVICE_DISABLED_MARKER}"* || "${describe_err}" == *"${GCLOUD_API_UNUSED_MARKER}"* ]]; then
    return 0
  fi
  echo "[ERROR] Failed to inspect WIF provider ${WIF_PROVIDER_NAME}:" >&2
  echo "${describe_err}" >&2
  exit 1
}

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

if ! is_valid_github_repo "$GITHUB_REPO"; then
  echo "[ERROR] Invalid --git_project '${GITHUB_REPO}': expected '<GITHUB_OWNER>/<REPO>' (owner: [A-Za-z0-9][A-Za-z0-9_-]*; repo: [A-Za-z0-9._-]+ containing at least one non-dot character)." >&2
  exit 1
fi

ERR_FILE="$(mktemp)"
cleanup() {
  rm -f "${ERR_FILE:-}"
}
trap cleanup EXIT

GITHUB_OWNER="${GITHUB_REPO%%/*}"
WIF_ATTRIBUTE_CONDITION="assertion.repository_owner == '${GITHUB_OWNER}' && assertion.repository == '${GITHUB_REPO}'"

CHAT_TOPIC_NAME="platform-agent-chat-events"
E2E_SA_NAME="github-actions-e2e"
E2E_SA_EMAIL="${E2E_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "===> Provisioning E2E CI Infrastructure on GCP Project: ${PROJECT_ID} for GitHub Repo: ${GITHUB_REPO}"

# Preflight: fail closed before mutating APIs, IAM bindings, or soft-deleted WIF resources
# if an active provider already carries a different non-legacy attribute condition (or if
# inspecting the provider fails for a reason other than NOT_FOUND / disabled IAM API).
describe_wif_provider "true"
if [[ "${WIF_PROVIDER_STATE}" == "${WIF_STATE_ACTIVE}" ]]; then
  ensure_active_provider_condition_is_updatable "${WIF_PROVIDER_CONDITION}"
fi

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

describe_wif_pool
case "${WIF_POOL_STATE}" in
  "")
    gcloud iam workload-identity-pools create "$WIF_POOL_NAME" \
        --project="${PROJECT_ID}" \
        --location="${WIF_LOCATION}" \
        --display-name="${WIF_POOL_DISPLAY_NAME}"
    WIF_POOL_ID=$(gcloud iam workload-identity-pools describe "$WIF_POOL_NAME" \
        --project="${PROJECT_ID}" \
        --location="${WIF_LOCATION}" \
        --quiet \
        --format="value(name)")
    ;;
  "${WIF_STATE_DELETED}")
    echo "[INFO] Undeleting soft-deleted WIF pool ${WIF_POOL_NAME}..."
    gcloud iam workload-identity-pools undelete "$WIF_POOL_NAME" \
        --location="${WIF_LOCATION}" \
        --project="$PROJECT_ID"
    ;;
  "${WIF_STATE_ACTIVE}")
    ;;
  *)
    echo "[ERROR] Unexpected WIF pool ${WIF_POOL_NAME} state: ${WIF_POOL_STATE}" >&2
    exit 1
    ;;
esac

describe_wif_provider "false"
case "${WIF_PROVIDER_STATE}" in
  "")
    gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER_NAME" \
        --project="${PROJECT_ID}" \
        --location="${WIF_LOCATION}" \
        --workload-identity-pool="$WIF_POOL_NAME" \
        --display-name="${WIF_PROVIDER_DISPLAY_NAME}" \
        --attribute-mapping="${WIF_ATTRIBUTE_MAPPING}" \
        --attribute-condition="${WIF_ATTRIBUTE_CONDITION}" \
        --issuer-uri="${WIF_OIDC_ISSUER_URI}"
    ;;
  "${WIF_STATE_DELETED}")
    echo "[INFO] Undeleting soft-deleted WIF provider ${WIF_PROVIDER_NAME}..."
    gcloud iam workload-identity-pools providers undelete "$WIF_PROVIDER_NAME" \
        --workload-identity-pool="$WIF_POOL_NAME" \
        --location="${WIF_LOCATION}" \
        --project="$PROJECT_ID"
    echo "[INFO] Setting WIF provider ${WIF_PROVIDER_NAME} attribute condition for ${GITHUB_REPO}..."
    if ! gcloud iam workload-identity-pools providers update-oidc "$WIF_PROVIDER_NAME" \
        --project="${PROJECT_ID}" \
        --location="${WIF_LOCATION}" \
        --workload-identity-pool="$WIF_POOL_NAME" \
        --attribute-condition="${WIF_ATTRIBUTE_CONDITION}"; then
      echo "[ERROR] Failed to update attribute condition on undeleted WIF provider ${WIF_PROVIDER_NAME}; re-deleting provider to avoid leaving its prior condition active..." >&2
      if ! gcloud iam workload-identity-pools providers delete "$WIF_PROVIDER_NAME" \
          --workload-identity-pool="$WIF_POOL_NAME" \
          --location="${WIF_LOCATION}" \
          --project="$PROJECT_ID" \
          --quiet; then
        echo "[ERROR] Rollback delete of WIF provider ${WIF_PROVIDER_NAME} also failed; manually delete or update its attribute condition immediately." >&2
      fi
      exit 1
    fi
    ;;
  "${WIF_STATE_ACTIVE}")
    ensure_active_provider_condition_is_updatable "${WIF_PROVIDER_CONDITION}"
    if [[ "${WIF_PROVIDER_CONDITION}" == "${WIF_ATTRIBUTE_CONDITION}" ]]; then
      echo "[INFO] WIF provider ${WIF_PROVIDER_NAME} already has the expected attribute condition for ${GITHUB_REPO}."
    else
      echo "[INFO] Tightening existing WIF provider ${WIF_PROVIDER_NAME} attribute condition for ${GITHUB_REPO}..."
      gcloud iam workload-identity-pools providers update-oidc "$WIF_PROVIDER_NAME" \
          --project="${PROJECT_ID}" \
          --location="${WIF_LOCATION}" \
          --workload-identity-pool="$WIF_POOL_NAME" \
          --attribute-condition="${WIF_ATTRIBUTE_CONDITION}"
    fi
    ;;
  *)
    echo "[ERROR] Unexpected WIF provider ${WIF_PROVIDER_NAME} state: ${WIF_PROVIDER_STATE}" >&2
    exit 1
    ;;
esac

echo "[INFO] Binding GitHub repository $GITHUB_REPO to $E2E_SA_EMAIL..."
gcloud iam service-accounts add-iam-policy-binding "${E2E_SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPO}"

gcloud iam service-accounts add-iam-policy-binding "${E2E_SA_EMAIL}" \
    --project="${PROJECT_ID}" \
    --role="roles/iam.serviceAccountTokenCreator" \
    --member="principalSet://iam.googleapis.com/${WIF_POOL_ID}/attribute.repository/${GITHUB_REPO}"

echo "[SUCCESS] E2E CI Service Account and WIF Provider successfully provisioned!"
echo "[INFO] WIF Provider Resource Name:"
echo "${WIF_POOL_ID}/providers/${WIF_PROVIDER_NAME}"
