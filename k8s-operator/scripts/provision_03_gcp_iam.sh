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

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Controller & Agent Identities"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"

# ─── Helper Functions for Agents ──────────────────────────────────────────────
verify_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
  
  gcloud iam service-accounts get-iam-policy "${gsa_email}" --project="${PROJECT_ID}" --format="json" 2>/dev/null | grep -F -q "${wi_member}" || return 1
  
  local project_roles
  project_roles=$(gcloud projects get-iam-policy "${PROJECT_ID}" --flatten="bindings[].members" --filter="bindings.members:serviceAccount:${gsa_email}" --format="value(bindings.role)" 2>/dev/null)
  for role in "${roles[@]}"; do
    echo "$project_roles" | grep -q "${role}" || return 1
  done
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
    # Wait for the service account to propagate so IAM can bind roles to it
    wait_for_a_bit 10 "Waiting for GSA creation to propagate"
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

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'container.googleapis.com' && \
  echo "$out" | grep -q 'aiplatform.googleapis.com' && \
  echo "$out" | grep -q 'cloudresourcemanager.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      container.googleapis.com \
      aiplatform.googleapis.com \
      cloudresourcemanager.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: Configure Controller IAM
verify_controller() {
  verify_agent_iam "${CONTROLLER_KSA_NAME}" "${CONTROLLER_GSA_NAME}" \
      "roles/container.clusterViewer" \
      "roles/container.admin" \
      "roles/container.clusterAdmin"
}
execute_controller() {
  execute_agent_iam "Kubeagents Controller Manager" "${CONTROLLER_KSA_NAME}" "${CONTROLLER_GSA_NAME}" \
      "roles/container.clusterViewer" \
      "roles/container.admin" \
      "roles/container.clusterAdmin"
}

# Step 3: Configure Platform Agent IAM
verify_platform_agent() {
  verify_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer"
}
execute_platform_agent() {
  execute_agent_iam "Platform Agent" "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer"
}

# Step 4: Configure Operator Agent IAM
verify_operator_agent() {
  verify_agent_iam "${OPERATOR_AGENT_KSA_NAME}" "${OPERATOR_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer" \
      "roles/container.admin" \
      "roles/container.clusterAdmin"
}
execute_operator_agent() {
  execute_agent_iam "Operator Agent" "${OPERATOR_AGENT_KSA_NAME}" "${OPERATOR_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer" \
      "roles/container.admin" \
      "roles/container.clusterAdmin"
}

# Step 5: Configure DevTeam Agent IAM
verify_devteam_agent() {
  verify_agent_iam "${DEVTEAM_AGENT_KSA_NAME}" "${DEVTEAM_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer"
}
execute_devteam_agent() {
  execute_agent_iam "DevTeam Agent" "${DEVTEAM_AGENT_KSA_NAME}" "${DEVTEAM_AGENT_GSA_NAME}" \
      "roles/aiplatform.user" \
      "roles/container.clusterViewer"
}

# Step 6: Annotate Controller KSA & Restart Controller Manager Deployment
verify_controller_annotation() {
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 1
  fi
  connect_cluster
  local gsa_email="${CONTROLLER_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  local ann=$(kubectl get serviceaccount "${CONTROLLER_KSA_NAME}" -n "${NAMESPACE}" -o jsonpath='{.metadata.annotations.iam\.gke\.io/gcp-service-account}' 2>/dev/null || echo "")
  [ "$ann" = "$gsa_email" ]
}
execute_controller_annotation() {
  local gsa_email="${CONTROLLER_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  print_info "Annotating ServiceAccount ${CONTROLLER_KSA_NAME} with GSA email..."
  kubectl annotate serviceaccount "${CONTROLLER_KSA_NAME}" \
      --namespace "${NAMESPACE}" \
      iam.gke.io/gcp-service-account="${gsa_email}" \
      --overwrite || return 1

  print_info "Restarting Controller Manager Deployment to apply changes..."
  kubectl rollout restart deployment/kubeagents-controller-manager -n "${NAMESPACE}" || return 1
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable APIs" verify_apis execute_apis 10
run_step "2. Configure Controller Workload Identity & GCP IAM" verify_controller execute_controller 5
run_step "3. Configure Platform Agent Workload Identity & GCP IAM" verify_platform_agent execute_platform_agent 5
run_step "4. Configure Operator Agent Workload Identity & GCP IAM" verify_operator_agent execute_operator_agent 5
run_step "5. Configure DevTeam Agent Workload Identity & GCP IAM" verify_devteam_agent execute_devteam_agent 5
run_step "6. Annotate Controller KSA & Restart Deployment" verify_controller_annotation execute_controller_annotation 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Controller & Agent GCP Permissions Configured Successfully!  <<<${C_RESET}"
