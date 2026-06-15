#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 1: GCP APIs & GKE Cluster Initialization
# ==============================================================================
# Idempotent setup script to bootstrap the bare GKE cluster and namespace.
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

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "NAMESPACE" "agent-system" "Enter GKE Target Namespace"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"

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
      --project="$PROJECT_ID"
}

# Step 2: GKE Cluster Provisioning
verify_cluster() {
  gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_cluster() {
  print_info "Creating GKE Standard Cluster with Workload Identity. This takes approximately 5-8 minutes in Google Cloud..."
  gcloud beta container clusters create "$CLUSTER_NAME" \
      --region "$REGION" \
      --machine-type="e2-standard-4" \
      --num-nodes=1 \
      --workload-pool="${PROJECT_ID}.svc.id.goog" \
      --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS \
      --project "$PROJECT_ID" \
      --quiet
}

# Step 3: Connect kubectl & Create Namespace
verify_kubeconfig() {
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  connect_cluster
  print_info "Creating namespace '$NAMESPACE'..."
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP Cluster APIs" verify_apis execute_apis 30
run_step "2. Provision GKE Cluster" verify_cluster execute_cluster 10
run_step "3. Connect kubectl & Create Namespace" verify_kubeconfig execute_kubeconfig 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  GKE Infrastructure Provisioned Successfully!  <<<${C_RESET}"
