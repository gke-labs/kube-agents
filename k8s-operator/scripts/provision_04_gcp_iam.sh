#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 4: Controller & Agent GCP Workload Identity & GCP IAM Permissions
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

  # Reconcile the legacy broad logging grant unless a custom role set still requests it.
  if [[ ! " ${roles[*]} " =~ " roles/logging.admin " ]] && \
     echo "$project_roles" | grep -Fxq "roles/logging.admin"; then
    return 1
  fi
  
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

  if [[ ! " ${roles[*]} " =~ " roles/logging.admin " ]]; then
    gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${gsa_email}" \
        --role="roles/logging.admin" \
        --condition=None \
        --quiet >/dev/null 2>&1 || true
  fi
  
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

# Ensure Cluster Workload Identity Pool
verify_cluster_workload_pool() {
  local pool
  pool=$(gcloud container clusters describe "${CLUSTER_NAME}" --location="${REGION}" --project="${PROJECT_ID}" --format="value(workloadIdentityConfig.workloadPool)" 2>/dev/null || echo "")
  [ "$pool" = "${PROJECT_ID}.svc.id.goog" ]
}
execute_cluster_workload_pool() {
  print_info "Enabling Workload Identity pool on target GKE cluster ${CLUSTER_NAME}..."
  gcloud container clusters update "${CLUSTER_NAME}" \
    --location="${REGION}" \
    --project="${PROJECT_ID}" \
    --workload-pool="${PROJECT_ID}.svc.id.goog" \
    --quiet
}

# Enabling the cluster-level pool does not migrate pre-existing node pools off
# the legacy GCE metadata server; pods on such pools receive the node's GCE
# service account instead of the federated identity and fail GCP auth.
get_legacy_metadata_node_pools() {
  gcloud container node-pools list \
      --cluster="${CLUSTER_NAME}" \
      --location="${REGION}" \
      --project="${PROJECT_ID}" \
      --format="csv[no-heading](name,config.workloadMetadataConfig.mode)" 2>/dev/null \
    | awk -F',' '$2 != "GKE_METADATA" {print $1}'
}
verify_node_pool_metadata() {
  [ -z "$(get_legacy_metadata_node_pools)" ]
}
execute_node_pool_metadata() {
  local pool
  for pool in $(get_legacy_metadata_node_pools); do
    print_warning "Node pool '${pool}' uses the legacy GCE metadata server; migrating to GKE_METADATA (this recreates the pool's nodes)..."
    gcloud container node-pools update "${pool}" \
        --cluster="${CLUSTER_NAME}" \
        --location="${REGION}" \
        --project="${PROJECT_ID}" \
        --workload-metadata=GKE_METADATA \
        --quiet || return 1
  done
}


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
    "roles/compute.viewer"
    "roles/monitoring.viewer"
    "roles/logging.viewer"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
    "roles/mcp.toolUser"
  )
  local gke_admin_roles=(
    "roles/container.clusterAdmin"
    "roles/container.admin"
    "roles/compute.viewer"
    "roles/monitoring.admin"
    # The agent can query logs for diagnostics but must not administer the audit-log sink.
    "roles/logging.viewer"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
    "roles/mcp.toolUser"
  )

  case "${PLATFORM_AGENT_PERMISSION_SET:-read-only}" in
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
    gke-admin)
      echo "${gke_admin_roles[*]}"
      ;;
    *)
      # Fail closed. init_var_platform_agent_permission_set rejects unknown
      # values, so reaching here means the script was invoked with the variable
      # pre-set (CI, a sourced vars.sh, a typo'd export). Granting admin on an
      # unrecognized value would make a typo an escalation; warn on stderr
      # (never stdout — the caller captures it) and use the least-privilege set.
      print_warning "Unrecognized PLATFORM_AGENT_PERMISSION_SET '${PLATFORM_AGENT_PERMISSION_SET}'; falling back to read-only." >&2
      echo "${read_only_roles[*]}"
      ;;
  esac
}

verify_platform_agent() {
  local -a roles=($(get_platform_agent_roles))
  verify_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${roles[@]}" || return 1

  local gsa_email="${PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  local sandbox_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${PLATFORM_AGENT_SANDBOX_KSA_NAME}]"
  ! gcloud iam service-accounts get-iam-policy "${gsa_email}" --project="${PROJECT_ID}" --format="json" 2>/dev/null | grep -F -q "${sandbox_member}"
}
execute_platform_agent() {
  local -a roles=($(get_platform_agent_roles))
  execute_agent_iam "Platform Agent" "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${roles[@]}"

  local gsa_email="${PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  local sandbox_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${PLATFORM_AGENT_SANDBOX_KSA_NAME}]"
  print_info "Removing legacy sandbox Workload Identity binding from ${PLATFORM_AGENT_SANDBOX_KSA_NAME}..."
  gcloud iam service-accounts remove-iam-policy-binding "${gsa_email}" \
      --role="roles/iam.workloadIdentityUser" \
      --member="${sandbox_member}" \
      --project="${PROJECT_ID}" \
      --condition=None \
      --quiet >/dev/null 2>&1 || true
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
run_step "2. Ensure GKE Workload Identity Pool" verify_cluster_workload_pool execute_cluster_workload_pool 10
run_step "3. Migrate Node Pools to the GKE Metadata Server" verify_node_pool_metadata execute_node_pool_metadata 5
run_step "4. Configure Platform Agent Workload Identity & GCP IAM" verify_platform_agent execute_platform_agent 5
run_step "5. Configure GitHub Token Minter Workload Identity" verify_github_minter_iam execute_github_minter_iam 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Controller & Agent GCP Permissions Configured Successfully!  <<<${C_RESET}"
