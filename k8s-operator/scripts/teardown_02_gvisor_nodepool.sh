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

if ! is_truthy "${ENABLE_GVISOR:-false}"; then
  print_info "Skipping gVisor node pool teardown (ENABLE_GVISOR=${ENABLE_GVISOR:-false})."
  exit 0
fi

if [ -z "${GVISOR_POOL_NAME:-}" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ] || [ "${NO_CONFIRM:-0}" -eq 1 ] || is_ci_pipeline; then
    export GVISOR_POOL_NAME="gvisor-pool"
  else
    export GVISOR_POOL_NAME="${GVISOR_POOL_NAME:-gvisor-pool}"
    echo -ne "  ${C_CYAN}Enter GKE Sandbox (gVisor) Node Pool Name [${C_WHITE}${GVISOR_POOL_NAME}${C_CYAN}]: ${C_RESET}"
    read -r INPUT_GVISOR_POOL_NAME
    export GVISOR_POOL_NAME="${INPUT_GVISOR_POOL_NAME:-$GVISOR_POOL_NAME}"
  fi
fi

gcloud config set project "$PROJECT_ID" --quiet 2>/dev/null || true

# ─── Check & Confirm Deletion ─────────────────────────────────────────────────
POOL_EXISTS=$(gcloud container node-pools describe "$GVISOR_POOL_NAME" --cluster="$CLUSTER_NAME" --region="$REGION" --project="$PROJECT_ID" 2>/dev/null || echo "")

if [ -n "$POOL_EXISTS" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would prompt to delete gVisor node pool ('$GVISOR_POOL_NAME').${C_RESET}"
  else
    if [ "$NO_CONFIRM" -ne 1 ]; then
      echo -ne "  ${C_CYAN}Do you want to delete the dedicated gVisor node pool ('$GVISOR_POOL_NAME')? (y/N): ${C_RESET}"
      read -r -n 1 REMOVE_GVISOR || true
      echo
    else
      REMOVE_GVISOR="y"
    fi

    if is_truthy "${REMOVE_GVISOR:-n}"; then
      echo -e "  ${C_CYAN}ℹ Deleting gVisor node pool ('$GVISOR_POOL_NAME') in cluster '$CLUSTER_NAME'...${C_RESET}"
      echo -e "    ${C_YELLOW}Note: This takes approximately 3-5 minutes in Google Cloud...${C_RESET}"
      gcloud container node-pools delete "$GVISOR_POOL_NAME" --cluster="$CLUSTER_NAME" --region="$REGION" --project="${PROJECT_ID}" --quiet
      echo -e "  ${C_GREEN}✓ gVisor node pool ('$GVISOR_POOL_NAME') successfully deleted.${C_RESET}"
    else
      echo -e "  ${C_GREEN}✓ Kept gVisor node pool.${C_RESET}"
    fi
  fi
else
  echo -e "  ${C_GREEN}✓ gVisor node pool ('$GVISOR_POOL_NAME') does not exist.${C_RESET}"
fi
