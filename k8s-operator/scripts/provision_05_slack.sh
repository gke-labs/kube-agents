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

loop_add_tokens() {
  local var_name=$1
  local token_prefix=$2
  local start_index=${3:-1}

  print_info "Enter additional tokens one by one. Press [Enter] without typing a token when finished."
  while true; do
    echo -ne "  ${C_CYAN}Enter ${var_name} #${start_index} (${token_prefix} or press ENTER to finish): ${C_RESET}"
    read -s -r next_token
    echo ""
    if [ -z "$next_token" ]; then
      break
    fi
    local current_val="${!var_name}"
    if [ -n "$current_val" ]; then
      export "${var_name}=${current_val},${next_token}"
    else
      export "${var_name}=${next_token}"
    fi
    start_index=$((start_index + 1))
  done
}

# --- SLACK_BOT_TOKEN ---
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  export SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"
else
  if [ -n "${SLACK_BOT_TOKEN:-}" ]; then
    IFS=',' read -r -a existing_bot <<< "$SLACK_BOT_TOKEN"
    print_info "Detected ${#existing_bot[@]} Slack bot token(s) currently configured."
    echo -ne "  ${C_CYAN}Do you want to (a)dd additional tokens, (r)eplace existing tokens, or (k)eep current configuration? [a/r/K]: ${C_RESET}"
    read -r action_bot
    action_bot=$(echo "${action_bot:-k}" | tr '[:upper:]' '[:lower:]')

    if [ "$action_bot" = "a" ] || [ "$action_bot" = "add" ]; then
      loop_add_tokens "SLACK_BOT_TOKEN" "xoxb-..." $((${#existing_bot[@]} + 1))
    elif [ "$action_bot" = "r" ] || [ "$action_bot" = "replace" ]; then
      export SLACK_BOT_TOKEN=""
      loop_add_tokens "SLACK_BOT_TOKEN" "xoxb-..." 1
    fi
  else
    echo -ne "  ${C_CYAN}Enter your primary SLACK_BOT_TOKEN (xoxb-...): ${C_RESET}"
    read -s -r input_bot
    echo ""
    if [ -n "$input_bot" ]; then
      export SLACK_BOT_TOKEN="${input_bot}"
      echo -ne "  ${C_CYAN}Do you want to configure additional Slack bot tokens for other workspaces? (y/N): ${C_RESET}"
      read -r reply_multi
      if [[ "$reply_multi" =~ ^[Yy]$ ]]; then
        loop_add_tokens "SLACK_BOT_TOKEN" "xoxb-..." 2
      fi
    fi
  fi
fi

if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
  print_warning "SLACK_BOT_TOKEN is empty. Slack integration may not work properly until provided."
fi
save_var "SLACK_BOT_TOKEN" "${SLACK_BOT_TOKEN:-}"

# --- SLACK_APP_TOKEN ---
if [ "${DRY_RUN:-0}" -eq 1 ]; then
  export SLACK_APP_TOKEN="${SLACK_APP_TOKEN:-}"
else
  if [ -n "${SLACK_APP_TOKEN:-}" ]; then
    IFS=',' read -r -a existing_app <<< "$SLACK_APP_TOKEN"
    print_info "Detected ${#existing_app[@]} Slack app token(s) currently configured."
    echo -ne "  ${C_CYAN}Do you want to (r)eplace existing app tokens or (k)eep current configuration? [r/K]: ${C_RESET}"
    read -r action_app
    action_app=$(echo "${action_app:-k}" | tr '[:upper:]' '[:lower:]')

    if [ "$action_app" = "r" ] || [ "$action_app" = "replace" ]; then
      export SLACK_APP_TOKEN=""
    fi
  fi

  if [ -z "${SLACK_APP_TOKEN:-}" ]; then
    echo -ne "  ${C_CYAN}Enter your SLACK_APP_TOKEN (xapp-...): ${C_RESET}"
    read -s -r INPUT_APP_TOKEN
    echo ""
    export SLACK_APP_TOKEN="${INPUT_APP_TOKEN:-}"
  fi
fi

if [ -z "${SLACK_APP_TOKEN:-}" ]; then
  print_warning "SLACK_APP_TOKEN is empty. Slack integration may not work properly until provided."
fi
save_var "SLACK_APP_TOKEN" "${SLACK_APP_TOKEN:-}"

init_var "SLACK_ALLOWED_USERS" "" "Enter Allowed Slack User IDs (comma separated). Leaving empty allows all users."
init_var "SLACK_HOME_CHANNEL" "" "Enter Slack Home Channel ID (optional)."
init_var "SLACK_HOME_CHANNEL_NAME" "" "Enter Slack Home Channel Name (optional)."

echo -e "\n${C_MAGENTA}${C_BOLD}>>>  Slack Integration Configuration Initialized!  <<<${C_RESET}"
"${SCRIPT_DIR}/print_instructions_slack.sh" "$@"
