#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 1a: Optional Teardown of Dedicated gVisor Node Pool
# ==============================================================================
# Idempotent script to clean up the dedicated GKE Sandbox (gVisor) node pool
# and RuntimeClass. Can be run independently to test disabling gVisor.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true

# ─── Check & Confirm Deletion ─────────────────────────────────────────────────
POOL_EXISTS=$(gcloud container node-pools describe gvisor-pool --cluster="$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" 2>/dev/null || echo "")

if [ -n "$POOL_EXISTS" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would prompt to delete gVisor node pool ('gvisor-pool').${C_RESET}"
  else
    if [ "$NO_CONFIRM" -ne 1 ]; then
      echo -ne "  ${C_CYAN}Do you want to delete the dedicated gVisor node pool ('gvisor-pool')? (y/N): ${C_RESET}"
      read -r -n 1 REMOVE_GVISOR || true
      echo
    else
      REMOVE_GVISOR="y"
    fi

    if [[ ${REMOVE_GVISOR:-n} =~ ^[Yy]$ ]]; then
      echo -e "  ${C_CYAN}ℹ Deleting gVisor node pool ('gvisor-pool') in cluster '$CLUSTER_NAME'...${C_RESET}"
      echo -e "    ${C_YELLOW}Note: This takes approximately 3-5 minutes in Google Cloud...${C_RESET}"
      gcloud container node-pools delete gvisor-pool --cluster="$CLUSTER_NAME" --region="$REGION" --project="${PROJECT_ID}" --quiet
      echo -e "  ${C_GREEN}✓ gVisor node pool ('gvisor-pool') successfully deleted.${C_RESET}"
    else
      echo -e "  ${C_GREEN}✓ Kept gVisor node pool.${C_RESET}"
    fi
  fi
else
  echo -e "  ${C_GREEN}✓ gVisor node pool ('gvisor-pool') does not exist.${C_RESET}"
fi
