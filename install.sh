#!/usr/bin/env bash
# ==============================================================================
# 🤖 Kubernetes Agentic Harness (kube-agents) Zero-Friction Installer
# ==============================================================================
# Usage (Interactive):
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash
#
# Usage (AI Agents & Non-Interactive Automation):
#   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/main/install.sh | bash -s -- \
#     --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent"
#
# Designed for Google Cloud Shell, Linux, macOS, and AI Agent harnesses.
# ==============================================================================

set -Eeuo pipefail

# ─── ANSI Colors & Terminal Responsive Helpers ─────────────────────────────────
# A function because scripts/installer/common.sh defines the same variables
# unconditionally: sourcing it would re-enable colour under NO_COLOR or in a pipe,
# so the installer re-applies its own policy afterwards.
configure_colors() {
  if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    C_CYAN='' C_GREEN='' C_YELLOW='' C_MAGENTA='' C_RED='' C_RESET='' C_BOLD='' C_UNDERLINE=''
  else
    # Use \033 rather than \e: bash 3.2 (the /bin/bash macOS ships) does not
    # expand \e in `echo -e`, so the raw escapes leak into the terminal.
    C_CYAN='\033[96m' C_GREEN='\033[92m' C_YELLOW='\033[93m' C_MAGENTA='\033[95m' C_RED='\033[91m' C_RESET='\033[0m' C_BOLD='\033[1m' C_UNDERLINE='\033[4m'
  fi
}
configure_colors

# Defined here, ahead of everything that reports, because loading install.env
# below is the first thing this script does and it has to be able to say so.
# scripts/installer/common.sh defines its own print_* helpers formatted for
# the state file, so source_provisioning_helpers re-applies these afterwards.
define_print_helpers() {
  print_step() { echo -e "\n${C_MAGENTA}${C_BOLD}>>> $1 <<<${C_RESET}"; }
  print_success() { echo -e "  ${C_GREEN}✓ $1${C_RESET}"; }
  print_info() { echo -e "  ${C_CYAN}ℹ $1${C_RESET}"; }
  print_warning() { echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"; }
  print_error() { echo -e "  ${C_RED}✗ $1${C_RESET}"; }
}
define_print_helpers

# ─── Process Lock File & Error Trap Handling ────────────────────────────────
LOCK_FILE="/tmp/kube-agents-install.lock"
# The gateway's service account id when the kustomize path's LITELLM_GSA_NAME is
# not in the environment; must agree with module.litellm_vertex_iam in
# terraform/examples/full-install/main.tf.
LITELLM_GSA_DEFAULT_NAME="kubeagents-litellm-gsa"
if command -v flock >/dev/null 2>&1; then
  if ( : >"$LOCK_FILE" ) 2>/dev/null && exec 200>"$LOCK_FILE"; then
    if ! flock -n 200 2>/dev/null; then
      echo -e "  \033[93m⚠ Another instance of kube-agents installer is currently running. Exiting.\033[0m" >&2
      exit 1
    fi
  fi
fi

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  echo -e "\n\033[91m\033[1m✗ Error encountered at line ${line_no} (exit code ${exit_code}): ${bash_cmd}\033[0m" >&2
  write_json_report "FAILED" "${line_no}" "${bash_cmd}" 2>/dev/null || true
  # A half-written install.env must not be left where the next run would load
  # it. The real file is only ever moved into place complete.
  if [ -n "${INSTALL_ENV_FILE:-}" ] && [ -f "${INSTALL_ENV_FILE}.tmp" ]; then
    rm -f -- "${INSTALL_ENV_FILE}.tmp"
  fi
  # Same for the tfvars the generator was midway through writing. It is mode
  # 600 and carries every secret the run was given, and it is named one
  # character from the file the next reader would open. write_tfvars_from_state
  # publishes the path while the write is in flight and clears it after the mv.
  if [ -n "${TFVARS_TMP_FILE:-}" ] && [ -f "${TFVARS_TMP_FILE}" ]; then
    rm -f -- "${TFVARS_TMP_FILE}"
  fi
  exit "$exit_code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

# Sourced/baked release version. On developer checkouts (main), this is empty.
# Release automation stamps this value (e.g. BAKED_RELEASE_VERSION="0.2.0") when publishing a GA release.
BAKED_RELEASE_VERSION=""

# ─── Install Defaults (install.defaults.env) ──────────────────────────────────
# Sourced before the parameter block so the DEFAULT_* values are in scope where
# the parameters are declared, and no default has to be spelled a second time
# here. Without `set -a`: these are the project's defaults, not the install's
# configuration, and they must not enter the environment Terraform sees.
#
# Absent when install.sh is downloaded on its own (curl | bash), where there is
# no checkout yet. That is not fatal — resolve_shared_defaults applies the same
# values once the workspace step has cloned the repository and sourced
# installer_common.sh, which reads this same file. Same source either way.
_install_defaults_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
if [ -n "$_install_defaults_dir" ] && [ -r "${_install_defaults_dir}/install.defaults.env" ]; then
  # shellcheck source=install.defaults.env disable=SC1091
  . "${_install_defaults_dir}/install.defaults.env"
fi
unset _install_defaults_dir

# ─── Install Configuration Input (install.env) ────────────────────────────────
# The hand-authored record of what this install is, loaded BEFORE the parameter
# block below so that every `${VAR:-}` seed in it inherits from the file. That
# ordering is the whole mechanism: inheriting prior configuration stops being
# something each flag has to remember to do and becomes the default path.
#
# An input the installer reads and does not rewrite. It creates one at the end
# of a first install, when there is nothing there, and never touches it again:
# a file the documentation tells you to edit and the next run overwrites is
# exactly the complaint against vars.sh, whose header said "auto-generated"
# while INSTALL.md told you to hand-edit it.
#
# `set -a` rather than a K=V parser: these values have to reach
# write_tfvars_from_state and the TF_VAR_* handoff at the end of it, both of
# which read the environment. A conventional dotenv without `export` would parse
# and then not travel.
#
# Always resolves to a path, whether or not a file is there yet -- a first
# install has nothing to read, and this is also where the file gets written.
#
# The directory is the CHECKOUT the run will end up in, which under
# `curl … | bash` is not the directory the operator is standing in. There
# ${BASH_SOURCE[0]} names no file, so a script-relative path resolves to the
# invocation directory; acquire_source_repo then clones to $HOME/kube-agents
# and cd's there, and every other front door resolves the file as
# ${repo_dir}/install.env (default_install_env_file). Freezing the invocation
# directory here would put the whole configuration -- API_SERVER_KEY and the
# plaintext model keys included -- where no later run looks: upgrade.sh would
# take its fail-closed branch, and a re-run of the one-liner would rebuild
# every PARAM_* from defaults.
#
# So resolve the same repo_dir acquire_source_repo will pick, by the same test
# and in the same order, before the parameter block reads any of it. This has
# to stay in step with acquire_source_repo; the shared marker file is the
# coupling, and test_install_script.py pins the pair.
_resolve_repo_dir_for_state() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
  if [ -n "$script_dir" ] && [ -f "${script_dir}/scripts/installer/installer_common.sh" ]; then
    printf '%s' "$script_dir"
  elif [ -f "scripts/installer/installer_common.sh" ]; then
    pwd
  else
    printf '%s' "$HOME/kube-agents"
  fi
}
_state_repo_dir="$(_resolve_repo_dir_for_state)"

INSTALL_ENV_FILE="${KUBE_AGENTS_INSTALL_ENV:-}"
INSTALL_ENV_EXPLICIT="false"
if [ -n "$INSTALL_ENV_FILE" ]; then
  INSTALL_ENV_EXPLICIT="true"
else
  _install_env_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
  # An install.env the operator actually put beside the script, or in the
  # directory they are standing in, still wins -- those are deliberate acts and
  # predate this resolution. Only when neither is there does the checkout
  # decide, which is the case that was landing outside it.
  if [ -n "$_install_env_dir" ] && [ -f "${_install_env_dir}/install.env" ]; then
    INSTALL_ENV_FILE="${_install_env_dir}/install.env"
  elif [ -f "install.env" ]; then
    INSTALL_ENV_FILE="$(pwd)/install.env"
  else
    INSTALL_ENV_FILE="${_state_repo_dir}/install.env"
  fi
  unset _install_env_dir
fi

# The state file install.env replaces. Loaded FIRST so install.env wins on
# every key it carries, and only from a checkout -- a fresh clone has none.
# This is the migration: an existing install keeps working with no action from
# its owner, and the run writes their values into install.env on the way out.
#
# Resolved against the same checkout, for the same reason: under
# `curl … | bash` a script-relative path names the invocation directory, not
# the clone the migration has to read.
LEGACY_VARS_FILE=""
if [ -f "${_state_repo_dir}/k8s-operator/scripts/vars.sh" ]; then
  LEGACY_VARS_FILE="${_state_repo_dir}/k8s-operator/scripts/vars.sh"
fi
unset _state_repo_dir

load_legacy_vars_file() {
  local file="${1:-}"
  [ -n "$file" ] && [ -f "$file" ] || return 0
  if ! bash -n "$file" 2>/dev/null; then
    print_error "Legacy install state '$file' is not valid shell and could not be loaded."
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  # stderr, like the load message below and for the same reason.
  print_warning "Loaded legacy install state from ${file}; install.env replaces it." >&2
  # Telling the operator to delete vars.sh is only safe once its values are
  # somewhere else, and this function cannot promise that. bootstrap_install_env_file
  # is the sole writer, it runs near the end of main(), and it returns early
  # when install.env already exists -- so --help, --menu, --dry-run, any abort,
  # and every run against a file the operator wrote themselves reach the write
  # never or as a no-op. An existing install.env is the dangerous case rather
  # than the safe-looking one: `cp install.env.example install.env` carries no
  # MEMORY, so discarding vars.sh there loses the only record that the install
  # runs Hindsight, and the next apply derives multiuser_memory and tears it down.
  if [ -f "$INSTALL_ENV_FILE" ]; then
    print_info "${INSTALL_ENV_FILE} already exists and this run will not rewrite it. Copy anything you still need from vars.sh into it before deleting vars.sh." >&2
  else
    print_info "Once this run creates ${INSTALL_ENV_FILE}, check it against vars.sh and then delete vars.sh." >&2
  fi
}

# Named apart from installer_common.sh's load_install_env, which this file
# sources later and which upgrade.sh and uninstall.sh use. The two differ on
# purpose: that one returns 1 for "no file" so a caller can report it, while
# this one runs before any helper is available and treats an explicitly named
# file that is missing as fatal. Sharing a name would leave the later
# definition silently replacing this one.
bootstrap_install_env() {
  local file="${1:-}"
  [ -n "$file" ] || return 0
  if [ ! -f "$file" ]; then
    if [ "$INSTALL_ENV_EXPLICIT" = "true" ]; then
      # Asked for by name and not there. That is a mistake, not a first
      # install, and continuing would provision from defaults.
      print_error "KUBE_AGENTS_INSTALL_ENV names '$file', which does not exist." >&2
      exit 1
    fi
    return 0
  fi
  # Checked before sourcing: a stray quote would otherwise abort the run through
  # the ERR trap with a bash parse error and no indication of which file.
  if ! bash -n "$file" 2>/dev/null; then
    print_error "Install configuration '$file' is not valid shell and could not be loaded." >&2
    print_info "Each line is NAME=value; quote any value containing spaces." >&2
    exit 1
  fi
  # Tighten a world- or group-readable configuration before reading it. The
  # documented way to create one is `cp install.env.example install.env`, and
  # install.env.example is tracked 100644, so a stock umask 022 leaves the file
  # 0644 -- and it is where the operator then writes GEMINI_API_KEY,
  # SLACK_BOT_TOKEN, API_SERVER_KEY and the rest. Nothing else reaches it:
  # bootstrap_install_env_file returns early once the destination exists, so
  # its chmod 600 never runs, and save_env_var's is reachable only from the
  # Day-2 menu. INSTALL.md states flatly that the file is 0600, and the
  # predecessor vars.sh always was, being installer-created under umask 077.
  # Announced rather than silent: the permissions of a file the operator owns
  # are theirs to know about.
  local mode=""
  # GNU first, BSD second, and the order is load-bearing. On coreutils `-f` is
  # --file-system and takes no argument, so `stat -f '%OLp' FILE` prints a
  # multi-line filesystem block on stdout and exits 1 -- non-empty output on
  # the failure path, which the `||` chain then concatenates with the real mode
  # and the warning below prints verbatim. BSD `stat -c` fails with empty
  # stdout, so putting GNU first costs macOS nothing.
  mode="$(stat -c '%a' "$file" 2>/dev/null || stat -f '%OLp' "$file" 2>/dev/null || echo "")"
  if [ -n "$mode" ] && [ "${mode: -2}" != "00" ]; then
    if chmod 600 "$file" 2>/dev/null; then
      print_warning "Tightened permissions on ${file} to 0600 (was ${mode}); it holds credentials." >&2
    else
      print_warning "${file} is mode ${mode} and holds credentials; chmod 600 it." >&2
    fi
  fi
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  # stderr, not stdout. This runs at source time, before main(), so anything on
  # stdout here lands in front of whatever the caller went on to capture --
  # including a function's echoed return value when the test suite sources this
  # file to exercise one. It is a diagnostic either way, not data.
  print_success "Loaded install configuration from: ${file}" >&2
}
# --help needs nothing either of these loads, and both have side effects a
# request for the flag list should not pay for: bootstrap_install_env chmods a
# group-readable install.env, and it exits 1 when KUBE_AGENTS_INSTALL_ENV names
# a path that is gone -- so a shell still carrying that variable from an earlier
# run answers `./install.sh --help` with an error about a file nobody asked
# about. .agents/skills/install-kube-agents/SKILL.md calls this output the
# authoritative list of flags, so it has to survive a stale environment.
#
# Scanned here rather than in parse_args because the loads run at source time,
# before it. An exact match only: --help-me is not --help, and a value that
# merely contains the word (--project-id=help-desk) is not the flag.
wants_help_only() {
  local arg
  for arg in "$@"; do
    case "$arg" in
      -h | --help | -\?) return 0 ;;
    esac
  done
  return 1
}

if ! wants_help_only "$@"; then
  load_legacy_vars_file "$LEGACY_VARS_FILE"
  bootstrap_install_env "$INSTALL_ENV_FILE"
fi

# ─── Agentic & Automation Parameter States ────────────────────────────────────
PARAM_NON_INTERACTIVE="${NONINTERACTIVE:-false}"
PARAM_DRY_RUN="${DRY_RUN:-false}"
PARAM_PROJECT_ID="${PROJECT_ID:-}"
PARAM_REGION="${REGION:-}"
PARAM_CLUSTER_NAME="${CLUSTER_NAME:-}"
# Only consulted when this run creates the cluster. Against one that already
# exists the tfvars generator's live probe decides the shape; see
# write_tfvars_from_state in scripts/installer/installer_common.sh. Empty
# means "not chosen yet" — the interview asks, and falls back to
# installer_common.sh's DEFAULT_CLUSTER_MODE when there is nobody to ask. The
# shape is deliberately not named here: that table is the one home for it.
PARAM_CLUSTER_MODE="${CLUSTER_MODE:-}"
# Left empty on purpose: resolved from installer_common.sh's DEFAULT_* once
# the installer helpers are sourced, so no default is spelled twice.
PARAM_MODEL_PROVIDER="${MODEL_PROVIDER:-}"
PARAM_VERTEX_PROJECT_ID="${VERTEX_PROJECT_ID:-}"
PARAM_VERTEX_LOCATION="${VERTEX_LOCATION:-}"
PARAM_VERTEX_MANAGE_SERVING_PROJECT="${VERTEX_MANAGE_SERVING_PROJECT:-}"
PARAM_GEMINI_API_KEY="${GEMINI_API_KEY:-}"
PARAM_OPENAI_API_KEY="${OPENAI_API_KEY:-}"
PARAM_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
PARAM_GITOPS_ORG="${GITOPS_ORG:-${GITHUB_ORG:-}}"
PARAM_GITOPS_REPO="${GITOPS_REPO:-${GITHUB_REPO:-}}"
# Left empty where installer_common.sh owns the default, the way
# PARAM_MODEL_PROVIDER above is: resolve_shared_defaults fills them in once the
# helpers are sourced, so no default is spelled twice.
PARAM_PERMISSION_SET="${PLATFORM_AGENT_PERMISSION_SET:-}"
PARAM_CUSTOM_ROLES="${PLATFORM_AGENT_CUSTOM_ROLES:-}"
PARAM_ENABLE_PUBSUB_PLATFORM="${ENABLE_PUBSUB_PLATFORM:-false}"
PARAM_ENABLE_STOCKOUT_INVESTIGATOR="${ENABLE_STOCKOUT_INVESTIGATOR:-false}"
# Set-ness, never ${VAR:-...}: `--gvisor=` with no value sets this to the empty
# string, and that has to survive to the validator in main rather than being
# silently read back as the default. The default itself comes from
# install.defaults.env, sourced above — and again through
# resolve_shared_defaults for the curl | bash case, where there was no checkout
# to read it from yet.
#
# Leaving PARAM_ENABLE_GVISOR *unset* when neither name is set is what makes
# that second route work. Assigning the empty string here would be
# indistinguishable from `--gvisor=`: under curl | bash there is no
# install.defaults.env beside the script, so DEFAULT_ENABLE_GVISOR is unset at
# this point, and resolve_shared_defaults' own ${PARAM_ENABLE_GVISOR-...} would
# see a variable already set and leave it empty for the validator to reject.
if [ -n "${ENABLE_GVISOR+x}" ]; then
  PARAM_ENABLE_GVISOR="$ENABLE_GVISOR"
elif [ -n "${DEFAULT_ENABLE_GVISOR+x}" ]; then
  PARAM_ENABLE_GVISOR="$DEFAULT_ENABLE_GVISOR"
fi
# HERMES_DASHBOARD_ENABLED as well as ENABLE_WEBUI: the flag is spelled
# --enable-web-ui and the install records the setting under the Hermes name, so
# a file written from a previous install carries the second spelling and only
# the second. The same asymmetry applies to MEMORY / MEMORY_PROVIDER below and
# to GOOGLE_CHAT_ENABLED below.
PARAM_ENABLE_WEBUI="${ENABLE_WEBUI:-${HERMES_DASHBOARD_ENABLED:-}}"
# MEMORY is the input spelling (file | hindsight | off). MEMORY_PROVIDER is what
# the install records, so translate it back when that is all there is.
memory_mode_from_provider() {
  case "${1:-}" in
    kube_agents_memory) echo "hindsight" ;;
    none) echo "off" ;;
    multiuser_memory) echo "file" ;;
    *) echo "" ;;
  esac
}
PARAM_MEMORY="${MEMORY:-$(memory_mode_from_provider "${MEMORY_PROVIDER:-}")}"
PARAM_ALLOWED_USERS="${ALLOWED_USERS:-}"
PARAM_IMAGE_TAG="${IMAGE_TAG:-}"
PARAM_ALLOW_UNVERIFIED_SOURCE="${ALLOW_UNVERIFIED_SOURCE:-false}"
# "<repo_dir>@<ref>" already checked by verify_local_source_ref, so the pre-flight
# check and the one at the workspace step do not report the same verdict twice.
SOURCE_REF_VERIFIED=""
PARAM_REGISTRY_PREFIX="${REGISTRY_PREFIX:-}"
# Empty means "leave the third-party images on their upstream registries", the
# supported default. Unlike REGISTRY_PREFIX this has no fallback in common.sh,
# because widening REGISTRY_PREFIX to cover images its mirror was never given is
# exactly the failure third_party_registry_prefix() exists to avoid.
PARAM_THIRD_PARTY_REGISTRY_PREFIX="${THIRD_PARTY_REGISTRY_PREFIX:-}"
# Seeded from GOOGLE_CHAT_ENABLED so Google Chat inherits the way Slack does.
# The chat gate reads SLACK_ENABLED out of the loaded configuration; without
# this seed PARAM_ENABLE_GOOGLE_CHAT would come from the flag alone, and a
# re-run that did not repeat --enable-google-chat would regenerate
# google_chat_enabled = false and plan the Pub/Sub topic and subscription away.
PARAM_ENABLE_GOOGLE_CHAT="${GOOGLE_CHAT_ENABLED:-}"
PARAM_CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-}"
PARAM_GOOGLE_CHAT_MODE="${GOOGLE_CHAT_MODE:-}"
PARAM_MODEL_DEFAULT_NAME="${MODEL_DEFAULT_NAME:-}"
PARAM_USER_PROFILE_ENABLED="${USER_PROFILE_ENABLED:-}"

