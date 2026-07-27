#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 1a: Optional Dedicated gVisor Node Pool Initialization
# ==============================================================================
# Idempotent script to bootstrap a dedicated GKE Sandbox (gVisor) node pool
# on an existing GKE Standard cluster. Can be run independently for migration.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State"
load_state

init_var "ENABLE_GVISOR" "false" "Enable GKE Sandbox (gVisor) runtime isolation? (true/false)"
if ! is_truthy "$ENABLE_GVISOR"; then
  print_info "Skipping gVisor node pool provisioning (ENABLE_GVISOR=${ENABLE_GVISOR})."
  exit 0
fi

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "GVISOR_POOL_NAME" "gvisor-pool" "Enter GKE Sandbox (gVisor) Node Pool Name"


# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Provision gVisor Node Pool
verify_gvisor_pool() {
  gcloud container node-pools describe "$GVISOR_POOL_NAME" --cluster="$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_gvisor_pool() {
  print_info "Creating dedicated gVisor node pool ('$GVISOR_POOL_NAME'). This takes approximately 3-5 minutes..."
  gcloud container node-pools create "$GVISOR_POOL_NAME" \
      --cluster="$CLUSTER_NAME" \
      --region="$REGION" \
      --machine-type="e2-standard-4" \
      --num-nodes=1 \
      --image-type="cos_containerd" \
      --sandbox=type=gvisor \
      --workload-metadata=GKE_METADATA \
      --project="$PROJECT_ID" \
      --quiet
}

# Step 2: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]]
}
execute_kubeconfig() {
  connect_cluster
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Provision gVisor Node Pool" verify_gvisor_pool execute_gvisor_pool 10
run_step "2. Connect kubectl" verify_kubeconfig execute_kubeconfig 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  GKE gVisor Node Pool Provisioned Successfully!  <<<${C_RESET}"
