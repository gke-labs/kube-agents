#!/usr/bin/env bash
# ==============================================================================
# 📢 Google Chat Instructions Printer
# ==============================================================================
set -e

# An install.env that records no PLATFORM_AGENT_MODE predates the key, so it
# is a today install whatever DEFAULT_PLATFORM_AGENT_MODE now says.
readonly GCHAT_UNRECORDED_PLATFORM_AGENT_MODE="today"

# Any position, as common.sh reads its own flags: `-y --help` is still a
# request for this text, not a run.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'USAGE'
Usage: print_instructions_gchat.sh [--dry-run] [-y|--no-confirm]

Prints the Google Chat setup checklist for this install: the app settings to
verify, the first message to send, and the steps after it. The steps follow
the Google Chat path the install uses: the A2A gateway under
PLATFORM_AGENT_MODE=next, the Hermes listener otherwise. It reads
the install.env at the root of the checkout it sits in ($KUBE_AGENTS_INSTALL_ENV
instead, when set) and scripts/installer/vars.sh, and prints the checklist
only when GOOGLE_CHAT_ENABLED is on there (true, yes, 1 or on). install.sh runs it
after every apply that leaves Google Chat on, --menu's Save & Apply included;
run it again at any time. It changes nothing on the
cluster, but like every installer step it records REGISTRY_PREFIX in
vars.sh (not under --dry-run), asking for it first on a terminal when
nothing sets it.
USAGE
      exit 0
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/common.sh" "$@"

# The Google Workspace Add-ons service agent, which Chat creates in the project and
# which terraform/modules/chat-pubsub/main.tf grants publisher on the events topic.
readonly WORKSPACE_ADDONS_SA_DOMAIN="gcp-sa-gsuiteaddons.iam.gserviceaccount.com"

# A standalone run takes the mode from install.env alone, as install.sh's own
# bootstrap does.
if [ -z "${GCHAT_STEPS_PLATFORM_AGENT_MODE+set}" ] && [ -f "$(install_env_file_for_state)" ]; then
  unset PLATFORM_AGENT_MODE
fi
load_state

# install.sh passes what its run applied, which load_state's install.env would
# otherwise override. A standalone run passes none and reads the file.
if [ -n "${GCHAT_STEPS_PLATFORM_AGENT_MODE+set}" ]; then
  PLATFORM_AGENT_MODE="$GCHAT_STEPS_PLATFORM_AGENT_MODE"
  GOOGLE_CHAT_ENABLED="${GCHAT_STEPS_GOOGLE_CHAT_ENABLED:-}"
  GOOGLE_CHAT_HOME_CHANNEL="${GCHAT_STEPS_HOME_CHANNEL:-}"
  ALLOWED_USERS="${GCHAT_STEPS_ALLOWED_USERS:-}"
  CHAT_TOPIC_NAME="${GCHAT_STEPS_TOPIC_NAME:-}"
  CHAT_SUB_NAME="${GCHAT_STEPS_SUB_NAME:-}"
  # load_state takes NAMESPACE from the files alone; --agent-namespace sets
  # it for the run.
  NAMESPACE="${GCHAT_STEPS_NAMESPACE:-${NAMESPACE:-}}"
fi
# An empty subscription name is one the tfvars generator derives from the
# topic, as an install.env that turned Google Chat on in the Day-2 menu has.
if [ -n "${CHAT_TOPIC_NAME:-}" ] && [ -z "${CHAT_SUB_NAME:-}" ]; then
  CHAT_SUB_NAME="$(derive_chat_sub_name "$CHAT_TOPIC_NAME")"
fi

