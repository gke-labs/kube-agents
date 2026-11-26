#!/usr/bin/env bash
# ==============================================================================
# 📢 Google Chat Instructions Printer
# ==============================================================================
set -e

# Any position, as common.sh reads its own flags: `-y --help` is still a
# request for this text, not a run.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'USAGE'
Usage: print_instructions_gchat.sh [--dry-run] [-y|--no-confirm]

Prints the Google Chat setup checklist for this install: the app settings to
verify, the first message to send, and the steps after it (the A2A gateway
holds Google Chat). It reads
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
readonly CHAT_API_PUSH_SA="chat-api-push@system.gserviceaccount.com"
readonly CROSS_PROJECT_CHAT_GSA_NAME="kubeagents-chat-gsa"
readonly GSUITEADDONS_API="gsuiteaddons.googleapis.com"
readonly CHAT_API="chat.googleapis.com"
readonly PUBSUB_API="pubsub.googleapis.com"
readonly ROLE_TOKEN_CREATOR="roles/iam.serviceAccountTokenCreator"
readonly ROLE_PUBSUB_PUBLISHER="roles/pubsub.publisher"
readonly ROLE_PUBSUB_SUBSCRIBER="roles/pubsub.subscriber"
readonly ROLE_PUBSUB_VIEWER="roles/pubsub.viewer"
readonly CHAT_SUB_ACK_DEADLINE_SECONDS=60

load_state

# install.sh passes what its run applied, which load_state's install.env would
# otherwise override. A standalone run passes none and reads the file.
if [ -n "${GCHAT_STEPS_GOOGLE_CHAT_ENABLED+set}" ]; then
  GOOGLE_CHAT_ENABLED="${GCHAT_STEPS_GOOGLE_CHAT_ENABLED:-}"
  GOOGLE_CHAT_HOME_CHANNEL="${GCHAT_STEPS_HOME_CHANNEL:-}"
  ALLOWED_USERS="${GCHAT_STEPS_ALLOWED_USERS:-}"
  CHAT_TOPIC_NAME="${GCHAT_STEPS_TOPIC_NAME:-}"
  CHAT_SUB_NAME="${GCHAT_STEPS_SUB_NAME:-}"
fi
if [ -n "${GCHAT_STEPS_CHAT_PROJECT_ID+set}" ]; then
  CHAT_PROJECT_ID="${GCHAT_STEPS_CHAT_PROJECT_ID:-}"
fi
if [ -n "${GCHAT_STEPS_CHAT_MANAGE_PROJECT+set}" ]; then
  CHAT_MANAGE_PROJECT="${GCHAT_STEPS_CHAT_MANAGE_PROJECT:-}"
fi
if [ -n "${GCHAT_STEPS_CHAT_PROJECT_NUMBER+set}" ]; then
  CHAT_PROJECT_NUMBER="${GCHAT_STEPS_CHAT_PROJECT_NUMBER:-}"
fi
# An empty subscription name is one the tfvars generator derives from the
# topic, as an install.env that turned Google Chat on in the Day-2 menu has.
if [ -n "${CHAT_TOPIC_NAME:-}" ] && [ -z "${CHAT_SUB_NAME:-}" ]; then
  CHAT_SUB_NAME="$(derive_chat_sub_name "$CHAT_TOPIC_NAME")"
fi