show_help() {
  cat << EOF
🤖 kube-agents Zero-Friction Installer

Usage:
  ./install.sh [FLAGS]

Flags for AI Agents & Automation:
  -y, --yes, --non-interactive  Run in non-interactive mode (use flags/defaults)
  --dry-run                     Validate prerequisites & output config/plan without creating resources
  --project-id=ID               Target GCP Project ID
  --region=REGION               Target GCP Region (default: install.defaults.env
                                DEFAULT_REGION, currently us-central1)
  --cluster-name=NAME           GKE Cluster Name (default: DEFAULT_CLUSTER_NAME,
                                currently platform-agent-host)
  --cluster-mode=MODE           Shape of a cluster this run creates: autopilot | standard
                                (default: DEFAULT_CLUSTER_MODE, currently autopilot).
                                Autopilot clusters are regional. Passing this flag with
                                autopilot and a zonal --region is an error; leaving it
                                unset at a zonal --region builds Standard instead.
                                Ignored when installing onto a cluster that already
                                exists — its live shape wins.
  --model-provider=PROVIDER     Model provider: gemini | vertex_ai | anthropic | openai
                                (default: DEFAULT_MODEL_PROVIDER, currently gemini)
  --model-default-name=NAME     Default model name for the provider
  --vertex-project-id=ID        GCP project serving Vertex AI models (default: --project-id)
  --vertex-location=LOCATION    Vertex AI serving location, a region or "global"
                                (default: DEFAULT_VERTEX_LOCATION, currently global)
  --vertex-manage-serving-project=BOOL
                                Whether the install enables the Vertex AI API in
                                --vertex-project-id and grants the gateway's service
                                account roles/aiplatform.user there. Pass false when
                                that project is one you cannot administer, and enable
                                the API and make the grant by hand
                                (default: DEFAULT_VERTEX_MANAGE_SERVING_PROJECT, currently true)
  --gemini-api-key=KEY          Gemini API Key
  --openai-api-key=KEY          OpenAI API Key
  --anthropic-api-key=KEY       Anthropic API Key
  --gitops-org=ORG              GitHub Org/Username for GitOps repo
  --gitops-repo=REPO            GitOps IaC Repository Name (default: DEFAULT_GITOPS_REPO,
                                currently gke-fleet-iac)
  --permission-set=SET          Agent GCP IAM permission set: read-only | custom
                                (default: DEFAULT_PERMISSION_SET, currently read-only)
  --custom-roles=ROLES          Roles for --permission-set=custom (space- or comma-separated)
  --gvisor=true|false           Enable GKE Sandbox (gVisor) runtime isolation
                                (default: DEFAULT_ENABLE_GVISOR, currently true)
  --enable-web-ui=true|false    Enable Hermes Web UI port 9119 dashboard
                                (default: DEFAULT_ENABLE_WEBUI, currently false)
  --user-profile-enabled=BOOL   Enable user profile persona extensions
                                (default: DEFAULT_USER_PROFILE_ENABLED,
                                currently false)
  --memory=MODE                 Long-term agent memory: file | hindsight | off
                                (default: DEFAULT_MEMORY, currently file)
                                  file      SMALL / PERSONAL deployments, and the default —
                                            it is what every install got before the searchable
                                            store existed, so an upgrade that says nothing
                                            keeps the store it already has. Per-user Markdown
                                            files inside the pod (multiuser_memory). No extra
                                            services, but the whole store is loaded into the
                                            model's context every turn, so it stops scaling
                                            once there is more than a few pages of it.
                                  hindsight ENTERPRISE deployments. Searchable, ranked recall
                                            that stays affordable as the store grows
                                            (kube_agents_memory). Deploys the Hindsight API
                                            and a Postgres database into the cluster.
                                  off       nothing is retained between sessions. No memory
                                            provider, and no database to run.
  --image-tag=TAG               Validated immutable release tag or full commit SHA
                                (default: this checkout's HEAD; required via curl | bash)
  --registry-prefix=PATH        Container registry path without a URL scheme, for the images
                                this project builds (operator, agent, credential proxy, replay
                                proxy)
  --third-party-registry-prefix=PATH
                                Registry path holding the mirrored third-party images
                                (LiteLLM, fluent-bit, the GitHub token minter, Hindsight).
                                Unset, they stay on their upstream registries --
                                --registry-prefix deliberately does not cover them.
                                See 'make mirror-images'
  --allow-unverified-source     Provision from a dirty or mismatched checkout (local script edits
                                are applied even though the deployed image was built elsewhere)
  --enable-google-chat          Enable Google Chat integration
  --enable-pubsub-platform      Enable Pub/Sub platform adapter AgentPlugin (default: false)
  --enable-stockout-investigator
                                Enable GKE Stockout Investigator AgentPlugin (default: false)
  --allowed-users=EMAILS        Comma-separated user emails allowed to talk to the
                                agent over Google Chat. Empty allows all users
  --chat-topic-name=TOPIC       Pub/Sub topic name for Google Chat
                                (default: DEFAULT_CHAT_TOPIC_NAME,
                                currently platform-agent-chat-events)
  --google-chat-mode=MODE       Google Chat output mode: default | debug
                                (default: DEFAULT_GOOGLE_CHAT_MODE, currently default)
  --menu, --config              Launch interactive Day-2 Control Panel Menu (raspi-config style)
  -h, --help, -?                Show this help message

Configuration file:
  install.env beside this script (override with KUBE_AGENTS_INSTALL_ENV) is
  loaded first, and a flag beats it. It is sourced with 'set -a', so a key it
  carries also beats an exported variable of the same name -- a flag is what
  overrides a recorded value for one run. Start from install.env.example.
  Anything it sets is inherited by later runs, so a re-run that omits a flag
  keeps the value rather than reverting it to the default above.
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --menu|--config|--configure|menu|config) PARAM_MENU_MODE="true"; shift ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-mode=*) PARAM_CLUSTER_MODE="${1#*=}"; shift ;;
      --model-provider=*) PARAM_MODEL_PROVIDER="${1#*=}"; shift ;;
      --model-default-name=*) PARAM_MODEL_DEFAULT_NAME="${1#*=}"; shift ;;
      --vertex-project-id=*) PARAM_VERTEX_PROJECT_ID="${1#*=}"; shift ;;
      --vertex-location=*) PARAM_VERTEX_LOCATION="${1#*=}"; shift ;;
      --vertex-manage-serving-project=*) PARAM_VERTEX_MANAGE_SERVING_PROJECT="${1#*=}"; shift ;;
      --gemini-api-key=*) PARAM_GEMINI_API_KEY="${1#*=}"; shift ;;
      --openai-api-key=*) PARAM_OPENAI_API_KEY="${1#*=}"; shift ;;
      --anthropic-api-key=*) PARAM_ANTHROPIC_API_KEY="${1#*=}"; shift ;;
      --gitops-org=*) PARAM_GITOPS_ORG="${1#*=}"; shift ;;
      --gitops-repo=*) PARAM_GITOPS_REPO="${1#*=}"; shift ;;
      --permission-set=*) PARAM_PERMISSION_SET="${1#*=}"; shift ;;
      --custom-roles=*) PARAM_CUSTOM_ROLES="${1#*=}"; shift ;;
      --gvisor=*) PARAM_ENABLE_GVISOR="${1#*=}"; shift ;;
      --enable-web-ui=*|--enable-webui=*|--webui=*) PARAM_ENABLE_WEBUI="${1#*=}"; shift ;;
      --enable-web-ui|--enable-webui|--webui) PARAM_ENABLE_WEBUI="true"; shift ;;
      --user-profile-enabled=*) PARAM_USER_PROFILE_ENABLED="${1#*=}"; shift ;;
      --enable-pubsub-platform=*|--enable-pubsub=*) PARAM_ENABLE_PUBSUB_PLATFORM="${1#*=}"; shift ;;
      --enable-pubsub-platform|--enable-pubsub) PARAM_ENABLE_PUBSUB_PLATFORM="true"; shift ;;
      --enable-stockout-investigator=*|--enable-stockout=*) PARAM_ENABLE_STOCKOUT_INVESTIGATOR="${1#*=}"; shift ;;
      --enable-stockout-investigator|--enable-stockout) PARAM_ENABLE_STOCKOUT_INVESTIGATOR="true"; shift ;;
      --memory=*) PARAM_MEMORY="${1#*=}"; shift ;;
      --image-tag=*) PARAM_IMAGE_TAG="${1#*=}"; shift ;;
      --registry-prefix=*) PARAM_REGISTRY_PREFIX="${1#*=}"; shift ;;
      --third-party-registry-prefix=*) PARAM_THIRD_PARTY_REGISTRY_PREFIX="${1#*=}"; shift ;;
      --allow-unverified-source|--allow-dirty) PARAM_ALLOW_UNVERIFIED_SOURCE="true"; shift ;;
      --enable-google-chat|--google-chat) PARAM_ENABLE_GOOGLE_CHAT="true"; shift ;;
      --allowed-users=*) PARAM_ALLOWED_USERS="${1#*=}"; shift ;;
      --chat-topic-name=*) PARAM_CHAT_TOPIC_NAME="${1#*=}"; shift ;;
      --google-chat-mode=*) PARAM_GOOGLE_CHAT_MODE="${1#*=}"; shift ;;
      -h|--help|-\?|help) show_help; exit 0 ;;
      *) print_error "Unknown parameter: $1"; show_help >&2; return 2 ;;
    esac
  done
}

get_term_width() {
  local cols
  cols=$(tput cols 2>/dev/null || echo 80)
  if ! [[ "$cols" =~ ^[0-9]+$ ]] || [ "$cols" -lt 40 ]; then
    cols=80
  fi
  echo "$cols"
}

draw_separator() {
  local width
  width=$(get_term_width)
  if [ "$width" -gt 75 ]; then
    width=75
  fi
  printf '%*s' "$width" '' | tr ' ' '='
  printf '\n'
}

print_banner() {
  local term_w
  term_w=$(get_term_width)

  printf '%b\n' "${C_CYAN}${C_BOLD}"
  draw_separator

  if [ "$term_w" -ge 60 ]; then
    cat << "EOF"
    __ ____  ______  ______     ___   _____________   _____________
   / //_/ / / / __ )/ ____/    /   | / ____/ ____/ | / /_  __/ ___/
  / ,< / / / / __  / __/______/ /| |/ / __/ __/ /  |/ / / /  \__ \
 / /| / /_/ / /_/ / /__/_____/ ___ / /_/ / /___/ /|  / / /  ___/ /
/_/ |_\____/_____/_____/    /_/  |_\____/_____/_/ |_/ /_/  /____/
EOF
  else
    printf '%b\n' "🤖 KUBE-AGENTS PLATFORM HARNESS"
  fi

  printf '\n%b\n' "🤖 Kubernetes Agentic Harness (kube-agents) Zero-Friction Installer"
  draw_separator
  printf '%b\n\n' "${C_RESET}"
}

# Minimum tool versions, kept in scripts/installer/min_versions.sh so the
# numbers live in exactly one place. This installer is also downloaded and run
# on its own, before any checkout exists, so the source is guarded: in that
# case the workspace step clones the repository and the check runs against the
# clone's copy.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
_min_versions="${_script_dir}/scripts/installer/min_versions.sh"
if [ -r "$_min_versions" ]; then
  # CI runs shellcheck without -x, so the source= hint alone still raises
  # SC1091 for a file it was not handed as input.
  # shellcheck source=scripts/installer/min_versions.sh disable=SC1091
  source "$_min_versions"
else
  require_min_gcloud_version() { return 0; }
  require_min_terraform_version() { return 0; }
fi
unset _min_versions

validate_immutable_ref() {
  local ref="${1:-}"
  if [ -z "$ref" ]; then
    print_error "An immutable image/source ref is required. Pass --image-tag with a validated release tag or full commit SHA."
    return 1
  fi
  case "$ref" in
    latest|main|master|HEAD)
      print_error "Mutable image/source ref '$ref' is not supported. Use a validated release tag or full commit SHA."
      return 1
      ;;
  esac
  if [[ ! "$ref" =~ ^[0-9a-fA-F]{40}$ ]] \
    && [[ ! "$ref" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    print_error "Image/source ref must be a full 40-character commit SHA or a pure numeric SemVer release tag (X.Y.Z, e.g. 0.1.0)."
    return 1
  fi
}

# Resolves the shape a run that CREATES a cluster will build, given what the
# caller asked for ($1, may be empty) and the location ($2). Echoes the mode
# and nothing else, so the caller can compare and explain.
#
# A function rather than an inline `:-` because this one line is what a bare
# ./install.sh actually builds, and the inline form was untestable: install.sh
# exports CLUSTER_MODE before the generator ever reads it, so
# installer_common.sh's own fallback never decides anything for this front
# door, and a test of that fallback proves nothing about this.
resolve_creatable_cluster_mode() {
  local requested="${1:-}" location="${2:-}"
  if [ -n "$requested" ]; then
    echo "$requested"
    return 0
  fi
  # A defaulted Autopilot steps aside at a zonal location rather than failing:
  # nobody asked for Autopilot here, and the alternative is an abort blaming
  # --region for a shape the installer chose itself. An explicit
  # --cluster-mode=autopilot still fails in require_creatable_cluster_mode —
  # that request is impossible, not merely inconvenient.
  if [ "${DEFAULT_CLUSTER_MODE}" = "autopilot" ] && ! location_is_region "$location"; then
    echo "standard"
    return 0
  fi
  echo "${DEFAULT_CLUSTER_MODE}"
}

# A cluster shape this install can create in this location. is_valid_cluster_mode
# comes from installer_common.sh, so this runs after the workspace step.
#
# The region rule is the gke-cluster module's Autopilot precondition, checked
# here as well because reaching it costs the whole interview first: a location
# that only turns out to be wrong at terraform validate has already collected
# every API key and integration answer.
require_creatable_cluster_mode() {
  local mode="${1:-}" location="${2:-}"
  if ! is_valid_cluster_mode "$mode"; then
    print_error "--cluster-mode must be either autopilot or standard (got '${mode}')."
    exit 1
  fi
  if [ "$mode" = "autopilot" ] && ! location_is_region "$location"; then
    print_error "GKE Autopilot clusters are regional: --region must be a region such as us-central1, not '${location}'."
    print_info "For a zonal cluster, pass --cluster-mode=standard."
    exit 1
  fi
}

# How GKE writes the shape. bash 3.2, still macOS's /bin/bash, has no ${var^}.
cluster_mode_label() {
  case "${1:-}" in
    autopilot) echo "Autopilot" ;;
    *) echo "Standard" ;;
  esac
}

# The image tag doubles as the source ref that verify_local_source_ref checks the
# checkout against. When downloaded as an official release via curl | bash, the baked
# release tag takes precedence. In local Git checkouts, an exact SemVer release tag or
# HEAD commit SHA is used as the default.
default_image_tag() {
  local repo_dir="${1:-.}"
  # 1. Baked release version takes precedence (for curl | bash from official release URLs)
  if [ -n "${BAKED_RELEASE_VERSION:-}" ]; then
    echo "$BAKED_RELEASE_VERSION"
    return 0
  fi
  # Only a kube-agents checkout may supply the default. Without this guard,
  # running the curl | bash one-liner from inside any unrelated Git repository
  # would offer that repository's HEAD, which then fails at `git fetch` for a
  # ref the kube-agents clone has never heard of.
  if [ ! -f "${repo_dir}/scripts/installer/installer_common.sh" ]; then
    return 0
  fi
  # 2. Check if local git repo is checked out at an exact SemVer release tag
  local exact_tag=""
  exact_tag="$(git -C "$repo_dir" describe --tags --exact-match --match="[0-9]*" 2>/dev/null || echo "")"
  if [[ "$exact_tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    echo "$exact_tag"
    return 0
  fi
  # 3. Check if running inside an unpacked release archive directory (e.g. kube-agents-0.1.0 or kube-agents-0.2.0)
  local base_dir=""
  base_dir="$(basename "$(cd "$repo_dir" 2>/dev/null && pwd || echo "$repo_dir")")"
  if [[ "$base_dir" =~ ^kube-agents-([0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?)$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  # 4. Fall back to local HEAD commit SHA for developer iterations
  git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo ""
}

# How that default is shown in a prompt: the full SHA is unreadable, so abbreviate
# it the way git does and say where it came from. Empty outside a Git worktree.
default_image_tag_label() {
  local repo_dir="${1:-.}"
  local tag
  tag="$(default_image_tag "$repo_dir")"
  if [ -z "$tag" ]; then
    return 0
  fi

  if [ -n "${BAKED_RELEASE_VERSION:-}" ] && [ "$tag" = "$BAKED_RELEASE_VERSION" ]; then
    printf 'official release %s' "$tag"
  elif [ "$tag" = "$(git -C "$repo_dir" describe --tags --exact-match --match="[0-9]*" 2>/dev/null || echo "")" ]; then
    printf 'release tag %s' "$tag"
  elif [[ "$(basename "$(cd "$repo_dir" 2>/dev/null && pwd || echo "$repo_dir")")" =~ ^kube-agents-${tag}$ ]]; then
    printf 'release archive %s' "$tag"
  else
    printf 'local HEAD checkout %s' "${tag:0:7}"
  fi
}

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

write_env_var() {
  local destination="$1"
  local var_name="$2"
  local var_value="$3"
  # No `export`: install.env is a conventional dotenv, and install.sh loads it
  # with `set -a` so the keyword would be redundant. %q still does the quoting,
  # so a value with spaces or a quote survives the round trip.
  printf '%s=%q\n' "$var_name" "$var_value" >> "$destination"
}

# Credentials follow PERSIST_SECRETS_ON_DISK: false keeps them out of every
# file the installer writes. They still travel to Terraform for this run as
# TF_VAR_*, and later runs recover them from the live 'platform-agent-secrets'
# Secret (see write_tfvars_from_state).
write_secret_env_var() {
  local destination="$1"
  local var_name="$2"
  local var_value="$3"
  if [ -z "$var_value" ]; then
    return 0
  fi
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-$DEFAULT_PERSIST_SECRETS_ON_DISK}"; then
    write_env_var "$destination" "$var_name" "$var_value"
  fi
}

# Create install.env from what this run resolved, but ONLY when there is no
# file there. An operator who hand-authored one owns it: rewriting it would
# discard their comments and their formatting, and re-introduce the exact
# complaint against vars.sh -- a file the documentation tells you to edit and
# the next run overwrites.
#
# Called after write_tfvars_from_state so the API_SERVER_KEY it records is the
# one the install settled on: recovered from a live Secret when there was one,
# freshly minted only when there was not.
#
# Derived values are left out by construction. PROJECT_NUMBER and KMS_LOCATION
# are recomputed wherever they are used, and the cluster shape written here is
# the one the interview asked for, never the probed TFVARS_CLUSTER_MODE, which
# write_tfvars_from_state re-derives on every run.
# The value install.env records for one key, empty when it records none.
# Reads the file rather than the environment: install.env was sourced at
# startup into these very names, and the interview has since overwritten them,
# so the environment no longer remembers what the file said.
#
# Always returns 0. A `return 1` for "no such key" would be the natural
# signature and is the wrong one here: this is called from a command
# substitution, `set -E` propagates the ERR trap into that subshell, and the
# trap fires on the non-zero return before the caller's `||` is ever consulted
# -- printing an abort banner per absent key. Callers test presence separately.
#
# The value is unquoted the way sourcing the file would unquote it, because
# both things that write install.env quote it. write_env_var here and
# save_env_var in scripts/installer/installer_common.sh both serialise with
# `printf '%s=%q\n'`, and %q renders the empty string as the two-character
# literal '' and escapes anything the shell would treat specially -- so
# `#gke-alerts` is written `\#gke-alerts`. Comparing a quoted recorded value
# against an unquoted environment one reports every empty key as drifted, and
# the line the banner prints for each (`KEY=`) changes nothing, so the next run
# reports them again. That buries the
# one case the warning exists for.
#
# A hand-authored file is the other half of the same problem and the reason
# this cannot simply re-quote the current value and compare the quoted forms:
# an operator writes `SLACK_HOME_CHANNEL="#gke-alerts"`, which %q would render
# `\#gke-alerts`, and the two spellings of one value would not match.
unquote_shell_value() {
  local raw="${1:-}"
  case "$raw" in
    # Single quotes are literal all the way through, which is also how %q
    # spells the empty string.
    "'"*"'")
      raw="${raw#\'}"
      printf '%s' "${raw%\'}"
      return 0
      ;;
    '"'*'"')
      raw="${raw#\"}"
      raw="${raw%\"}"
      ;;
  esac
  # Outside single quotes a backslash escapes the next character. That is how
  # %q writes '#', a space, and every other metacharacter.
  printf '%s' "$raw" | sed 's/\\\(.\)/\1/g'
}

recorded_install_env_value() {
  local file="${1:-}" key="${2:-}" line=""
  [ -n "$file" ] && [ -f "$file" ] || return 0
  # install.env.example tells the operator `export K=V` is harmless, and every
  # other reader of the file honours that: save_env_var, live_test_lease.py and
  # project_config.py all skip an optional `export`. Matching it here keeps this
  # reader in step -- missing the prefix skips the key silently, and in the
  # direction of no warning at all.
  # The `${line#*=}` below strips through the first `=`, so the longer prefix
  # needs nothing further.
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null | tail -1 || true)"
  [ -n "$line" ] || return 0
  line="${line#*=}"
  unquote_shell_value "$line"
}

# Say so when an interactive answer changed something the file still records
# differently.
#
# install.env is an input: install.sh creates one when there is none and never
# rewrites it. The interview, though, still runs in full on every interactive
# invocation, and its answers go straight into the environment
# write_tfvars_from_state reads. So an operator who answers "None" at the chat
# menu gets the Pub/Sub topic destroyed by this apply and re-created by the
# next run, because the file still says the integration is on. Nothing else
# tells them: "Left your install configuration as you wrote it" reads as
# reassurance, and the Day-2 menu's Save & Apply is the only path that writes a
# key back.
#
# A warning rather than a write, deliberately. Making install.sh persist here
# would contradict the contract stated in install.env.example, INSTALL.md and
# the chart's CI story -- #1117 renders this file on an ephemeral runner and
# needs the installer to treat it as read-only. Naming the drift costs nothing
# and leaves the decision where it belongs.
#
# The list is the interview's own settings, not everything the file holds:
# IMAGE_TAG and the recovered secrets legitimately differ on a normal run and
# would make this noise. Keep it in step with the interview.
warn_unrecorded_interview_answers() {
  local file="${1:-}"
  [ -n "$file" ] && [ -f "$file" ] || return 0
  # A non-interactive run typed nothing; its answers came from flags and this
  # very file, so there is no drift to report that the operator did not author.
  [ "$PARAM_NON_INTERACTIVE" != "true" ] || return 0
  has_controlling_tty || return 0
  [ "$PARAM_DRY_RUN" != "true" ] || return 0

  # A key the file does not carry is not drift: it inherits the default, and
  # warning about every unset key would bury the ones that matter.
  #
  # Two rules decide what belongs in this list, and getting either wrong makes
  # an entry inert rather than loud:
  #
  #   1. The setting must have an interview question. ENABLE_GKE_BACKUP_PLAN and
  #      GVISOR_POOL_NAME are deliberately kept out of the export block below
  #      precisely because nothing asks about them, so they cannot drift and
  #      listing them here would only ever compare a value against itself.
  #   2. The answer must be readable under the key's own name. Most of the
  #      interview re-exports into exactly that name, but MEMORY does not: its
  #      answer lands in PARAM_MEMORY and in MEMORY_PROVIDER, and MEMORY itself
  #      still holds whatever install.env set at startup. Comparing `$MEMORY`
  #      would therefore always find them equal and never report the one case
  #      that matters most -- switching to Hindsight, getting it provisioned,
  #      and having the next run derive multiuser_memory from the unchanged file
  #      and tear the Hindsight API and its Postgres back down.
  local key recorded current drifted=""
  for key in GOOGLE_CHAT_ENABLED SLACK_ENABLED ALLOWED_USERS SLACK_ALLOWED_USERS \
    SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_HOME_CHANNEL SLACK_HOME_CHANNEL_NAME \
    CHAT_TOPIC_NAME MODEL_PROVIDER MODEL_DEFAULT_NAME PLATFORM_AGENT_PERMISSION_SET \
    PLATFORM_AGENT_CUSTOM_ROLES ENABLE_GVISOR HERMES_DASHBOARD_ENABLED MEMORY \
    USER_PROFILE_ENABLED GITOPS_ORG GITOPS_REPO GITHUB_APP_ID GITHUB_PEM_PATH; do
    grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}=" "$file" 2>/dev/null || continue
    recorded="$(recorded_install_env_value "$file" "$key")"
    case "$key" in
      MEMORY) current="${PARAM_MEMORY:-}" ;;
      *) current="${!key:-}" ;;
    esac
    [ "$recorded" != "$current" ] || continue
    drifted="${drifted}${drifted:+ }${key}"
  done
  [ -n "$drifted" ] || return 0

  print_warning "This run applied answers that ${file} does not record."
  print_info "install.env is an input: install.sh reads it and never rewrites it."
  print_info "The next run -- or upgrade.sh, or the Day-2 menu -- regenerates from"
  print_info "the file, which will revert what you just changed. Update these keys:"
  for key in $drifted; do
    case "$key" in
      *TOKEN | *_KEY | *SECRET)
        print_info "  ${key}=<the value you entered>"
        ;;
      # Same indirection as the comparison above, for the same reason: printing
      # ${MEMORY} here would hand the operator the value they just changed away
      # from, which is worse than printing nothing.
      MEMORY)
        print_info "  MEMORY=${PARAM_MEMORY:-}"
        ;;
      *)
        print_info "  ${key}=${!key:-}"
        ;;
    esac
  done
  print_info "Or re-run './install.sh --menu' and use Save & Apply, which writes them for you."
}

