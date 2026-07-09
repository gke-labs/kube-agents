#!/usr/bin/env bash
# ==============================================================================
# 🧹 Step 5: Teardown Slack Integration Setup
# ==============================================================================
# Idempotent script to clean up Slack integration state and tokens.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
source "${SCRIPT_DIR}/common.sh" "$@"

# ─── Configuration State Restoration ──────────────────────────────────────────
ensure_teardown_state

# ─── Confirmation Prompt ──────────────────────────────────────────────────────
confirm_action "This will reset Slack integration configuration settings."

print_info "Cleaning up Slack integration configuration state..."

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  print_success "[DRY-RUN] Would reset Slack configuration variables in vars.sh."
else
  save_var "SLACK_ENABLED" "false"
  save_var "SLACK_BOT_TOKEN" ""
  save_var "SLACK_APP_TOKEN" ""
  save_var "SLACK_ALLOWED_USERS" ""
  save_var "SLACK_HOME_CHANNEL" ""
  save_var "SLACK_HOME_CHANNEL_NAME" ""
  print_success "Slack integration configuration state reset."
fi
