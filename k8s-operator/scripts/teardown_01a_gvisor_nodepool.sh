#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 1a: Optional Teardown of Dedicated gVisor Node Pool
# ==============================================================================
# Idempotent script to clean up the dedicated GKE Sandbox (gVisor) node pool
# and RuntimeClass. Can be run independently to test disabling gVisor.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true

# ─── Check & Confirm Deletion ─────────────────────────────────────────────────
POOL_EXISTS=$(gcloud container node-pools describe gvisor-pool --cluster="$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" 2>/dev/null || echo "")

if [ -n "$POOL_EXISTS" ] || kubectl get runtimeclass gvisor >/dev/null 2>&1; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would prompt to delete gVisor node pool ('gvisor-pool') and RuntimeClass 'gvisor'.${C_RESET}"
  else
    if [ "$NO_CONFIRM" -ne 1 ]; then
      echo -ne "  ${C_CYAN}Do you want to delete the dedicated gVisor node pool ('gvisor-pool') and RuntimeClass? (y/N): ${C_RESET}"
      read -r -n 1 REMOVE_GVISOR || true
      echo
    else
      REMOVE_GVISOR="y"
    fi

    if [[ ${REMOVE_GVISOR:-n} =~ ^[Yy]$ ]]; then
      echo -e "  ${C_CYAN}ℹ Deleting gVisor RuntimeClass from Kubernetes...${C_RESET}"
      kubectl delete runtimeclass gvisor --ignore-not-found=true 2>/dev/null || true

      if [ -n "$POOL_EXISTS" ]; then
        echo -e "  ${C_CYAN}ℹ Deleting gVisor node pool ('gvisor-pool') in cluster '$CLUSTER_NAME'...${C_RESET}"
        echo -e "    ${C_YELLOW}Note: This takes approximately 3-5 minutes in Google Cloud...${C_RESET}"
        gcloud container node-pools delete gvisor-pool --cluster="$CLUSTER_NAME" --region="$REGION" --project="${PROJECT_ID}" --quiet
        echo -e "  ${C_GREEN}✓ gVisor node pool ('gvisor-pool') successfully deleted.${C_RESET}"
      fi
    else
      echo -e "  ${C_GREEN}✓ Kept gVisor node pool and RuntimeClass.${C_RESET}"
    fi
  fi
else
  echo -e "  ${C_GREEN}✓ gVisor node pool ('gvisor-pool') and RuntimeClass do not exist.${C_RESET}"
fi
