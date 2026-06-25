#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 8: Teardown GitHub Token Minter
# ==============================================================================
# Idempotent script to clean up the GitHub Token Minter.
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
confirm_action "This will permanently delete the GitHub Token Minter and destroy the KMS key versions." \
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


# ─── Step 3.5: Undeploy GitHub Token Minter ───────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying GitHub Token Minter workloads...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would delete ConfigMap and Deployment for github-token-minter.${C_RESET}"
else
  GITHUB_INTEGRATION_DIR="${OPERATOR_DIR}/config/integrations/github"
  if [ -d "$GITHUB_INTEGRATION_DIR" ]; then
    # Export variables for envsubst
    export PROJECT_ID REGION CLUSTER_NAME NAMESPACE GITHUB_MINTER_KSA_NAME GITHUB_MINTER_GSA_NAME KMS_KEYRING KMS_KEY GITHUB_ORG GITHUB_REPO KSA_NAME PLATFORM_AGENT_GSA_NAME OPERATOR_AGENT_GSA_NAME DEVTEAM_AGENT_GSA_NAME
    
    active_version=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --filter="state=ENABLED" --format="value(name)" --quiet 2>/dev/null | awk -F'/' '{print $NF}' | sort -n | tail -n 1)
    export KMS_KEY_VERSION="${active_version:-1}"
    
    make -C "${OPERATOR_DIR}" undeploy-github || true
  else
    # Fallback: raw kubectl deletion if manifests are missing
    kubectl delete deployment/github-token-minter -n "${NAMESPACE}" --ignore-not-found=true || true
    kubectl delete configmap/github-token-minter-config -n "${NAMESPACE}" --ignore-not-found=true || true
    kubectl delete service/github-token-minter -n "${NAMESPACE}" --ignore-not-found=true || true
    kubectl delete networkpolicy/github-token-minter-policy -n "${NAMESPACE}" --ignore-not-found=true || true
  fi
  echo -e "  ${C_GREEN}✓ GitHub Token Minter workloads undeployed.${C_RESET}"
fi


# ─── Step 5: Clean up GCP KMS Key (Disable & Destroy Versions) ─────────────────
echo -e "  ${C_CYAN}ℹ Cleaning up GCP KMS Key '${KMS_KEY}'...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would disable and schedule all versions of KMS Key '${KMS_KEY}' for destruction.${C_RESET}"
else
  versions=$(gcloud kms keys versions list --key="${KMS_KEY}" --keyring="${KMS_KEYRING}" --location="${REGION}" --project="${PROJECT_ID}" --format="value(name)" --quiet 2>/dev/null || echo "")
  
  if [ -n "$versions" ]; then
    for ver_path in $versions; do
      ver_state=$(gcloud kms keys versions describe "$ver_path" --format="value(state)" --quiet 2>/dev/null || echo "")
      
      if [ "$ver_state" = "ENABLED" ]; then
        print_info "Disabling KMS key version: $ver_path..."
        gcloud kms keys versions disable "$ver_path" --quiet >/dev/null 2>&1 || true
      fi
      
      if [ "$ver_state" != "DESTROYED" ] && [ "$ver_state" != "DESTROY_PENDING" ] && [ "$ver_state" != "IMPORT_FAILED" ]; then
        print_info "Scheduling KMS key version for destruction: $ver_path..."
        gcloud kms keys versions destroy "$ver_path" --quiet >/dev/null 2>&1 || true
      fi
    done
    echo -e "  ${C_GREEN}✓ KMS Key versions scheduled for destruction.${C_RESET}"
  else
    echo -e "  ${C_GREEN}✓ KMS Key has no versions to clean up.${C_RESET}"
  fi
fi

echo -e "\n${C_GREEN}${C_BOLD}✅ GitHub Token Minter successfully cleaned up!${C_RESET}"
