#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 7: Teardown GKE Secrets
# ==============================================================================
# Idempotent script to clean up Kubernetes secrets and sanitize local state.
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

# ─── Step 2: Sanitize Sensitive Secrets from Local State & Clean Temp Files ───
if [ -f "$VARS_FILE" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would sanitize sensitive secret variables from vars.sh.${C_RESET}"
  else
    if [ "$NO_CONFIRM" -ne 1 ]; then
      echo -ne "  ${C_CYAN}Sanitize plaintext API keys and tokens from local vars.sh? (y/N): ${C_RESET}"
      read -r -n 1 SANITIZE_VARS || true
      echo
    else
      SANITIZE_VARS="y"
    fi
    if is_truthy "${SANITIZE_VARS:-n}"; then
      local_umask=$(umask)
      umask 077
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      for secret_var in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY API_SERVER_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
        grep -E -v "^[[:space:]]*export[[:space:]]+${secret_var}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
        chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
        mv "$VARS_FILE.tmp" "$VARS_FILE"
        chmod 600 "$VARS_FILE" 2>/dev/null || true
      done
      umask "$local_umask"
      echo -e "  ${C_GREEN}✓ Sanitized sensitive secret variables from ${VARS_FILE}.${C_RESET}"
    else
      echo -e "  ${C_CYAN}ℹ Kept sensitive secret variables in ${VARS_FILE} for subsequent provisioning.${C_RESET}"
    fi
  fi
fi

if [ "${DRY_RUN:-0}" -ne 1 ]; then
  # Securely remove any residual temp files
  shred -u "${VARS_FILE}.tmp" "${VARS_FILE}".*tmp 2>/dev/null || rm -f "${VARS_FILE}.tmp" "${VARS_FILE}".*tmp 2>/dev/null || true
fi

