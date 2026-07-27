#!/usr/bin/env bash
# ==============================================================================
# 📢 Google Chat Instructions Printer
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

load_state

if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
  if [ -z "${CHAT_TOPIC_NAME:-}" ] || [ -z "${CHAT_SUB_NAME:-}" ]; then
    print_warning "Google Chat integration is enabled but CHAT_TOPIC_NAME or CHAT_SUB_NAME is missing. It may not work properly."
  fi

  echo -e "${C_CYAN}${C_BOLD}--- [Google Chat Integration Instructions] ---${C_RESET}"
  echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
  echo -e "       ${C_WHITE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${PROJECT_ID}${C_RESET}"
  echo -e "       - Name: ${C_GREEN}GKE Platform Agent Bot${C_RESET}"
  echo -e "       - Avatar: ${C_GREEN}https://raw.githubusercontent.com/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png${C_RESET}"
  echo -e "       - Connection Settings: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
  echo -e "       - Pub/Sub Topic Name: ${C_GREEN}projects/${PROJECT_ID}/topics/${CHAT_TOPIC_NAME}${C_RESET}"
  echo -e "       - Under Visibility, check: ${C_GREEN}Only specific people (add your email/emails: ${ALLOWED_USERS:-your-email})${C_RESET}"
  echo -e "       - After saving, refresh the page and verify a ${C_BOLD}Service account email${C_RESET} appears under Connection settings"
  echo -e "         ${C_CYAN}(this field only exists when \"Build this Chat app as a Workspace add-on\" is checked — the default, locked-on state for new apps).${C_RESET}"
  echo -e "         ${C_YELLOW}If it stays blank, Chat will silently deliver NO events — re-run provision_05_gcp_gchat.sh and re-save this config.${C_RESET}"
  echo -e ""
  echo -e "[ ] 2. Send a DM to the Bot on Google Chat:"
  echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
  echo -e ""
  echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
  echo -e "       ${C_CYAN}(Only required for first-time bot deployments. If the bot responds instantly, skip this!)${C_RESET}"
  echo -e "       ${C_WHITE}kubectl exec -it deploy/platform-agent-gateway -n ${NAMESPACE:-kubeagents-system} -- hermes pairing approve google_chat <PAIRING_CODE>${C_RESET}"
  echo -e ""
fi
