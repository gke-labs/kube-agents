#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 6: Deploy PlatformAgent Custom Resource Manifest
# ==============================================================================
# Idempotent script that connects to GKE, renders the platform-agent.yaml 
# template, and deploys it to the cluster.
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
print_step "Setting up Configuration State for Agent Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "ENABLE_GVISOR" "false" "Enable GKE Sandbox (gVisor) runtime isolation? (true/false)"
init_var_model_provider

# Map global state variables to expected template variables
export GSA_NAME="${PLATFORM_AGENT_GSA_NAME}"
export KSA_NAME="${PLATFORM_AGENT_KSA_NAME}"

DEFAULT_AGENT_IMAGE="ghcr.io/gke-labs/kube-agents/platform-agent"
init_var "AGENT_IMAGE" "$DEFAULT_AGENT_IMAGE" "Enter Platform Agent Image Path"
init_var "AGENT_TAG" "latest" "Enter Platform Agent Image Tag"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Connect kubectl
verify_kubeconfig() {
  local current_ctx
  current_ctx=$(kubectl config current-context 2>/dev/null || echo "")
  [[ "$current_ctx" == *"${PROJECT_ID}"* && "$current_ctx" == *"${CLUSTER_NAME}"* ]] && \
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

  # Determine if Google Chat should be enabled
  if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
    export GOOGLE_CHAT_ENABLED="true"
    if [ -z "${CHAT_TOPIC_NAME}" ] || [ -z "${CHAT_SUB_NAME}" ]; then
      print_warning "Google Chat integration is enabled but CHAT_TOPIC_NAME or CHAT_SUB_NAME is missing. It may not work properly."
    fi
  else
    export GOOGLE_CHAT_ENABLED="false"
    export CHAT_TOPIC_NAME=""
    export CHAT_SUB_NAME=""
    export ALLOWED_USERS=""
  fi

  # Determine if Slack should be enabled
  if [ "${SLACK_ENABLED:-false}" = "true" ]; then
    export SLACK_ENABLED="true"
    if [ -z "${SLACK_BOT_TOKEN}" ] || [ -z "${SLACK_APP_TOKEN}" ]; then
      print_warning "Slack integration is enabled but SLACK_BOT_TOKEN or SLACK_APP_TOKEN is missing. It may not work properly."
    fi
  else
    export SLACK_ENABLED="false"
    export SLACK_BOT_TOKEN=""
    export SLACK_APP_TOKEN=""
    export SLACK_ALLOWED_USERS=""
    export SLACK_HOME_CHANNEL=""
    export SLACK_HOME_CHANNEL_NAME=""
  fi

  # Handle optional GitHub integration variables
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    export GITHUB_FULL_REPO="${GITHUB_ORG}/${GITHUB_REPO}"
  else
    export GITHUB_FULL_REPO=""
  fi

  # Ensure variables are explicitly exported so envsubst can access them
  export PROJECT_ID REGION CLUSTER_NAME MODEL_DEFAULT_NAME MODEL_PROVIDER GSA_NAME CHAT_SUB_NAME CHAT_TOPIC_NAME GOOGLE_CHAT_MODE ALLOWED_USERS AGENT_IMAGE NAMESPACE KSA_NAME GOOGLE_CHAT_ENABLED SLACK_ENABLED SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME AGENT_TAG GITHUB_FULL_REPO

  envsubst < "$CR_TEMPLATE" > "$CR_MANIFEST"
  
  if [[ "$ENABLE_GVISOR" =~ ^(true|yes|1)$ ]]; then
    print_info "Enabling gVisor runtimeClassName in '$CR_MANIFEST'..."
    sed -i.bak 's/# runtimeClassName: gvisor/runtimeClassName: gvisor/g' "$CR_MANIFEST" && rm -f "${CR_MANIFEST}.bak"
  fi

  print_info "Applying 'platform-agent' Custom Resource to the GKE cluster..."
  kubectl apply -f "$CR_MANIFEST"
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Apply PlatformAgent Custom Resource" verify_custom_resource execute_custom_resource 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ PlatformAgent Custom Resource applied successfully to GKE!${C_RESET}"
