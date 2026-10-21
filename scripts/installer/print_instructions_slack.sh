#!/usr/bin/env bash
# ==============================================================================
# 📢 Slack Instructions Printer
# ==============================================================================
set -e

# An install.env that records no PLATFORM_AGENT_MODE predates the key, so it
# is a today install whatever DEFAULT_PLATFORM_AGENT_MODE now says.
readonly SLACK_UNRECORDED_PLATFORM_AGENT_MODE="today"
readonly SLACK_NEXT_APP_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#for-specmode-next-the-a2a-gateway"
readonly SLACK_NEXT_MOVE_APP_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#moving-an-existing-app-to-next"
readonly SLACK_NEXT_HOME_CHANNEL_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#home-channel"

# Any position, as common.sh reads its own flags: `-y --help` is still a
# request for this text, not a run.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'USAGE'
Usage: print_instructions_slack.sh [--dry-run] [-y|--no-confirm]

Prints the Slack setup checklist for this install: the app settings to
verify, the first message to send, and the steps after it. The steps follow
the Slack path the install uses: the A2A gateway under PLATFORM_AGENT_MODE=next
without Google Chat, the Hermes listener otherwise. It reads
the install.env at the root of the checkout it sits in ($KUBE_AGENTS_INSTALL_ENV
instead, when set) and scripts/installer/vars.sh, and prints the checklist
only when SLACK_ENABLED=true there. install.sh runs it at the end of an install
with Slack enabled; run it again at any time. It changes nothing on the
cluster, but like every installer step it records REGISTRY_PREFIX in
vars.sh (not under --dry-run), asking for it first on a terminal when
nothing sets it.
USAGE
      exit 0
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" "$@"

# A standalone run takes the mode from install.env alone, as install.sh's own
# bootstrap does. An exported GOOGLE_CHAT_ENABLED or SLACK_HOME_CHANNEL the
# file does not assign is kept: install.sh applies those too.
if [ -z "${SLACK_STEPS_PLATFORM_AGENT_MODE+set}" ] && [ -f "$(install_env_file_for_state)" ]; then
  unset PLATFORM_AGENT_MODE
fi
load_state

# install.sh passes what its run applied, which load_state's install.env would
# otherwise override. A standalone run passes none and reads the file.
if [ -n "${SLACK_STEPS_PLATFORM_AGENT_MODE+set}" ]; then
  PLATFORM_AGENT_MODE="$SLACK_STEPS_PLATFORM_AGENT_MODE"
  SLACK_ENABLED="${SLACK_STEPS_SLACK_ENABLED:-}"
  GOOGLE_CHAT_ENABLED="${SLACK_STEPS_GOOGLE_CHAT_ENABLED:-}"
  SLACK_HOME_CHANNEL="${SLACK_STEPS_HOME_CHANNEL:-}"
fi

# Slack on the A2A gateway: no slash commands, no pairing, and a home channel
# that only SLACK_HOME_CHANNEL sets.
print_a2a_gateway_steps() {
  local ns="${NAMESPACE:-$DEFAULT_NAMESPACE}"
  local home not_an_id
  home="$(trim_go_space "${SLACK_HOME_CHANNEL:-}")"
  not_an_id="$(slack_home_channel_not_an_id "$home")"
  echo -e "[ ] 1. Check the Slack app against the spec.mode next manifest:"
  echo -e "       ${SLACK_NEXT_APP_DOC}"
  echo -e "       Socket Mode on, the scopes and message events it lists, and ${C_YELLOW}no slash commands${C_RESET}:"
  echo -e "       the A2A gateway does not acknowledge them, so Slack shows the member a timeout error."
  echo -e "       An app made for today: delete its ${C_WHITE}features.slash_commands${C_RESET} block instead of pasting the new manifest over it:"
  echo -e "       ${SLACK_NEXT_MOVE_APP_DOC}"
  echo -e ""
  echo -e "[ ] 2. Check that the gateway connected to Slack (it can take a minute after the install):"
  echo -e "       ${C_WHITE}kubectl logs deploy/${PLATFORM_AGENT_A2A_GATEWAY_DEPLOYMENT} -c ${PLATFORM_AGENT_A2A_GATEWAY_CONTAINER} -n ${ns} | grep 'slack connected'${C_RESET}"
  echo -e ""
  echo -e "[ ] 3. DM the app in Slack (Apps, the app's name, Messages):"
  echo -e "       Type: ${C_WHITE}\"what clusters can you see?\"${C_RESET}"
  echo -e ""
  if [ -z "$home" ]; then
    echo -e "[ ] 4. No home channel is configured, so the agent posts no alerts or scheduled reports to Slack."
    echo -e "       Set ${C_WHITE}SLACK_HOME_CHANNEL${C_RESET} in install.env to a channel ID (C... or G...) and re-run the installer."
    echo -e "       A chat message or ${C_WHITE}/sethome${C_RESET} does not set it under next: ${SLACK_NEXT_HOME_CHANNEL_DOC}"
  elif [ -n "$not_an_id" ]; then
    echo -e "[ ] 4. SLACK_HOME_CHANNEL is '${not_an_id}', which is not a channel ID, so the gateway posts no alerts or scheduled reports to Slack."
    echo -e "       Set ${C_WHITE}SLACK_HOME_CHANNEL${C_RESET} in install.env to a channel ID (C... or G...) and re-run the installer: ${SLACK_NEXT_HOME_CHANNEL_DOC}"
  else
    echo -e "[ ] 4. Invite the app to the home channel ${C_WHITE}${home}${C_RESET}, or Slack refuses its alerts and scheduled reports there:"
    echo -e "       ${C_WHITE}/invite @<app display name>${C_RESET} in that channel."
  fi
  echo -e ""
}