bootstrap_install_env_file() {
  local destination="${1:-}" image_tag="${2:-}"
  [ -n "$destination" ] || return 0
  if [ -f "$destination" ]; then
    print_info "Left your install configuration as you wrote it: ${destination}"
    warn_unrecorded_interview_answers "$destination"
    return 0
  fi
  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_info "Dry-run: not creating ${destination}."
    return 0
  fi

  local old_umask
  old_umask="$(umask)"
  umask 077
  local tmp="${destination}.tmp"
  {
    printf '%s\n' "# kube-agents install configuration, created by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)."
    printf '%s\n' "# This file is yours now: install.sh reads it and never rewrites it."
    printf '%s\n' "# Edit it and re-run the installer to change the install."
    printf '%s\n' "# See install.env.example for every supported key and what it does."
    printf '%s\n' "#"
    printf '%s\n' "# Installed at image tag ${image_tag}. IMAGE_TAG is deliberately absent:"
    printf '%s\n' "# it is chosen per run with --image-tag, not inherited."
    printf '\n'
  } > "$tmp"
  write_env_var "$tmp" PROJECT_ID "${PROJECT_ID:-}"
  write_env_var "$tmp" CLUSTER_NAME "${CLUSTER_NAME:-}"
  write_env_var "$tmp" REGION "${REGION:-}"
  write_env_var "$tmp" CLUSTER_MODE "${CLUSTER_MODE:-}"
  write_env_var "$tmp" MODEL_PROVIDER "${MODEL_PROVIDER:-}"
  write_env_var "$tmp" MODEL_DEFAULT_NAME "${MODEL_DEFAULT_NAME:-}"
  write_env_var "$tmp" VERTEX_PROJECT_ID "${VERTEX_PROJECT_ID:-}"
  write_env_var "$tmp" VERTEX_LOCATION "${VERTEX_LOCATION:-}"
  write_env_var "$tmp" VERTEX_MANAGE_SERVING_PROJECT "${VERTEX_MANAGE_SERVING_PROJECT:-}"
  write_secret_env_var "$tmp" GEMINI_API_KEY "${GEMINI_API_KEY:-}"
  write_secret_env_var "$tmp" OPENAI_API_KEY "${OPENAI_API_KEY:-}"
  write_secret_env_var "$tmp" ANTHROPIC_API_KEY "${ANTHROPIC_API_KEY:-}"
  write_env_var "$tmp" ALLOWED_USERS "${ALLOWED_USERS:-}"
  write_env_var "$tmp" CHAT_TOPIC_NAME "${CHAT_TOPIC_NAME:-}"
  write_env_var "$tmp" CHAT_SUB_NAME "${CHAT_SUB_NAME:-}"
  write_env_var "$tmp" GOOGLE_CHAT_ENABLED "${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}"
  write_env_var "$tmp" GOOGLE_CHAT_MODE "${GOOGLE_CHAT_MODE:-$DEFAULT_GOOGLE_CHAT_MODE}"
  write_env_var "$tmp" SLACK_ENABLED "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"
  write_secret_env_var "$tmp" SLACK_BOT_TOKEN "${SLACK_BOT_TOKEN:-}"
  write_secret_env_var "$tmp" SLACK_APP_TOKEN "${SLACK_APP_TOKEN:-}"
  write_env_var "$tmp" SLACK_ALLOWED_USERS "${SLACK_ALLOWED_USERS:-}"
  write_env_var "$tmp" SLACK_HOME_CHANNEL "${SLACK_HOME_CHANNEL:-}"
  write_env_var "$tmp" SLACK_HOME_CHANNEL_NAME "${SLACK_HOME_CHANNEL_NAME:-}"
  write_secret_env_var "$tmp" API_SERVER_KEY "${API_SERVER_KEY:-}"
  write_env_var "$tmp" PLATFORM_AGENT_PERMISSION_SET "${PLATFORM_AGENT_PERMISSION_SET:-$DEFAULT_PERMISSION_SET}"
  if [ "${PLATFORM_AGENT_PERMISSION_SET:-}" = "custom" ]; then
    write_env_var "$tmp" PLATFORM_AGENT_CUSTOM_ROLES "${PLATFORM_AGENT_CUSTOM_ROLES:-}"
  fi
  write_env_var "$tmp" GITOPS_ORG "${GITOPS_ORG:-}"
  write_env_var "$tmp" GITOPS_REPO "${GITOPS_REPO:-}"
  write_env_var "$tmp" GITHUB_APP_ID "${GITHUB_APP_ID:-}"
  write_env_var "$tmp" KMS_KEYRING "${KMS_KEYRING:-}"
  write_env_var "$tmp" KMS_KEY "${KMS_KEY:-}"
  write_env_var "$tmp" GITHUB_PEM_PATH "${GITHUB_PEM_PATH:-}"
  write_env_var "$tmp" MEMORY "$PARAM_MEMORY"
  write_env_var "$tmp" USER_PROFILE_ENABLED "${USER_PROFILE_ENABLED:-$DEFAULT_USER_PROFILE_ENABLED}"
  write_env_var "$tmp" HERMES_DASHBOARD_ENABLED "${HERMES_DASHBOARD_ENABLED:-$DEFAULT_ENABLE_WEBUI}"
  write_env_var "$tmp" ENABLE_GVISOR "${ENABLE_GVISOR:-$DEFAULT_ENABLE_GVISOR}"
  write_env_var "$tmp" ENABLE_GKE_BACKUP_PLAN "${ENABLE_GKE_BACKUP_PLAN:-$DEFAULT_ENABLE_GKE_BACKUP_PLAN}"
  write_env_var "$tmp" ENABLE_PUBSUB_PLATFORM "${PARAM_ENABLE_PUBSUB_PLATFORM:-false}"
  write_env_var "$tmp" ENABLE_STOCKOUT_INVESTIGATOR "${PARAM_ENABLE_STOCKOUT_INVESTIGATOR:-false}"
  
  write_env_var "$tmp" REGISTRY_PREFIX "${REGISTRY_PREFIX:-}"
  if [ -n "${THIRD_PARTY_REGISTRY_PREFIX:-}" ]; then
    write_env_var "$tmp" THIRD_PARTY_REGISTRY_PREFIX "${THIRD_PARTY_REGISTRY_PREFIX}"
  fi
  if ! is_truthy "${PERSIST_SECRETS_ON_DISK:-$DEFAULT_PERSIST_SECRETS_ON_DISK}"; then
    printf '\n%s\n' "# PERSIST_SECRETS_ON_DISK=false: credentials are deliberately absent." >> "$tmp"
    write_env_var "$tmp" PERSIST_SECRETS_ON_DISK "false"
  fi
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$destination"
  umask "$old_umask"
  print_success "Wrote your install configuration to: ${destination}"
  print_info "Edit that file and re-run install.sh to change this install. It is never overwritten."
}

matches_release_bundle_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  local bundle_file="${repo_dir}/.release-bundle"

  if [ -f "$bundle_file" ]; then
    local bundle_version bundle_tag
    bundle_version="$(grep -E "^version=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    bundle_tag="$(grep -E "^tag=" "$bundle_file" 2>/dev/null | cut -d'=' -f2- | tr -d '[:space:]' || echo "")"
    if [ -n "$bundle_version" ] && { [ "$bundle_version" = "$expected_ref" ] || [ "$bundle_tag" = "$expected_ref" ]; }; then
      echo "$bundle_version"
      return 0
    fi
  fi
  return 1
}

verify_local_source_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  # The installer runs scripts/installer/* from this checkout while deploying
  # the container image built from $expected_ref, so a mismatch means the cluster
  # gets manifests from one revision and an agent runtime from another. --dry-run
  # touches nothing, and --allow-unverified-source is the explicit opt-out for
  # developing against locally modified scripts; both downgrade this to a warning.
  local lenient="false"
  local unverified="false"
  if [ "$PARAM_DRY_RUN" = "true" ] || [ "$PARAM_ALLOW_UNVERIFIED_SOURCE" = "true" ]; then
    lenient="true"
  fi
  if [ "$SOURCE_REF_VERIFIED" = "${repo_dir}@${expected_ref}" ]; then
    return 0
  fi

  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # In official stamped release archives (unpacked tarball/zip outside Git),
    # BAKED_RELEASE_VERSION is stamped during release automation.
    if [ -n "${BAKED_RELEASE_VERSION:-}" ] && [ "${BAKED_RELEASE_VERSION}" = "${expected_ref}" ]; then
      local bundle_version=""
      if bundle_version="$(matches_release_bundle_ref "$repo_dir" "$expected_ref")"; then
        SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
        print_success "Verified install sources match official release bundle ${bundle_version}."
        return 0
      fi
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      print_success "Verified install sources match baked official release ${BAKED_RELEASE_VERSION}."
      return 0
    fi
    if [ "$lenient" = "true" ]; then
      print_warning "Cannot verify source/image alignment because '$repo_dir' is not a Git worktree."
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      return 0
    fi
    print_error "Refusing to provision from an unversioned source directory: $repo_dir"
    print_info "Pass --allow-unverified-source to provision anyway."
    return 1
  fi

  local expected_commit current_commit
  if ! expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)"; then
    if [ "$lenient" = "true" ]; then
      print_warning "Cannot verify source/image alignment: ref '$expected_ref' is not present in this checkout."
      SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
      return 0
    fi
    print_error "The requested image/source ref '$expected_ref' is not present in the current checkout. Check out that exact revision first."
    print_info "Pass --allow-unverified-source to provision anyway."
    return 1
  fi
  current_commit="$(git -C "$repo_dir" rev-parse HEAD)"
  if [ "$current_commit" != "$expected_commit" ]; then
    if [ "$lenient" = "true" ]; then
      unverified="true"
      print_warning "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
    else
      print_error "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
      print_info "Pass --allow-unverified-source to provision anyway."
      return 1
    fi
  fi

  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    if [ "$lenient" = "true" ]; then
      unverified="true"
      print_warning "Provisioning scripts have uncommitted changes; they do not match '$expected_ref'."
    else
      print_error "Refusing to provision from a dirty checkout because its sources do not exactly match '$expected_ref'."
      print_info "Pass --allow-unverified-source to provision anyway, or stash the changes first."
      return 1
    fi
  fi

  SOURCE_REF_VERIFIED="${repo_dir}@${expected_ref}"
  if [ "$unverified" = "true" ]; then
    print_warning "Continuing with unverified install sources: the cluster will get this checkout's configuration plus the image built from ${expected_ref}."
    return 0
  fi
  print_success "Verified install sources and image ref resolve to commit ${expected_commit}."
}

