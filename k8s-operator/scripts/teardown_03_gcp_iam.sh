#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 3: Teardown Controller & Agent GCP Workload Identity & GCP IAM
# ==============================================================================
# Idempotent script to remove cluster management and Workload Identity bindings
# from the Controller manager and all Agent GSAs, and delete the GSAs.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will remove GSA permissions, Workload Identity bindings, and delete GSAs for the Controller and all Agent types." \
  "GCP Project:$PROJECT_ID" \
  "Controller GSA:$CONTROLLER_GSA_NAME" \
  "Platform Agent GSA:$PLATFORM_AGENT_GSA_NAME" \
  "Operator Agent GSA:$OPERATOR_AGENT_GSA_NAME" \
  "DevTeam Agent GSA:$DEVTEAM_AGENT_GSA_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Helper Functions for Teardown ────────────────────────────────────────────
cleanup_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  if gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo -e "  ${C_CYAN}ℹ Removing project-level IAM policy bindings for ${gsa_name}...${C_RESET}"
    for role in "${roles[@]}"; do
      gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
          --member="serviceAccount:${gsa_email}" \
          --role="${role}" \
          --quiet || true
    done

    echo -e "  ${C_CYAN}ℹ Removing Workload Identity Policy Binding for ${gsa_name}...${C_RESET}"
    local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
    gcloud iam service-accounts remove-iam-policy-binding "${gsa_email}" \
        --role="roles/iam.workloadIdentityUser" \
        --member="${wi_member}" \
        --project="${PROJECT_ID}" \
        --quiet || true

    echo -e "  ${C_CYAN}ℹ Deleting GSA ${gsa_name}...${C_RESET}"
    gcloud iam service-accounts delete "${gsa_email}" --project="${PROJECT_ID}" --quiet || true
    echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' successfully removed.${C_RESET}"
  else
    echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' does not exist. Skipping cleanup.${C_RESET}"
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
cleanup_agent_iam "${CONTROLLER_KSA_NAME}" "${CONTROLLER_GSA_NAME}" \
    "roles/container.clusterViewer" \
    "roles/container.admin" \
    "roles/container.clusterAdmin"

cleanup_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" \
    "roles/aiplatform.user" \
    "roles/container.clusterViewer"

cleanup_agent_iam "${OPERATOR_AGENT_KSA_NAME}" "${OPERATOR_AGENT_GSA_NAME}" \
    "roles/aiplatform.user" \
    "roles/container.clusterViewer" \
    "roles/container.admin" \
    "roles/container.clusterAdmin"

cleanup_agent_iam "${DEVTEAM_AGENT_KSA_NAME}" "${DEVTEAM_AGENT_GSA_NAME}" \
    "roles/aiplatform.user" \
    "roles/container.clusterViewer"

echo -e "\n${C_GREEN}${C_BOLD}✅ Controller & Agent GCP IAM configurations fully cleaned up!${C_RESET}"
