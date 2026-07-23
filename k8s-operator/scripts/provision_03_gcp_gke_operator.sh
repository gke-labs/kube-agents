#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 2: Deploy Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script that installs the CRDs and deploys the operator to the cluster.
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
check_prereqs "gcloud" "kubectl" "make"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Operator Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  (kubectl get ns "${NAMESPACE}" >/dev/null 2>&1 || kubectl get ns default >/dev/null 2>&1)
}
execute_kubeconfig() {
  connect_cluster
}

# Step 2: Ensure cert-manager is installed
verify_cert_manager() {
  local avail
  avail=$(kubectl get deployment cert-manager-webhook -n cert-manager -o jsonpath='{.status.availableReplicas}' 2>/dev/null || echo 0)
  [ "${avail:-0}" -ge 1 ]
}
execute_cert_manager() {
  print_info "cert-manager not found. Installing cert-manager..."

  # Check if the cluster is a GKE Autopilot cluster
  local is_autopilot
  is_autopilot=$(kubectl get nodes -o jsonpath='{.items[*].spec.providerID}' 2>/dev/null | grep -q "gce://.*/gk3-" && echo "true" || echo "false")

  if [ "$is_autopilot" = "true" ]; then
    print_info "GKE Autopilot cluster detected. Deploying cert-manager with leader-election disabled..."
  else
    print_info "Standard cluster detected. Installing standard cert-manager..."
  fi

  kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml || return 1
  
  # Wait for the deployments to be created by the API server
  ensure_k8s_resource_exists "deployment/cert-manager-cainjector" "cert-manager" || return 1
  ensure_k8s_resource_exists "deployment/cert-manager" "cert-manager" || return 1
  ensure_k8s_resource_exists "deployment/cert-manager-webhook" "cert-manager" || return 1
  
  if [ "$is_autopilot" = "true" ]; then
    # Patch deployments to disable leader election due to Autopilot kube-system namespace restrictions
    print_info "Patching cert-manager cainjector and controller arguments..."
    kubectl patch deployment cert-manager-cainjector -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/1", "value": "--leader-elect=false"}]' || return 1
    kubectl patch deployment cert-manager -n cert-manager --type='json' -p='[{"op": "replace", "path": "/spec/template/spec/containers/0/args/2", "value": "--leader-elect=false"}]' || return 1
  fi

  print_info "Patching cert-manager resources to comply with baseline quotas..."
  local resources_patch='[{"op": "add", "path": "/spec/template/spec/containers/0/resources", "value": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "128Mi"}}}]'
  kubectl patch deployment cert-manager -n cert-manager --type='json' -p="${resources_patch}" || return 1
  kubectl patch deployment cert-manager-cainjector -n cert-manager --type='json' -p="${resources_patch}" || return 1
  kubectl patch deployment cert-manager-webhook -n cert-manager --type='json' -p="${resources_patch}" || return 1

  # Wait for cert-manager pods to become healthy
  wait_for_k8s_resource "deployment/cert-manager" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-cainjector" "cert-manager" "Available" "120s" || return 1
  wait_for_k8s_resource "deployment/cert-manager-webhook" "cert-manager" "Available" "120s" || return 1
}

# Step 3: Deploy Operator (CRDs & Controller manager)
verify_operator() {
  # Always return false to ensure operator updates/re-deployments are applied
  return 1
}
execute_operator() {
  print_info "Installing Custom Resource Definitions (CRDs)..."
  make -C "$OPERATOR_DIR" install || return 1
  print_info "Deploying Operator Controller Manager to the GKE cluster..."
  make -C "$OPERATOR_DIR" deploy || return 1
  wait_for_k8s_resource "deployment/kubeagents-controller-manager" "${NAMESPACE:-kubeagents-system}" "Available" "180s" || return 1
}

# Step 1b: Ensure Filestore CSI Driver is enabled for RWX storage
verify_filestore_addon() {
  local enabled
  enabled=$(gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" --format="value(addonsConfig.gcpFilestoreCsiDriverConfig.enabled)" 2>/dev/null || echo "false")
  [ "$enabled" = "True" ] || [ "$enabled" = "true" ]
}
execute_filestore_addon() {
  print_info "Enabling GKE Filestore CSI Driver for RWX storage support..."
  local active_op
  active_op=$(gcloud container operations list --region="$REGION" --project="$PROJECT_ID" --filter="targetLink:$CLUSTER_NAME AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for ongoing cluster operation $active_op to complete..."
    gcloud container operations wait "$active_op" --region="$REGION" --project="$PROJECT_ID" || true
  fi

  gcloud container clusters update "$CLUSTER_NAME" \
      --region "$REGION" \
      --update-addons GcpFilestoreCsiDriver=ENABLED \
      --project "$PROJECT_ID"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_deploy_step "1b. Ensure Filestore CSI Driver" verify_filestore_addon execute_filestore_addon 5
run_deploy_step "2. Ensure cert-manager" verify_cert_manager execute_cert_manager 5
run_deploy_step "3. Deploy Kubernetes Operator" verify_operator execute_operator 0

print_success "Kubernetes Operator deployed successfully!"