# Put the install sources on disk and return the directory holding them.
# Runs before the interview so a bad source ref or a dirty tree fails immediately,
# and so installer_common.sh — which owns every installer default — can be sourced.
acquire_source_repo() {
  # Stores the directory in the variable named by $1 rather than echoing it: the
  # progress lines below would otherwise be captured along with the path.
  local dest_var="$1"
  local expected_ref="$2"
  local resolved_dir=""
  local script_dir=""
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || echo "")"
  if [ -n "$script_dir" ] && [ -f "${script_dir}/scripts/installer/installer_common.sh" ]; then
    resolved_dir="$script_dir"
    print_success "Using repository directory: $resolved_dir"
  elif [ -f "scripts/installer/installer_common.sh" ]; then
    resolved_dir="$(pwd)"
    print_success "Using current repository directory: $resolved_dir"
  else
    resolved_dir="$HOME/kube-agents"
    if [ -d "$resolved_dir" ]; then
      print_info "Using existing repository at $resolved_dir without modifying local changes."
    else
      print_info "Cloning kube-agents install sources at '$expected_ref' into $resolved_dir..."
      git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$resolved_dir"
      if [[ "$expected_ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
        git -C "$resolved_dir" fetch --depth=1 https://github.com/gke-labs/kube-agents.git "$expected_ref"
      else
        git -C "$resolved_dir" fetch --depth=1 https://github.com/gke-labs/kube-agents.git "+refs/tags/${expected_ref}:refs/tags/${expected_ref}"
      fi
      git -C "$resolved_dir" checkout --detach FETCH_HEAD
    fi
    cd "$resolved_dir"
  fi
  verify_local_source_ref "$resolved_dir" "$expected_ref"
  printf -v "$dest_var" '%s' "$resolved_dir"
}

# scripts/installer/installer_common.sh is the source of truth for install
# defaults, validation rules, and the terraform.tfvars generator. The installer
# sources it rather than keeping its own copies, which is how the two drifted
# apart before (an installer menu whose permission-set default disagreed with
# the provisioner's, a us-central1 default against us-east4, a second copy of
# derive_kms_location).
source_provisioning_helpers() {
  local repo_dir="$1"
  local helper_script="${repo_dir}/scripts/installer/installer_common.sh"
  if [ ! -f "$helper_script" ]; then
    print_error "Cannot find installer helpers at $helper_script."
    exit 1
  fi
  SCRIPT_DIR="${repo_dir}/scripts/installer"
  # The legacy state file, still at its original address: an install made
  # before the move has one there and nowhere else.
  VARS_FILE="${repo_dir}/k8s-operator/scripts/vars.sh"
  # shellcheck source=/dev/null
  source "$helper_script"
  # gke_dns_endpoint_flag, for the credentials fetch before the health checks.
  # shellcheck source=/dev/null
  source "${SCRIPT_DIR}/gke_dns_endpoint.sh"
  print_success "Loaded installer defaults from scripts/installer/installer_common.sh"
}

# Fill in the parameters whose default lives in installer_common.sh. Called
# once, after sourcing, so a flag, an environment variable or an install.env
# value still wins over the shared default.
#
# Every default this installer applies goes through here. The alternative --
# `${PARAM_X:-false}` at each point of use -- is a second copy of the default
# living next to the code that reads it, and two copies drift. It also reads
# as though the value might legitimately be unset at that point, which it
# cannot be: this runs in step 2, before the interview.
resolve_shared_defaults() {
  normalize_gitops_repo_vars
  PARAM_MODEL_PROVIDER="${PARAM_MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}"
  PARAM_REGISTRY_PREFIX="${PARAM_REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}"
  PARAM_PERMISSION_SET="${PARAM_PERMISSION_SET:-$DEFAULT_PERMISSION_SET}"
  # ${VAR-...}, not ${VAR:-...}: an explicit `--gvisor=` sets it to empty, and
  # that has to survive to the validator rather than being read as the default.
  PARAM_ENABLE_GVISOR="${PARAM_ENABLE_GVISOR-$DEFAULT_ENABLE_GVISOR}"
  PARAM_ENABLE_WEBUI="${PARAM_ENABLE_WEBUI:-$DEFAULT_ENABLE_WEBUI}"
  PARAM_USER_PROFILE_ENABLED="${PARAM_USER_PROFILE_ENABLED:-$DEFAULT_USER_PROFILE_ENABLED}"
  PARAM_MEMORY="${PARAM_MEMORY:-$DEFAULT_MEMORY}"
  PARAM_ENABLE_GOOGLE_CHAT="${PARAM_ENABLE_GOOGLE_CHAT:-$DEFAULT_GOOGLE_CHAT_ENABLED}"
  PARAM_GOOGLE_CHAT_MODE="${PARAM_GOOGLE_CHAT_MODE:-$DEFAULT_GOOGLE_CHAT_MODE}"
  PARAM_CHAT_TOPIC_NAME="${PARAM_CHAT_TOPIC_NAME:-$DEFAULT_CHAT_TOPIC_NAME}"
  PARAM_GITOPS_REPO="${PARAM_GITOPS_REPO:-$DEFAULT_GITOPS_REPO}"
}

# Wait for one deployment to roll out, animating a spinner with the elapsed time
# and kubectl's own latest progress line. Falls back to plain streaming output
# when stdout is not a terminal (CI, piped logs). Returns kubectl's exit status.
wait_for_rollout() {
  local deployment="$1"
  local namespace="$2"
  local timeout_secs="$3"

  if [ ! -t 1 ]; then
    kubectl rollout status "deployment/${deployment}" -n "$namespace" --timeout="${timeout_secs}s"
    return $?
  fi

  local log_file=""
  log_file="$(mktemp -t kube-agents-rollout.XXXXXX)"
  kubectl rollout status "deployment/${deployment}" -n "$namespace" --timeout="${timeout_secs}s" \
    >"$log_file" 2>&1 &
  local kubectl_pid=$!

  local frames=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
  local frame=0
  local started=$SECONDS
  local status_line=""
  local term_width=0
  term_width="$(get_term_width)"
  # Everything except the kubectl line: two spaces, spinner, name, "(NNNs)",
  # separators. Keep one column spare so the line never wraps — a wrapped line
  # cannot be rewritten with \r and would scroll the spinner down the screen.
  local status_width=$((term_width - ${#deployment} - 15))
  if [ "$status_width" -lt 10 ]; then
    status_width=10
  fi
  tput civis 2>/dev/null || true
  while kill -0 "$kubectl_pid" 2>/dev/null; do
    status_line="$(tail -n 1 "$log_file" 2>/dev/null | tr -d '\r' | cut -c1-"$status_width")"
    printf '\r  %b%s%b %s %b(%ss)%b %-*s' \
      "$C_CYAN" "${frames[$((frame % 10))]}" "$C_RESET" "$deployment" \
      "$C_YELLOW" "$((SECONDS - started))" "$C_RESET" "$status_width" "$status_line"
    frame=$((frame + 1))
    sleep 0.2
  done
  tput cnorm 2>/dev/null || true
  printf '\r%*s\r' "$term_width" ''

  local rc=0
  wait "$kubectl_pid" || rc=$?
  if [ "$rc" -eq 0 ]; then
    print_success "$deployment rolled out in $((SECONDS - started))s"
  else
    tail -n 3 "$log_file" | tr -d '\r' | while IFS= read -r line; do
      [ -n "$line" ] && print_info "$line"
    done
  fi
  rm -f -- "$log_file"
  return "$rc"
}

# Wait for a Deployment object to exist, ahead of waiting for it to roll out.
# `kubectl rollout status` on a Deployment that is not there yet fails
# immediately rather than waiting, and the operator writes the agent's after the
# apply returns — later still when it has a RuntimeClass to resolve first. This
# is the difference between "the operator has not got to it" and "the operator
# refuses to create it", which is worth the wait to tell apart.
wait_for_deployment_object() {
  local deployment="$1"
  local namespace="$2"
  local timeout_secs="$3"

  local deadline=$((SECONDS + timeout_secs))
  while ! kubectl get deployment "$deployment" -n "$namespace" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep "$DEPLOYMENT_POLL_INTERVAL_SECS"
  done
  return 0
}

has_controlling_tty() {
  [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null
}

# Safe prompt helper: supports non-interactive mode and /dev/tty fallback
prompt_read() {
  local prompt_text="$1"
  local var_name="$2"
  local default_val="${3:-}"
  local secret_mode="${4:-false}"
  # What "[default: …]" shows, when the stored value reads badly (a 40-character
  # SHA) or does not read at all (an empty list) but must still be what an empty
  # answer selects. Supplying a label also makes the hint appear for an empty default.
  local default_label="${5:-}"

  # Non-interactive mode override
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    local current_val="${!var_name:-}"
    if [ -n "$current_val" ]; then
      printf -v "$var_name" '%s' "$current_val"
    else
      printf -v "$var_name" '%s' "$default_val"
    fi
    if [ "$secret_mode" = "true" ]; then
      print_info "Auto-selected ($var_name): [REDACTED]"
    else
      print_info "Auto-selected ($var_name): ${!var_name}"
    fi
    return 0
  fi

  if [ -n "$default_val" ] || [ -n "$default_label" ]; then
    prompt_text="$prompt_text [default: ${C_BOLD}${default_label:-$default_val}${C_RESET}]: "
  else
    prompt_text="$prompt_text: "
  fi

  local input_val=""
  echo -ne "${C_CYAN}${prompt_text}${C_RESET}" >/dev/tty
  if [ "$secret_mode" = "true" ]; then
    read -r -s input_val </dev/tty
    echo "" >/dev/tty
  else
    read -r input_val </dev/tty
  fi

  if [ -z "$input_val" ] && [ -n "$default_val" ]; then
    printf -v "$var_name" '%s' "$default_val"
  else
    printf -v "$var_name" '%s' "$input_val"
  fi
}

prompt_menu() {
  local prompt_text="$1"
  shift
  local options=("$@")
  local var_name="${options[${#options[@]}-1]}"
  unset 'options[${#options[@]}-1]'

  # The option an empty answer selects. A caller that has already worked out
  # which option matches the loaded configuration pre-sets the choice variable,
  # and pressing enter then keeps that setting instead of reverting it to option
  # 1. Anything that is not an option number falls back to 1, so a caller that
  # sets nothing behaves exactly as before.
  local default_choice="${!var_name:-1}"
  if ! [[ "$default_choice" =~ ^[0-9]+$ ]] ||
    [ "$default_choice" -lt 1 ] || [ "$default_choice" -gt "${#options[@]}" ]; then
    default_choice=1
  fi

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    printf -v "$var_name" '%s' "$default_choice"
    print_info "Auto-selected option ($var_name): $default_choice"
    return 0
  fi

  if has_controlling_tty; then
    echo -e "\n${C_BOLD}$prompt_text${C_RESET}" >/dev/tty
    for i in "${!options[@]}"; do
      echo -e "  ${C_YELLOW}$((i+1)))${C_RESET} ${options[$i]}" >/dev/tty
    done
  else
    echo -e "\n${C_BOLD}$prompt_text${C_RESET}"
    for i in "${!options[@]}"; do
      echo -e "  ${C_YELLOW}$((i+1)))${C_RESET} ${options[$i]}"
    done
  fi

  local choice=""
  while true; do
    prompt_read "Select an option (1-${#options[@]})" choice "$default_choice"
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#options[@]}" ]; then
      printf -v "$var_name" '%s' "$choice"
      break
    else
      print_error "Invalid selection. Please enter a number between 1 and ${#options[@]}." >/dev/tty
    fi
  done
}

# How long each deployment gets to report ready in the post-install health check.
ROLLOUT_TIMEOUT_SECS=300

# How long each deployment gets to exist at all before that check calls it
# missing. The operator creates the agent Deployment asynchronously and, when a
# RuntimeClass is asked for, only after that RuntimeClass resolves — retrying on
# a 30s requeue (validateRuntimeClass in
# k8s-operator/internal/controller/platformagent_controller.go). Three requeues
# is the budget: below one, "not yet" and "never" are indistinguishable.
DEPLOYMENT_APPEAR_TIMEOUT_SECS=90
DEPLOYMENT_POLL_INTERVAL_SECS=5

# Number of projects listed in the interactive project picker. Accounts with
# more projects than this can still type an ID that the list does not show.
PROJECT_LIST_LIMIT=5

# GCP project IDs are 6-30 characters, start with a lowercase letter, and hold
# only lowercase letters, digits, and hyphens. A valid ID is never all digits,
# so a numeric answer is unambiguously a menu index.
is_valid_project_id() {
  local id="${1:-}"
  # Legacy domain-scoped IDs ("example.com:my-project") keep the same rules on
  # each side of the colon.
  if [[ "$id" == *:* ]]; then
    [[ "${id%%:*}" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]] || return 1
    id="${id#*:}"
  fi
  [[ "$id" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
}

# Interactive GCP project picker. Accepts either a menu number or a project ID
# typed in full, so an account whose project is missing from the truncated list
# is not stuck. Stores the result in the variable named by $1.
select_gcp_project() {
  local dest_var="$1"
  local current_proj="${2:-}"
  local listed=""
  local ids=()
  local labels=()
  local p_id="" p_name="" idx=0

  print_info "Fetching available GCP projects from your account..."
  listed=$(gcloud projects list --sort-by=projectId \
    --format="value(projectId,name)" --limit="$PROJECT_LIST_LIMIT" 2>/dev/null || echo "")

  # The active project leads the menu even when it falls outside the listing.
  if [ -n "$current_proj" ]; then
    ids+=("$current_proj")
    labels+=("$current_proj ${C_GREEN}[active]${C_RESET}")
  fi
  while IFS=$'\t' read -r p_id p_name; do
    if [ -n "$p_id" ] && [ "$p_id" != "$current_proj" ]; then
      ids+=("$p_id")
      if [ -n "$p_name" ] && [ "$p_name" != "$p_id" ]; then
        labels+=("$p_id ($p_name)")
      else
        labels+=("$p_id")
      fi
    fi
  done <<< "$listed"

  if [ "${#ids[@]}" -eq 0 ]; then
    prompt_read "Target GCP Project ID" "$dest_var" "$current_proj"
    return 0
  fi

  local sink="/dev/stdout"
  if has_controlling_tty; then
    sink="/dev/tty"
  fi
  {
    echo -e "\n${C_BOLD}Select target GCP Project:${C_RESET}"
    for idx in "${!labels[@]}"; do
      echo -e "  ${C_YELLOW}$((idx+1)))${C_RESET} ${labels[$idx]}"
    done
    if [ "$(printf '%s\n' "$listed" | grep -c '[^[:space:]]')" -ge "$PROJECT_LIST_LIMIT" ]; then
      echo -e "  ${C_CYAN}ℹ Showing the first ${PROJECT_LIST_LIMIT} projects — type a project ID to use one that is not listed.${C_RESET}"
    fi
  } > "$sink"

  local answer=""
  while true; do
    prompt_read "Select a number, or type a GCP Project ID" answer "${ids[0]}"
    if [[ "$answer" =~ ^[0-9]+$ ]]; then
      if [ "$answer" -ge 1 ] && [ "$answer" -le "${#ids[@]}" ]; then
        printf -v "$dest_var" '%s' "${ids[$((answer-1))]}"
        return 0
      fi
      print_error "Invalid selection. Enter a number between 1 and ${#ids[@]}, or type a project ID."
    elif is_valid_project_id "$answer"; then
      printf -v "$dest_var" '%s' "$answer"
      return 0
    else
      print_error "'$answer' is neither a menu number nor a valid GCP project ID (6-30 characters: lowercase letters, digits, hyphens)."
    fi
  done
}

# Auto-install missing CLI tool if possible
auto_install_tool() {
  local tool="$1"
  print_warning "Missing required CLI tool: $tool"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_error "Dry-run validation will not install missing tools. Install '$tool' and retry."
    exit 1
  fi

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    print_info "Non-interactive mode: Auto-installing $tool..."
    local install_choice="y"
  else
    local install_choice=""
    prompt_read "Attempt automatic installation of '$tool'? (y/N)" install_choice "y"
  fi

  if [[ "$install_choice" =~ ^[Yy]$ ]]; then
    if command -v brew >/dev/null 2>&1; then
      print_info "Installing $tool via Homebrew..."
      if [ "$tool" = "terraform" ]; then
        # homebrew-core disabled the terraform formula after the licence
        # change; HashiCorp's tap is the supported source.
        brew install hashicorp/tap/terraform || true
      else
        brew install "$tool" || true
      fi
    elif command -v apt-get >/dev/null 2>&1; then
      print_info "Installing $tool via apt..."
      if [ "$tool" = "terraform" ]; then
        # Stock apt has no terraform package; add HashiCorp's repository the
        # way their docs prescribe.
        type -p curl >/dev/null || sudo apt-get install curl -y
        type -p gpg >/dev/null || sudo apt-get install gnupg -y
        curl -fsSL https://apt.releases.hashicorp.com/gpg | sudo gpg --yes --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
        # shellcheck disable=SC1091  # /etc/os-release exists on every apt host; shellcheck cannot follow it
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo "$VERSION_CODENAME") main" | sudo tee /etc/apt/sources.list.d/hashicorp.list > /dev/null
        sudo apt-get update >/dev/null 2>&1 || true
        sudo apt-get install terraform -y || true
      elif [ "$tool" = "gh" ]; then
        type -p curl >/dev/null || sudo apt-get install curl -y
        curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
        sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        sudo apt-get install gh -y || true
      else
        sudo apt-get update >/dev/null 2>&1 || true
        sudo apt-get install -y "$tool" || true
      fi
    else
      print_error "Could not auto-install $tool. Package manager not recognized."
    fi
  fi

  if command -v "$tool" >/dev/null 2>&1; then
    print_success "CLI tool '$tool' installed successfully!"
  else
    print_error "Tool '$tool' is still missing. Please install $tool manually."
    exit 1
  fi
}

# Generate Machine-Readable JSON Report for AI Agents
write_json_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-install-report.json"
  local timestamp
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")

  local report_gitops_repo=""
  if [ -n "${github_org:-}" ] && [ -n "${github_repo:-}" ]; then
    report_gitops_repo="https://github.com/${github_org}/${github_repo}"
  fi

  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "project_id": "$(json_escape "${project_id:-}")",
  "project_number": "$(json_escape "${project_number:-}")",
  "cluster_name": "$(json_escape "${cluster_name:-}")",
  "cluster_mode": "$(json_escape "${TFVARS_CLUSTER_MODE:-${cluster_mode:-}}")",
  "region": "$(json_escape "${region:-}")",
  "model_provider": "$(json_escape "${model_provider:-}")",
  "permission_set": "$(json_escape "${permission_set:-}")",
  "gvisor_enabled": ${enable_gvisor:-false},
  "memory_mode": "$(json_escape "${memory_mode:-file}")",
  "gitops_repo": "$(json_escape "$report_gitops_repo")",
  "install_env_file": "$(json_escape "${INSTALL_ENV_FILE:-}")",
  "timestamp": "$(json_escape "$timestamp")"
}
EOF
  print_success "Machine-readable report written to: ${C_BOLD}${report_file}${C_RESET}"
}

# ─── Terraform Engine ─────────────────────────────────────────────────────────
# The install engine is terraform/examples/full-install driven through its
# lifecycle.sh (which adopts undeletable KMS resources before every apply).
# State lives in a GCS bucket derived from the install coordinates — see
# installer_common.sh's tf_state_bucket/tf_state_prefix — so uninstall.sh and
# upgrade.sh can find it from a fresh clone.
tf_compose_dir() {
  echo "${1}/terraform/examples/full-install"
}

# Runs lifecycle.sh apply against the generated terraform.tfvars. Reads the
# install coordinates from the environment (load install.env first).
run_lifecycle_apply() {
  local repo_dir="$1"
  local log_file="$2"
  (
    cd "$(tf_compose_dir "$repo_dir")"
    export KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-auto}"
    export KUBE_AGENTS_STATE_PREFIX
    KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"
    ./lifecycle.sh apply -auto-approve -input=false
  ) 2>&1 | tee "$log_file"
}

# CMEK on a pre-existing cluster is the one create-path behaviour Terraform
# cannot express: a data source cannot mutate the cluster it reads. Ensures
# the keyring/key and the GKE service agent's binding, then updates the
# cluster, and skips clusters that are already encrypted, do
# not exist yet (Terraform creates those encrypted), or where the operator
# explicitly allowed unencrypted secrets.
ensure_existing_cluster_cmek() {
  local project_id="$1" cluster_name="$2" region="$3"
  local enc_state
  enc_state=$(gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(databaseEncryption.state)" 2>/dev/null || echo "")
  [ -n "$enc_state" ] || return 0
  if is_valid_cmek_encryption_state "$enc_state"; then
    print_success "Existing cluster '$cluster_name' already has CMEK database encryption ($enc_state)."
    return 0
  fi
  if is_truthy "${ALLOW_UNENCRYPTED_SECRETS:-false}"; then
    print_warning "Existing cluster '$cluster_name' has no CMEK encryption ('$enc_state'), but ALLOW_UNENCRYPTED_SECRETS=true is set. Skipping."
    return 0
  fi

  local kms_location keyring="${GKE_DB_KMS_KEYRING:-platform-agent-keyring}" key="${GKE_DB_KMS_KEY:-k8s-secret-encryption-key}"
  kms_location="$(derive_kms_location "$region")"
  local key_resource="projects/${project_id}/locations/${kms_location}/keyRings/${keyring}/cryptoKeys/${key}"
  print_info "Enabling CMEK database encryption on existing cluster '$cluster_name' (key: $key_resource)..."
  gcloud services enable cloudkms.googleapis.com --project="$project_id"
  gcloud kms keyrings create "$keyring" --location="$kms_location" --project="$project_id" 2>/dev/null || true
  gcloud kms keys create "$key" --keyring="$keyring" --location="$kms_location" \
    --purpose="encryption" --project="$project_id" 2>/dev/null || true
  local project_number service_agent
  project_number=$(gcloud projects describe "$project_id" --format="value(projectNumber)")
  service_agent="service-${project_number}@container-engine-robot.iam.gserviceaccount.com"
  gcloud beta services identity create --service=container.googleapis.com --project="$project_id" 2>/dev/null || true
  gcloud kms keys add-iam-policy-binding "$key" --keyring="$keyring" --location="$kms_location" \
    --member="serviceAccount:${service_agent}" \
    --role="roles/cloudkms.cryptoKeyEncrypterDecrypter" --project="$project_id" --quiet >/dev/null
  print_info "Updating the live cluster control plane; this can take several minutes..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --database-encryption-key="$key_resource" --project "$project_id" --quiet
}

# Workload Identity on a pre-existing cluster is the other such behaviour:
# kube-agents requires the pool (every KSA→GSA binding rides it — without it
# the pods silently run as the node's service account), and the module's
# data source can only read it, so it is enabled here. No-op when the
# cluster does not exist yet:
# Terraform creates those with the pool on. The gke-cluster module's
# postcondition backstops installs driven through bare Terraform.
ensure_existing_cluster_workload_identity() {
  local project_id="$1" cluster_name="$2" region="$3"
  local pool is_autopilot

  is_autopilot=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(autopilot.enabled)" 2>/dev/null) || is_autopilot="false"
  if [ "$is_autopilot" = "True" ]; then
    print_success "Existing cluster '$cluster_name' is GKE Autopilot (Workload Identity enabled natively)."
    return 0
  fi

  # `trap - ERR` inside the substitution: bash 3.2 (macOS's default, the
  # curl|bash audience) runs the inherited ERR trap in the subshell even
  # though the outer failure is handled, printing a spurious abort banner
  # and writing a FAILED report mid-run.
  pool=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(workloadIdentityConfig.workloadPool)" 2>/dev/null) || return 0
  if [ "$pool" = "${project_id}.svc.id.goog" ]; then
    print_success "Existing cluster '$cluster_name' already has Workload Identity ($pool)."
  else
    print_info "Enabling the Workload Identity pool on existing cluster '$cluster_name'..."
    print_info "Updating the live cluster control plane; this can take several minutes..."
    gcloud container clusters update "$cluster_name" --location "$region" \
      --project "$project_id" --workload-pool="${project_id}.svc.id.goog" --quiet
  fi

  # Enabling the pool does not migrate node pools off the legacy GCE metadata
  # server, and pods on such pools still get the node's service account.
  # Standard-cluster concern: Autopilot pools are managed onto GKE_METADATA
  # already.
  local legacy_pool
  while IFS= read -r legacy_pool; do
    [ -n "$legacy_pool" ] || continue
    print_warning "Node pool '${legacy_pool}' uses the legacy GCE metadata server; migrating to GKE_METADATA (this recreates the pool's nodes)..."
    gcloud container node-pools update "$legacy_pool" \
      --cluster="$cluster_name" --location="$region" --project="$project_id" \
      --workload-metadata=GKE_METADATA --quiet
  done < <(trap - ERR; gcloud container node-pools list --cluster="$cluster_name" \
      --location="$region" --project="$project_id" \
      --format="csv[no-heading](name,config.workloadMetadataConfig.mode)" 2>/dev/null \
    | awk -F',' '$2 != "GKE_METADATA" {print $1}' || true)
}

# NetworkPolicy enforcement on a pre-existing cluster is the third such
# behaviour: every NetworkPolicy this install ships — LiteLLM's, the
# minter's, Hindsight's, and the ones the operator generates around the
# agent — is accepted and silently inert on a cluster with neither Dataplane
# V2 nor the legacy Calico addon, which is GKE Standard's default shape.
# Terraform-created clusters always have Dataplane V2; adopted ones get the
# legacy addon enabled here. The gke-cluster module's postcondition backstops
# bare-Terraform installs.
ensure_existing_cluster_network_policy() {
  local project_id="$1" cluster_name="$2" region="$3"
  local dp_provider
  # trap - ERR: same bash-3.2 subshell-trap suppression as the Workload
  # Identity probe above.
  dp_provider=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(networkConfig.datapathProvider)" 2>/dev/null) || return 0
  if [ "$dp_provider" = "ADVANCED_DATAPATH" ]; then
    print_success "Existing cluster '$cluster_name' runs Dataplane V2; NetworkPolicy enforcement is built in."
    return 0
  fi
  local legacy_np
  legacy_np=$(trap - ERR; gcloud container clusters describe "$cluster_name" \
    --location="$region" --project="$project_id" \
    --format="value(networkPolicy.enabled)" 2>/dev/null || echo "")
  if [ "$legacy_np" = "True" ] || [ "$legacy_np" = "true" ]; then
    print_success "Existing cluster '$cluster_name' already enforces NetworkPolicy (legacy Calico addon)."
    return 0
  fi
  # Two calls, in this order. GKE rejects --enable-network-policy with "The
  # network policy addon must be enabled before updating the nodes" (HTTP 400)
  # until the Calico addon is on the control plane, and gcloud puts
  # --update-addons and --enable-network-policy in the same "exactly one of
  # these must be specified" argparse group, so they cannot be combined into a
  # single invocation.
  #
  # Unconditional, matching Google's documented procedure. Gating it on
  # addonsConfig.networkPolicyConfig.disabled looks tempting for the re-run
  # case — the guard above reads networkPolicy.enabled, so a re-run after the
  # enforcement call failed arrives here with the addon already on — but that
  # field cannot express it: GKE omits false booleans from addonsConfig, so
  # "off" prints True and "on" prints nothing, which is also what a failed
  # describe prints. A gate that skips on empty reintroduces the 400 the
  # moment describe fails. Re-enabling an already-enabled addon is a no-op.
  print_info "Enabling the NetworkPolicy addon on existing cluster '$cluster_name'..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --update-addons=NetworkPolicy=ENABLED --project "$project_id" --quiet
  print_info "Enabling NetworkPolicy enforcement on existing cluster '$cluster_name' (node pools may be recreated; this can take several minutes)..."
  gcloud container clusters update "$cluster_name" --location "$region" \
    --enable-network-policy --project "$project_id" --quiet
  local active_op
  active_op=$(gcloud container operations list --location="$region" --project="$project_id" \
    --filter="targetLink:$cluster_name AND status=RUNNING" --format="value(name)" 2>/dev/null | head -n1)
  if [ -n "$active_op" ]; then
    print_info "Waiting for operation $active_op to complete..."
    gcloud container operations wait "$active_op" --location="$region" --project="$project_id" ||
      print_warning "Operation wait returned non-zero (it may have finished between list and wait); proceeding..."
  fi
  print_warning "Legacy Network Policy enabled. FQDN-based NetworkPolicies stay unsupported without Dataplane V2."
}

# Neither google provider has a field for --managed-otel-scope, so it is set
# out-of-band after the apply. Best-effort by design: on a gcloud where the
# update surface lacks the flag, the install is
# still complete — only managed OpenTelemetry collection needs a manual step.
apply_managed_otel_scope() {
  local project_id="$1" cluster_name="$2" region="$3"
  if gcloud container clusters update "$cluster_name" --location "$region" --project "$project_id" \
    --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS --quiet >/dev/null 2>&1; then
    print_success "Managed OpenTelemetry scope set on '$cluster_name'."
  else
    print_warning "Could not set --managed-otel-scope on '$cluster_name' (create-only on this gcloud?)."
    print_info "Set it manually if you want managed OTel collection: gcloud container clusters update $cluster_name --location $region --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS"
  fi
}

# One-shot import of the GitHub App private key into the minter's KMS signing
# key, via the Minty CLI. The PEM never enters Terraform state — that is why
# this is not a Terraform resource. Skipped when a key version is already
# ENABLED (the import happened on an earlier run) and downgraded to printed
# instructions when Go is unavailable.
import_github_pem() {
  local project_id="$1" region="$2"
  [ -n "${GITOPS_ORG:-}" ] && [ -n "${GITOPS_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ] || return 0
  local pem_path="${GITHUB_PEM_PATH:-}"
  local kms_location keyring="${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" key="${KMS_KEY:-$DEFAULT_KMS_KEY}"
  kms_location="$(derive_kms_location "$region")"

  local enabled_version
  enabled_version=$(gcloud kms keys versions list --key "$key" --keyring "$keyring" \
    --location "$kms_location" --project "$project_id" \
    --filter='state=ENABLED' --format='value(name.basename())' 2>/dev/null | head -1 || echo "")
  if [ -n "$enabled_version" ]; then
    print_success "GitHub minter KMS key already has an ENABLED version ($enabled_version); skipping PEM import."
    return 0
  fi

  # Clone the tag and run the CLI from the tree:
  # `go run github.com/abcxyz/github-token-minter/cmd/minty@v2.7.1`
  # cannot work: the upstream go.mod declares the module without the /v2 suffix
  # its v2 tags require, so Go rejects the version with or without /v2 in the
  # path. The gcloud-only recovery recipe lives in
  # k8s-operator/config/integrations/github/README.md.
  local import_cmd="git clone --depth 1 --branch v2.7.1 https://github.com/abcxyz/github-token-minter.git /tmp/minty && cd /tmp/minty && go run ./cmd/minty tools import-pk -project-id=${project_id} -location=${kms_location} -key-ring=${keyring} -key=${key} -private-key=@<path-to-pem>"
  if [ -z "$pem_path" ] || [ ! -f "$pem_path" ]; then
    print_warning "No GitHub App private key PEM available (GITHUB_PEM_PATH='${pem_path}')."
    print_info "The minter deployment stays unready until the key is imported: ${import_cmd}"
    return 0
  fi
  if ! command -v go >/dev/null 2>&1; then
    print_warning "Go is not installed, so the App key cannot be imported automatically."
    print_info "Import it manually: ${import_cmd/<path-to-pem>/$pem_path}"
    print_info "Without Go, the gcloud-only import recipe is in k8s-operator/config/integrations/github/README.md."
    return 0
  fi
  # The ring and key normally come from Terraform, but this import runs
  # BEFORE the apply — the minter Deployment cannot pass readiness without an
  # imported key, and the composition's helm release waits on every
  # Deployment, so importing after the apply would wedge it. Ensure they
  # exist first, matching terraform/modules/github-minter exactly; adopt-kms
  # imports them into state at apply time, the same way it re-adopts them
  # after a destroy.
  print_info "Ensuring the minter's KMS keyring and import-only signing key exist..."
  gcloud services enable cloudkms.googleapis.com --project="$project_id"

  # Both creates keep their errors instead of discarding them. Re-running the
  # installer is the common case and "already exists" is the expected answer to
  # it, so the output is only surfaced when the resource is missing afterwards —
  # which is the check that actually matters. Discarding stderr outright is what
  # hid the bug below; tolerating one specific error would still have hidden a
  # permission denial, a disabled API or a quota refusal, all of which end the
  # same way: no key, and an import that fails against something that is not there.
  #
  # `trap - ERR` inside each substitution, for the reason spelled out at the
  # Workload Identity probe above: bash 3.2 runs the inherited ERR trap in the
  # subshell even though `|| true` handles the failure, so a re-run — where
  # "already exists" is the expected answer — would print two fatal-looking
  # abort banners and leave a FAILED install report behind mid-run.
  local kms_ring_err="" kms_key_err=""
  kms_ring_err="$(trap - ERR; gcloud kms keyrings create "$keyring" --location="$kms_location" \
    --project="$project_id" 2>&1)" || true

  # --skip-initial-version-creation is required, not optional: KMS answers
  # `INVALID_ARGUMENT: Import-only keys must skip initial version creation` without
  # it. It matches skip_initial_version_creation in terraform/modules/github-minter,
  # which is where the key normally comes from.
  kms_key_err="$(trap - ERR; gcloud kms keys create "$key" --keyring="$keyring" --location="$kms_location" \
    --purpose=asymmetric-signing --default-algorithm=rsa-sign-pkcs1-2048-sha256 \
    --import-only --skip-initial-version-creation \
    --protection-level=software --project="$project_id" 2>&1)" || true

  # The assertion, not the create, is what makes a failure visible. Whatever went
  # wrong above, the import cannot work without this key, and saying so here names
  # the cause instead of leaving a confusing failure two steps later.
  if ! gcloud kms keys describe "$key" --keyring="$keyring" --location="$kms_location" \
    --project="$project_id" >/dev/null 2>&1; then
    # Deliberately says "could not be confirmed" rather than "does not exist":
    # describe also fails on an IAM denial for cloudkms.cryptoKeys.get or an API
    # blip, and asserting absence from that would be stating more than was
    # established. Whatever the cause, the import cannot safely proceed.
    print_warning "The minter's KMS signing key ${kms_location}/${keyring}/${key} could not be confirmed to exist."
    [ -n "$kms_ring_err" ] && print_info "Keyring create said: ${kms_ring_err}"
    [ -n "$kms_key_err" ] && print_info "Key create said: ${kms_key_err}"
    print_info "The PEM import needs the keyring and the key, so it is being skipped; the minter deployment stays unready until both exist."
    # Not the README's import recipe: that one presupposes the key and only covers
    # loading a PEM into it. What failed here is the creation, so print the two
    # commands that create it. --skip-initial-version-creation is the one that is
    # easy to lose and the one KMS refuses an import-only key without.
    print_info "Create them by hand with:"
    print_info "  gcloud kms keyrings create ${keyring} --location=${kms_location} --project=${project_id}"
    print_info "  gcloud kms keys create ${key} --keyring=${keyring} --location=${kms_location} --purpose=asymmetric-signing --default-algorithm=rsa-sign-pkcs1-2048-sha256 --import-only --skip-initial-version-creation --protection-level=software --project=${project_id}"
    print_info "Then import the PEM with the recipe in k8s-operator/config/integrations/github/README.md."
    return 0
  fi

  print_info "Importing the GitHub App private key into KMS via the Minty CLI..."
  local minty_dir pem_abs
  minty_dir="$(mktemp -d "${TMPDIR:-/tmp}/minty-XXXXXX")"
  pem_abs="$(realpath "$pem_path" 2>/dev/null || echo "$pem_path")"
  if git clone --quiet --depth 1 --branch v2.7.1 \
      https://github.com/abcxyz/github-token-minter.git "$minty_dir" &&
    (cd "$minty_dir" && retry 6 5 go run ./cmd/minty tools import-pk \
      -project-id="$project_id" -location="$kms_location" -key-ring="$keyring" -key="$key" \
      -private-key=@"$pem_abs"); then
    print_success "GitHub App private key imported into ${keyring}/${key}."
  else
    print_warning "PEM import failed; the minter deployment stays unready until it succeeds."
    print_info "Retry manually: ${import_cmd/<path-to-pem>/$pem_path}"
    print_info "If Go itself is the problem (killed compiler, no toolchain), the gcloud-only recipe is in k8s-operator/config/integrations/github/README.md."
  fi
  rm -rf "$minty_dir"
}

# ─── Day-2 Control Panel Menu System (raspi-config style) ──────────────────────
run_menu_system() {
  # The control panel is inherently interactive: without a terminal its menu
  # loop would auto-select the first option forever instead of ever exiting.
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    print_error "The Day-2 control panel requires an interactive terminal."
    print_info "Re-run './install.sh --menu' from a TTY, without -y/--non-interactive."
    exit 1
  fi

  local repo_dir
  repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local vars_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  local helper_script="${repo_dir}/scripts/installer/installer_common.sh"

  if [ ! -f "$helper_script" ]; then
    print_error "Cannot find installer helpers at $helper_script."
    exit 1
  fi
  export VARS_FILE="$vars_file"
  # shellcheck disable=SC1090
  source "$helper_script"

  if [ -f "$vars_file" ]; then
    # shellcheck disable=SC1090
    if ! source "$vars_file"; then
      print_error "Configuration state is invalid and could not be loaded: $vars_file"
      exit 1
    fi
  fi
  # install.env was already loaded at startup, but sourcing vars.sh just now put
  # the derived state back over the top of it. Re-apply the input so the
  # hand-authored file is what the panel opens on, whichever order the two
  # files disagree in.
  load_install_env "$INSTALL_ENV_FILE" || true
  # ...and the same for the memory setting, which the two files spell
  # differently (install.env MEMORY, legacy vars.sh MEMORY_PROVIDER) so load
  # order alone cannot make the input win. Save & Apply generates tfvars
  # directly, without passing through the parameter block that resolves this
  # pair on install.sh's own run.
  normalize_memory_vars

  local project_id="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}"
  local project_number="${PROJECT_NUMBER:-}"
  local cluster_name="${CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}"
  local region="${REGION:-$DEFAULT_REGION}"
  local model_provider="${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}"
  local model_default_name="${MODEL_DEFAULT_NAME:-$(default_model_for_provider "${MODEL_PROVIDER:-$DEFAULT_MODEL_PROVIDER}")}"
  local vertex_project_id="${VERTEX_PROJECT_ID:-$project_id}"
  local vertex_location="${VERTEX_LOCATION:-$DEFAULT_VERTEX_LOCATION}"
  local gemini_api_key="${GEMINI_API_KEY:-}"
  local openai_api_key="${OPENAI_API_KEY:-}"
  local anthropic_api_key="${ANTHROPIC_API_KEY:-}"
  local google_chat_enabled="${GOOGLE_CHAT_ENABLED:-$DEFAULT_GOOGLE_CHAT_ENABLED}"
  local slack_enabled="${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"
  local allowed_users="${ALLOWED_USERS:-}"
  local chat_topic_name="${CHAT_TOPIC_NAME:-$DEFAULT_CHAT_TOPIC_NAME}"
  local chat_sub_name="${CHAT_SUB_NAME:-$DEFAULT_CHAT_SUB_NAME}"
  local permission_set="${PLATFORM_AGENT_PERMISSION_SET:-$DEFAULT_PERMISSION_SET}"
  local custom_roles="${PLATFORM_AGENT_CUSTOM_ROLES:-}"
  # Not the fresh-install default. The control panel describes an install that
  # already exists and its Save & Apply re-applies what it displays, so a
  # vars.sh with no ENABLE_GVISOR has to read as the standard runtime — that is
  # what such a cluster is actually running. Defaulting on here would show
  # "gVisor Sandbox" for an unsandboxed install and then provision a node pool
  # nobody asked for on the next apply.
  local enable_gvisor="${ENABLE_GVISOR:-false}"
  # DEFAULT_ENABLE_WEBUI is "false" and the paragraph above applies to it too:
  # the panel has to read as what an unconfigured install is running. Flipping
  # that default on would make this show a dashboard nobody deployed, so the
  # two have to be reconsidered together.
  local enable_webui="${HERMES_DASHBOARD_ENABLED:-$DEFAULT_ENABLE_WEBUI}"
  local github_org="${GITOPS_ORG:-${GITHUB_ORG:-}}"
  local github_repo="${GITOPS_REPO:-${GITHUB_REPO:-$DEFAULT_GITOPS_REPO}}"
  local github_app_id="${GITHUB_APP_ID:-}"
  local kms_keyring="${KMS_KEYRING:-}"
  local kms_key="${KMS_KEY:-}"
  local github_pem_path="${GITHUB_PEM_PATH:-}"
  local image_tag="${IMAGE_TAG:-}"

  while true; do
    echo -e "\n${C_CYAN}${C_BOLD}"
    draw_separator
    echo "🛠️  Kubernetes Agentic Harness (kube-agents) Day-2 Control Panel"
    draw_separator
    echo -e "${C_RESET}"
    echo -e "${C_BOLD}Active Configuration State:${C_RESET}"
    echo -e "  • ${C_CYAN}GCP Project ID:${C_RESET} ${project_id:-Not Set}"
    echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${cluster_name:-Not Set} (${region:-$DEFAULT_REGION})"
    echo -e "  • ${C_CYAN}Hermes Web UI (Port 9119):${C_RESET} $([ "$enable_webui" = "true" ] && echo -e "${C_GREEN}ENABLED${C_RESET}" || echo -e "${C_YELLOW}DISABLED${C_RESET}")"
    echo -e "  • ${C_CYAN}Chat Integrations:${C_RESET} Google Chat: $([ "$google_chat_enabled" = "true" ] && echo -e "${C_GREEN}ON${C_RESET}" || echo "OFF"), Slack: $([ "$slack_enabled" = "true" ] && echo -e "${C_GREEN}ON${C_RESET}" || echo "OFF")"
    echo -e "  • ${C_CYAN}AI Model Provider:${C_RESET} ${model_provider} (${model_default_name})$([ "$model_provider" = "vertex_ai" ] && echo " @ ${vertex_project_id}/${vertex_location}" || echo "")"
    echo -e "  • ${C_CYAN}Permission Boundary:${C_RESET} ${permission_set}"
    echo -e "  • ${C_CYAN}Runtime Isolation:${C_RESET} $([ "$enable_gvisor" = "true" ] && echo -e "${C_GREEN}gVisor Sandbox${C_RESET}" || echo "Standard")"

    local menu_choice=""
    prompt_menu "Select configuration task:" \
      "🌐 Toggle Hermes Web UI (Port 9119 Dashboard)" \
      "💬 Manage Chat & Messaging Integrations (Google Chat / Slack)" \
      "🔑 Manage AI Model Provider & Credentials (Gemini / Vertex / OpenAI)" \
      "🛡️ Modify Security & Permission Boundaries (gVisor / SRE vs Read-Only)" \
      "🗄️ Manage GitOps Repository & GitHub Auth (gke-fleet-iac)" \
      "🚀 Save & Apply Configuration Changes (~15s update)" \
      "🚪 Exit Control Panel" \
      menu_choice

    case "$menu_choice" in
      1)
        if [ "$enable_webui" = "true" ]; then
          enable_webui="false"
          print_success "Hermes Web UI disabled."
        else
          enable_webui="true"
          print_success "Hermes Web UI enabled!"
        fi
        ;;
      2)
        local c_opt=""
        prompt_menu "Select Chat Integration:" \
          "Google Chat (Pub/Sub Event Streaming)" \
          "Slack (Socket Mode App)" \
          "Disable All Chat Integrations" \
          c_opt
        case "$c_opt" in
          1)
            google_chat_enabled="true"
            local gchat_users_hint=""
            if [ -z "$allowed_users" ]; then
              gchat_users_hint="empty list"
            fi
            prompt_read "Allowed Google Chat User Emails (comma-separated, empty allows all users)" \
              allowed_users "$allowed_users" false "$gchat_users_hint"
            ;;
          2) slack_enabled="true" ;;
          3) google_chat_enabled="false"; slack_enabled="false" ;;
        esac
        ;;
      3)
        local m_opt=""
        prompt_menu "Select AI Model Provider:" \
          "Google Gemini ($(default_model_for_provider gemini))" \
          "Google Vertex AI / Model Garden (no API key — Workload Identity)" \
          "OpenAI ($(default_model_for_provider openai))" \
          "Anthropic ($(default_model_for_provider anthropic))" \
          m_opt
        case "$m_opt" in
          1)
            model_provider="gemini"
            model_default_name="$(default_model_for_provider gemini)"
            prompt_read "Gemini API Key" gemini_api_key "$gemini_api_key" true
            ;;
          2)
            model_provider="vertex_ai"
            prompt_read "Vertex AI Project ID" vertex_project_id "$vertex_project_id"
            prompt_read "Vertex AI Location" vertex_location "$vertex_location"
            prompt_read "Vertex Model ID (publisher model, e.g. gemini-3.5-flash)" model_default_name "${model_default_name:-$(default_model_for_provider vertex_ai)}"
            # Same notice main() prints on the first-install path: switching a
            # running install to Vertex through this panel lands on the global
            # endpoint too, and must not do so silently.
            if [ "$vertex_location" = "global" ]; then
              print_warning "The global endpoint gives no in-region ML processing guarantee. Set a region above if you have a data-residency requirement."
            fi
            ;;
          3)
            model_provider="openai"
            model_default_name="$(default_model_for_provider openai)"
            prompt_read "OpenAI API Key" openai_api_key "$openai_api_key" true
            ;;
          4)
            model_provider="anthropic"
            model_default_name="$(default_model_for_provider anthropic)"
            prompt_read "Anthropic API Key" anthropic_api_key "$anthropic_api_key" true
            ;;
        esac
        ;;
      4)
        local p_opt=""
        prompt_menu "Select GCP IAM Permission Set:" \
          "read-only — auditing and observability, no GCP write capability (Default)" \
          "custom — exactly the roles you list, no built-in bundle" \
          p_opt
        case "$p_opt" in
          1) permission_set="read-only" ;;
          2)
            permission_set="custom"
            while true; do
              prompt_read "Custom GCP IAM Roles (space- or comma-separated)" custom_roles "$custom_roles"
              [ -n "$custom_roles" ] && break
              print_error "The custom permission set needs at least one role, e.g. roles/container.viewer."
            done
            warn_on_overreaching_custom_roles "$custom_roles"
            ;;
        esac
        ;;
      5)
        prompt_read "GitHub Org / Username" github_org "$github_org"
        prompt_read "GitOps Repository Name" github_repo "$github_repo"
        ;;
      6)
        print_step "Saving & Re-applying Configuration State"
        if [ -z "$image_tag" ]; then
          prompt_read "Container image tag (validated release tag or full commit SHA)" \
            image_tag "$(default_image_tag "$repo_dir")" false "$(default_image_tag_label "$repo_dir")"
        fi
        validate_immutable_ref "$image_tag"
        verify_local_source_ref "$repo_dir" "$image_tag"
        export PARAM_PROJECT_ID="$project_id" PARAM_CLUSTER_NAME="$cluster_name" PARAM_REGION="$region"
        export PARAM_ENABLE_WEBUI="$enable_webui" PARAM_MODEL_PROVIDER="$model_provider"
        export PARAM_PERMISSION_SET="$permission_set" PARAM_ENABLE_GVISOR="$enable_gvisor"
        export GOOGLE_CHAT_ENABLED="$google_chat_enabled" SLACK_ENABLED="$slack_enabled"

        # Into install.env, one key at a time, leaving the operator's comments
        # and ordering alone. This panel is the one place allowed to write
        # there: "Save & Apply" is an explicit instruction to record a change,
        # unlike install.sh silently regenerating an input.
        #
        # PROJECT_NUMBER, KMS_LOCATION and NO_CONFIRM are deliberately not
        # written. The first two are derived wherever they are used, and the third
        # describes an invocation rather than the install.
        save_env_var PROJECT_ID "$project_id"
        save_env_var CLUSTER_NAME "$cluster_name"
        save_env_var REGION "$region"
        save_env_var MODEL_PROVIDER "$model_provider"
        save_env_var MODEL_DEFAULT_NAME "$model_default_name"
        save_env_var VERTEX_PROJECT_ID "$vertex_project_id"
        save_env_var VERTEX_LOCATION "$vertex_location"
        save_env_var VERTEX_MANAGE_SERVING_PROJECT "${VERTEX_MANAGE_SERVING_PROJECT:-$DEFAULT_VERTEX_MANAGE_SERVING_PROJECT}"
        save_secret_env_var GEMINI_API_KEY "$gemini_api_key"
        save_secret_env_var OPENAI_API_KEY "$openai_api_key"
        save_secret_env_var ANTHROPIC_API_KEY "$anthropic_api_key"
        save_env_var ALLOWED_USERS "$allowed_users"
        save_env_var CHAT_TOPIC_NAME "$chat_topic_name"
        save_env_var CHAT_SUB_NAME "$chat_sub_name"
        save_env_var GOOGLE_CHAT_ENABLED "$google_chat_enabled"
        save_env_var SLACK_ENABLED "$slack_enabled"
        save_env_var PLATFORM_AGENT_PERMISSION_SET "$permission_set"
        if [ "$permission_set" = "custom" ]; then
          save_env_var PLATFORM_AGENT_CUSTOM_ROLES "$custom_roles"
        fi
        save_env_var ENABLE_GVISOR "$enable_gvisor"
        save_env_var HERMES_DASHBOARD_ENABLED "$enable_webui"
        save_env_var GITOPS_ORG "$github_org"
        save_env_var GITOPS_REPO "$github_repo"
        save_env_var GITHUB_APP_ID "$github_app_id"
        save_env_var KMS_KEYRING "$kms_keyring"
        save_env_var KMS_KEY "$kms_key"
        save_env_var GITHUB_PEM_PATH "$github_pem_path"
        print_success "Updated configuration saved to: $INSTALL_ENV_FILE"

        # One engine for every kind of change: a full terraform apply
        # reconciles GCP resources and chart values alike, so a Vertex switch
        # lands its IAM, the gateway, and the agent in one pass. When nothing
        # GCP-side moved, the apply is a fast no-op around the Helm upgrade.
        #
        # No re-source: save_env_var exports as it writes, so the environment
        # write_tfvars_from_state reads is already current.
        write_tfvars_from_state "$(tf_compose_dir "$repo_dir")/terraform.tfvars" "$image_tag"
        print_info "Re-applying the install to GKE cluster '$cluster_name' (terraform apply)..."
        run_lifecycle_apply "$repo_dir" "/tmp/kube-agents-apply-$(date -u +%Y%m%dT%H%M%SZ).log"
        print_success "Configuration applied!"
        ;;
      7)
        print_info "Exiting Control Panel."
        break
        ;;
    esac
  done
}

