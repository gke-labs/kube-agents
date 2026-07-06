#!/usr/bin/env bash
# ==============================================================================
# 🧹 Master GKE Standard & Cloud-Agnostic Operator E2E Teardown Script
# ==============================================================================
# Master script to orchestrate the clean up and deletion of all GCP and GKE
# resources provisioned by the provisioning scripts in reverse order.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently delete all GChat integration resources, GKE cluster, GCP resources, and Secret Manager keys." \
  "GCP Project:$PROJECT_ID" \
  "GKE Cluster:${CLUSTER_NAME:-platform-agent-host}"

DRY_RUN_ARG=""
if [ "$DRY_RUN" -eq 1 ]; then
  DRY_RUN_ARG="--dry-run"
fi

# Execute teardown steps in reverse order (10 down to 01)
echo -e "\n${C_RED}${C_BOLD}🧹 Running Teardown Steps...${C_RESET}"
"${SCRIPT_DIR}/teardown_10_deploy_inference_replay.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_09_deploy_github_minter.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_08_deploy_litellm.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_07_deploy_platform_agent.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_06_gcp_k8s_secrets.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_05_slack.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_04_gcp_gchat.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_03_gcp_iam.sh" --no-confirm $DRY_RUN_ARG || true
"${SCRIPT_DIR}/teardown_02_gcp_gke_operator.sh" --no-confirm $DRY_RUN_ARG || true
if [ "${DEV_ARTIFACT_REGISTRY_CREATED:-false}" = "true" ]; then
  "${SCRIPT_DIR}/dev/teardown_dev_01_gcp_artifact_registry.sh" --no-confirm $DRY_RUN_ARG || true
fi
"${SCRIPT_DIR}/teardown_01_gcp_cluster.sh" --no-confirm $DRY_RUN_ARG

echo -e "\n${C_GREEN}${C_BOLD}====================================================${C_RESET}"
echo -e "${C_GREEN}${C_BOLD}✅ Teardown Complete! All resources cleaned up.${C_RESET}"
echo -e "${C_GREEN}${C_BOLD}====================================================${C_RESET}"
