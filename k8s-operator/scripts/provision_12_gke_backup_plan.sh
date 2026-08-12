#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 12: GKE Backup Plan (optional)
# ==============================================================================
# Sets up Google Cloud Backup for GKE BackupPlan for automated cluster
# and persistent volume snapshots. Skipped unless ENABLE_GKE_BACKUP_PLAN=true.
#
# Note: If BACKUP_CRON_SCHEDULE or BACKUP_RETAIN_DAYS are modified after initial
# provisioning, re-running this script automatically reconciles the existing
# backup plan in-place using 'gcloud beta container backup-restore backup-plans update'.
#
# Cost: Incurs charges based on the number of GKE pods backed up and persistent
# volume snapshot storage used. Defaults to ENABLE_GKE_BACKUP_PLAN=false.
#
# Security: Backups include Kubernetes Secrets and persistent volume data, so
# GCP IAM policies should restrict backup/restore permissions to authorized admins.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Prerequisites Check ──────────────────────────────────────────────────────
print_step "Checking Local Prerequisites"
check_prereqs "gcloud"

print_step "Setting up Configuration State for GKE Backup Plan"
load_state

init_var "ENABLE_GKE_BACKUP_PLAN" "false" "Enable automated Google Cloud Backup for GKE on cluster (true/false)"
if ! is_truthy "$ENABLE_GKE_BACKUP_PLAN"; then
  echo -e "  ${C_YELLOW}ℹ Skipping GKE Backup Plan setup per user request (ENABLE_GKE_BACKUP_PLAN=${ENABLE_GKE_BACKUP_PLAN}).${C_RESET}"
  exit 0
fi

if [ "${DRY_RUN:-0}" -ne 1 ] && ! gcloud beta --help &>/dev/null; then
  print_error "The 'gcloud beta' component is required for Backup for GKE commands. Please install it (e.g., 'gcloud components install beta' or 'apt-get install google-cloud-cli-beta') and rerun."
  exit 1
fi

ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
DEFAULT_PROJECT_ID="${ACTIVE_PROJECT:-$(whoami 2>/dev/null || echo "user")}"

init_var "PROJECT_ID" "$DEFAULT_PROJECT_ID" "Enter Target GCP Project ID"
init_var "REGION" "$DEFAULT_REGION" "Enter GKE GCP Region"
init_var "CLUSTER_NAME" "$DEFAULT_CLUSTER_NAME" "Enter GKE Cluster Name"
init_var "BACKUP_CRON_SCHEDULE" "0 2 * * *" "Enter GKE Backup Plan cron schedule"
if [ -n "${BACKUP_CRON_SCHEDULE:-}" ]; then
  field_count=$(echo "$BACKUP_CRON_SCHEDULE" | awk '{print NF}')
  if [ "$field_count" -ne 5 ]; then
    print_error "BACKUP_CRON_SCHEDULE must be a valid 5-field cron expression (e.g., '0 2 * * *'). Got: '${BACKUP_CRON_SCHEDULE}'"
    exit 1
  fi
fi
init_var "BACKUP_RETAIN_DAYS" "30" "Enter backup retention in days"
BACKUP_PLAN_NAME="${CLUSTER_NAME}-backup-plan"
init_var "BACKUP_ENCRYPTION_KEY" "" "Enter optional KMS encryption key for backups (leave empty for Google-managed)"
if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
  if [[ ! "${BACKUP_ENCRYPTION_KEY}" =~ ^projects/[^/]+/locations/[^/]+/keyRings/[^/]+/cryptoKeys/[^/]+$ ]]; then
    print_error "BACKUP_ENCRYPTION_KEY must be a valid Cloud KMS cryptoKey resource path (e.g., projects/PROJECT_ID/locations/REGION/keyRings/KEY_RING/cryptoKeys/KEY_NAME) or empty."
    exit 1
  fi
fi

# ─── Step Implementations ─────────────────────────────────────────────────────

# Step 1: Ensure Backup for GKE API is enabled
verify_backup_api() {
  gcloud services list --enabled --project="$PROJECT_ID" --format="value(config.name)" 2>/dev/null | grep -q 'gkebackup.googleapis.com'
}
execute_backup_api() {
  print_info "Enabling Backup for GKE API (gkebackup.googleapis.com)..."
  gcloud services enable gkebackup.googleapis.com --project="$PROJECT_ID"
}

# Step 2: Ensure Backup for GKE is enabled on cluster
verify_cluster_backup_enabled() {
  gcloud container clusters describe "$CLUSTER_NAME" \
      --location="$REGION" --project="$PROJECT_ID" \
      --format="value(addonsConfig.gkeBackupAgentConfig.enabled)" 2>/dev/null | grep -iq "True"
}
execute_cluster_backup_enabled() {
  print_info "Enabling Backup for GKE on cluster '${CLUSTER_NAME}'..."
  gcloud container clusters update "$CLUSTER_NAME" \
      --location="$REGION" \
      --project="$PROJECT_ID" \
      --update-addons=BackupRestore=ENABLED \
      --quiet
}

