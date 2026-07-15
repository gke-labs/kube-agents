#!/usr/bin/env bash
# ==============================================================================
# 📢 Google Chat Instructions Printer
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

load_state

if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
  if [ -z "${GOOGLE_CHAT_DOMAIN:-}" ]; then
    GOOGLE_CHAT_DOMAIN="<your-domain-or-static-ip>"
  fi

  echo -e "${C_CYAN}${C_BOLD}--- [Google Chat Integration Instructions] ---${C_RESET}"
  echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
  echo -e "       ${C_WHITE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT_ID}${C_RESET}"
  echo -e "       - Name: ${C_GREEN}GKE Platform Agent Bot${C_RESET}"
  if [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; then
    echo -e "       - Avatar: ${C_GREEN}https://platform-agent.nousresearch.com/docs/img/logo.png${C_RESET}"
    echo -e "       - Connection Settings: Select ${C_BOLD}App URL (HTTP Webhook)${C_RESET}"
    echo -e "       - App URL: ${C_GREEN}https://${GOOGLE_CHAT_DOMAIN}/googlechat${C_RESET}"
  else
    echo -e "       - Connection Settings: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
    echo -e "       - Pub/Sub Topic Name: ${C_GREEN}projects/${PROJECT_ID}/topics/${CHAT_TOPIC_NAME:-platform-agent-chat-events}${C_RESET}"
  fi
  echo -e "       - Under Visibility, check: ${C_GREEN}Only specific people (add your email/emails: ${ALLOWED_USERS:-your-email})${C_RESET}"
  echo -e ""
  BOT_NAME="Hermes"
  if [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; then
    BOT_NAME="OpenClaw"
  fi

  echo -e "[ ] 2. Send a DM to the Bot on Google Chat:"
  echo -e "       Type: ${C_WHITE}\"Hi ${BOT_NAME}\"${C_RESET}"
  echo -e ""
  if [ "${HARNESS_FRAMEWORK:-hermes}" = "openclaw" ]; then
    echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
    echo -e "       ${C_CYAN}(Only required if device auth is enabled. If the bot responds instantly, skip this!)${C_RESET}"
    echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- openclaw pairing approve googlechat <PAIRING_CODE>${C_RESET}"
    echo -e ""
  else
    echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
    echo -e "       ${C_CYAN}(Only required for first-time bot deployments. If the bot responds instantly, skip this!)${C_RESET}"
    echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes pairing approve google_chat <PAIRING_CODE>${C_RESET}"
    echo -e ""
  fi
fi
