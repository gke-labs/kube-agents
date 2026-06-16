#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 6: Deploy Agent Custom Resource Manifest
# ==============================================================================
# Idempotent script that connects to the cluster, renders the platform-agent.yaml
# using envsubst, and applies it to the Kubernetes environment.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Custom Resource Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "MODEL_DEFAULT_NAME" "gemini-3.1-flash-lite" "Enter Model Default Name"
init_var "MODEL_PROVIDER" "gemini" "Enter Model Provider"

# Vars needed for the template via envsubst and the checklist
init_var "GSA_NAME" "platform-agent-gsa" "Enter Google Service Account Name for the Agent"
init_var "CHAT_SUB_NAME" "platform-agent-chat-events-sub" "Enter Pub/Sub Subscription Name"
init_var "CHAT_TOPIC_NAME" "platform-agent-chat-events" "Enter Pub/Sub Topic Name"
init_var "ALLOWED_USERS" "" "Enter Allowed Google Chat Users Emails (comma separated). Leaving it empty will allow all users."
DEFAULT_AGENT_IMAGE="ghcr.io/gke-labs/kube-agents/platform-agent"
init_var "AGENT_IMAGE" "$DEFAULT_AGENT_IMAGE" "Enter Platform Agent Image Path"

# If the user did not provide a tag/digest, default to latest
if [[ "$AGENT_IMAGE" != *":"* && "$AGENT_IMAGE" != *"@"* ]]; then
  AGENT_IMAGE="${AGENT_IMAGE}:latest"
fi

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  connect_cluster
}

# Step 2: Apply PlatformAgent Custom Resource
verify_custom_resource() {
  # Always return false to ensure configuration updates are applied to the Custom Resource
  return 1
}
execute_custom_resource() {
  print_info "Generating custom resource manifest 'platform-agent.yaml' from template..."
  local CR_TEMPLATE="${SCRIPT_DIR}/platform-agent.yaml.template"
  local CR_MANIFEST="${SCRIPT_DIR}/platform-agent.yaml"

  if [ ! -f "$CR_TEMPLATE" ]; then
    print_error "Custom resource template '$CR_TEMPLATE' not found!"
    exit 1
  fi

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME CHAT_SUB_NAME CHAT_TOPIC_NAME ALLOWED_USERS AGENT_IMAGE

  envsubst < "$CR_TEMPLATE" > "$CR_MANIFEST"
  
  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent Custom Resource applied successfully to GKE!${C_RESET}"
