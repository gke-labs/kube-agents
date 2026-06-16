#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 4: GKE Kubernetes Secrets Setup
# ==============================================================================
# Idempotent setup script to configure local Kubernetes secrets directly.
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
check_prereqs "gcloud" "kubectl" "openssl"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"

# Securely prompt for Gemini API Key if not present in environment or state
if [ -z "${GEMINI_API_KEY:-}" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    export GEMINI_API_KEY="placeholder"
  else
    echo -ne "  ${C_CYAN}Enter your GEMINI_API_KEY (press ENTER to default to empty placeholder): ${C_RESET}"
    read -s -r INPUT_KEY
    echo ""
    export GEMINI_API_KEY="${INPUT_KEY:-placeholder}"
    printf "export GEMINI_API_KEY=%q\n" "${GEMINI_API_KEY}" >> "$VARS_FILE"
  fi
fi

if [ -z "${API_SERVER_KEY:-}" ]; then
  print_info "Generating a secure random API_SERVER_KEY..."
  export API_SERVER_KEY=$(openssl rand -hex 16)
  printf "export API_SERVER_KEY=%q\n" "${API_SERVER_KEY}" >> "$VARS_FILE"
fi

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

# Step 2: Sync API Keys to GKE Namespace Secrets
verify_k8s_secrets() {
  # Always return false to ensure secret updates are synchronized to GKE
  return 1
}
execute_k8s_secrets() {
  if [ "$GEMINI_API_KEY" = "placeholder" ]; then
    print_warning "GEMINI_API_KEY is currently a placeholder. The platform agent will run but cannot authenticate with Gemini until updated."
  fi

  print_info "Writing Kubernetes Secret 'platform-agent-secrets' into '$NAMESPACE'..."
  kubectl create secret generic platform-agent-secrets \
      --namespace="$NAMESPACE" \
      --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
      --from-literal=API_SERVER_KEY="$API_SERVER_KEY" \
      --dry-run=client -o yaml | kubectl apply -f -
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Write GKE Namespace Secrets" verify_k8s_secrets execute_k8s_secrets 0

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Secrets Configured & Synchronized Successfully!  <<<${C_RESET}"
