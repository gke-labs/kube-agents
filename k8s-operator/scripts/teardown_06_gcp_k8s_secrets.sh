#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 4: Teardown GKE Secrets
# ==============================================================================
# Idempotent script to clean up Kubernetes secrets.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently delete GKE platform-agent-secrets." \
  "GCP Project:$PROJECT_ID" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster & Delete K8s Secret ───────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would connect to GKE cluster and check for K8s secret.${C_RESET}"
  else
    connect_cluster || true
  fi

  # Check if Namespace exists
  NS_EXISTS=""
  if [ "${DRY_RUN:-0}" -ne 1 ]; then
    NS_EXISTS=$(kubectl get namespace "${NAMESPACE}" --ignore-not-found 2>/dev/null || echo "")
  fi
  
  if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$NS_EXISTS" ]; then
    SECRET_EXISTS=""
    if [ "${DRY_RUN:-0}" -ne 1 ]; then
      SECRET_EXISTS=$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || echo "")
    fi

    if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$SECRET_EXISTS" ]; then
      echo -e "  ${C_CYAN}ℹ Deleting GKE Secret 'platform-agent-secrets' from namespace '${NAMESPACE}'...${C_RESET}"
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would delete GKE Secret 'platform-agent-secrets' from namespace '${NAMESPACE}'.${C_RESET}"
      else
        kubectl delete secret platform-agent-secrets -n "${NAMESPACE}" --ignore-not-found || true
        echo -e "  ${C_GREEN}✓ GKE Secret successfully deleted.${C_RESET}"
      fi
    else
      echo -e "  ${C_GREEN}✓ GKE Secret 'platform-agent-secrets' does not exist in namespace '${NAMESPACE}'.${C_RESET}"
    fi

    # Clean up GitHub Minter Credentials Secret
    MINTER_SECRET_EXISTS=""
    if [ "${DRY_RUN:-0}" -ne 1 ]; then
      MINTER_SECRET_EXISTS=$(kubectl get secret github-app-credentials -n "${NAMESPACE}" --ignore-not-found 2>/dev/null || echo "")
    fi

    if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$MINTER_SECRET_EXISTS" ]; then
      echo -e "  ${C_CYAN}ℹ Deleting GKE Secret 'github-app-credentials' from namespace '${NAMESPACE}'...${C_RESET}"
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would delete GKE Secret 'github-app-credentials' from namespace '${NAMESPACE}'.${C_RESET}"
      else
        kubectl delete secret github-app-credentials -n "${NAMESPACE}" --ignore-not-found || true
        echo -e "  ${C_GREEN}✓ GKE Secret 'github-app-credentials' successfully deleted.${C_RESET}"
      fi
    else
      echo -e "  ${C_GREEN}✓ GKE Secret 'github-app-credentials' does not exist in namespace '${NAMESPACE}'.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ Namespace '${NAMESPACE}' does not exist.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping K8s secret deletion.${C_RESET}"
fi