if is_truthy "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"; then
  if [ -z "${SLACK_BOT_TOKEN:-}" ]; then
    print_warning "SLACK_BOT_TOKEN is empty. Slack integration may not work properly until provided."
  fi
  if [ -z "${SLACK_APP_TOKEN:-}" ]; then
    print_warning "SLACK_APP_TOKEN is empty. Slack integration may not work properly until provided."
  fi

  echo -e "${C_CYAN}${C_BOLD}--- [Slack Integration Instructions] ---${C_RESET}"
  if slack_moves_to_a2a_gateway "${PLATFORM_AGENT_MODE:-$SLACK_UNRECORDED_PLATFORM_AGENT_MODE}" \
    "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}" "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}"; then
    print_a2a_gateway_steps
    exit 0
  fi
  echo -e "[ ] 1. Verify Slack Bot configuration:"
  echo -e "       - Ensure Socket Mode is ${C_GREEN}enabled${C_RESET} in Slack App Console."
  echo -e "       - Ensure the Bot Token holds every bot scope in the manifest from step 4"
  echo -e "         (${C_WHITE}hermes slack manifest${C_RESET}); that list, not this printout, is the source of truth."
  echo -e "       - ${C_YELLOW}The *:history scopes are the ones a hand-built app most often lacks${C_RESET}: im:read is"
  echo -e "         conversation metadata, and DM text arrives on message.im, which needs im:history"
  echo -e "         (groups:history and mpim:history likewise). A bot without them connects and reads nothing."
  echo -e "       - ${C_YELLOW}files:write is easy to miss${C_RESET}: without it, file artifacts fail to upload with"
  echo -e "         missing_scope and the user is told the task completed."
  echo -e "       - ${C_YELLOW}reactions:write is quieter still${C_RESET}: without it the agent cannot put the"
  echo -e "         eyes on a message it picked up, or the check/cross on how the turn ended. The"
  echo -e "         adapter logs missing_scope at debug and carries on, so the only symptom is that"
  echo -e "         reactions never appear."
  echo -e ""
  echo -e "[ ] 2. Send a DM or mention the Bot in Slack:"
  echo -e "       Type: ${C_WHITE}\"Hi Platform Agent\"${C_RESET}"
  echo -e ""
  echo -e "[ ] 3. ${C_YELLOW}[Optional]${C_RESET} Approve a pairing code, only if you set the Hermes Slack DM policy to pairing yourself (the install uses the allowlist):"
  echo -e "       ${C_WHITE}kubectl exec -it deploy/${PLATFORM_AGENT_DEPLOYMENT} -n ${NAMESPACE:-$DEFAULT_NAMESPACE} -- hermes pairing approve slack <PAIRING_CODE>${C_RESET}"
  echo -e ""
  echo -e "[ ] 4. ${C_YELLOW}[Optional]${C_RESET} Register the native slash commands so Slack autocompletes them:"
  echo -e "       ${C_WHITE}kubectl exec deploy/${PLATFORM_AGENT_DEPLOYMENT} -n ${NAMESPACE:-$DEFAULT_NAMESPACE} -- hermes slack manifest${C_RESET}"
  echo -e "       Paste the printed JSON into the Slack App Console (Features -> App Manifest -> Edit), save, and reinstall when prompted."
  echo -e "       That manifest replaces the whole app definition. To keep the app you already configured, add"
  echo -e "       ${C_WHITE}--slashes-only${C_RESET} and merge the printed array into the manifest's existing ${C_WHITE}features.slash_commands${C_RESET}."
  echo -e "       Without this, Slack delivers a typed ${C_WHITE}/hermes ...${C_RESET} as a plain message; the agent still understands it, but there is no autocomplete."
  echo -e ""

  if [ -z "${SLACK_HOME_CHANNEL:-}" ]; then
    echo -e "[ ] 5. No home channel was configured at install. If step 2 was the install's first chat message, it set one there."
    echo -e "       Otherwise scheduled audits have nowhere to post on Slack. Set or move it from the channel you want: ${C_WHITE}/hermes sethome${C_RESET}"
    echo -e "       Setting ${C_WHITE}SLACK_HOME_CHANNEL${C_RESET} by re-running this step applies only while no chat message has set one; after that, use ${C_WHITE}/hermes sethome${C_RESET}."
    echo -e ""
  fi
fi
