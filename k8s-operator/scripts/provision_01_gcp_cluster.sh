#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 1: GCP APIs & GKE Cluster Initialization
# ==============================================================================
# Idempotent setup script that enables the GCP APIs and bootstraps the bare
# GKE cluster. The target namespace is created later, by the operator deploy
# in step 03.
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

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "REGION" "us-east4" "Enter GKE GCP Region"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'container.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      container.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: GKE Cluster Provisioning
verify_cluster() {
  gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_cluster() {
  local cluster_type="${CLUSTER_TYPE:-standard}"
  local machine_type="${MACHINE_TYPE:-e2-standard-4}"
  local num_nodes="${NUM_NODES:-1}"
  local min_nodes="${MIN_NODES:-1}"
  local max_nodes="${MAX_NODES:-5}"
  local enable_autoscaling="${ENABLE_AUTOSCALING:-true}"

  if [ "$cluster_type" = "autopilot" ]; then
    print_info "Creating GKE Autopilot Cluster '$CLUSTER_NAME'..."
    print_info "⏱  Estimated Duration: ~5-8 minutes (GCP managed control plane & node auto-provisioning)."
    gcloud container clusters create-auto "$CLUSTER_NAME" \
        --region "$REGION" \
        --project "$PROJECT_ID" \
        --quiet
  else
    print_info "Creating GKE Standard Cluster '$CLUSTER_NAME' (Type: $machine_type, Autoscaling: $enable_autoscaling [$min_nodes..$max_nodes])..."
    print_info "⏱  Estimated Duration: ~3-5 minutes (Phase 1: Control Plane ~2m | Phase 2: Node Pool ~2m)."
    local create_cmd=(
      gcloud container clusters create "$CLUSTER_NAME"
      --region "$REGION"
      --machine-type="$machine_type"
      --num-nodes="$num_nodes"
      --workload-pool="${PROJECT_ID}.svc.id.goog"
      --addons=GcpFilestoreCsiDriver
      --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS
      --project "$PROJECT_ID"
      --quiet
    )
    if [ "$enable_autoscaling" = "true" ]; then
      create_cmd+=(--enable-autoscaling --min-nodes="$min_nodes" --max-nodes="$max_nodes")
    fi
    "${create_cmd[@]}"
  fi
}

# Step 3: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]]
}
execute_kubeconfig() {
  connect_cluster
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP Cluster APIs" verify_apis execute_apis 30
run_step "2. Provision GKE Cluster" verify_cluster execute_cluster 10
run_step "3. Connect kubectl" verify_kubeconfig execute_kubeconfig 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  GKE Infrastructure Provisioned Successfully!  <<<${C_RESET}"
