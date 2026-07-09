#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 7: Teardown LiteLLM Gateway
# ==============================================================================
# Idempotent script to undeploy the LiteLLM gateway.
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

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently undeploy the LiteLLM Gateway." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping LiteLLM Gateway cleanup.${C_RESET}"
  exit 0
fi


# ─── Step 2: Undeploy LiteLLM Gateway ─────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying LiteLLM Gateway...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy LiteLLM Gateway in namespace '${NAMESPACE}'.${C_RESET}"
else
  export NAMESPACE MODEL_PROVIDER MODEL_DEFAULT_NAME
  make -C "${OPERATOR_DIR}" undeploy-litellm || true
  echo -e "  ${C_GREEN}✓ LiteLLM Gateway undeploy command completed.${C_RESET}"
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ LiteLLM Gateway successfully undeployed!${C_RESET}"