# Step 3: Ensure GKE Backup Plan
# Contract: Sets BACKUP_PLAN_EXISTS="true"|"false" as a global side-effect read by execute_backup_plan().
verify_backup_plan() {
  local out
  local err=0
  out=$(gcloud beta container backup-restore backup-plans describe "$BACKUP_PLAN_NAME" \
      --location="$REGION" --project="$PROJECT_ID" 2>&1) || err=$?
  if [ "$err" -eq 0 ]; then
    BACKUP_PLAN_EXISTS="true"
    local curr_retain curr_cron curr_enc curr_paused
    curr_retain=$(echo "$out" | grep -E "^[[:space:]]*backupRetainDays:" | head -n1 | sed -E 's/^[[:space:]]*backupRetainDays:[[:space:]]*//' | tr -d "'\"[:space:]")
    curr_cron=$(echo "$out" | grep -E "^[[:space:]]*cronSchedule:" | head -n1 | sed -E 's/^[[:space:]]*cronSchedule:[[:space:]]*//' | sed -E "s/^['\"]//;s/['\"]$//")
    curr_enc=$(echo "$out" | grep -E "^[[:space:]]*gcpKmsEncryptionKey:" | head -n1 | sed -E 's/^[[:space:]]*gcpKmsEncryptionKey:[[:space:]]*//' | tr -d "'\"[:space:]")
    curr_paused=$(echo "$out" | grep -E "^[[:space:]]*paused:" | head -n1 | sed -E 's/^[[:space:]]*paused:[[:space:]]*//' | tr -d "'\"[:space:]")

    local enc_matches="true"
    if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
      if [ "$curr_enc" != "$BACKUP_ENCRYPTION_KEY" ]; then
        enc_matches="false"
      fi
    elif [ -n "$curr_enc" ]; then
      echo -e "  ${C_YELLOW}⚠ Existing BackupPlan '${BACKUP_PLAN_NAME}' has CMEK encryption enabled. Clearing CMEK key via empty BACKUP_ENCRYPTION_KEY is unsupported; preserving existing key.${C_RESET}"
    fi

    if [ "$curr_retain" = "$BACKUP_RETAIN_DAYS" ] && [ "$curr_cron" = "$BACKUP_CRON_SCHEDULE" ] && [ "$enc_matches" = "true" ] && ! is_truthy "$curr_paused"; then
      return 0
    else
      echo -e "  ${C_YELLOW}ℹ Configuration drift detected on existing BackupPlan '${BACKUP_PLAN_NAME}'. Will update.${C_RESET}"
      return 1
    fi
  elif [ "${DRY_RUN:-0}" -eq 1 ] || echo "$out" | grep -iq "not found"; then
    BACKUP_PLAN_EXISTS="false"
    return 1
  else
    echo -e "  ${C_RED}✗ Error checking GKE Backup Plan '${BACKUP_PLAN_NAME}':${C_RESET}" >&2
    echo "$out" >&2
    exit "$err"
  fi
}
execute_backup_plan() {
  local enc_flag=()
  if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
    enc_flag=("--encryption-key=${BACKUP_ENCRYPTION_KEY}")
  fi

  if [ "${BACKUP_PLAN_EXISTS:-false}" = "true" ]; then
    print_info "Updating GKE Backup Plan '${BACKUP_PLAN_NAME}' to reconcile configuration drift..."
    gcloud beta container backup-restore backup-plans update "$BACKUP_PLAN_NAME" \
        --project="$PROJECT_ID" \
        --location="$REGION" \
        --cron-schedule="$BACKUP_CRON_SCHEDULE" \
        --backup-retain-days="$BACKUP_RETAIN_DAYS" \
        --no-paused \
        "${enc_flag[@]}" \
        --no-async \
        --quiet
  else
    print_info "Creating default GKE Backup Plan '${BACKUP_PLAN_NAME}'..."
    gcloud beta container backup-restore backup-plans create "$BACKUP_PLAN_NAME" \
        --project="$PROJECT_ID" \
        --location="$REGION" \
        --cluster="projects/${PROJECT_ID}/locations/${REGION}/clusters/${CLUSTER_NAME}" \
        --selected-namespaces="${NAMESPACE:-kubeagents-system}" \
        --include-secrets \
        --include-volume-data \
        --cron-schedule="$BACKUP_CRON_SCHEDULE" \
        --backup-retain-days="$BACKUP_RETAIN_DAYS" \
        --no-paused \
        "${enc_flag[@]}" \
        --no-async \
        --quiet
  fi
}

# ─── Execution Pipeline ───────────────────────────────────────────────────────
run_step "1. Ensure Backup for GKE API enabled" verify_backup_api execute_backup_api 10
run_step "2. Ensure Backup for GKE enabled on cluster" verify_cluster_backup_enabled execute_cluster_backup_enabled 5
run_step "3. Ensure GKE Backup Plan" verify_backup_plan execute_backup_plan 0

# ─── Conclusion Checklist ─────────────────────────────────────────────────────
echo -e "\n${C_GREEN}${C_BOLD}✓ GKE Backup Plan '${BACKUP_PLAN_NAME}' provisioned successfully!${C_RESET}"
