#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 9: Teardown Inference Replay Proxy
# ==============================================================================
# Idempotent script to undeploy the Inference Replay proxy and restore the
# original LiteLLM Service. Safe to run even when the proxy was never deployed.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "$SCRIPT_DIR" == */scripts ]]; then
  OPERATOR_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  OPERATOR_DIR="${SCRIPT_DIR}"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# Default values used only for envsubst expansion during delete; their concrete
# values do not affect which resources are removed.
export REPLAY_IMAGE="${REPLAY_IMAGE:-placeholder}"
export REPLAY_MODE="${REPLAY_MODE:-off}"

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently undeploy the Inference Replay Proxy. The persistent cache (PVC) will be deleted." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping Inference Replay cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 2: Undeploy Inference Replay Proxy ──────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying Inference Replay Proxy...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy Inference Replay Proxy in namespace '${NAMESPACE}'.${C_RESET}"
else
  export NAMESPACE REPLAY_IMAGE REPLAY_MODE
  make -C "${OPERATOR_DIR}" undeploy-inference-replay ignore-not-found=true || true
  echo -e "  ${C_GREEN}✓ Inference Replay Proxy undeploy command completed.${C_RESET}"
fi

# ─── Step 3: Restore original LiteLLM Service ─────────────────────────────────
# The proxy overrides the `litellm` Service to forward to itself. Removing the
# proxy also removes that Service, so we re-apply the LiteLLM base to bring the
# original Service back. Safe no-op when LiteLLM was never deployed.
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would re-apply LiteLLM base to restore the original Service.${C_RESET}"
else
  if kubectl get deployment litellm -n "$NAMESPACE" >/dev/null 2>&1; then
    print_info "Restoring original LiteLLM Service..."
    export NAMESPACE MODEL_PROVIDER MODEL_DEFAULT_NAME
    make -C "${OPERATOR_DIR}" deploy-litellm || true
  fi
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ Inference Replay Proxy successfully undeployed!${C_RESET}"
