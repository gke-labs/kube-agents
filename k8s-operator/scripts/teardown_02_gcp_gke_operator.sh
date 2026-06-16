#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 2: Teardown Kubernetes Operator (CRDs & Controller Manager)
# ==============================================================================
# Idempotent script to clean up the deployed operator and CRDs.
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
confirm_action "This will permanently undeploy the Kubernetes Operator and remove its CRDs from the GKE cluster." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping operator cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 2: Undeploy Operator Manager ────────────────────────────────────────
OPERATOR_DEPLOYED=$(kubectl get deployment kubeagents-controller-manager -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || echo "")
if [ -n "$OPERATOR_DEPLOYED" ]; then
  echo -e "  ${C_CYAN}ℹ Undeploying Operator Controller Manager from GKE cluster...${C_RESET}"
  make -C "$OPERATOR_DIR" undeploy ignore-not-found=true
  echo -e "  ${C_GREEN}✓ Operator Controller Manager undeployed successfully.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ Operator Controller Manager is already undeployed.${C_RESET}"
fi

# ─── Step 3: Uninstall Custom Resource Definitions (CRDs) ─────────────────────
CRDS_INSTALLED=$(kubectl get crds -o jsonpath='{.items[*].metadata.name}' 2>/dev/null | grep -o 'platformagents.kubeagents.x-k8s.io' || echo "")
if [ -n "$CRDS_INSTALLED" ]; then
  echo -e "  ${C_CYAN}ℹ Uninstalling CRDs from GKE cluster...${C_RESET}"
  make -C "$OPERATOR_DIR" uninstall ignore-not-found=true
  echo -e "  ${C_GREEN}✓ CRDs uninstalled successfully.${C_RESET}"
else
  echo -e "  ${C_GREEN}✓ CRDs are already uninstalled.${C_RESET}"
fi