# ─── Main Installer Procedure ──────────────────────────────────────────────────
main() {
  parse_args "$@"
  print_banner

  if [ "${PARAM_MENU_MODE:-false}" = "true" ]; then
    run_menu_system
    exit 0
  fi

  # 1. Environment Detection (Google Cloud Shell vs Linux/macOS Terminal)
  local is_cloud_shell="false"
  if [ "${CLOUD_SHELL:-false}" = "true" ] || [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
    is_cloud_shell="true"
    print_success "Environment Detected: ${C_BOLD}Google Cloud Shell${C_RESET} ☁️"
  else
    print_info "Environment Detected: ${C_BOLD}Standard Workstation / Linux Terminal${C_RESET} 💻"
  fi

  if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
    print_info "Execution Mode: ${C_BOLD}Non-Interactive / AI Agent Automated Mode${C_RESET} 🤖"
  fi

  local image_tag="${PARAM_IMAGE_TAG:-}"
  if [ -z "$image_tag" ]; then
    local head_sha=""
    head_sha="$(default_image_tag)"
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      if [ -z "$head_sha" ]; then
        print_error "--image-tag is required; use a validated release tag or full commit SHA."
        exit 1
      fi
      image_tag="$head_sha"
      print_info "Defaulting image tag to $(default_image_tag_label): ${C_BOLD}${image_tag}${C_RESET}"
    else
      prompt_read "Container image tag (validated release tag or full commit SHA)" \
        image_tag "$head_sha" false "$(default_image_tag_label)"
    fi
  fi
  validate_immutable_ref "$image_tag"

  # 2. Prerequisite CLI Tools Check & Auto-Installation
  print_step "1. Checking Prerequisites & Installing Missing Tools"
  # terraform is the install engine (terraform/examples/full-install through
  # lifecycle.sh); kubectl is used by lifecycle.sh and the health checks; helm
  # serves upgrade.sh's fast path; jq and gh remain for the surrounding
  # tooling. Everything is checked up front rather than discovered halfway
  # through with the cluster already created.
  for tool in git gcloud kubectl gh helm jq terraform; do
    if command -v "$tool" >/dev/null 2>&1; then
      print_success "Found CLI tool: $tool"
    else
      auto_install_tool "$tool"
    fi
  done
  require_min_gcloud_version || exit 1
  require_min_terraform_version || exit 1

  # 3. Provisioning Sources & Shared Defaults
  print_step "2. Setting up Workspace Repository"
  local repo_dir=""
  acquire_source_repo repo_dir "$image_tag"
  source_provisioning_helpers "$repo_dir"
  resolve_shared_defaults

  # 3. Google Cloud Authentication Check
  print_step "3. Verifying Google Cloud Authentication"
  local active_account=""
  active_account=$(gcloud config get-value account 2>/dev/null || echo "")

  if [ -z "$active_account" ] || ! gcloud auth print-access-token >/dev/null 2>&1; then
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      print_error "gcloud CLI is not authenticated and non-interactive mode is enabled."
      print_info "Please run 'gcloud auth login' before executing the installer."
      exit 1
    fi
    print_warning "gcloud CLI is not authenticated."
    print_info "Launching Google Cloud authentication..."
    gcloud auth login </dev/tty >/dev/tty
    gcloud auth application-default login </dev/tty >/dev/tty
    active_account=$(gcloud config get-value account 2>/dev/null || echo "")
  fi
  print_success "Authenticated as: ${C_BOLD}${active_account:-Google Cloud User}${C_RESET}"

  # 4. GCP Project Target Configuration
  print_step "4. Google Cloud Target Configuration"
  local active_proj=""
  if [ "$is_cloud_shell" = "true" ] && [ -n "${DEVSHELL_PROJECT_ID:-}" ]; then
    active_proj="${DEVSHELL_PROJECT_ID}"
  else
    active_proj=$(gcloud config get-value project 2>/dev/null || echo "")
  fi

  local project_id=""
  if [ -n "$PARAM_PROJECT_ID" ]; then
    project_id="$PARAM_PROJECT_ID"
  elif [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    prompt_read "Target GCP Project ID" project_id "$active_proj"
  else
    select_gcp_project project_id "$active_proj"
  fi

  if [ -z "$project_id" ]; then
    print_error "No GCP project selected. Re-run with --project-id=<project-id>."
    exit 1
  fi

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_info "Dry-run: leaving the active gcloud project unchanged (target: ${project_id})."
  elif ! gcloud config set project "$project_id" >/dev/null; then
    print_error "Unable to select GCP project '$project_id'. Verify the project ID and your access."
    exit 1
  fi
  print_success "Selected Project ID: ${C_BOLD}${project_id}${C_RESET}"

  # Auto-resolve Project Number
  local project_number=""
  project_number=$(gcloud projects describe "$project_id" --format="value(projectNumber)" 2>/dev/null || echo "")
  if [ -z "$project_number" ]; then
    print_error "Unable to resolve the project number for '$project_id'. Verify the project ID and your access."
    exit 1
  fi
  print_success "Resolved Project Number: ${C_BOLD}${project_number}${C_RESET}"

  # Region Selection
  local active_region=""
  active_region=$(gcloud config get-value compute/region 2>/dev/null || echo "")
  local region="${PARAM_REGION:-}"
  if [ -z "$region" ]; then
    prompt_read "Target GCP Region" region "${active_region:-$DEFAULT_REGION}"
  fi

  # Checked here as well as after the menu below so a bad --cluster-mode fails
  # before the rest of the interview, not after it.
  local cluster_mode="${PARAM_CLUSTER_MODE:-}"
  [ -z "$cluster_mode" ] || require_creatable_cluster_mode "$cluster_mode" "$region"

  # 5. GKE Cluster Selection & Provisioning Strategy
  print_step "5. GKE Cluster Topology & Capacity Setup"
  local cluster_choice=""
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || [ -n "$PARAM_CLUSTER_NAME" ]; then
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      cluster_choice="2"
    else
      cluster_choice="1"
    fi
  else
    prompt_menu "How would you like to handle the GKE Cluster?" \
      "Provision a NEW GKE Cluster from scratch (Recommended)" \
      "Use an EXISTING GKE Cluster" \
      cluster_choice
  fi

  local cluster_name="${PARAM_CLUSTER_NAME:-}"
  # Set on the branches where the user has demonstrably asked for a cluster
  # that does not exist yet, which is the only case --cluster-mode decides.
  # Picking one out of the discovered list, or naming one with --cluster-name,
  # does not qualify: the generator probes those and the live shape wins.
  local ask_cluster_shape="false"
  if [ "$cluster_choice" = "1" ]; then
    if [ -z "$cluster_name" ]; then
      prompt_read "New GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
    fi
    ask_cluster_shape="true"
  else
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      cluster_name="$PARAM_CLUSTER_NAME"
    else
      # Auto-discover existing clusters
      print_info "Querying existing GKE clusters in project '$project_id'..."
      local cluster_lines=""
      cluster_lines=$(gcloud container clusters list --project="$project_id" --format="value(name,location)" 2>/dev/null || echo "")

      if [ -n "$cluster_lines" ]; then
        local cluster_opts=()
        local cluster_names=()
        local cluster_locations=()
        while IFS=$'\t' read -r c_name c_loc; do
          if [ -n "$c_name" ]; then
            cluster_names+=("$c_name")
            cluster_locations+=("$c_loc")
            cluster_opts+=("$c_name (location: $c_loc)")
          fi
        done <<< "$cluster_lines"
        cluster_opts+=("Type an unlisted cluster name manually")

        local c_choice=""
        prompt_menu "Select existing GKE cluster:" "${cluster_opts[@]}" c_choice
        if [ "$c_choice" -le "${#cluster_names[@]}" ]; then
          cluster_name="${cluster_names[$((c_choice-1))]}"
          region="${cluster_locations[$((c_choice-1))]}"
          print_success "Using discovered cluster location: ${C_BOLD}${region}${C_RESET}"
        else
          prompt_read "Existing GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
          # A name the project's own cluster list did not offer, so it very
          # likely does not exist and this run creates it.
          ask_cluster_shape="true"
        fi
      else
        print_warning "No existing GKE clusters found in project '$project_id'."
        prompt_read "Existing GKE Cluster Name" cluster_name "$DEFAULT_CLUSTER_NAME"
        # Nothing to adopt in this project, so whatever is named here is about
        # to be created.
        ask_cluster_shape="true"
      fi
    fi
  fi
  # Only when --cluster-mode said nothing: a flag the caller passed is an
  # answer already, and re-asking would let a mis-keyed menu choice override
  # it.
  #
  # The order comes from resolve_creatable_cluster_mode rather than being
  # hardcoded, because prompt_menu's enter default is option 1. A fixed
  # Autopilot-first order makes pressing enter an *explicit* autopilot
  # request, which the resolver is then right to refuse to demote — so a
  # zonal interactive install would abort here rather than build Standard.
  # Deriving the order
  # keeps the "(Default)" label, the enter key and the resolver saying the
  # same thing at both kinds of location.
  if [ -z "$cluster_mode" ] && [ "$ask_cluster_shape" = "true" ] &&
    [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local mode_choice="" menu_default=""
    local autopilot_option="Autopilot — Google manages the nodes and you pay per Pod; regional only, and gVisor comes from its built-in RuntimeClass"
    local standard_option="Standard — you size and pay for the node pool; carries the GKE Sandbox pool for --gvisor, and is the only shape that can be zonal"
    menu_default="$(resolve_creatable_cluster_mode "" "$region")"
    if [ "$menu_default" = "autopilot" ]; then
      prompt_menu "Which shape should the GKE cluster be, if this run creates it?" \
        "${autopilot_option} (Default)" \
        "${standard_option}" \
        mode_choice
      case "$mode_choice" in
        1) cluster_mode="autopilot" ;;
        2) cluster_mode="standard" ;;
      esac
    else
      # Zonal location. Autopilot stays on the menu so picking it is still an
      # explicit request that require_creatable_cluster_mode rejects by name,
      # rather than a shape that silently turns into something else.
      prompt_menu "Which shape should the GKE cluster be, if this run creates it?" \
        "${standard_option} (Default)" \
        "${autopilot_option} — not available at a zonal location" \
        mode_choice
      case "$mode_choice" in
        1) cluster_mode="standard" ;;
        2) cluster_mode="autopilot" ;;
      esac
    fi
  fi
  # Nothing asked, nothing passed: installer_common.sh owns the default, and
  # resolve_creatable_cluster_mode applies it. Explaining the demotion is the
  # caller's job so the resolver can echo the mode and nothing else.
  #
  # This matters most on the --cluster-name path, where ask_cluster_shape is
  # false and the check below therefore never runs: a named cluster that does
  # not exist yet would otherwise be written as autopilot at a zone and
  # rejected by the module's precondition at terraform validate, after the
  # whole interview had already been collected.
  local cluster_mode_requested="$cluster_mode"
  cluster_mode="$(resolve_creatable_cluster_mode "$cluster_mode" "$region")"
  # ask_cluster_shape gates the message for the same reason it gates the check
  # below: on both adoption paths no cluster is created by this run, so the
  # advice to "pass --region with a region" would point at a location the
  # target cluster does not live at. On --cluster-name that is not merely
  # noise — write_tfvars_from_state probes with --location "$REGION", so
  # re-running with the suggested region misses the live cluster, takes the
  # confirmed-NOT_FOUND branch, and creates a second one under -auto-approve.
  if [ "$ask_cluster_shape" = "true" ] && [ -z "$cluster_mode_requested" ] &&
    [ "$cluster_mode" != "$DEFAULT_CLUSTER_MODE" ]; then
    print_info "Location '${region}' is a zone and Autopilot clusters are regional, so a cluster created by this run will be Standard. Pass --region with a region to get the default Autopilot shape."
  fi

  # Only where a cluster is about to be created. Adopting a discovered cluster
  # replaced $region with that cluster's own location, which may be a zone —
  # and failing an adoption over a location the installer chose itself, for a
  # shape the generator is about to overrule anyway, blames the wrong input.
  # The flag/region pair the caller did supply was already checked above.
  if [ "$ask_cluster_shape" = "true" ]; then
    require_creatable_cluster_mode "$cluster_mode" "$region"
  fi
  print_success "Selected Cluster Name: ${C_BOLD}${cluster_name}${C_RESET}"

  # 6. Chat & Messaging Platform Integration
  print_step "6. Chat & Messaging Integrations Setup"
  # The option the loaded configuration already corresponds to. It is a
  # PRE-SELECTION, not a decision: prompt_menu takes a pre-set choice variable
  # as its default (install.sh:1076), so enter keeps the current integration
  # and any other answer changes it. Every other setting reworked here -- the
  # permission set, gVisor, the Web UI, memory, the model provider -- inherits
  # this way and still asks.
  #
  # SLACK_ENABLED (with SLACK_BOT_TOKEN / SLACK_APP_TOKEN and the other SLACK_*
  # variables) is the non-interactive spelling of the Slack interview, the same
  # variables the Day-2 menu reads. Without it Slack would be reachable only
  # through a controlling tty.
  #
  # Read through is_truthy, not string-compared against the lowercase literal.
  # Every boolean the generator writes goes through hcl_bool -> is_truthy, which
  # accepts True/yes/y/1/on; these two were the only ones that did not, and
  # install.env is a file the documentation now tells operators to hand-write.
  # A string compare against the lowercase literal would read
  # `GOOGLE_CHAT_ENABLED=True` as off, drop chat_choice to 4 and plan the
  # Pub/Sub topic away, while upgrade.sh read the same file as enabled. The
  # sibling booleans fail loudly on their ^(true|false)$ validators instead;
  # only these two are silent.
  local chat_choice=""
  if is_truthy "$PARAM_ENABLE_GOOGLE_CHAT" && is_truthy "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"; then
    chat_choice="3"
  elif is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"; then
    chat_choice="1"
  elif is_truthy "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"; then
    chat_choice="2"
  fi
  # Nothing configured and nobody to ask: "None", as before. Left unset when
  # there IS someone to ask, so prompt_menu falls back to option 1 for a first
  # install exactly as it used to.
  if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
    chat_choice="${chat_choice:-4}"
  fi
  prompt_menu "Select Chat Channel Integration(s):" \
    "Google Chat (Pub/Sub Event Streaming)" \
    "Slack (Socket Mode App)" \
    "Both Google Chat and Slack" \
    "None (CLI & REST API Gateway only)" \
    chat_choice

  local google_chat_enabled="false"
  local slack_enabled="false"
  # Empty by default: the allowlist is opt-in, and an unset list allows all users.
  # PARAM_ALLOWED_USERS carries both --allowed-users and the loaded ALLOWED_USERS,
  # so an install that had an allowlist keeps it on a re-run that says nothing.
  local allowed_users="${PARAM_ALLOWED_USERS:-}"
  local allowed_users_hint=""
  if [ -z "$allowed_users" ]; then
    allowed_users_hint="empty list"
  fi
  local chat_topic_name="$PARAM_CHAT_TOPIC_NAME"
  local chat_sub_name="${CHAT_SUB_NAME:-$DEFAULT_CHAT_SUB_NAME}"
  local google_chat_mode="$PARAM_GOOGLE_CHAT_MODE"
  if [[ ! "$google_chat_mode" =~ ^(default|debug)$ ]]; then
    print_error "--google-chat-mode must be either 'default' or 'debug'."
    exit 1
  fi
  # Seeded from the environment so the non-interactive path can carry the
  # Slack settings: prompt_read keeps a non-empty current value there.
  local slack_bot_token="${SLACK_BOT_TOKEN:-}"
  local slack_app_token="${SLACK_APP_TOKEN:-}"
  local slack_allowed_users="${SLACK_ALLOWED_USERS:-}"
  local slack_home_channel="${SLACK_HOME_CHANNEL:-}"
  local slack_home_channel_name="${SLACK_HOME_CHANNEL_NAME:-}"

  # One definition for both arms that ask it. Arms 2 and 3 ran identical
  # copies, and the copies are what drifted: the "pass the current value, not
  # a bare empty string" fix landed on the GitOps prompts a screen below and
  # not here.
  #
  # Each prompt takes its OWN current value as the default. A bare "" only
  # looks harmless -- prompt_read keeps a non-empty current value on the
  # non-interactive path (install.sh:998), but the interactive branch applies
  # the default argument, and `[ -z "$input_val" ] && [ -n "$default_val" ]`
  # is false when that default is empty, so it falls through and assigns the
  # empty string, so a bare "" default clears the tokens, the home channel and
  # the Slack allowlist. The tokens are usually
  # rescued by the Secret-recovery loop in installer_common.sh; the allowlist
  # is not, and an empty slack_allowed_users means every workspace member may
  # talk to the agent.
  #
  # The allowlist also took $allowed_users -- the GOOGLE CHAT list -- which on
  # arm 3 replaced the Slack allowlist with the Chat one.
  _prompt_slack_settings() {
    # A secret must not be echoed back as a visible "[default: xoxb-…]", so the
    # tokens pass a label instead of letting prompt_read print the value.
    local bot_hint="" app_hint="" slack_allowed_hint=""
    [ -n "$slack_bot_token" ] && bot_hint="keep existing"
    [ -n "$slack_app_token" ] && app_hint="keep existing"
    # Same shape as allowed_users_hint above: an empty list has to read as a
    # deliberate choice rather than as a missing default.
    [ -z "$slack_allowed_users" ] && slack_allowed_hint="empty list"
    prompt_read "Slack Bot Token (xoxb-...)" slack_bot_token "$slack_bot_token" true "$bot_hint"
    prompt_read "Slack App Token (xapp-...)" slack_app_token "$slack_app_token" true "$app_hint"
    prompt_read "Allowed Slack User IDs / Emails (comma-separated)" \
      slack_allowed_users "$slack_allowed_users" false "$slack_allowed_hint"
    prompt_read "Slack Home Channel ID (optional, e.g. C0123456789)" \
      slack_home_channel "$slack_home_channel"
    prompt_read "Slack Home Channel Name (optional, e.g. #gke-alerts)" \
      slack_home_channel_name "$slack_home_channel_name"
  }

  case "$chat_choice" in
    1)
      google_chat_enabled="true"
      prompt_read "Allowed User Email(s) for Google Chat (comma-separated, empty allows all users)" \
        allowed_users "$allowed_users" false "$allowed_users_hint"
      prompt_read "Pub/Sub Topic Name for Google Chat" chat_topic_name "$chat_topic_name"
      ;;
    2)
      slack_enabled="true"
      _prompt_slack_settings
      ;;
    3)
      google_chat_enabled="true"
      slack_enabled="true"
      prompt_read "Allowed User Email(s) for Google Chat (comma-separated, empty allows all users)" \
        allowed_users "$allowed_users" false "$allowed_users_hint"
      prompt_read "Pub/Sub Topic Name for Google Chat" chat_topic_name "$chat_topic_name"
      _prompt_slack_settings
      ;;
    4)
      print_info "Chat integrations disabled. Agent will operate via CLI / REST API Gateway."
      ;;
  esac

  # 7. LLM Model Provider Selection & API Key Auto-Discovery
  print_step "7. AI Model Provider Credentials"
  local model_provider="$PARAM_MODEL_PROVIDER"
  if ! is_valid_model_provider "$model_provider"; then
    print_error "Unsupported model provider '$model_provider'. Use gemini, vertex_ai, anthropic, or openai."
    exit 1
  fi
  local model_default_name="${PARAM_MODEL_DEFAULT_NAME:-${MODEL_DEFAULT_NAME:-}}"
  if [ -z "$model_default_name" ]; then
    model_default_name="$(default_model_for_provider "$model_provider")"
  fi

  # Vertex authenticates with Workload Identity rather than an API key, so these
  # two are the only credentials it needs. The project defaults to the install
  # target; the location does not, because a model is only callable from a
  # location that serves it and the cluster's region often is not one — see
  # DEFAULT_VERTEX_LOCATION in scripts/installer/installer_common.sh.
  local vertex_project_id="${PARAM_VERTEX_PROJECT_ID:-$project_id}"
  local vertex_location="${PARAM_VERTEX_LOCATION:-$DEFAULT_VERTEX_LOCATION}"
  # Loud like --gvisor and --enable-web-ui, not lenient like the chat booleans:
  # a typo read as false would silently skip the two serving-project resources,
  # and the first sign would be the gateway's 403 on its first model call.
  local vertex_manage_serving_project="${PARAM_VERTEX_MANAGE_SERVING_PROJECT:-$DEFAULT_VERTEX_MANAGE_SERVING_PROJECT}"
  if [[ ! "$vertex_manage_serving_project" =~ ^(true|false)$ ]]; then
    print_error "--vertex-manage-serving-project must be either true or false."
    exit 1
  fi

  local detected_gemini_key="${PARAM_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}"
  if [ -z "$detected_gemini_key" ]; then
    detected_gemini_key=$(gcloud secrets versions access latest --secret="gemini-api-key" --project="$project_id" 2>/dev/null || echo "")
  fi
  local gemini_api_key="${detected_gemini_key:-}"
  local openai_api_key="${PARAM_OPENAI_API_KEY:-}"
  local anthropic_api_key="${PARAM_ANTHROPIC_API_KEY:-}"

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    # Pre-set to the provider already configured, so pressing enter keeps it.
    # Every arm below assigns unconditionally, so an unseeded menu would reset
    # the configured provider.
    local model_choice=""
    case "$model_provider" in
      gemini) model_choice="1" ;;
      vertex_ai) model_choice="2" ;;
      openai) model_choice="3" ;;
      anthropic) model_choice="4" ;;
    esac
    prompt_menu "Select Model Provider for the Platform Agent:" \
      "Google Gemini (Recommended: $(default_model_for_provider gemini) / Gemini API)" \
      "Google Vertex AI / Model Garden (no API key — Workload Identity)" \
      "OpenAI ($(default_model_for_provider openai) / OpenAI API)" \
      "Anthropic ($(default_model_for_provider anthropic) / Anthropic API)" \
      model_choice

    # A model the install already pins survives a re-run that leaves the
    # provider alone; changing provider has to take the new provider's default,
    # because the old model name is not valid for it. An arm that assigned
    # unconditionally would downgrade a pinned MODEL_DEFAULT_NAME to the
    # provider default on a re-run that changed nothing.
    local model_provider_was="$model_provider"
    local model_name_was="$model_default_name"
    case "$model_choice" in
      1)
        model_provider="gemini"
        if [ "$model_provider_was" != "gemini" ] || [ -z "$model_name_was" ]; then
          model_default_name="$(default_model_for_provider gemini)"
        fi
        local detected_key="${GEMINI_API_KEY:-}"
        if [ -z "$detected_key" ]; then
          detected_key=$(gcloud secrets versions access latest --secret="gemini-api-key" --project="$project_id" 2>/dev/null || echo "")
        fi
        prompt_read "Gemini API Key" gemini_api_key "$detected_key" true
        ;;
      2)
        model_provider="vertex_ai"
        prompt_read "Vertex AI Project ID" vertex_project_id "$vertex_project_id"
        prompt_read "Vertex AI Location" vertex_location "$vertex_location"
        local vertex_model_default
        vertex_model_default="$(default_model_for_provider vertex_ai)"
        if [ "$model_provider_was" = "vertex_ai" ] && [ -n "$model_name_was" ]; then
          vertex_model_default="$model_name_was"
        fi
        prompt_read "Vertex Model ID (publisher model, e.g. gemini-3.5-flash)" model_default_name "$vertex_model_default"
        ;;
      3)
        model_provider="openai"
        if [ "$model_provider_was" != "openai" ] || [ -z "$model_name_was" ]; then
          model_default_name="$(default_model_for_provider openai)"
        fi
        prompt_read "OpenAI API Key" openai_api_key "${OPENAI_API_KEY:-}" true
        ;;
      4)
        model_provider="anthropic"
        if [ "$model_provider_was" != "anthropic" ] || [ -z "$model_name_was" ]; then
          model_default_name="$(default_model_for_provider anthropic)"
        fi
        prompt_read "Anthropic API Key" anthropic_api_key "${ANTHROPIC_API_KEY:-}" true
        ;;
    esac
  fi

  case "$model_provider" in
    gemini)
      [ -n "$gemini_api_key" ] || print_warning "No Gemini API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
    vertex_ai)
      print_info "Vertex AI needs no API key: LiteLLM authenticates as ${LITELLM_GSA_NAME:-kubeagents-litellm-gsa}@${project_id}.iam.gserviceaccount.com via Workload Identity."
      print_info "Serving ${model_default_name} from projects/${vertex_project_id}/locations/${vertex_location}."
      if [ "$vertex_manage_serving_project" != "true" ]; then
        print_info "The install will not touch project ${vertex_project_id}. Enable aiplatform.googleapis.com there and grant roles/aiplatform.user to ${LITELLM_GSA_NAME:-$LITELLM_GSA_DEFAULT_NAME}@${project_id}.iam.gserviceaccount.com yourself; model calls fail until you do."
        print_info "If an earlier apply of this install created that grant, remove both serving-project resources from Terraform state before continuing, or this apply revokes it — terraform/examples/full-install/README.md names the two addresses."
      fi
      # The literal, not $DEFAULT_VERTEX_LOCATION: this warns about a property
      # of the global endpoint, not about the default being in effect. Tying it
      # to the constant would fire with false text if the default ever moved to
      # a region, and stay silent for an explicit --vertex-location=global.
      #
      # An `if` rather than `[ ... ] && ...`: the AND-list form returns non-zero
      # whenever the test fails, which is a live hazard under this file's
      # `set -Eeuo pipefail` the moment it becomes the last statement in a
      # function. The `||` idiom used elsewhere in this case block always
      # returns 0; the `&&` form does not.
      if [ "$vertex_location" = "global" ]; then
        print_warning "The global endpoint gives no in-region ML processing guarantee. Pass --vertex-location=<region> if you have a data-residency requirement."
      fi
      ;;
    openai)
      [ -n "$openai_api_key" ] || print_warning "No OpenAI API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
    anthropic)
      [ -n "$anthropic_api_key" ] || print_warning "No Anthropic API key was provided; the agent will require a credential update before model calls can succeed."
      ;;
  esac

  # 8. GitOps Infrastructure Repository Connection
  print_step "8. GitOps Infrastructure Repository Setup"
  local github_org="${PARAM_GITOPS_ORG:-}"
  local github_repo="$PARAM_GITOPS_REPO"
  # Env fallbacks, not bare empties: the non-interactive path never reaches
  # the interview prompts below, so GITHUB_APP_ID / GITHUB_PEM_PATH exported
  # into the run are the only way an automated install can enable the minter.
  local github_app_id="${GITHUB_APP_ID:-}"
  local kms_keyring="${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}"
  local kms_key="${KMS_KEY:-$DEFAULT_KMS_KEY}"
  local github_pem_path="${GITHUB_PEM_PATH:-}"

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    # An install that already names an org has a repository to connect, so
    # option 2 is what pressing enter should mean. Options 1 and 2 run the same
    # block, so this only makes the offered wording match the install — what
    # actually keeps GITOPS_ORG and the minter credentials across a re-run is
    # that each prompt below defaults to the loaded value.
    local gitops_choice=""
    if [ -n "$github_org" ]; then
      gitops_choice="2"
    fi
    prompt_menu "Would you like to connect or create a GitOps repo for automated PRs?" \
      "Create a NEW GitHub Repository automatically (Recommended)" \
      "Connect an EXISTING GitHub Repository" \
      "Skip for now (Can be enabled later)" \
      gitops_choice

    if [ "$gitops_choice" = "1" ] || [ "$gitops_choice" = "2" ]; then
      # The repo must be organization-owned: the token minter resolves App
      # installations at /orgs/{org}/installation, which does not exist for
      # personal accounts. So the default offered here is the operator's first
      # organization, never their login — suggesting a username would guarantee
      # the failure below.
      local detected_gh_org=""
      detected_gh_org=$(gh api user/orgs -q '.[0].login' 2>/dev/null || echo "")
      print_info "The GitOps repo must belong to a GitHub organization; a personal account cannot"
      print_info "mint tokens. A free organization is enough."
      # Every default below is the loaded value first, the project default or
      # the probe second. prompt_read assigns the empty input when its default
      # is empty, so passing a bare "" here meant pressing enter through the
      # interview cleared GITHUB_APP_ID and the PEM path on an install that had
      # them — write_tfvars_from_state's three-way guard then set
      # enable_github_minter = false and the apply removed the minter.
      while true; do
        prompt_read "GitHub Organization" github_org "${github_org:-$detected_gh_org}"

        local org_problem=""
        if [ -z "$github_org" ]; then
          org_problem="A GitHub organization is required to connect a GitOps repo."
        elif ! is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
          case "$(github_account_type "$github_org")" in
            organization) ;;
            user) org_problem="'${github_org}' is a personal GitHub account, not an organization. The token minter cannot mint tokens for it." ;;
            missing) org_problem="'${github_org}' does not exist on GitHub. Check the spelling." ;;
            *) print_warning "Could not reach GitHub to verify '${github_org}'; continuing." ;;
          esac
        fi
        [ -z "$org_problem" ] && break

        print_error "$org_problem"
        # The minter cannot mint tokens for a personal account, and a
        # non-organization owner would only surface as a failure after the
        # cluster, node pools and operator are already built. Settle it
        # here, while nothing has been created yet.
        if [ "$PARAM_NON_INTERACTIVE" = "true" ] || ! has_controlling_tty; then
          print_error "Set GITOPS_ORG to an organization and re-run, or export SKIP_GITHUB_ORG_CHECK=true to bypass this check."
          exit 1
        fi
      done
      prompt_read "GitOps Repository Name" github_repo "${github_repo:-$DEFAULT_GITOPS_REPO}"

      print_info "GitHub access uses the short-lived GitHub App token minter."
      prompt_read "GitHub App ID" github_app_id "${github_app_id}"
      prompt_read "Cloud KMS Keyring Name" kms_keyring "${kms_keyring:-$DEFAULT_KMS_KEYRING}"
      prompt_read "Cloud KMS Key Name" kms_key "${kms_key:-$DEFAULT_KMS_KEY}"
      prompt_read "Path to downloaded GitHub App Private Key (.pem)" github_pem_path "${github_pem_path}"
    fi
  fi

  # 9. Agent Permissions & Sandbox Isolation Boundary
  print_step "9. Agent Security & Runtime Isolation Boundary"
  local permission_set="$PARAM_PERMISSION_SET"
  # Normalise and keep the normalised value, the way common.sh does. The gate
  # below normalises its own argument so that every spelling reaches the right
  # message, but it cannot fix the caller's variable -- and everything
  # downstream compares against the lowercase literal: the custom-roles check
  # and the over-reach warning just below, the exported PLATFORM_AGENT_*
  # pair, and terraform's case-sensitive contains() on permission_set. Passing
  # `Custom` through raw would clear the gate and then miss all four.
  permission_set=$(printf '%s' "$permission_set" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  # require_supported_permission_set (installer_common.sh) is the one home for
  # the accepted vocabulary and for the explanation the removed admin bundle
  # gets -- a PLATFORM_AGENT_PERMISSION_SET inherited from a vars.sh or a CI
  # environment variable written before the removal lands here.
  require_supported_permission_set "$permission_set" || exit 1
  local custom_roles="${PARAM_CUSTOM_ROLES:-}"
  # This rule is also written in init_var_platform_agent_permission_set
  # (scripts/installer/common.sh), which has no caller left in the repository
  # -- the numbered provision scripts that used to invoke it went with #797. So
  # this is the only place it runs, not a duplicate of somewhere it also runs.
  if [ "$permission_set" = "custom" ] && [ "$PARAM_NON_INTERACTIVE" = "true" ] && [ -z "$custom_roles" ]; then
    print_error "--permission-set=custom requires --custom-roles with at least one role."
    exit 1
  fi
  if [ "$permission_set" = "custom" ] && [ -n "$custom_roles" ]; then
    warn_on_overreaching_custom_roles "$custom_roles"
  fi
  # No `:-` fallback: resolve_shared_defaults already applied
  # DEFAULT_ENABLE_GVISOR with ${VAR-...}, which leaves `--gvisor=` (set, but
  # empty) empty on purpose so the validator below rejects it instead of
  # silently reading it as the default.
  local enable_gvisor="$PARAM_ENABLE_GVISOR"
  if [[ ! "$enable_gvisor" =~ ^(true|false)$ ]]; then
    print_error "--gvisor must be either true or false."
    exit 1
  fi
  if [[ ! "$PARAM_ENABLE_WEBUI" =~ ^(true|false)$ ]]; then
    print_error "--enable-web-ui must be either true or false."
    exit 1
  fi
  # An agent that forgets every conversation is the worse default, so memory is
  # on unless it is turned off. The choice decides two things: whether the
  # harness keeps memory at all, and — when it does — whether that costs an
  # extra API server and Postgres database in the cluster. Nothing downstream
  # infers one from the other, so both are recorded.
  #
  # `file` is the default because it is what every install got before the
  # searchable store existed: an upgrade that says nothing about memory keeps
  # the store it already has, and no install grows a Postgres database it never
  # asked for. Enterprise deployments opt in with --memory=hindsight.
  local memory_mode="$PARAM_MEMORY"
  if [[ ! "$memory_mode" =~ ^(off|file|hindsight)$ ]]; then
    print_error "--memory must be one of: off, file, hindsight."
    exit 1
  fi
  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    # These are GCP IAM role bundles for the agent's GSA, nothing else. Kubernetes
    # RBAC stays read-only in every set, and the GitOps pull-request path works in
    # every set, so neither belongs in these labels. read-only leads because it is
    # the documented default and the only set that enforces no cloud-plane writes.
    # See docs/site/src/content/docs/reference/security-and-iam.md.
    # The "(Default)" tag follows the option enter keeps — the seeded one on a
    # re-run — for the reason the gVisor prompt below gives: a static tag on
    # option 1 contradicts what an empty answer does once a recorded setting
    # seeds the choice. The order stays fixed so the option numbers are stable.
    local perm_choice="" perm_tag_ro=" (Default)" perm_tag_custom=""
    if [ "$permission_set" = "custom" ]; then
      perm_choice="2"
      perm_tag_ro=""
      perm_tag_custom=" (Default)"
    fi
    prompt_menu "Select Platform Agent GCP IAM Permission Set:" \
      "read-only — auditing and observability, no GCP write capability${perm_tag_ro}" \
      "custom — exactly the roles you list, no built-in bundle${perm_tag_custom}" \
      perm_choice

    case "$perm_choice" in
      1) permission_set="read-only" ;;
      2) permission_set="custom" ;;
    esac

    while [ "$permission_set" = "custom" ] && [ -z "$custom_roles" ]; do
      prompt_read "Custom GCP IAM Roles (space- or comma-separated)" custom_roles ""
      if [ -z "$custom_roles" ]; then
        # An empty custom list would only be rejected once the cluster and
        # operator are already provisioned; catch it at the prompt.
        print_error "The custom permission set needs at least one role, e.g. roles/container.viewer."
      fi
    done
    # Repeated rather than moved: the call above runs on the --custom-roles flag
    # path, which is decided before this prompt exists. An operator who runs
    # ./install.sh and types the roles in reaches only this one.
    if [ "$permission_set" = "custom" ] && [ -n "$custom_roles" ]; then
      warn_on_overreaching_custom_roles "$custom_roles"
    fi

    # prompt_menu answers an empty line with option 1, so the current value has
    # to be listed first — otherwise the "(Default)" label contradicts what a
    # bare Enter actually produces. The value reaching here is the sandbox
    # unless --gvisor=false said otherwise, so the usual order is Yes first;
    # the else branch keeps an explicit --gvisor=false from being re-enabled by
    # someone confirming the prompt. Option 2 is "the other one" either way.
    local gvisor_choice=""
    local gvisor_yes="Yes - gVisor Secure Kernel Sandbox (Hardened Workload Isolation)"
    local gvisor_no="No - Standard Container Runtime"
    local gvisor_prompt="Enable GKE Sandbox (gVisor) Runtime Isolation for Agent Workloads?"
    if [ "$enable_gvisor" = "true" ]; then
      prompt_menu "$gvisor_prompt" "${gvisor_yes} (Default)" "$gvisor_no" gvisor_choice
      if [ "$gvisor_choice" = "2" ]; then
        enable_gvisor="false"
      fi
    else
      prompt_menu "$gvisor_prompt" "${gvisor_no} (Default)" "$gvisor_yes" gvisor_choice
      if [ "$gvisor_choice" = "2" ]; then
        enable_gvisor="true"
      fi
    fi

    # Ordered by the current value, as the gVisor prompt above is: the
    # "(Default)" tag, the enter key and the resulting value have to agree.
    # Every branch assigns, so either answer takes effect.
    local webui_yes="Yes - Enabled for local browser debugging (port 9119)"
    local webui_no="No - Disabled for reduced attack surface"
    local webui_prompt="Enable Hermes Web UI (Port 9119 Dashboard) for Agent Observability?"
    local webui_choice=""
    if is_truthy "$PARAM_ENABLE_WEBUI"; then
      prompt_menu "$webui_prompt" "${webui_yes} (Default)" "$webui_no" webui_choice
      if [ "$webui_choice" = "2" ]; then
        PARAM_ENABLE_WEBUI="false"
      else
        PARAM_ENABLE_WEBUI="true"
      fi
    else
      prompt_menu "$webui_prompt" "${webui_no} (Default)" "$webui_yes" webui_choice
      if [ "$webui_choice" = "2" ]; then
        PARAM_ENABLE_WEBUI="true"
      else
        PARAM_ENABLE_WEBUI="false"
      fi
    fi

    # The two stores differ in what they cost to run and in how far they scale,
    # and the label says which so the choice can be made without reading a design
    # doc: the file store adds no services but is loaded into the model's context
    # whole on every turn, so it is bounded by the window; Hindsight retrieves only
    # what a question needs, at the price of an API server and a database.
    #
    # The file store is listed first because it is the one an install should get
    # for saying nothing — it is what installs got before the searchable store
    # existed, and it is the only option that adds no services to the cluster.
    # An install that already chose otherwise seeds its own option below, so
    # "saying nothing" on a re-run means keeping what is there, not taking the
    # first entry, so that omitting --memory cannot delete a Hindsight
    # deployment.
    # The "(Default)" tag follows the option enter keeps, like the permission-set
    # prompt above: on a re-run that seeded hindsight or off, a static tag on the
    # file store would claim enter does something it does not. The order stays
    # fixed so the option numbers are stable.
    local memory_choice="" mem_tag_file="" mem_tag_hind="" mem_tag_off=""
    case "$memory_mode" in
      file) memory_choice="1" ;;
      hindsight) memory_choice="2" ;;
      off) memory_choice="3" ;;
    esac
    case "$memory_choice" in
      2) mem_tag_hind=" (Default)" ;;
      3) mem_tag_off=" (Default)" ;;
      *) mem_tag_file=" (Default)" ;;
    esac
    prompt_menu "Should the agent remember things between conversations?" \
      "Files on the agent's own disk${mem_tag_file} - For small or personal deployments. Per-user Markdown, no extra services to run, does not scale past a few pages" \
      "Searchable store${mem_tag_hind} - For enterprise deployments. Ranked recall that scales, deploys Hindsight (API + Postgres) into the cluster" \
      "No${mem_tag_off} - Nothing is retained once a session ends" \
      memory_choice

    # Every branch assigns, rather than letting option 1 fall through to
    # --memory=: an answer given at the prompt is the more recent instruction of
    # the two, and the permission-set and gVisor prompts above already work this way.
    case "$memory_choice" in
      1) memory_mode="file" ;;
      2) memory_mode="hindsight" ;;
      3) memory_mode="off" ;;
    esac
  fi

  # bootstrap_install_env_file records PARAM_MEMORY, not this local, so the
  # answer has to travel back. Without it bootstrap_install_env_file would
  # record MEMORY=file for an install that chose Hindsight, and the next run
  # would tear down what this one built.
  PARAM_MEMORY="$memory_mode"

  # MEMORY_PROVIDER carries the whole choice — including "no memory at all",
  # which is what `none` means. Everything downstream reads it and nothing else:
  # provisioning step 13 deploys Hindsight only for a Hindsight-backed provider,
  # the specialist overlay blanks anything that cannot be made read-only, and the
  # entrypoint gates the one-way file import the same way.
  #
  # MEMORY_ENABLED is a different switch and stays false. It turns on Hermes'
  # *built-in* MEMORY.md/USER.md, which has no per-user scoping and would sit
  # alongside whichever provider is chosen — two competing stores in front of one
  # agent. Every provider here replaces it rather than supplementing it. Nothing
  # about memory keys off this flag, so an upgrade cannot read a false left in an
  # old vars.sh as "this install wanted no memory".
  #
  # `none` rather than an empty string: the choice has to survive the trip
  # through the CR, and an absent provider takes the CRD default. The operator
  # translates `none` back to Hermes' own spelling — see MEMORY_PROVIDER_CHOICES
  # in scripts/installer/common.sh.
  #
  # `multiuser_memory` is the default provider everywhere it is named with no
  # install to ask (the CRD default, common.sh, and both profiles' config.yaml),
  # and `file` is what an install that says nothing about memory gets — the same
  # store those installs already had before the searchable one existed.
  local memory_enabled="false"
  # memory_provider_from_mode (installer_common.sh) owns the mode → provider
  # table; upgrade.sh and the Day-2 menu resolve the same pair through it, and a
  # second copy here is how the three drift. It returns empty for a mode it does
  # not recognise, which is what the fallback covers.
  local memory_provider
  memory_provider="$(memory_provider_from_mode "$memory_mode")"
  [ -n "$memory_provider" ] || memory_provider="$DEFAULT_MEMORY_PROVIDER"

  print_step "10. Resolving Install Configuration"
  local registry_prefix="${PARAM_REGISTRY_PREFIX%/}"
  if [ -z "$registry_prefix" ] || [[ "$registry_prefix" == *"://"* ]]; then
    print_error "--registry-prefix must be a non-empty registry path without a URL scheme."
    exit 1
  fi
  # Empty is the default and means "upstream", so only the scheme is rejected.
  local third_party_registry_prefix="${PARAM_THIRD_PARTY_REGISTRY_PREFIX%/}"
  if [[ "$third_party_registry_prefix" == *"://"* ]]; then
    print_error "--third-party-registry-prefix must be a registry path without a URL scheme."
    exit 1
  fi

  # Whatever the loaded configuration carries, and nothing invented here. When
  # it carries none, write_tfvars_from_state tries the live Secret first and
  # mints one only if that fails — see KUBE_AGENTS_GENERATE_API_SERVER_KEY,
  # exported below. Generating one here instead would replace the live Secret
  # on every re-run and restart the pods holding it.
  local api_server_key="${API_SERVER_KEY:-}"

  # Straight into the environment, which is where write_tfvars_from_state and
  # the TF_VAR_* handoff read from. Nothing is persisted here: install.env is
  # an input, and a file that is both read as configuration and written as
  # findings has two answers for one question.
  #
  # Four values are deliberately absent, because they are derived and a stored
  # copy can only disagree with the live answer.
  # PROJECT_NUMBER comes from `gcloud projects describe` and KMS_LOCATION from
  # derive_kms_location, both re-run every time they are needed; the effective
  # CLUSTER_MODE and create_cluster come from the generator's own probe of the
  # live cluster. NO_CONFIRM is gone too: it is a property of this invocation,
  # set by -y/--non-interactive, not configuration to inherit.
  export PROJECT_ID="$project_id"
  export PROJECT_NUMBER="$project_number"
  export CLUSTER_NAME="$cluster_name"
  export CLUSTER_MODE="$cluster_mode"
  export REGION="$region"
  export ENABLE_GVISOR="$enable_gvisor"
  # No GVISOR_POOL_NAME. It has no flag and no interview question, so anything
  # exported here would be a constant written over whatever install.env says --
  # the generator already applies DEFAULT_GVISOR_POOL_NAME when nothing sets it,
  # which leaves the operator's value free to win. The same reasoning keeps
  # ENABLE_GKE_BACKUP_PLAN out of this block.
  export MODEL_PROVIDER="$model_provider"
  export MODEL_DEFAULT_NAME="$model_default_name"
  export VERTEX_PROJECT_ID="$vertex_project_id"
  export VERTEX_LOCATION="$vertex_location"
  export VERTEX_MANAGE_SERVING_PROJECT="$vertex_manage_serving_project"
  export GEMINI_API_KEY="$gemini_api_key"
  export OPENAI_API_KEY="$openai_api_key"
  export ANTHROPIC_API_KEY="$anthropic_api_key"
  export ALLOWED_USERS="$allowed_users"
  export CHAT_TOPIC_NAME="$chat_topic_name"
  export CHAT_SUB_NAME="$chat_sub_name"
  export GOOGLE_CHAT_ENABLED="$google_chat_enabled"
  export GOOGLE_CHAT_MODE="$google_chat_mode"
  export SLACK_ENABLED="$slack_enabled"
  export SLACK_BOT_TOKEN="$slack_bot_token"
  export SLACK_APP_TOKEN="$slack_app_token"
  export SLACK_ALLOWED_USERS="$slack_allowed_users"
  export SLACK_HOME_CHANNEL="$slack_home_channel"
  export SLACK_HOME_CHANNEL_NAME="$slack_home_channel_name"
  export API_SERVER_KEY="$api_server_key"
  export PLATFORM_AGENT_PERMISSION_SET="$permission_set"
  export PLATFORM_AGENT_CUSTOM_ROLES="$custom_roles"
  export GITOPS_ORG="$github_org"
  export GITOPS_REPO="$github_repo"
  # One release of overlap: the agent runtime and the chart still speak
  # GITHUB_*, and normalize_gitops_repo_vars keeps them equal to the GITOPS_*
  # values rather than letting them be a second source of truth.
  normalize_gitops_repo_vars
  export GITHUB_APP_ID="$github_app_id"
  export KMS_KEYRING="$kms_keyring"
  export KMS_KEY="$kms_key"
  export GITHUB_PEM_PATH="$github_pem_path"
  export MEMORY_ENABLED="$memory_enabled"
  export MEMORY_PROVIDER="$memory_provider"
  export USER_PROFILE_ENABLED="$PARAM_USER_PROFILE_ENABLED"
  export HERMES_DASHBOARD_ENABLED="$PARAM_ENABLE_WEBUI"
  export REGISTRY_PREFIX="$registry_prefix"
  export ENABLE_PUBSUB_PLATFORM="${PARAM_ENABLE_PUBSUB_PLATFORM:-false}"
  export ENABLE_STOCKOUT_INVESTIGATOR="${PARAM_ENABLE_STOCKOUT_INVESTIGATOR:-false}"
  # Exported only when asked for, the way it was only ever persisted when asked
  # for: an empty value here is an override the installer never took a flag
  # for, turning "leave the third-party images upstream" from a default into an
  # instruction.
  if [ -n "$third_party_registry_prefix" ]; then
    export THIRD_PARTY_REGISTRY_PREFIX="$third_party_registry_prefix"
  fi
  # No *_IMAGE variables. The operator reads OPERATOR_IMAGE and
  # PLATFORM_AGENT_IMAGE from its own pod environment, where the chart sets them
  # from values.yaml. The images this install pulls are decided by
  # REGISTRY_PREFIX above and the image_tag the tfvars generator writes.

  local tfvars_file
  tfvars_file="$(tf_compose_dir "$repo_dir")/terraform.tfvars"
  # install.sh is the one front door allowed to mint an API_SERVER_KEY, and only
  # after the generator has tried the live Secret. upgrade.sh and uninstall.sh
  # leave this unset so an unfindable key stays an error for them.
  KUBE_AGENTS_GENERATE_API_SERVER_KEY=true \
    write_tfvars_from_state "$tfvars_file" "$image_tag"
  print_success "Terraform input saved to: $tfvars_file"

  # Written once, and only when there is nothing there. The probed cluster
  # shape is deliberately NOT recorded: a file that is read as configuration
  # and also written as findings has two answers for one question. The probe is
  # authoritative on every run regardless of what the file says, which is what
  # stops a hand-written CLUSTER_MODE=standard from planning a live Autopilot
  # cluster's replacement.
  bootstrap_install_env_file "$INSTALL_ENV_FILE" "$image_tag"

  # Pre-Flight Summary & Final Confirmation Checkpoint
  print_step "11. Pre-Flight Configuration Summary"
  echo -e "${C_CYAN}${C_BOLD}"
  draw_separator
  echo -e "${C_RESET}${C_BOLD}Please review your selections before provisioning begins:${C_RESET}"
  echo -e "  • ${C_CYAN}GCP Target Project:${C_RESET} ${C_BOLD}${project_id}${C_RESET} (Project Number: ${project_number:-unknown})"
  # The generator's answer, not the interview's: on an existing cluster it
  # probed the live shape and the flag had no say.
  echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${C_BOLD}${cluster_name}${C_RESET} (${region}, GKE $(cluster_mode_label "${TFVARS_CLUSTER_MODE:-$cluster_mode}"))"
  echo -e "  • ${C_CYAN}gVisor Sandbox Isolation:${C_RESET} ${enable_gvisor}"
  echo -e "  • ${C_CYAN}AI Model Provider:${C_RESET} ${model_provider} (${model_default_name})"
  if [ "$model_provider" = "vertex_ai" ]; then
    echo -e "  • ${C_CYAN}Vertex AI Endpoint:${C_RESET} projects/${vertex_project_id}/locations/${vertex_location}"
  fi
  echo -e "  • ${C_CYAN}Permission Boundary:${C_RESET} ${permission_set}"
  echo -e "  • ${C_CYAN}Long-Term Memory:${C_RESET} ${memory_mode}"
  # Only shown for a mirrored install: on a default one both lines restate the
  # defaults. The second line is the one worth seeing before confirming, because
  # a mirror that covers only the first-party images fails at cert-manager, with
  # the cluster already built.
  if [ "$registry_prefix" != "$DEFAULT_REGISTRY_PREFIX" ] || [ -n "$third_party_registry_prefix" ]; then
    echo -e "  • ${C_CYAN}Container Registry:${C_RESET} ${registry_prefix}"
    echo -e "  • ${C_CYAN}Third-Party Images:${C_RESET} ${third_party_registry_prefix:-upstream registries (quay.io, ghcr.io, docker.io, us-docker.pkg.dev)}"
  fi
  if [ -n "$github_org" ] && [ -n "$github_repo" ]; then
    echo -e "  • ${C_CYAN}GitOps Infrastructure Repo:${C_RESET} https://github.com/${github_org}/${github_repo}"
  fi
  echo -e "${C_CYAN}${C_BOLD}"
  draw_separator
  echo -e "${C_RESET}"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    # A real resource preview, not just a config write: validate always, and
    # plan when Application Default Credentials exist. Local state only —
    # a dry run must not create the state bucket.
    print_info "Dry-run: validating the Terraform configuration (local state; nothing is created)."
    (
      cd "$(tf_compose_dir "$repo_dir")"
      terraform init -backend=false -input=false >/dev/null
      terraform validate >/dev/null
    )
    print_success "Terraform configuration is valid."
    if gcloud auth application-default print-access-token >/dev/null 2>&1; then
      print_info "Previewing the resources a real run would create (terraform plan)..."
      (
        cd "$(tf_compose_dir "$repo_dir")"
        terraform plan -input=false -lock=false
      )
    else
      print_warning "No Application Default Credentials; skipping the resource preview (terraform plan)."
      print_info "Run 'gcloud auth application-default login' for a full dry-run preview."
    fi
    print_success "Dry-run execution complete! Configuration generated without touching cloud resources."
    write_json_report "DRY_RUN_SUCCESS"
    exit 0
  fi

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    local confirm_choice=""
    prompt_read "\nProceed with automated GKE cluster & Platform Agent provisioning? (Y/n)" confirm_choice "y"
    if [[ ! "$confirm_choice" =~ ^[Yy]$ ]]; then
      print_warning "Provisioning paused by user. Configuration saved to: $INSTALL_ENV_FILE"
      print_info "To launch provisioning later, run: ${C_BOLD}cd terraform/examples/full-install && KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply${C_RESET}"
      write_json_report "PAUSED"
      exit 0
    fi
  fi

  # 12. Execute the Terraform Engine
  print_step "12. Applying the Install (Terraform + Helm)"
  print_info "Provisioning GCP APIs, GKE Cluster, cert-manager, Operator, LiteLLM gateway, and Platform Agent..."

  # Re-validate the GitOps org before spending an apply on it. The interview
  # already settled it interactively; this catches an install.env edited by hand
  # and the non-interactive flag path. Warns-only when GitHub is unreachable;
  # SKIP_GITHUB_ORG_CHECK=true bypasses it.
  check_github_org_is_organization "${GITOPS_ORG:-}"

  # The three script behaviours a data source cannot express: CMEK, the
  # Workload Identity pool, and NetworkPolicy enforcement on a cluster that
  # already exists. All are no-ops when the cluster does not exist yet or is
  # already configured.
  ensure_existing_cluster_cmek "$project_id" "$cluster_name" "$region"
  ensure_existing_cluster_workload_identity "$project_id" "$cluster_name" "$region"
  ensure_existing_cluster_network_policy "$project_id" "$cluster_name" "$region"

  # The App key import sits here — after the dry-run exit and the operator's
  # confirmation (it enables the KMS API, creates permanent key rings, and
  # uploads the key, none of which a preview or a declined run may do), and
  # before the apply, whose helm release waits on a minter that can only
  # pass readiness once the key is imported. The generator enabled the
  # minter on the promise of this import, so a failed one stops the run
  # here rather than wedging the apply.
  import_github_pem "$project_id" "$region"
  local minter_enabled_version=""
  minter_enabled_version="$({ gcloud kms keys versions list --key "${KMS_KEY:-$DEFAULT_KMS_KEY}" \
    --keyring "${KMS_KEYRING:-$DEFAULT_KMS_KEYRING}" \
    --location "$(derive_kms_location "$region")" --project "$project_id" \
    --filter='state=ENABLED' --format='value(name)' 2>/dev/null || true; } | head -1)"
  if grep -q '^enable_github_minter = true$' "$tfvars_file" 2>/dev/null && [ -z "$minter_enabled_version" ]; then
    print_error "The GitHub minter is enabled in the generated configuration, but its KMS signing key still has no ENABLED version — the apply would wait on a minter that can never become ready."
    print_info "Fix the App key import (see the messages above) and re-run, or unset GITHUB_APP_ID to install without the minter."
    exit 1
  fi

  local provisioning_log
  provisioning_log="/tmp/kube-agents-provision-$(date -u +%Y%m%dT%H%M%SZ).log"
  print_info "Provisioning output is also being saved to: ${C_BOLD}${provisioning_log}${C_RESET}"
  run_lifecycle_apply "$repo_dir" "$provisioning_log"

  # The one post-apply step Terraform cannot carry: the managed-OTel scope
  # (no provider field; the GitHub App key import runs BEFORE the apply,
  # since the minter's readiness depends on it and the apply waits on the
  # minter). The OTel scope is set only on a cluster this install created —
  # silently changing the telemetry scope of a cluster somebody else made is
  # not an install's call.
  if [ "${TFVARS_CREATE_CLUSTER:-true}" = "true" ]; then
    apply_managed_otel_scope "$project_id" "$cluster_name" "$region"
  else
    print_info "Existing cluster: leaving its managed-OTel scope untouched. Set it yourself if you want managed OTel collection: gcloud container clusters update $cluster_name --location $region --managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS"
  fi

  # 12. Workload & Pod Health Verification Checkpoint
  print_step "13. Verifying Workload & Pod Health"
  print_info "Verifying deployment rollouts in namespace 'kubeagents-system'..."
  GKE_DNS_ENDPOINT_FLAG=""
  gke_dns_endpoint_flag "$cluster_name" "$region" "$project_id" || true
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "$cluster_name" --location "$region" \
    --project "$project_id" $GKE_DNS_ENDPOINT_FLAG >/dev/null
  if ! kubectl get ns kubeagents-system >/dev/null 2>&1; then
    print_error "Namespace 'kubeagents-system' was not created. Installation is incomplete."
    exit 1
  fi
  local slow_rollouts=()
  # kube-agents-controller-manager, not kubeagents-: the chart prefixes the
  # operator Deployment with the release name.
  for deployment in kube-agents-controller-manager litellm platform-agent-gateway; do
    if ! wait_for_deployment_object "$deployment" kubeagents-system "$DEPLOYMENT_APPEAR_TIMEOUT_SECS"; then
      print_error "Expected deployment '$deployment' was not created within ${DEPLOYMENT_APPEAR_TIMEOUT_SECS}s."
      # platform-agent-gateway is the agent, and the sandbox is the one thing
      # that stops the operator writing it while leaving everything else
      # healthy: no gvisor RuntimeClass, no Deployment, and the reason is on the
      # CR rather than in any of the logs an operator would reach for first.
      if [ "$deployment" = "platform-agent-gateway" ] && [ "$enable_gvisor" = "true" ]; then
        print_info "The agent asks for the ${C_BOLD}gvisor${C_RESET} RuntimeClass; the operator will not create its Deployment until that RuntimeClass exists."
        print_info "Read the reason with: ${C_BOLD}kubectl get platformagent -n kubeagents-system -o jsonpath='{.items[*].status.conditions}'${C_RESET}"
        print_info "Re-run with ${C_BOLD}--gvisor=false${C_RESET} to run the agent on the standard container runtime instead."
      fi
      exit 1
    fi
    # The agent pulls a large image and waits on LiteLLM before it reports ready,
    # so a couple of minutes is normal. Running past the budget means "still
    # coming up", not "broken": say so and keep the summary below, which carries
    # the chat links and port-forward command.
    if ! wait_for_rollout "$deployment" kubeagents-system "$ROLLOUT_TIMEOUT_SECS"; then
      slow_rollouts+=("$deployment")
      print_warning "$deployment did not report ready within ${ROLLOUT_TIMEOUT_SECS}s."
    fi
  done
  if [ "${#slow_rollouts[@]}" -eq 0 ]; then
    print_success "All core control plane deployments are healthy and available!"
    write_json_report "SUCCESS"
  else
    print_warning "Still waiting on: ${slow_rollouts[*]}"
    print_info "Keep watching with: ${C_BOLD}kubectl rollout status deployment/${slow_rollouts[0]} -n kubeagents-system${C_RESET}"
    print_info "Inspect a stuck pod with: ${C_BOLD}kubectl describe pod -l app=${slow_rollouts[0]} -n kubeagents-system${C_RESET}"
    write_json_report "SUCCESS_PENDING_ROLLOUT"
  fi

  # 13. Installation Summary & Next Steps
  print_step "🎉 Installation Complete!"
  echo -e "${C_GREEN}${C_BOLD}"
  echo '============================================================================='
  echo '🏆  Kubernetes Agentic Harness (kube-agents) is Live & Operational!'
  echo '============================================================================='
  echo -e "${C_RESET}"

  echo -e "${C_BOLD}Component Status Summary:${C_RESET}"
  echo -e "  • ${C_CYAN}GCP Project:${C_RESET} ${project_id} (Project Number: ${project_number})"
  echo -e "  • ${C_CYAN}GKE Cluster:${C_RESET} ${cluster_name} (${region}, GKE $(cluster_mode_label "${TFVARS_CLUSTER_MODE:-$cluster_mode}"))"
  echo -e "  • ${C_CYAN}Runtime Isolation:${C_RESET} ${enable_gvisor:-false} (gVisor Sandbox)"
  echo -e "  • ${C_CYAN}Model Provider:${C_RESET} ${model_provider} (${model_default_name})"
  echo -e "  • ${C_CYAN}Permission Mode:${C_RESET} ${permission_set}"
  if [ "${google_chat_enabled:-false}" = "true" ]; then
    echo -e "  • ${C_CYAN}Google Chat Direct Bot Link:${C_RESET} ${C_UNDERLINE}https://chat.google.com/dm/${project_number}${C_RESET}"
    echo -e "  • ${C_CYAN}Google Chat App Console:${C_RESET} ${C_UNDERLINE}https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat?project=${project_id}${C_RESET}"
  fi
  if [ "${slack_enabled:-false}" = "true" ]; then
    echo -e "  • ${C_CYAN}Slack App Link:${C_RESET} ${C_UNDERLINE}https://app.slack.com/client${C_RESET}"
  fi
  if [ "$PARAM_ENABLE_WEBUI" = "true" ]; then
    echo -e "  • ${C_CYAN}Hermes Web UI (Port 9119):${C_RESET} ${C_GREEN}Enabled${C_RESET}"
    # A sandboxed pod cannot be reached with `kubectl port-forward`: the forward
    # is established in the host-side CNI netns while the dashboard listens in
    # the sandbox's own network stack, so the connection is refused. The relay
    # in scripts/exec_tunnel.py goes through `kubectl exec` instead; print
    # whichever one will actually work here.
    if [ "${enable_gvisor:-false}" = "true" ]; then
      echo -e "    ${C_YELLOW}Workstation Access Command:${C_RESET} ${repo_dir}/scripts/hermes-dashboard-tunnel.py"
      echo -e "      (the agent is sandboxed under gVisor, which kubectl port-forward cannot reach)"
    else
      echo -e "    ${C_YELLOW}Workstation Access Command:${C_RESET} kubectl port-forward deploy/platform-agent-gateway -n kubeagents-system 9119:9119"
    fi
    echo -e "    ${C_YELLOW}Browser Dashboard URL:${C_RESET} ${C_UNDERLINE}http://localhost:9119${C_RESET}"
  fi

  if [ "${google_chat_enabled:-false}" = "true" ]; then
    echo ""
    IMAGE_TAG="$image_tag" bash "${repo_dir}/scripts/installer/print_instructions_gchat.sh" || true
  fi
  if [ "${slack_enabled:-false}" = "true" ]; then
    echo ""
    IMAGE_TAG="$image_tag" bash "${repo_dir}/scripts/installer/print_instructions_slack.sh" || true
  fi
}

if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ]; then
  main "$@"
else
  echo "ℹ️ Sourced install.sh functions without executing main (KUBE_AGENTS_SOURCE_ONLY=true)." >&2
fi
