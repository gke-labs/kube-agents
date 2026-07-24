#!/usr/bin/env bash
# ==============================================================================
# 📢 Slack Instructions Printer
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

load_state

if is_truthy "${SLACK_ENABLED:-false}"; then
  if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
    print_warning "SLACK_BOT_TOKEN is empty. Slack integration may not work properly until provided."
  fi
  if [ -z "${SLACK_APP_TOKEN:-}" ]; then
    print_warning "SLACK_APP_TOKEN is empty. Slack integration may not work properly until provided."
  fi

  echo -e "${C_CYAN}${C_BOLD}--- [Slack Integration Instructions] ---${C_RESET}"
  echo -e "[ ] 1. Verify Slack Bot configuration:"
  echo -e "       - Ensure Socket Mode is ${C_GREEN}enabled${C_RESET} in Slack App Console."
  echo -e "       - Ensure Bot Token scopes include: ${C_GREEN}app_mentions:read, channels:history, chat:write, channels:read, groups:read, im:read, mpim:read${C_RESET}."
  echo -e ""
  echo -e "[ ] 2. Send a DM or mention the Bot in Slack:"
  echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
  echo -e ""
  echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container (if pairing mode enabled):"
  echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes pairing approve slack <PAIRING_CODE>${C_RESET}"
  echo -e ""
fi
