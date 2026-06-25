#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 6: Teardown PlatformAgent Custom Resource
# ==============================================================================
# Idempotent script to clean up the applied PlatformAgent Custom Resource (CR)
# and delete the local generated manifest file.
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
confirm_action "This will permanently delete the PlatformAgent Custom Resource and its generated manifest file." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping custom resource cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 2: Delete PlatformAgent Custom Resource ─────────────────────────────
CRD_EXISTS=$(kubectl get crd platformagents.kubeagents.x-k8s.io --ignore-not-found 2>/dev/null || echo "")
if [ -n "$CRD_EXISTS" ]; then
  CR_EXISTS=$(kubectl get platformagents.kubeagents.x-k8s.io platform-agent -n "$NAMESPACE" --ignore-not-found 2>/dev/null || echo "")
  if [ -n "$CR_EXISTS" ]; then
    echo -e "  ${C_CYAN}ℹ Deleting PlatformAgent 'platform-agent'...${C_RESET}"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete PlatformAgent 'platform-agent' in namespace '${NAMESPACE}'.${C_RESET}"
    else
      kubectl delete platformagents.kubeagents.x-k8s.io platform-agent -n "$NAMESPACE" --timeout=60s || {
        echo -e "  ${C_YELLOW}⚠ Timeout waiting for PlatformAgent deletion. Force removing finalizers if present...${C_RESET}"
        kubectl delete validatingwebhookconfiguration kubeagents-validating-webhook-configuration --ignore-not-found 2>/dev/null || true
        kubectl patch platformagents.kubeagents.x-k8s.io platform-agent -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
        kubectl delete platformagents.kubeagents.x-k8s.io platform-agent -n "$NAMESPACE" --ignore-not-found --timeout=30s || true
      }
      echo -e "  ${C_GREEN}✓ PlatformAgent 'platform-agent' successfully deleted.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ PlatformAgent 'platform-agent' does not exist.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ CRD 'platformagents.kubeagents.x-k8s.io' is not registered. Skipping.${C_RESET}"
fi

# ─── Step 3: Undeploy LiteLLM Gateway ─────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying LiteLLM Gateway...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy LiteLLM Gateway in namespace '${NAMESPACE}'.${C_RESET}"
else
  export NAMESPACE MODEL_PROVIDER MODEL_DEFAULT_NAME
  make -C "${OPERATOR_DIR}" undeploy-litellm || true
  echo -e "  ${C_GREEN}✓ LiteLLM Gateway undeploy command completed.${C_RESET}"
fi

# ─── Step 4: Clean up Local Manifest File ─────────────────────────────────────
local_yaml="${SCRIPT_DIR}/platform-agent.yaml"
if [ -f "$local_yaml" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would delete local manifest platform-agent.yaml.${C_RESET}"
  else
    rm -f "$local_yaml"
    echo -e "  ${C_GREEN}✓ Deleted platform-agent.yaml${C_RESET}"
  fi
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ PlatformAgent Custom Resource successfully cleaned up!${C_RESET}"
