#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 13: Teardown Hindsight Memory Store
# ==============================================================================
# Idempotent script to undeploy the Hindsight API and its Postgres database.
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
confirm_action "This will permanently undeploy the Hindsight memory store." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:$CLUSTER_NAME" \
  "Namespace:$NAMESPACE"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Step 1: Connect to GKE Cluster ───────────────────────────────────────────
CLUSTER_EXISTS=$(cluster_exists)
if [ -n "$CLUSTER_EXISTS" ]; then
  connect_cluster || true
else
  echo -e "  ${C_GREEN}✓ GKE cluster '${CLUSTER_NAME}' does not exist. Skipping Hindsight cleanup.${C_RESET}"
  exit 0
fi

# ─── Step 2: Undeploy Hindsight ───────────────────────────────────────────────
echo -e "  ${C_CYAN}ℹ Undeploying Hindsight memory store...${C_RESET}"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  echo -e "  ${C_GREEN}[DRY-RUN] Would undeploy Hindsight in namespace '${NAMESPACE}'.${C_RESET}"
else
  export NAMESPACE
  make -C "${OPERATOR_DIR}" undeploy-hindsight || true
  echo -e "  ${C_GREEN}✓ Hindsight undeploy command completed.${C_RESET}"
fi

# ─── Step 3: The database volume ──────────────────────────────────────────────
#
# Left in place deliberately. A StatefulSet's volumeClaimTemplate PVC is not
# owned by the manifests above, so deleting them does not delete it — and that is
# the behaviour to keep, because this volume *is* the memory. Re-running the
# provisioning step reattaches it with everything intact. Removing the cluster
# takes it with the cluster.
echo -e "  ${C_CYAN}ℹ Kept PVC 'data-hindsight-postgresql-0' — it holds every stored${C_RESET}"
echo -e "  ${C_CYAN}  memory. Delete it by hand to discard them:${C_RESET}"
echo -e "  ${C_CYAN}    kubectl delete pvc data-hindsight-postgresql-0 -n ${NAMESPACE}${C_RESET}"

echo -e "\n${C_GREEN}${C_BOLD}✅ Hindsight memory store successfully undeployed!${C_RESET}"
