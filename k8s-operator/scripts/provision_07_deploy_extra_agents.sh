#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 7: Deploy OperatorAgent & DevTeamAgent Custom Resources
# ==============================================================================
# Idempotent script that lists GKE clusters, selects the first one,
# discovers its first active namespace/workload, and deploys targeted 
# OperatorAgent and DevTeamAgent custom resources.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Extra Agents"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter Host GKE Cluster Name"
init_var "REGION" "us-east4" "Enter Host GKE Region"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl to Host
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  connect_cluster
}

# Step 2: Discover target cluster & namespace, and deploy extra agents
verify_extra_agents() {
  # Always return 1 to ensure custom resources are applied/updated
  return 1
}
# Helper function to dynamically create and bind GCP GSAs for isolated Workload Identity
create_dedicated_agent_iam() {
  local agent_name=$1
  local ksa_name=$2
  local gsa_name=$3
  shift 3
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  if ! gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    print_info "Creating dedicated GSA ${gsa_name} for ${agent_name}..."
    gcloud iam service-accounts create "${gsa_name}" \
        --display-name="${agent_name} GSA" \
        --project="${PROJECT_ID}"
    sleep 10
  fi
  
  print_info "Configuring GCP IAM roles for dedicated GSA ${gsa_name}..."
  for role in "${roles[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${gsa_email}" \
        --role="${role}" \
        --quiet >/dev/null
  done
  
  print_info "Binding Workload Identity for ${gsa_name} to host KSA ${ksa_name}..."
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
  gcloud iam service-accounts add-iam-policy-binding "${gsa_email}" \
      --role="roles/iam.workloadIdentityUser" \
      --member="${wi_member}" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null
}

execute_extra_agents() {
  export TARGET_CLUSTER_NAME="ac-3"
  export TARGET_CLUSTER_LOCATION="us-central1"
  export TARGET_NAMESPACE="devteam-app-ns"
  
  print_success "Selected Target Cluster: ${TARGET_CLUSTER_NAME} (${TARGET_CLUSTER_LOCATION})"
  print_success "Target Workload Namespace: ${TARGET_NAMESPACE}"

  # Define unique, aligned Operator Agent KSA and GSA names (reconciled by Go controller)
  export TARGET_OP_KSA="operator-agent-${TARGET_CLUSTER_NAME}"
  export TARGET_OP_GSA="op-gsa-${TARGET_CLUSTER_NAME}"

  # Dynamically bind Workload Identity in GCP for the unique KSA
  create_dedicated_agent_iam "Operator Agent" "${TARGET_OP_KSA}" "${TARGET_OP_GSA}" \
      "roles/container.clusterViewer" \
      "roles/monitoring.viewer" \
      "roles/logging.viewer" \
      "roles/iam.serviceAccountUser"

  # Define unique, aligned DevTeam Agent KSA and GSA names (reconciled by Go controller)
  export TARGET_DT_KSA="devteam-agent-${TARGET_CLUSTER_NAME}-${TARGET_NAMESPACE}"
  export TARGET_DT_GSA="dt-gsa-${TARGET_CLUSTER_NAME}"

  # Dynamically bind Workload Identity in GCP for the unique KSA
  create_dedicated_agent_iam "DevTeam Agent" "${TARGET_DT_KSA}" "${TARGET_DT_GSA}" \
      "roles/container.clusterViewer" \
      "roles/monitoring.viewer" \
      "roles/logging.viewer" \
      "roles/iam.serviceAccountUser"

  # Reconnect to host cluster to deploy custom resources
  print_info "Reconnecting to host cluster ${CLUSTER_NAME}..."
  connect_cluster

  # Deploy OperatorAgent Custom Resource
  local OP_TEMPLATE="${OPERATOR_DIR}/examples/operatoragent.yaml.template"
  local OP_MANIFEST="${OPERATOR_DIR}/examples/operatoragent.yaml"
  if [ -f "$OP_TEMPLATE" ]; then
    print_info "Generating OperatorAgent manifest..."
    envsubst < "$OP_TEMPLATE" > "$OP_MANIFEST"
    print_info "Applying OperatorAgent Custom Resource..."
    kubectl apply -f "$OP_MANIFEST"
  else
    print_warning "OperatorAgent template not found at ${OP_TEMPLATE}!"
  fi

  # Deploy DevTeamAgent Custom Resource
  local DT_TEMPLATE="${OPERATOR_DIR}/examples/devteamagent.yaml.template"
  local DT_MANIFEST="${OPERATOR_DIR}/examples/devteamagent.yaml"
  if [ -f "$DT_TEMPLATE" ]; then
    print_info "Generating DevTeamAgent manifest..."
    envsubst < "$DT_TEMPLATE" > "$DT_MANIFEST"
    print_info "Applying DevTeamAgent Custom Resource..."
    kubectl apply -f "$DT_MANIFEST"
  else
    print_warning "DevTeamAgent template not found at ${DT_TEMPLATE}!"
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect Host kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Discover and Deploy Extra Agents" verify_extra_agents execute_extra_agents 0

print_success "Extra agents deployed successfully!"
