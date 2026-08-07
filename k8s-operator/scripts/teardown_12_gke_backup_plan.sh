#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 12: Teardown GKE Backup Plan
# ==============================================================================
# Idempotent script to delete the Google Cloud Backup for GKE BackupPlan.
# Safely deletes any remaining backup snapshots in background batches before
# removing the BackupPlan. Safe to run even if the backup plan was never created.
# Set PRESERVE_BACKUPS=true to preserve existing BackupPlan and snapshots during teardown (defaults to false).
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

ensure_teardown_state

if [ "${DRY_RUN:-0}" -ne 1 ] && ! gcloud beta --help &>/dev/null; then
  print_info "The 'gcloud beta' component is not installed. Skipping GKE Backup Plan teardown."
  exit 0
fi

BACKUP_PLAN_NAME="${CLUSTER_NAME}-backup-plan"
PRESERVE_BACKUPS="${PRESERVE_BACKUPS:-false}"

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will delete the GKE Backup Plan '${BACKUP_PLAN_NAME}'." \
  "GCP Project:$PROJECT_ID" \
  "GCP Region:$REGION"

print_step "Checking and removing GKE Backup Plan"

describe_out=""
describe_err=0
describe_out=$(gcloud beta container backup-restore backup-plans describe "$BACKUP_PLAN_NAME" \
    --location="$REGION" --project="$PROJECT_ID" 2>&1) || describe_err=$?

if [ "$describe_err" -eq 0 ]; then
  if is_truthy "${PRESERVE_BACKUPS}"; then
    echo -e "  ${C_YELLOW}ℹ PRESERVE_BACKUPS=true: Preserving GKE Backup Plan '${BACKUP_PLAN_NAME}' and its existing backup snapshots.${C_RESET}"
    echo -e "  ${C_CYAN}ℹ To delete all backup snapshots and the plan, run with PRESERVE_BACKUPS=false (default).${C_RESET}"
  else
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      echo -e "  ${C_GREEN}[DRY-RUN] Would delete GKE Backup Plan '${BACKUP_PLAN_NAME}'.${C_RESET}"
    else
      backups=$(gcloud beta container backup-restore backups list \
          --backup-plan="$BACKUP_PLAN_NAME" \
          --location="$REGION" \
          --project="$PROJECT_ID" \
          --format="value(name.basename())" 2>/dev/null || echo "")

      if [ -n "$backups" ]; then
        echo -e "  ${C_CYAN}ℹ Deleting existing backup snapshots in '${BACKUP_PLAN_NAME}'...${C_RESET}"
        count=0
        for backup in $backups; do
          if [ -n "$backup" ]; then
            echo -e "    ${C_CYAN}ℹ Triggering deletion for snapshot '${backup}' (in background)...${C_RESET}"
            (
              del_err=0
              del_out=$(gcloud beta container backup-restore backups delete "$backup" \
                  --backup-plan="$BACKUP_PLAN_NAME" \
                  --location="$REGION" \
                  --project="$PROJECT_ID" \
                  --quiet 2>&1) || del_err=$?
              if [ "$del_err" -ne 0 ]; then
                echo -e "    ${C_YELLOW}⚠ Failed to delete snapshot '${backup}':${C_RESET}" >&2
                echo "$del_out" | sed 's/^/      /' >&2
              fi
            ) &
            count=$((count + 1))
            if [ $((count % 5)) -eq 0 ]; then
              wait
            fi
          fi
        done
        wait
      fi

      retry=0
      max_retries=5
      while [ $retry -lt $max_retries ]; do
        remaining_backups=$(gcloud beta container backup-restore backups list \
            --backup-plan="$BACKUP_PLAN_NAME" \
            --location="$REGION" \
            --project="$PROJECT_ID" \
            --format="value(name.basename())" 2>/dev/null || echo "")
        if [ -z "$remaining_backups" ]; then
          break
        fi
        retry=$((retry + 1))
        if [ $retry -lt $max_retries ]; then
          echo -e "  ${C_YELLOW}ℹ Waiting for backup snapshot deletions to complete on GCP (attempt ${retry}/${max_retries})...${C_RESET}"
          sleep 5
        fi
      done

      if [ -n "$remaining_backups" ]; then
        echo -e "  ${C_RED}✗ Some backup snapshots could not be deleted. Remaining snapshots:${C_RESET}" >&2
        echo "$remaining_backups" | while read -r rem; do
          if [ -n "$rem" ]; then
            echo -e "    - ${rem}" >&2
          fi
        done
        echo -e "  ${C_RED}✗ Cannot delete BackupPlan '${BACKUP_PLAN_NAME}' until all snapshots are removed.${C_RESET}" >&2
        exit 1
      fi

      echo -e "  ${C_CYAN}ℹ Deleting GKE Backup Plan '${BACKUP_PLAN_NAME}'...${C_RESET}"
      gcloud beta container backup-restore backup-plans delete "$BACKUP_PLAN_NAME" \
          --location="$REGION" \
          --project="$PROJECT_ID" \
          --quiet
      echo -e "  ${C_GREEN}✓ Deleted GKE Backup Plan '${BACKUP_PLAN_NAME}'.${C_RESET}"
    fi
  fi
elif [ "${DRY_RUN:-0}" -eq 1 ] || echo "$describe_out" | grep -iq -E "not found|service_disabled|has not been used in project|is disabled"; then
  echo -e "  ${C_GREEN}✓ GKE Backup Plan '${BACKUP_PLAN_NAME}' does not exist (or dry-run skip). Skipping.${C_RESET}"
else
  echo -e "  ${C_RED}✗ Error describing GKE Backup Plan '${BACKUP_PLAN_NAME}':${C_RESET}" >&2
  echo "$describe_out" >&2
  exit "$describe_err"
fi

echo -e "\n${C_GREEN}${C_BOLD}✓ GKE Backup Plan cleanup completed successfully!${C_RESET}"
