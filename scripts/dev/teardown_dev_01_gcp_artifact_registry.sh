#!/usr/bin/env bash
# ==============================================================================
# 🧹 Dev Teardown: Artifact Registry Repository
# ==============================================================================
# Idempotent script to clean up the Artifact Registry repository created during
# local agent dev rebuilds (dev_rebuild_agent.sh).
# ==============================================================================

set -euo pipefail

# See dev_rebuild_agent.sh: the shared helpers are the sibling directory, and
# VARS_FILE points there so load_state does not create a stray one here.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
INSTALLER_DIR="${REPO_ROOT}/scripts/installer"
# shellcheck disable=SC2034 # read by the installer's load_state, sourced below
VARS_FILE="${INSTALLER_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${INSTALLER_DIR}/common.sh" "$@"
check_prereqs "gcloud"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will permanently delete Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME' and all container images inside it." \
  "GCP Project:$PROJECT_ID" \
  "Region:$REGION" \
  "Artifact Repo:$GCP_ARTIFACT_REGISTRY_REPO_NAME"

# ─── Step 1: Delete Artifact Registry Repository ──────────────────────────────
echo -e "\n${C_BOLD}=== Tearing Down Artifact Registry Repo ===${C_RESET}"
REPO_EXISTS=""
if [ "${DRY_RUN:-0}" -ne 1 ]; then
  # Only a NOT_FOUND answer means the repository is gone. A missing login, a
  # missing role or a broken gcloud fails the probe too, and must not read as
  # "already deleted".
  if describe_output=$(gcloud artifacts repositories describe "$GCP_ARTIFACT_REGISTRY_REPO_NAME" --location="$REGION" --project="$PROJECT_ID" --format="value(name)" 2>&1); then
    REPO_EXISTS="$describe_output"
  elif [[ "$describe_output" == *NOT_FOUND* ]]; then
    REPO_EXISTS=""
  else
    print_error "Could not check Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME': ${describe_output}"
    exit 1
  fi
fi

if [ "${DRY_RUN:-0}" -eq 1 ] || [ -n "$REPO_EXISTS" ]; then
  echo -e "  ${C_CYAN}ℹ Deleting Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME' in location '$REGION'...${C_RESET}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    echo -e "  ${C_GREEN}[DRY-RUN] Would delete Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME'.${C_RESET}"
  else
    gcloud artifacts repositories delete "$GCP_ARTIFACT_REGISTRY_REPO_NAME" --location="$REGION" --project="$PROJECT_ID" --quiet || {
      print_error "Could not delete Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME'; DEV_ARTIFACT_REGISTRY_CREATED in scripts/installer/vars.sh is unchanged."
      exit 1
    }
    echo -e "  ${C_GREEN}✓ Artifact Registry repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME' successfully deleted.${C_RESET}"
  fi
else
  echo -e "  ${C_GREEN}✓ Repository '$GCP_ARTIFACT_REGISTRY_REPO_NAME' already deleted or does not exist.${C_RESET}"
fi

save_var "DEV_ARTIFACT_REGISTRY_CREATED" "false"
