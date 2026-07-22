#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 10: Teardown Inference Replay Proxy
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

# Default value used only for envsubst expansion during delete; its concrete
# value does not affect which resources are removed.
export REPLAY_IMAGE="${REPLAY_IMAGE:-placeholder}"

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
  export NAMESPACE REPLAY_IMAGE
  make -C "${OPERATOR_DIR}" undeploy-inference-replay ignore-not-found=true || true
  echo -e "  ${C_GREEN}✓ Inference Replay Proxy undeploy command completed.${C_RESET}"
fi

# ─── Step 3: Restore original LiteLLM Service ─────────────────────────────────
# The proxy overrides the `litellm` Service to forward to itself. Removing the
# proxy also removes that Service, so we re-apply just the LiteLLM Service
# manifest to bring the original selector back. Applying only the Service
# (rather than `make deploy-litellm`) avoids re-running envsubst over the
# LiteLLM ConfigMap, which templates ${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}
# and would silently corrupt the running LiteLLM if those vars are unset.
# Safe no-op when LiteLLM was never deployed.
LITELLM_SERVICE_MANIFEST="${OPERATOR_DIR}/config/integrations/litellm/base/service.yaml"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would re-apply ${LITELLM_SERVICE_MANIFEST} to restore the original Service.${C_RESET}"
else
  if kubectl get deployment litellm -n "$NAMESPACE" >/dev/null 2>&1; then
    print_info "Restoring original LiteLLM Service..."
    kubectl apply -n "$NAMESPACE" -f "$LITELLM_SERVICE_MANIFEST" || true
  fi
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ Inference Replay Proxy successfully undeployed!${C_RESET}"
