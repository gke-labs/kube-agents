#!/usr/bin/env bash
# ==============================================================================
# 📢 Slack Instructions Printer
# ==============================================================================
set -e

# The Slack app setup page: the manifest the A2A gateway takes, how to move an
# app made for the Hermes Slack platform onto it, and the home channel.
readonly SLACK_GATEWAY_APP_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#for-the-a2a-gateway"
readonly SLACK_GATEWAY_MOVE_APP_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#moving-an-app-made-for-the-hermes-listener"
readonly SLACK_GATEWAY_HOME_CHANNEL_DOC="https://gke-labs.github.io/kube-agents/install/slack-app/#home-channel"

# Any position, as common.sh reads its own flags: `-y --help` is still a
# request for this text, not a run.
for arg in "$@"; do
  case "$arg" in
    -h|--help)
      cat <<'USAGE'
Usage: print_instructions_slack.sh [--dry-run] [-y|--no-confirm]

Prints the Slack setup checklist for this install: the app settings to
verify, the first message to send, and the steps after it. The steps are the
A2A gateway's, which holds Slack on every install. It reads
the install.env at the root of the checkout it sits in ($KUBE_AGENTS_INSTALL_ENV
instead, when set) and scripts/installer/vars.sh, and prints the checklist
only when SLACK_ENABLED is on there (true, yes, 1 or on). install.sh runs it
after every apply that leaves Slack on, --menu's Save & Apply included; run it
again at any time. It changes nothing on the
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

# A standalone run reads install.env; an exported SLACK_HOME_CHANNEL the file
# does not assign is kept: install.sh applies it too.
load_state

# install.sh passes what its run applied, which load_state's install.env would
# otherwise override. A standalone run passes none and reads the file.
if [ -n "${SLACK_STEPS_SLACK_ENABLED+set}" ]; then
  SLACK_ENABLED="${SLACK_STEPS_SLACK_ENABLED:-}"
  SLACK_HOME_CHANNEL="${SLACK_STEPS_HOME_CHANNEL:-}"
  # As in the Google Chat printer.
  NAMESPACE="${SLACK_STEPS_NAMESPACE:-${NAMESPACE:-}}"
fi

# Slack on the A2A gateway: no slash commands, no pairing, and a home channel
# that SLACK_HOME_CHANNEL sets, or the first top-level mention of the agent in
# a channel under homeChannelPolicy auto (the default).
print_a2a_gateway_steps() {
  local ns="${NAMESPACE:-$DEFAULT_NAMESPACE}"
  local home not_an_id
  home="$(trim_go_space "${SLACK_HOME_CHANNEL:-}")"
  not_an_id="$(slack_home_channel_not_an_id "$home")"
  echo -e "[ ] 1. Check the Slack app against the A2A gateway's manifest:"
  echo -e "       ${SLACK_GATEWAY_APP_DOC}"
  echo -e "       Socket Mode on, the scopes and message events it lists, and ${C_YELLOW}no slash commands${C_RESET}:"
  echo -e "       the A2A gateway does not acknowledge them, so Slack shows the member a timeout error."
  echo -e "       An app made for the Hermes Slack platform: delete its ${C_WHITE}features.slash_commands${C_RESET} block instead of pasting the new manifest over it:"
  echo -e "       ${SLACK_GATEWAY_MOVE_APP_DOC}"
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
    echo -e "       Or mention the agent once, top-level, in the channel you want: under homeChannelPolicy auto (the default) that channel becomes the home channel."
    echo -e "       Or pick the channel on the app's Home tab (Home channel panel)."
    echo -e "       ${C_WHITE}/sethome${C_RESET} does not set it on the A2A gateway: ${SLACK_GATEWAY_HOME_CHANNEL_DOC}"
  elif [ -n "$not_an_id" ]; then
    echo -e "[ ] 4. SLACK_HOME_CHANNEL is '${not_an_id}', which is not a channel ID, so the gateway posts no alerts or scheduled reports to Slack."
    echo -e "       Set ${C_WHITE}SLACK_HOME_CHANNEL${C_RESET} in install.env to a channel ID (C... or G...) and re-run the installer: ${SLACK_GATEWAY_HOME_CHANNEL_DOC}"
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
  print_a2a_gateway_steps
else
  # As in the Google Chat printer.
  print_info "SLACK_ENABLED is not on in $(install_env_file_for_state), so there is no Slack checklist. Set it there and run this again." >&2
fi
