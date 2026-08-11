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
  echo -e "       - Ensure Bot Token scopes include: ${C_GREEN}app_mentions:read, channels:history, chat:write, channels:read, groups:read, im:read, mpim:read, files:write${C_RESET}."
  echo -e "       - ${C_YELLOW}files:write is easy to miss${C_RESET}: without it, file artifacts fail to upload with"
  echo -e "         missing_scope and the user is told the task completed."
  echo -e ""
  echo -e "[ ] 2. Send a DM or mention the Bot in Slack:"
  echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
  echo -e ""
  echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container (if pairing mode enabled):"
  echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes pairing approve slack <PAIRING_CODE>${C_RESET}"
  echo -e ""
  echo -e "[ ] 4. ${C_YELLOW}[Optional]${C_RESET} Register the native slash commands so Slack autocompletes them:"
  echo -e "       ${C_WHITE}kubectl exec deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes slack manifest${C_RESET}"
  echo -e "       Paste the printed JSON into the Slack App Console (Features -> App Manifest -> Edit), save, and reinstall when prompted."
  echo -e "       That manifest replaces the whole app definition. To keep the app you already configured, add"
  echo -e "       ${C_WHITE}--slashes-only${C_RESET} and merge the printed array into the manifest's existing ${C_WHITE}features.slash_commands${C_RESET}."
  echo -e "       Without this, Slack delivers a typed ${C_WHITE}/hermes ...${C_RESET} as a plain message; the agent still understands it, but there is no autocomplete."
  echo -e ""

  if [ -z "${SLACK_HOME_CHANNEL:-}" ]; then
    echo -e "[ ] 5. No home channel is configured. Scheduled audits have nowhere to post until one is set."
    echo -e "       Set it from Slack in the channel you want: ${C_WHITE}/hermes sethome${C_RESET}"
    echo -e "       Or set ${C_WHITE}SLACK_HOME_CHANNEL${C_RESET} by re-running this step."
    echo -e ""
  fi
fi
