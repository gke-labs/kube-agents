#!/usr/bin/env bash
# ==============================================================================
# 🤖 Step 5b: Slack Integration Setup
# ==============================================================================
# Configures Slack bot tokens, app tokens, and home channel settings.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${SCRIPT_DIR}/vars.sh"

source "${SCRIPT_DIR}/common.sh" "$@"

print_step "Setting up Configuration State for Slack Integration"
load_state

if [ "${DRY_RUN:-0}" -eq 1 ]; then
  export SLACK_ENABLED="${SLACK_ENABLED:-false}"
else
  current_slack_val="${SLACK_ENABLED:-false}"
  default_slack_prompt="y/N"
  if [ "$current_slack_val" = "true" ]; then
    default_slack_prompt="Y/n"
  fi
  echo -ne "  ${C_CYAN}Do you want to enable Slack integration? (${default_slack_prompt}): ${C_RESET}"
  read -r REPLY_SLACK
  if [ -z "$REPLY_SLACK" ]; then
    export SLACK_ENABLED="$current_slack_val"
  elif [[ "$REPLY_SLACK" =~ ^[Yy]$ ]]; then
    export SLACK_ENABLED="true"
  else
    export SLACK_ENABLED="false"
  fi
fi
save_var "SLACK_ENABLED" "${SLACK_ENABLED}"

if [ "${SLACK_ENABLED}" != "true" ]; then
  print_info "Slack integration is disabled. Skipping Slack token setup."
  save_var "SLACK_BOT_TOKEN" ""
  save_var "SLACK_APP_TOKEN" ""
  save_var "SLACK_ALLOWED_USERS" ""
  save_var "SLACK_HOME_CHANNEL" ""
  save_var "SLACK_HOME_CHANNEL_NAME" ""
  exit 0
fi

if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    export SLACK_BOT_TOKEN=""
  else
    echo -ne "  ${C_CYAN}Enter your SLACK_BOT_TOKEN (xoxb-...): ${C_RESET}"
    read -s -r INPUT_BOT_TOKEN
    echo ""
    export SLACK_BOT_TOKEN="${INPUT_BOT_TOKEN:-}"
  fi
  if [ -z "${SLACK_BOT_TOKEN}" ]; then
    print_warning "SLACK_BOT_TOKEN is empty. Slack integration may not work properly until provided."
  fi
  save_var "SLACK_BOT_TOKEN" "${SLACK_BOT_TOKEN}"
fi

if [ -z "${SLACK_APP_TOKEN:-}" ]; then
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    export SLACK_APP_TOKEN=""
  else
    echo -ne "  ${C_CYAN}Enter your SLACK_APP_TOKEN (xapp-...): ${C_RESET}"
    read -s -r INPUT_APP_TOKEN
    echo ""
    export SLACK_APP_TOKEN="${INPUT_APP_TOKEN:-}"
  fi
  if [ -z "${SLACK_APP_TOKEN}" ]; then
    print_warning "SLACK_APP_TOKEN is empty. Slack integration may not work properly until provided."
  fi
  save_var "SLACK_APP_TOKEN" "${SLACK_APP_TOKEN}"
fi

init_var "SLACK_ALLOWED_USERS" "" "Enter Allowed Slack User IDs (comma separated). Leaving empty allows all users."
init_var "SLACK_HOME_CHANNEL" "" "Enter Slack Home Channel ID (optional)."
init_var "SLACK_HOME_CHANNEL_NAME" "" "Enter Slack Home Channel Name (optional)."

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Slack Integration Configuration Initialized!  <<<${C_RESET}"
"${SCRIPT_DIR}/print_instructions_slack.sh" "$@"
