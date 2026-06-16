#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 5: Agent GCP Workload Identity & AI Permissions
# ==============================================================================
# Idempotent script for granting AI and Workload Identity permissions to the 
# Agent GSA, allowing the Kubernetes Pods to authenticate and call Gemini.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Agent Identity"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "GSA_NAME" "platform-agent-gsa" "Enter Google Service Account Name for the Agent"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'aiplatform.googleapis.com' && \
  echo "$out" | grep -q 'cloudresourcemanager.googleapis.com'
}
execute_apis() {
  gcloud services enable \
      aiplatform.googleapis.com \
      cloudresourcemanager.googleapis.com \
      --project="$PROJECT_ID"
}

# Step 2: Bind Agent GSA AI & Workload Identity Permissions
verify_agent_iam() {
  local gsa_email="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]"
  
  # Verify Workload Identity binding on the GSA
  gcloud iam service-accounts get-iam-policy "${gsa_email}" --project="${PROJECT_ID}" --format="json" 2>/dev/null | grep -F -q "${wi_member}" || return 1
  
  # Verify project-level roles are bound to the GSA
  local project_roles
  project_roles=$(gcloud projects get-iam-policy "${PROJECT_ID}" --flatten="bindings[].members" --filter="bindings.members:serviceAccount:${gsa_email}" --format="value(bindings.role)" 2>/dev/null)
  echo "$project_roles" | grep -q "roles/aiplatform.user" && echo "$project_roles" | grep -q "roles/container.clusterViewer"
}
execute_agent_iam() {
  local gsa_email="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

  # Ensure the GSA exists in case this script is run out of sequence
  if ! gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    print_info "Creating GSA ${GSA_NAME}..."
    gcloud iam service-accounts create "${GSA_NAME}" \
        --display-name="Platform Agent Bot GSA" \
        --project="${PROJECT_ID}"
  fi

  print_info "Applying Workload Identity and AI IAM Policies..."

  # 1. Allow bot to call Gemini (Vertex AI)
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/aiplatform.user" \
      --quiet >/dev/null

  # 2. Allow operator/bot to view cluster info
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${gsa_email}" \
      --role="roles/container.clusterViewer" \
      --quiet >/dev/null

  # 3. Workload Identity Binding (maps Kubernetes SA to Google SA)
  local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]"
  gcloud iam service-accounts add-iam-policy-binding "${gsa_email}" \
      --role="roles/iam.workloadIdentityUser" \
      --member="${wi_member}" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable APIs" verify_apis execute_apis 10
run_step "2. Configure Agent Workload Identity & AI Permissions" verify_agent_iam execute_agent_iam 5

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Agent GCP Permissions Configured Successfully!  <<<${C_RESET}"
