#!/usr/bin/env bash
# ==============================================================================
# 🗑️ Staging Environment PoC Teardown
# ==============================================================================
# This script cleanly destroys the GKE clusters and associated resources
# provisioned by this PoC setup.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ANSI Colors
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'

log_info() {
  echo -e "${C_CYAN}INFO: $1${C_RESET}"
}

log_success() {
  echo -e "${C_GREEN}SUCCESS: $1${C_RESET}"
}

log_warn() {
  echo -e "${C_YELLOW}WARN: $1${C_RESET}"
}

# 1. (Optional) Helm Uninstall
# We don't strictly need to do this if we are destroying the cluster,
# but it's good practice to show how to clean up workloads only.
NAMESPACE="staging-workloads"
if command -v helm &> /dev/null && command -v kubectl &> /dev/null; then
  PROJECT_ID=$(cd "$SCRIPT_DIR" && terraform output -raw project_id 2>/dev/null || echo "")
  STANDARD_CLUSTERS_LIST=$(cd "$SCRIPT_DIR" && terraform output -raw standard_clusters_list 2>/dev/null || echo "")

  # Fetch Autopilot clusters list directly from Terraform
  AUTOPILOT_CLUSTERS_LIST=$(cd "$SCRIPT_DIR" && terraform output -raw autopilot_clusters_list 2>/dev/null || echo "")

  uninstall_from_cluster() {
    local c_name="$1"
    local c_loc="$2"
    local p_id="$3"
    
    if [ -n "$c_name" ]; then
      log_info "Configuring kubectl credentials for ${c_name}..."
      gcloud container clusters get-credentials "${c_name}" --region "${c_loc}" --project "${p_id}" &>/dev/null || return 0
      
      log_info "Attempting to uninstall Helm workloads from cluster '${c_name}'..."
      if helm status workload-bundle --namespace "$NAMESPACE" &>/dev/null; then
        helm uninstall workload-bundle --namespace "$NAMESPACE"
        log_success "Helm workloads uninstalled from ${c_name}."
      else
        log_info "No Helm release 'workload-bundle' found in namespace '${NAMESPACE}' on ${c_name}."
      fi
    fi
  }

  # Uninstall from Autopilot clusters dynamically
  if [ -n "$AUTOPILOT_CLUSTERS_LIST" ]; then
    while IFS='|' read -r c_name c_loc _; do
      if [ -n "$c_name" ]; then
        uninstall_from_cluster "$c_name" "$c_loc" "$PROJECT_ID"
      fi
    done <<< "$AUTOPILOT_CLUSTERS_LIST"
  fi

  # Uninstall from Standard clusters dynamically
  if [ -n "$STANDARD_CLUSTERS_LIST" ]; then
    while IFS='|' read -r c_name c_loc _; do
      if [ -n "$c_name" ]; then
        uninstall_from_cluster "$c_name" "$c_loc" "$PROJECT_ID"
      fi
    done <<< "$STANDARD_CLUSTERS_LIST"
  fi
fi

# 2. Terraform Destroy
log_info "Destroying infrastructure via Terraform..."
log_info "This will delete the GKE cluster and all resources inside it."
log_info "This may take 5-10 minutes..."

cd "$SCRIPT_DIR"
if [ -d ".terraform" ]; then
  terraform destroy -input=false -auto-approve
  log_success "Infrastructure destroyed successfully."
else
  log_warn "Terraform not initialized in this directory. Cannot destroy."
fi
