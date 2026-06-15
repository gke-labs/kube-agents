#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 2: GCP Secret Manager & GKE Secrets Setup
# ==============================================================================
# Idempotent setup script to bootstrap GCP Secret Manager APIs and K8s secrets.
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

if [ -z "${API_SERVER_KEY:-}" ]; then
  print_info "Generating a secure random API_SERVER_KEY..."
  export API_SERVER_KEY=$(openssl rand -hex 16)
  echo "export API_SERVER_KEY=\"${API_SERVER_KEY}\"" >> "$VARS_FILE"
fi

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "openssl"

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Enable APIs
verify_apis() {
  local out=$(gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null || echo "")
  echo "$out" | grep -q 'secretmanager.googleapis.com'
}
execute_apis() {
  gcloud services enable secretmanager.googleapis.com --project="$PROJECT_ID"
}

# Step 2: Connect kubectl
verify_kubeconfig() {
  kubectl get namespace "$NAMESPACE" >/dev/null 2>&1
}
execute_kubeconfig() {
  connect_cluster
}

# Step 3: Setup Secret Manager Placeholders
verify_secrets() {
  gcloud secrets describe "GEMINI_API_KEY" --project="$PROJECT_ID" >/dev/null 2>&1
}
execute_secrets() {
  for SECRET in "GEMINI_API_KEY"; do
    if ! gcloud secrets describe "$SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
      echo -ne "  ${C_CYAN}Secret '$SECRET' not found in cloud. Enter actual key value now (or press ENTER to create empty placeholder): ${C_RESET}"
      read -s -r INPUT_KEY
      echo ""
      local VAL="${INPUT_KEY:-placeholder}"
      echo -n "$VAL" | gcloud secrets create "$SECRET" --data-file=- --replication-policy="automatic" --project="$PROJECT_ID"
      print_success "Secret '$SECRET' created in GCP Secret Manager."
    fi
  done
}

# Step 4: Sync API Keys to GKE Namespace Secrets
verify_k8s_secrets() {
  # Always return false to ensure secret updates in Secret Manager are synchronized to GKE
  return 1
}
execute_k8s_secrets() {
  print_info "Resolving keys from GCP Secret Manager..."
  local GEMINI_KEY=$(gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "placeholder")
  
  if [ "$GEMINI_KEY" = "placeholder" ]; then
    print_warning "GEMINI_API_KEY is currently a placeholder in GCP Secret Manager. The platform agent will run but cannot authenticate with Gemini until updated."
  fi

  print_info "Writing Kubernetes Secret 'platform-agent-secrets' into '$NAMESPACE'..."
  kubectl create secret generic platform-agent-secrets \
      --namespace="$NAMESPACE" \
      --from-literal=GEMINI_API_KEY="$GEMINI_KEY" \
      --from-literal=API_SERVER_KEY="$API_SERVER_KEY" \
      --dry-run=client -o yaml | kubectl apply -f -
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Enable GCP Secret Manager API" verify_apis execute_apis 10
run_step "2. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "3. Setup Secret Manager Placeholders" verify_secrets execute_secrets 0
run_step "4. Sync API Keys to GKE Namespace Secrets" verify_k8s_secrets execute_k8s_secrets 0

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Secrets Configured & Synchronized Successfully!  <<<${C_RESET}"