# Google Chat on the A2A gateway: no pairing, and a home channel that only
# GOOGLE_CHAT_HOME_CHANNEL sets. $1 the number of the first step to print.
print_a2a_gateway_steps() {
  local step="$1" home not_a_space
  home="$(trim_go_space "${GOOGLE_CHAT_HOME_CHANNEL:-}")"
  not_a_space="$(gchat_home_channel_not_a_space_name "$home")"
  if [ -z "$home" ]; then
    echo -e "[ ] ${step}. No home channel is configured, so the agent posts no alerts or scheduled reports to Google Chat."
    echo -e "       Set ${C_WHITE}GOOGLE_CHAT_HOME_CHANNEL${C_RESET} in install.env to a space name (${C_WHITE}spaces/<id>${C_RESET}) and re-run the installer."
    echo -e "       A chat message or ${C_WHITE}/sethome${C_RESET} does not set it under next."
  elif [ -n "$not_a_space" ]; then
    echo -e "[ ] ${step}. GOOGLE_CHAT_HOME_CHANNEL is '${not_a_space}', which is not a space name, so the A2A gateway turns off its notify route:"
    echo -e "       no alerts or scheduled reports, and no card reports back to the conversation that asked for them."
    echo -e "       Set ${C_WHITE}GOOGLE_CHAT_HOME_CHANNEL${C_RESET} in install.env to ${C_WHITE}spaces/<id>${C_RESET} and re-run the installer."
    echo -e "       The id is the part after ${C_WHITE}/room/${C_RESET} or ${C_WHITE}/space/${C_RESET} in the space's URL."
  else
    echo -e "[ ] ${step}. GOOGLE_CHAT_HOME_CHANNEL is ${C_WHITE}${home}${C_RESET}: add the app to that space (@mention the app there),"
    echo -e "       or the agent's alerts and scheduled reports do not reach it."
  fi
  echo -e ""
  step=$((step + 1))
  if gchat_allowlist_is_empty "${ALLOWED_USERS:-}"; then
    echo -e "[ ] ${step}. ALLOWED_USERS is empty, so the A2A gateway answers everyone the app is visible to (step 1's Visibility)."
    echo -e "       To answer only some people, set ${C_WHITE}ALLOWED_USERS${C_RESET} in install.env to their email addresses and re-run the installer."
  else
    echo -e "[ ] ${step}. The A2A gateway answers only ${C_WHITE}${ALLOWED_USERS}${C_RESET}. Anyone else gets one notice"
    echo -e "       that asks an admin to add them to the allowed users, and no answers after it."
    echo -e "       To add a person, add the email address to ${C_WHITE}ALLOWED_USERS${C_RESET} in install.env and re-run the installer."
  fi
  echo -e ""
}

if is_truthy "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}"; then
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
  sa_display_format="service-${PROJECT_NUMBER:-<PROJECT_NUMBER>}@${WORKSPACE_ADDONS_SA_DOMAIN}"
  echo -e "       - After saving, refresh the page and verify the Workspace Add-ons ${C_BOLD}Service account email${C_RESET} appears under Connection settings"
  echo -e "         ${C_CYAN}(format: ${C_GREEN}${sa_display_format}${C_CYAN}; it is not the agent's ${C_YELLOW}${PLATFORM_AGENT_GSA_NAME:-$DEFAULT_PLATFORM_AGENT_GSA_NAME}${C_CYAN}).${C_RESET}"
  echo -e "         ${C_CYAN}(This field only exists when \"Build this Chat app as a Workspace add-on\" is checked — the default, locked-on state for new apps).${C_RESET}"
  echo -e "         ${C_YELLOW}If it stays blank, Chat will silently deliver NO events — re-run install.sh (it re-applies the Chat Pub/Sub resources) and re-save this config.${C_RESET}"
  echo -e ""
  next_step=2
  on_gateway=false
  if google_chat_moves_to_a2a_gateway "${PLATFORM_AGENT_MODE:-$GCHAT_UNRECORDED_PLATFORM_AGENT_MODE}" \
    "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}"; then
    on_gateway=true
  fi
  if [ -n "${PROJECT_NUMBER:-}" ]; then
    next_step=3
    echo -e "[ ] 2. Direct DM Link to Bot on Google Chat:"
    echo -e "       ${C_WHITE}https://chat.google.com/dm/${PROJECT_NUMBER}${C_RESET}"
    echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
    if ! $on_gateway && [ -z "${GOOGLE_CHAT_HOME_CHANNEL:-}" ]; then
      echo -e "       With no home channel configured, the install's first chat message makes its space the home channel for scheduled reports."
      echo -e "       To use a team space instead, send the first message there, or run ${C_WHITE}/sethome${C_RESET} there afterwards."
    fi
    echo -e ""
  else
    print_warning "PROJECT_NUMBER is unavailable; open the configured app from Google Chat instead of using a generated DM link."
  fi
  if $on_gateway; then
    print_a2a_gateway_steps "$next_step"
    exit 0
  fi
  echo -e "[ ] ${next_step}. ${C_YELLOW}[Optional]${C_RESET} Approve pairing code in GKE container:"
  echo -e "       ${C_CYAN}(Only required for first-time bot deployments. If the bot responds instantly, skip this!)${C_RESET}"
  echo -e "       ${C_WHITE}kubectl exec -it deploy/${PLATFORM_AGENT_DEPLOYMENT} -n ${NAMESPACE:-$DEFAULT_NAMESPACE} -- hermes pairing approve google_chat <PAIRING_CODE>${C_RESET}"
  echo -e ""
else
  # On stderr: a pipe still gets nothing, but a reader who expected the
  # checklist learns which file and key decided there is none.
  print_info "GOOGLE_CHAT_ENABLED is not on in $(install_env_file_for_state), so there is no Google Chat checklist. Set it there and run this again." >&2
fi
