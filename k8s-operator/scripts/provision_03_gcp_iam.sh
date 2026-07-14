#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 3: Controller & Agent GCP Workload Identity & GCP IAM Permissions
# ==============================================================================
# Idempotent script for granting GKE cluster management and Workload Identity
# permissions to the Operator Controller Manager and Agent GSAs.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Controller & Agent Identities"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var_platform_agent_permission_set


if [ -z "${GITHUB_ORG:-}" ]; then
  print_info "The GitHub Token Minter acts as a secure bridge allowing the GKE Agent to access GitHub."
  print_info "We collect the GitHub Org/Owner and Repository to configure authorization rules, ensuring that"
  print_info "only the GKE Agent's GCP Service Account can request GitHub access tokens for this specific repository."
  print_info "The GKE Agent will use this repository to perform write operations on the Kubernetes infrastructure using GitOps."
fi
init_var "GITHUB_ORG" "" "Enter GitHub Org/Owner (optional, for GitHub Token Minter)"
if [ -n "${GITHUB_ORG:-}" ]; then
  init_var "GITHUB_REPO" "" "Enter GitHub Repo (for GitHub Token Minter)"
  init_var "GITHUB_APP_ID" "" "Enter GitHub App ID (for GitHub Token Minter)"
  init_var "KMS_KEYRING" "github-token-minter-keyring" "Enter KMS Keyring Name (for GitHub Token Minter)"
  init_var "KMS_KEY" "github-token-minter-key" "Enter KMS Key Name (for GitHub Token Minter)"
  init_var "GITHUB_PEM_PATH" "" "Enter GitHub App Private Key PEM path (optional, for KMS import)"
fi

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"

# ─── Helper Functions for Agents ──────────────────────────────────────────────
verify_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
  
  # Ensure the service account exists
  gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1 || return 1
  
  # Ensure Workload Identity binding is present
  gcloud iam service-accounts get-iam-policy "${gsa_email}" --project="${PROJECT_ID}" --format="json" 2>/dev/null | grep -F -q "${wi_member}" || return 1
  
  local project_roles
  project_roles=$(gcloud projects get-iam-policy "${PROJECT_ID}" --flatten="bindings[].members" --filter="bindings.members:serviceAccount:${gsa_email}" --format="value(bindings.role)" 2>/dev/null)
  for role in "${roles[@]}"; do
    echo "$project_roles" | grep -q "${role}" || return 1
  done
  
  return 0
}

execute_agent_iam() {
  local agent_name=$1
  local ksa_name=$2
  local gsa_name=$3
  shift 3
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  if ! gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    print_info "Creating GSA ${gsa_name} for ${agent_name}..."
    gcloud iam service-accounts create "${gsa_name}" \
        --display-name="${agent_name} GSA" \
        --project="${PROJECT_ID}" || return 1
    sleep 15
  fi
  
  print_info "Configuring IAM roles for ${gsa_name}..."
  for role in "${roles[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${gsa_email}" \
        --role="${role}" \
        --quiet >/dev/null || return 1
  done
  
  print_info "Binding Workload Identity for ${gsa_name} to ${ksa_name}..."
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
  gcloud iam service-accounts add-iam-policy-binding "${gsa_email}" \
      --role="roles/iam.workloadIdentityUser" \
      --member="${wi_member}" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null || return 1
}

verify_ksa_annotation() {
  local ksa_name=$1
  local gsa_name=$2
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  local ann
  ann=$(kubectl get serviceaccount "${ksa_name}" -n "${NAMESPACE}" -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}' 2>/dev/null || echo "")
  [ "$ann" = "$gsa_email" ]
}

annotate_ksa() {
  local ksa_name=$1
  local gsa_name=$2
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  print_info "Annotating ServiceAccount ${ksa_name} with GSA email..."
  kubectl annotate serviceaccount "${ksa_name}" \
      --namespace "${NAMESPACE}" \
      iam.gke.io/gcp-service-account="${gsa_email}" \
      --overwrite || return 1
}

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'container.googleapis.com' && \
  echo "$out" | grep -q 'cloudresourcemanager.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      container.googleapis.com \
      cloudresourcemanager.googleapis.com \
      --project="$PROJECT_ID" || return 1
}


# Step 2: Configure Platform Agent IAM
get_platform_agent_roles() {
  local read_only_roles=(
    "roles/container.clusterViewer"
    "roles/container.viewer"
    "roles/monitoring.viewer"
    "roles/logging.viewer"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
  )
  local gke_admin_roles=(
    "roles/container.clusterAdmin"
    "roles/container.admin"
    "roles/monitoring.admin"
    "roles/logging.admin"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
  )

  case "${PLATFORM_AGENT_PERMISSION_SET:-gke-admin}" in
    read-only)
      echo "${read_only_roles[*]}"
      ;;
    custom)
      if declare -p PLATFORM_AGENT_CUSTOM_ROLES 2>/dev/null | grep -q 'declare -a'; then
        echo "${PLATFORM_AGENT_CUSTOM_ROLES[*]}"
      else
        local custom_roles_str="${PLATFORM_AGENT_CUSTOM_ROLES:-}"
        echo "${custom_roles_str//,/ }"
      fi
      ;;
    gke-admin|*)
      echo "${gke_admin_roles[*]}"
      ;;
  esac
}

verify_platform_agent() {
  local -a roles=($(get_platform_agent_roles))
  verify_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${roles[@]}"
}
execute_platform_agent() {
  local -a roles=($(get_platform_agent_roles))
  execute_agent_iam "Platform Agent" "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${roles[@]}"
}


# Step 6: Configure GitHub Token Minter IAM
verify_github_minter_iam() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    print_info "GitHub integration not configured. Skipping Minter IAM setup."
    return 0
  fi
  verify_agent_iam "${GITHUB_MINTER_KSA_NAME}" "${GITHUB_MINTER_GSA_NAME}"
}

execute_github_minter_iam() {
  if [ -z "${GITHUB_ORG:-}" ] || [ -z "${GITHUB_REPO:-}" ] || [ -z "${GITHUB_APP_ID:-}" ]; then
    return 0
  fi
  execute_agent_iam "GitHub Token Minter" "${GITHUB_MINTER_KSA_NAME}" "${GITHUB_MINTER_GSA_NAME}"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable APIs" verify_apis execute_apis 10
run_step "2. Configure Platform Agent Workload Identity & GCP IAM" verify_platform_agent execute_platform_agent 5
run_step "3. Configure GitHub Token Minter Workload Identity" verify_github_minter_iam execute_github_minter_iam 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Controller & Agent GCP Permissions Configured Successfully!  <<<${C_RESET}"
