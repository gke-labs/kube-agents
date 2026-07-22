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
confirm_action "This will remove GSA permissions, Workload Identity bindings, and delete GSAs for the Controller and Platform Agent." \
  "GCP Project:$PROJECT_ID" \
  "Controller GSA:$CONTROLLER_GSA_NAME" \
  "Platform Agent GSA:$PLATFORM_AGENT_GSA_NAME"

gcloud config set project "$PROJECT_ID" --quiet

# ─── Helper Functions for Teardown ────────────────────────────────────────────
cleanup_agent_iam() {
  local ksa_name=$1
  local gsa_name=$2
  shift 2
  local roles=("$@")
  
  local gsa_email="${gsa_name}@${PROJECT_ID}.iam.gserviceaccount.com"
  
  local gsa_exists=0
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    gsa_exists=1
  elif gcloud iam service-accounts describe "${gsa_email}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gsa_exists=1
  fi

  if [ "$gsa_exists" -eq 1 ]; then
    echo -e "  ${C_CYAN}ℹ Removing project-level IAM policy bindings for ${gsa_name}...${C_RESET}"
    for role in "${roles[@]}"; do
      if [ "${DRY_RUN:-0}" -eq 1 ]; then
        echo -e "  ${C_GREEN}[DRY-RUN] Would remove project-level IAM policy binding '${role}' for ${gsa_name}.${C_RESET}"
      else
        gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
            --member="serviceAccount:${gsa_email}" \
            --role="${role}" \
            --quiet 2>/dev/null || true
      fi
    done

    echo -e "  ${C_CYAN}ℹ Removing Workload Identity Policy Binding for ${gsa_name}...${C_RESET}"
    local wi_member="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${ksa_name}]"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would remove Workload Identity binding for ${gsa_name} to ${ksa_name}.${C_RESET}"
    else
      gcloud iam service-accounts remove-iam-policy-binding "${gsa_email}" \
          --role="roles/iam.workloadIdentityUser" \
          --member="${wi_member}" \
          --project="${PROJECT_ID}" \
          --quiet 2>/dev/null || true
    fi

    echo -e "  ${C_CYAN}ℹ Deleting GSA ${gsa_name}...${C_RESET}"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete GSA ${gsa_name}.${C_RESET}"
    else
      gcloud iam service-accounts delete "${gsa_email}" --project="${PROJECT_ID}" --quiet || true
      echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' successfully removed.${C_RESET}"
    fi
  else
    echo -e "  ${C_GREEN}✓ GSA '${gsa_name}' does not exist. Skipping cleanup.${C_RESET}"
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────

platform_roles=(
    "roles/container.clusterAdmin"
    "roles/container.admin"
    "roles/monitoring.admin"
    "roles/logging.admin"
    "roles/container.clusterViewer"
    "roles/container.viewer"
    "roles/monitoring.viewer"
    "roles/logging.viewer"
    "roles/iam.serviceAccountUser"
    "roles/iam.securityReviewer"
    "roles/aiplatform.user"
    "roles/mcp.toolUser"
)
if [ -n "${PLATFORM_AGENT_CUSTOM_ROLES:-}" ]; then
  custom_roles_str=""
  if declare -p PLATFORM_AGENT_CUSTOM_ROLES 2>/dev/null | grep -q 'declare -a'; then
    custom_roles_str="${PLATFORM_AGENT_CUSTOM_ROLES[*]}"
  else
    custom_roles_str="${PLATFORM_AGENT_CUSTOM_ROLES}"
  fi
  custom_roles=(${custom_roles_str//,/ })
  platform_roles+=("${custom_roles[@]}")
fi

cleanup_agent_iam "${PLATFORM_AGENT_KSA_NAME}" "${PLATFORM_AGENT_GSA_NAME}" "${platform_roles[@]}"



# Clean up GitHub Token Minter GSA
cleanup_agent_iam "${GITHUB_MINTER_KSA_NAME}" "${GITHUB_MINTER_GSA_NAME}"

echo -e "\n${C_GREEN}${C_BOLD}✅ Controller & Agent GCP IAM configurations fully cleaned up!${C_RESET}"