# Google Chat on the A2A gateway: no pairing, and a home channel that
# GOOGLE_CHAT_HOME_CHANNEL sets or, under homeChannelPolicy auto, the gateway
# learns from the first mention in a named space. $1 the number of the first
# step to print.
print_a2a_gateway_steps() {
  local step="$1" home not_a_space
  home="$(trim_go_space "${GOOGLE_CHAT_HOME_CHANNEL:-}")"
  not_a_space="$(gchat_home_channel_not_a_space_name "$home")"
  if [ -z "$home" ]; then
    echo -e "[ ] ${step}. No home channel is configured, so the agent posts no alerts or scheduled reports to Google Chat yet."
    echo -e "       Set ${C_WHITE}GOOGLE_CHAT_HOME_CHANNEL${C_RESET} in install.env to a space name (${C_WHITE}spaces/<id>${C_RESET}) and re-run the installer;"
    echo -e "       or, under homeChannelPolicy auto (the default), @mention the app once, top-level, in the named space you want."
    echo -e "       A direct message or ${C_WHITE}/sethome${C_RESET} does not set it."
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

  chat_project="${CHAT_PROJECT_ID:-${PROJECT_ID:-}}"
  chat_manage="${CHAT_MANAGE_PROJECT:-$DEFAULT_CHAT_MANAGE_PROJECT}"
  agent_gsa_email="${PLATFORM_AGENT_GSA_NAME:-$DEFAULT_PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID:-<PROJECT_ID>}.iam.gserviceaccount.com"
  if [ -n "${CHAT_PROJECT_ID:-}" ] && [ "${CHAT_PROJECT_ID}" != "${PROJECT_ID:-}" ]; then
    subscriber_sa_email="${CROSS_PROJECT_CHAT_GSA_NAME}@${chat_project}.iam.gserviceaccount.com"
    chat_project_number="${CHAT_PROJECT_NUMBER:-}"
  else
    subscriber_sa_email="$agent_gsa_email"
    chat_project_number="${CHAT_PROJECT_NUMBER:-${PROJECT_NUMBER:-}}"
  fi

  echo -e "${C_CYAN}${C_BOLD}--- [Google Chat Integration Instructions] ---${C_RESET}"
  if ! is_truthy "$chat_manage"; then
    echo -e "${C_YELLOW}CHAT_MANAGE_PROJECT=false: have an administrator of '${chat_project:-<CHAT_PROJECT_ID>}' run:${C_RESET}"
    echo -e "  gcloud services enable ${CHAT_API} ${GSUITEADDONS_API} ${PUBSUB_API} --project=${chat_project:-<CHAT_PROJECT_ID>}"
    echo -e "  gcloud beta services identity create --service=${GSUITEADDONS_API} --project=${chat_project:-<CHAT_PROJECT_ID>}"
    echo -e "  gcloud beta services identity create --service=${CHAT_API} --project=${chat_project:-<CHAT_PROJECT_ID>}"
    if [ "$subscriber_sa_email" != "$agent_gsa_email" ]; then
      echo -e "  gcloud iam service-accounts create ${CROSS_PROJECT_CHAT_GSA_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>} --display-name=\"Platform Agent Google Chat Service Account\""
      echo -e "  gcloud iam service-accounts add-iam-policy-binding ${subscriber_sa_email} --project=${chat_project:-<CHAT_PROJECT_ID>} --member=\"serviceAccount:${agent_gsa_email}\" --role=\"${ROLE_TOKEN_CREATOR}\""
    fi
    echo -e "  gcloud pubsub topics create ${CHAT_TOPIC_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>}"
    echo -e "  gcloud pubsub subscriptions create ${CHAT_SUB_NAME} --topic=${CHAT_TOPIC_NAME} --ack-deadline=${CHAT_SUB_ACK_DEADLINE_SECONDS} --project=${chat_project:-<CHAT_PROJECT_ID>}"
    echo -e "  gcloud pubsub topics add-iam-policy-binding ${CHAT_TOPIC_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>} --member=\"serviceAccount:${CHAT_API_PUSH_SA}\" --role=\"${ROLE_PUBSUB_PUBLISHER}\""
    echo -e "  gcloud pubsub topics add-iam-policy-binding ${CHAT_TOPIC_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>} --member=\"serviceAccount:service-${chat_project_number:-<CHAT_PROJECT_NUMBER>}@${WORKSPACE_ADDONS_SA_DOMAIN}\" --role=\"${ROLE_PUBSUB_PUBLISHER}\""
    echo -e "  gcloud pubsub subscriptions add-iam-policy-binding ${CHAT_SUB_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>} --member=\"serviceAccount:${subscriber_sa_email}\" --role=\"${ROLE_PUBSUB_SUBSCRIBER}\""
    echo -e "  gcloud pubsub subscriptions add-iam-policy-binding ${CHAT_SUB_NAME} --project=${chat_project:-<CHAT_PROJECT_ID>} --member=\"serviceAccount:${subscriber_sa_email}\" --role=\"${ROLE_PUBSUB_VIEWER}\""
    echo -e ""
  fi
  echo -e "[ ] 1. Configure GChat bot connection in GCP Console:"
  echo -e "       ${C_WHITE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${chat_project}${C_RESET}"
  echo -e "       - Name: ${C_GREEN}GKE Platform Agent Bot${C_RESET}"
  echo -e "       - Avatar: ${C_GREEN}https://raw.githubusercontent.com/cncf/artwork/master/projects/kubernetes/icon/color/kubernetes-icon-color.png${C_RESET}"
  echo -e "       - Connection Settings: Select ${C_BOLD}Cloud Pub/Sub${C_RESET}"
  echo -e "       - Pub/Sub Topic Name: ${C_GREEN}projects/${chat_project}/topics/${CHAT_TOPIC_NAME}${C_RESET}"
  echo -e "       - Under Visibility, check: ${C_GREEN}Only specific people (add your email/emails: ${ALLOWED_USERS:-your-email})${C_RESET}"
  sa_display_format="service-${chat_project_number:-<PROJECT_NUMBER>}@${WORKSPACE_ADDONS_SA_DOMAIN}"
  echo -e "       - After saving, refresh the page and verify the Workspace Add-ons ${C_BOLD}Service account email${C_RESET} appears under Connection settings"
  echo -e "         ${C_CYAN}(format: ${C_GREEN}${sa_display_format}${C_CYAN}; it is not the agent's ${C_YELLOW}${PLATFORM_AGENT_GSA_NAME:-$DEFAULT_PLATFORM_AGENT_GSA_NAME}${C_CYAN}).${C_RESET}"
  echo -e "         ${C_CYAN}(This field only exists when \"Build this Chat app as a Workspace add-on\" is checked — the default, locked-on state for new apps).${C_RESET}"
  echo -e "         ${C_YELLOW}If it stays blank, Chat will silently deliver NO events — re-run install.sh (it re-applies the Chat Pub/Sub resources) and re-save this config.${C_RESET}"
  echo -e ""
  next_step=2
  if [ -n "${chat_project_number:-}" ]; then
    next_step=3
    echo -e "[ ] 2. Direct DM Link to Bot on Google Chat:"
    echo -e "       ${C_WHITE}https://chat.google.com/dm/${chat_project_number}${C_RESET}"
    echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
    echo -e ""
  else
    print_warning "PROJECT_NUMBER is unavailable; open the configured app from Google Chat instead of using a generated DM link."
  fi
  print_a2a_gateway_steps "$next_step"
else
  # On stderr: a pipe still gets nothing, but a reader who expected the
  # checklist learns which file and key decided there is none.
  print_info "GOOGLE_CHAT_ENABLED is not on in $(install_env_file_for_state), so there is no Google Chat checklist. Set it there and run this again." >&2
fi
