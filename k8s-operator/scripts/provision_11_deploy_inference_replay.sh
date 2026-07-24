#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 10: Deploy Inference Replay Proxy (optional)
# ==============================================================================
# Idempotent script that deploys the Inference Replay proxy in front of the
# LiteLLM gateway. Skipped unless INFERENCE_REPLAY_ENABLED=true.
#
# The proxy intercepts the `litellm` Service so agents need no configuration
# changes. With REPLAY_MODE=off (default) it is a pure pass-through; flip the
# `inference-replay-config` ConfigMap to `on` to start recording/replaying.
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

# ─── Opt-In Gate ──────────────────────────────────────────────────────────────
init_var "INFERENCE_REPLAY_ENABLED" "false" "Deploy Inference Replay proxy? (true/false)"
if ! is_truthy "${INFERENCE_REPLAY_ENABLED}"; then
  echo -e "  ${C_CYAN}ℹ Skipping Inference Replay (INFERENCE_REPLAY_ENABLED=${INFERENCE_REPLAY_ENABLED}).${C_RESET}"
  exit 0
fi

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud" "kubectl" "envsubst"

# ─── Configuration & State Restoration ────────────────────────────────────────
print_step "Setting up Configuration State for Inference Replay Deployment"
load_state

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "us-east4" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
init_var "REPLAY_IMAGE" "ghcr.io/gke-labs/kube-agents/replay-proxy:latest" "Enter Replay Proxy container image"

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

# Step 2: Deploy Inference Replay proxy
verify_inference_replay() {
  # Always return false to ensure that Kustomize builds and configs are applied idempotently on every run
  return 1
}
execute_inference_replay() {
  print_info "Deploying Inference Replay proxy into GKE (image=${REPLAY_IMAGE})..."
  export NAMESPACE REPLAY_IMAGE
  make -C "${OPERATOR_DIR}" deploy-inference-replay || return 1
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Connect kubectl" verify_kubeconfig execute_kubeconfig 0
run_step "2. Deploy Inference Replay Proxy" verify_inference_replay execute_inference_replay 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ Inference Replay Proxy deployed successfully to GKE!${C_RESET}"
echo -e "  ${C_CYAN}ℹ Deployed in pass-through mode (mode=off). Toggle on at runtime (no pod restart):${C_RESET}"
echo -e "      ${C_WHITE}kubectl patch configmap inference-replay-config -n ${NAMESPACE} --type merge -p '{\"data\":{\"mode\":\"on\"}}'${C_RESET}"
