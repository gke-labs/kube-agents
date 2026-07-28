#!/usr/bin/env bash
# ==============================================================================
# Shared Bash Utilities for Provision & Teardown Pipeline
# ==============================================================================

# Determine paths relative to where this helper is loaded
if [ -z "${SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
VARS_FILE="${KUBE_AGENTS_VARS_FILE:-${SCRIPT_DIR}/vars.sh}"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_MAGENTA='\033[95m'
C_BLUE='\033[94m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'
C_WHITE='\033[97m'

# ─── UI Helpers ───────────────────────────────────────────────────────────────
print_step() { echo -e "\n${C_MAGENTA}${C_BOLD}>>>  $1  <<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓ $1${C_RESET}"; }
print_info() { echo -e "  ${C_CYAN}ℹ $1${C_RESET}"; }
print_warning() { echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"; }
print_error() { echo -e "  ${C_RED}✗ $1${C_RESET}"; }

wait_for_a_bit() {
  local seconds=$1
  local msg=$2
  local spinner=( "⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏" )
  echo -ne "  ${C_YELLOW}${msg} (${seconds}s)...  "
  tput civis 2>/dev/null || true
  for (( i=0; i<seconds*10; i++ )); do
    local idx=$(( i % 10 ))
    echo -ne "\b${spinner[$idx]}"
    sleep 0.1
  done
  echo -ne "\b ${C_RESET}\n"
  tput cnorm 2>/dev/null || true
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ${C_YELLOW}⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt...${C_RESET}" >&2
      sleep "$delay"
    fi
  done

  return 1
}

retry() {
  local max_retries=$1
  local delay=$2
  shift 2
  local count=0

  while [ $count -lt $max_retries ]; do
    count=$((count + 1))
    if "$@"; then
      return 0
    fi
    if [ $count -lt $max_retries ]; then
      echo -e "  ${C_YELLOW}⚠ [Retry $count/$max_retries] Waiting ${delay}s before next attempt...${C_RESET}" >&2
      sleep "$delay"
    fi
  done

  return 1
}

cleanup() { tput cnorm 2>/dev/null || true; }
trap cleanup EXIT

# ─── Universal Argument Parsing ──────────────────────────────────────────────
DRY_RUN=0
NO_CONFIRM=0
NON_INTERACTIVE=0
ADVANCED_SETUP=0
INTERACTION_MODE="${INTERACTION_MODE:-}"
READ_ONLY_CAPABILITIES_ARG_SET=0

parse_common_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --no-confirm|-y)
        NO_CONFIRM=1
        ;;
      --non-interactive)
        NON_INTERACTIVE=1
        ;;
      --advanced)
        ADVANCED_SETUP=1
        ;;
      --project=*)
        PROJECT_ID="${1#*=}"
        export PROJECT_ID
        ;;
      --project)
        if [ "$#" -lt 2 ]; then
          print_error "--project requires a value."
          return 2
        fi
        shift
        PROJECT_ID="$1"
        export PROJECT_ID
        ;;
      --cluster=*)
        CLUSTER_NAME="${1#*=}"
        export CLUSTER_NAME
        ;;
      --cluster)
        if [ "$#" -lt 2 ]; then
          print_error "--cluster requires a value."
          return 2
        fi
        shift
        CLUSTER_NAME="$1"
        export CLUSTER_NAME
        ;;
      --region=*)
        REGION="${1#*=}"
        export REGION
        ;;
      --region)
        if [ "$#" -lt 2 ]; then
          print_error "--region requires a value."
          return 2
        fi
        shift
        REGION="$1"
        export REGION
        ;;
      --permissions=*)
        PLATFORM_AGENT_PERMISSION_SET="${1#*=}"
        export PLATFORM_AGENT_PERMISSION_SET
        ;;
      --permissions)
        if [ "$#" -lt 2 ]; then
          print_error "--permissions requires a value."
          return 2
        fi
        shift
        PLATFORM_AGENT_PERMISSION_SET="$1"
        export PLATFORM_AGENT_PERMISSION_SET
        ;;
      --read-only-capabilities=*)
        PLATFORM_AGENT_READ_ONLY_CAPABILITIES="${1#*=}"
        READ_ONLY_CAPABILITIES_ARG_SET=1
        export PLATFORM_AGENT_READ_ONLY_CAPABILITIES
        ;;
      --read-only-capabilities)
        if [ "$#" -lt 2 ]; then
          print_error "--read-only-capabilities requires a value. Use 'none' for the minimum permission set."
          return 2
        fi
        shift
        PLATFORM_AGENT_READ_ONLY_CAPABILITIES="$1"
        READ_ONLY_CAPABILITIES_ARG_SET=1
        export PLATFORM_AGENT_READ_ONLY_CAPABILITIES
        ;;
      --model-provider=*)
        MODEL_PROVIDER="${1#*=}"
        export MODEL_PROVIDER
        ;;
      --model-provider)
        if [ "$#" -lt 2 ]; then
          print_error "--model-provider requires a value."
          return 2
        fi
        shift
        MODEL_PROVIDER="$1"
        export MODEL_PROVIDER
        ;;
      --model=*)
        MODEL_DEFAULT_NAME="${1#*=}"
        export MODEL_DEFAULT_NAME
        ;;
      --model)
        if [ "$#" -lt 2 ]; then
          print_error "--model requires a value."
          return 2
        fi
        shift
        MODEL_DEFAULT_NAME="$1"
        export MODEL_DEFAULT_NAME
        ;;
      --interaction=*)
        INTERACTION_MODE="${1#*=}"
        export INTERACTION_MODE
        ;;
      --interaction)
        if [ "$#" -lt 2 ]; then
          print_error "--interaction requires a value."
          return 2
        fi
        shift
        INTERACTION_MODE="$1"
        export INTERACTION_MODE
        ;;
      --agent-name=*)
        PLATFORM_AGENT_NAME="${1#*=}"
        export PLATFORM_AGENT_NAME
        ;;
      --agent-name)
        if [ "$#" -lt 2 ]; then
          print_error "--agent-name requires a value."
          return 2
        fi
        shift
        PLATFORM_AGENT_NAME="$1"
        export PLATFORM_AGENT_NAME
        ;;
      --agent-ksa=*)
        PLATFORM_AGENT_KSA_NAME="${1#*=}"
        export PLATFORM_AGENT_KSA_NAME
        ;;
      --agent-ksa)
        if [ "$#" -lt 2 ]; then
          print_error "--agent-ksa requires a value."
          return 2
        fi
        shift
        PLATFORM_AGENT_KSA_NAME="$1"
        export PLATFORM_AGENT_KSA_NAME
        ;;
      --agent-gsa=*)
        PLATFORM_AGENT_GSA_NAME="${1#*=}"
        export PLATFORM_AGENT_GSA_NAME
        ;;
      --agent-gsa)
        if [ "$#" -lt 2 ]; then
          print_error "--agent-gsa requires a value."
          return 2
        fi
        shift
        PLATFORM_AGENT_GSA_NAME="$1"
        export PLATFORM_AGENT_GSA_NAME
        ;;
    esac
    shift
  done
}

parse_common_args "$@"

save_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi
  if [ -f "$VARS_FILE" ]; then
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
}

# ─── Boolean Parsing ──────────────────────────────────────────────────────────
# Interpret a value as a boolean toggle. Returns 0 (success) for common
# affirmative spellings and 1 otherwise. Matching is case-insensitive and
# surrounding whitespace is ignored, so all of the following are truthy:
#   true, yes, y, 1, on  (in any letter case, e.g. "True", "YES", "On")
# Everything else — including false, no, n, 0, off, and empty/unset — is falsy.
is_truthy() {
  local val="${1:-}"
  val="${val//[[:space:]]/}"
  case "$val" in
    [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss] | [Yy] | 1 | [Oo][Nn]) return 0 ;;
    *) return 1 ;;
  esac
}

is_ci_pipeline() {
  is_truthy "${CI:-}"
}

init_var() {
  local var_name=$1
  local default_val=$2
  local prompt_msg=$3
  local current_val="${!var_name:-}"
  if [ -z "$current_val" ]; then
    local final_val
    if [ "${DRY_RUN:-0}" -eq 1 ] || [ "${NON_INTERACTIVE:-0}" -eq 1 ] || is_ci_pipeline; then
      final_val="$default_val"
    else
      echo -ne "  ${C_CYAN}${prompt_msg} [${C_WHITE}${default_val}${C_CYAN}]: ${C_RESET}"
      read -r input_val
      final_val="${input_val:-$default_val}"
    fi
    export "${var_name}=${final_val}"
    save_var "$var_name" "$final_val"
  fi
}

init_var_model_provider() {
  init_var "MODEL_PROVIDER" "gemini" "Enter Model Provider (gemini, anthropic, chatgpt, openai)"

  MODEL_PROVIDER=$(echo "$MODEL_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [[ ! "$MODEL_PROVIDER" =~ ^(gemini|anthropic|chatgpt|openai)$ ]]; then
    print_error "Invalid Model Provider '$MODEL_PROVIDER'. Must be one of: gemini, anthropic, chatgpt, openai."
    exit 1
  fi

  init_var "MODEL_DEFAULT_NAME" "$(default_model_for_provider "$MODEL_PROVIDER")" "Enter Model Default Name"
}

default_model_for_provider() {
  case "${1:-gemini}" in
    chatgpt|openai)
      echo "gpt-5.4"
      ;;
    anthropic)
      echo "claude-sonnet-4-5-20250929"
      ;;
    *)
      echo "gemini-3.5-flash"
      ;;
  esac
}

init_var_platform_agent_permission_set() {
  init_var "PLATFORM_AGENT_PERMISSION_SET" "read-only" "Enter Platform Agent Permission Set (read-only, gke-admin, custom)"

  PLATFORM_AGENT_PERMISSION_SET=$(echo "$PLATFORM_AGENT_PERMISSION_SET" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [[ ! "$PLATFORM_AGENT_PERMISSION_SET" =~ ^(read-only|gke-admin|custom)$ ]]; then
    print_error "Invalid Platform Agent Permission Set '$PLATFORM_AGENT_PERMISSION_SET'. Must be one of: read-only, gke-admin, custom."
    exit 1
  fi

  if [ "$PLATFORM_AGENT_PERMISSION_SET" = "custom" ]; then
    init_var "PLATFORM_AGENT_CUSTOM_ROLES" "" "Enter Custom GCP IAM Roles (space or comma-separated)"
    if [ -z "${PLATFORM_AGENT_CUSTOM_ROLES:-}" ]; then
      print_error "Custom permission set selected, but PLATFORM_AGENT_CUSTOM_ROLES is empty."
      exit 1
    fi
  fi
}

# The credential proxy runs `gcloud container clusters get-credentials`.
# Kubernetes Engine Cluster Viewer is therefore the fixed minimum GCP role:
# it can locate and connect to the cluster. The operator's Kubernetes RBAC
# controls which Kubernetes objects the agent may read.
platform_agent_required_read_only_roles() {
  echo "roles/container.clusterViewer"
}

# Keep each option as an id|role|label|hint record. This follows OpenClaw's
# data-driven wizard pattern while remaining dependency-free in Bash.
platform_agent_read_only_capability_records() {
  cat <<'EOF'
monitoring|roles/monitoring.viewer|Cloud Monitoring|Read metrics, dashboards, alert policies, and uptime checks.
logging|roles/logging.viewer|Cloud Logging|Read logs and log metadata.
iam-inspection|roles/iam.securityReviewer|IAM policy inspection|Read IAM policies, roles, and service-account metadata.
mcp-tools|roles/mcp.toolUser|Google Cloud MCP tools|Invoke supported Google Cloud MCP tools.
service-account-use|roles/iam.serviceAccountUser|Service account attachment|Allow iam.serviceAccounts.actAs; enable only when a workflow must attach a service account.
EOF
}

platform_agent_optional_read_only_role_is_supported() {
  local candidate=$1
  local id role label hint
  while IFS='|' read -r id role label hint; do
    if [ "$candidate" = "$role" ]; then
      return 0
    fi
  done < <(platform_agent_read_only_capability_records)
  return 1
}

platform_agent_role_list_contains() {
  local list=$1
  local candidate=$2
  local role
  for role in ${list//,/ }; do
    if [ "$role" = "$candidate" ]; then
      return 0
    fi
  done
  return 1
}

normalize_platform_agent_optional_read_only_roles() {
  local configured_roles="${1:-}"
  local normalized=""
  local role
  for role in ${configured_roles//,/ }; do
    if ! platform_agent_optional_read_only_role_is_supported "$role"; then
      print_warning "Ignoring unsupported optional read-only role '${role}'." >&2
      continue
    fi
    if ! platform_agent_role_list_contains "$normalized" "$role"; then
      normalized="${normalized:+${normalized} }${role}"
    fi
  done
  echo "$normalized"
}

platform_agent_capabilities_to_roles() {
  local requested="${1:-none}"
  requested=$(echo "$requested" | tr '[:upper:]' '[:lower:]')
  if [ -z "$requested" ] || [ "$requested" = "none" ]; then
    echo ""
    return 0
  fi

  local selected_roles=""
  local token id role label hint found
  for token in ${requested//,/ }; do
    found=0
    while IFS='|' read -r id role label hint; do
      if [ "$token" = "$id" ]; then
        if ! platform_agent_role_list_contains "$selected_roles" "$role"; then
          selected_roles="${selected_roles:+${selected_roles} }${role}"
        fi
        found=1
        break
      fi
    done < <(platform_agent_read_only_capability_records)
    if [ "$found" -ne 1 ]; then
      print_error "Unknown read-only capability '${token}'."
      return 1
    fi
  done
  echo "$selected_roles"
}

platform_agent_optional_roles_to_capabilities() {
  local selected_roles="${1:-}"
  local selected_ids=""
  local id role label hint
  while IFS='|' read -r id role label hint; do
    if platform_agent_role_list_contains "$selected_roles" "$role"; then
      selected_ids="${selected_ids:+${selected_ids},}${id}"
    fi
  done < <(platform_agent_read_only_capability_records)
  echo "${selected_ids:-none}"
}

remove_platform_agent_role_from_list() {
  local list=$1
  local unwanted=$2
  local result=""
  local role
  for role in $list; do
    if [ "$role" != "$unwanted" ]; then
      result="${result:+${result} }${role}"
    fi
  done
  echo "$result"
}

prompt_platform_agent_read_only_capabilities() {
  local selected_roles
  selected_roles="$(normalize_platform_agent_optional_read_only_roles "${PLATFORM_AGENT_READ_ONLY_ROLES:-}")"

  while true; do
    echo ""
    echo -e "${C_CYAN}${C_BOLD}Optional read-only capabilities${C_RESET}"
    echo -e "  ${C_GREEN}✓ Required:${C_RESET} GKE cluster connection (roles/container.clusterViewer)"
    echo -e "  Toggle optional capabilities by number. Press Enter to accept the current selection."
    echo ""

    local index=1
    local id role label hint mark
    while IFS='|' read -r id role label hint; do
      mark=" "
      if platform_agent_role_list_contains "$selected_roles" "$role"; then
        mark="x"
      fi
      printf "  [%s] %d) %s\n      %s\n" "$mark" "$index" "$label" "$hint"
      index=$((index + 1))
    done < <(platform_agent_read_only_capability_records)

    echo ""
    echo -ne "  ${C_CYAN}Toggle numbers (comma-separated), or press Enter to continue: ${C_RESET}"
    local input
    read -r input || input=""
    if [ -z "${input//[[:space:]]/}" ]; then
      break
    fi

    local token target_index
    for token in ${input//,/ }; do
      local maximum_selection=$((index - 1))
      if [[ ! "$token" =~ ^[0-9]+$ ]] ||
        [ "$token" -lt 1 ] ||
        [ "$token" -gt "$maximum_selection" ]; then
        print_warning "Ignoring invalid selection '${token}'. Choose a number from 1 to ${maximum_selection}."
        continue
      fi
      target_index=1
      while IFS='|' read -r id role label hint; do
        if [ "$target_index" -eq "$token" ]; then
          if platform_agent_role_list_contains "$selected_roles" "$role"; then
            selected_roles="$(remove_platform_agent_role_from_list "$selected_roles" "$role")"
          else
            selected_roles="${selected_roles:+${selected_roles} }${role}"
          fi
          break
        fi
        target_index=$((target_index + 1))
      done < <(platform_agent_read_only_capability_records)
    done
  done

  save_var "PLATFORM_AGENT_READ_ONLY_ROLES" "$selected_roles"
}

configure_platform_agent_read_only_capabilities() {
  local arg_was_set=${1:-0}
  local requested_capabilities="${2:-}"
  if [ "${PLATFORM_AGENT_PERMISSION_SET:-read-only}" != "read-only" ]; then
    return 0
  fi

  if [ "$arg_was_set" -eq 1 ]; then
    local requested_roles
    requested_roles="$(platform_agent_capabilities_to_roles "$requested_capabilities")" || return 1
    save_var "PLATFORM_AGENT_READ_ONLY_ROLES" "$requested_roles"
  elif [ "${NON_INTERACTIVE:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
    save_default_var "PLATFORM_AGENT_READ_ONLY_ROLES" ""
  else
    prompt_platform_agent_read_only_capabilities
  fi
}

get_platform_agent_read_only_roles() {
  local required_roles
  required_roles="$(platform_agent_required_read_only_roles)"
  local optional_roles
  optional_roles="$(normalize_platform_agent_optional_read_only_roles "${PLATFORM_AGENT_READ_ONLY_ROLES:-}")"
  echo "${required_roles}${optional_roles:+ ${optional_roles}}"
}

save_default_var() {
  local var_name=$1
  local default_val=$2
  if [ -z "${!var_name:-}" ]; then
    save_var "$var_name" "$default_val"
  fi
}

normalize_interaction_mode() {
  local mode="${INTERACTION_MODE:-}"
  if [ -z "$mode" ]; then
    if is_truthy "${GOOGLE_CHAT_ENABLED:-false}" && is_truthy "${SLACK_ENABLED:-false}"; then
      mode="both"
    elif is_truthy "${GOOGLE_CHAT_ENABLED:-false}"; then
      mode="google-chat"
    elif is_truthy "${SLACK_ENABLED:-false}"; then
      mode="slack"
    else
      mode="api"
    fi
  fi
  mode=$(echo "$mode" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  case "$mode" in
    api|api-only|none)
      INTERACTION_MODE="api"
      save_var "GOOGLE_CHAT_ENABLED" "false"
      save_var "SLACK_ENABLED" "false"
      ;;
    google-chat|gchat)
      INTERACTION_MODE="google-chat"
      save_var "GOOGLE_CHAT_ENABLED" "true"
      save_var "SLACK_ENABLED" "false"
      ;;
    slack)
      INTERACTION_MODE="slack"
      save_var "GOOGLE_CHAT_ENABLED" "false"
      save_var "SLACK_ENABLED" "true"
      ;;
    both)
      INTERACTION_MODE="both"
      save_var "GOOGLE_CHAT_ENABLED" "true"
      save_var "SLACK_ENABLED" "true"
      ;;
    *)
      print_error "Invalid interaction '$mode'. Must be one of: api, google-chat, slack, both."
      return 1
      ;;
  esac
  export INTERACTION_MODE
  save_var "INTERACTION_MODE" "$INTERACTION_MODE"
}

apply_standard_provision_defaults() {
  save_var "PROVISION_SETUP" "standard"
  save_default_var "PLATFORM_AGENT_PERMISSION_SET" "read-only"
  save_default_var "MODEL_PROVIDER" "gemini"
  save_default_var "MODEL_DEFAULT_NAME" "$(default_model_for_provider "$MODEL_PROVIDER")"
  save_default_var "ENABLE_GVISOR" "false"
  save_default_var "GVISOR_POOL_NAME" "gvisor-pool"
  save_default_var "AGENT_IMAGE" "ghcr.io/gke-labs/kube-agents/platform-agent"
  save_default_var "AGENT_TAG" "latest"
  save_default_var "MEMORY_ENABLED" "false"
  save_default_var "MEMORY_PROVIDER" "multiuser_memory"
  save_default_var "USER_PROFILE_ENABLED" "false"
  save_default_var "INFERENCE_REPLAY_ENABLED" "false"
  save_default_var "REPLAY_IMAGE" "ghcr.io/gke-labs/kube-agents/replay-proxy:latest"
  save_default_var "KMS_KEYRING" "github-token-minter-keyring"
  save_default_var "KMS_KEY" "github-token-minter-key"
  normalize_interaction_mode
}

print_provision_summary() {
  local gsa_email="${PLATFORM_AGENT_GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
  echo ""
  echo -e "${C_CYAN}${C_BOLD}Provisioning plan${C_RESET}"
  echo -e "  Project:      ${C_WHITE}${PROJECT_ID}${C_RESET}"
  echo -e "  Cluster:      ${C_WHITE}${CLUSTER_NAME}${C_RESET}"
  echo -e "  Region:       ${C_WHITE}${REGION}${C_RESET}"
  echo -e "  Permissions:  ${C_WHITE}${PLATFORM_AGENT_PERMISSION_SET}${C_RESET}"
  if [ "$PLATFORM_AGENT_PERMISSION_SET" = "read-only" ]; then
    echo -e "  Required IAM: ${C_WHITE}roles/container.clusterViewer${C_RESET}"
    echo -e "  Optional IAM: ${C_WHITE}$(platform_agent_optional_roles_to_capabilities "${PLATFORM_AGENT_READ_ONLY_ROLES:-}")${C_RESET}"
  fi
  echo -e "  Agent:        ${C_WHITE}${PLATFORM_AGENT_NAME}${C_RESET}"
  echo -e "  Identity:     ${C_WHITE}managed (${gsa_email})${C_RESET}"
  echo -e "  Model:        ${C_WHITE}${MODEL_PROVIDER} / ${MODEL_DEFAULT_NAME}${C_RESET}"
  echo -e "  Interaction:  ${C_WHITE}${INTERACTION_MODE:-configured in integration steps}${C_RESET}"
  echo -e "  gVisor:       ${C_WHITE}${ENABLE_GVISOR:-false}${C_RESET}"
  echo ""
}

confirm_provision_plan() {
  if [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${NON_INTERACTIVE:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
    return 0
  fi
  echo -ne "  ${C_CYAN}Apply this configuration? [${C_WHITE}Y${C_CYAN}/n]: ${C_RESET}"
  read -r reply
  case "${reply:-y}" in
    [Nn]|[Nn][Oo])
      print_info "Provisioning cancelled."
      return 1
      ;;
  esac
}

validate_agent_identity_names() {
  if [ "${#PLATFORM_AGENT_NAME}" -gt 63 ] ||
    [[ ! "${PLATFORM_AGENT_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    print_error "Invalid PlatformAgent name '${PLATFORM_AGENT_NAME}'. Use a lowercase Kubernetes DNS label of at most 63 characters."
    return 1
  fi
  if [ "${#PLATFORM_AGENT_KSA_NAME}" -gt 63 ] ||
    [[ ! "${PLATFORM_AGENT_KSA_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
    print_error "Invalid Kubernetes ServiceAccount name '${PLATFORM_AGENT_KSA_NAME}'. Use a lowercase DNS label of at most 63 characters."
    return 1
  fi
  if [ "${#PLATFORM_AGENT_GSA_NAME}" -gt 30 ] ||
    [[ ! "${PLATFORM_AGENT_GSA_NAME}" =~ ^[a-z]([-a-z0-9]{4,28}[a-z0-9])$ ]]; then
    print_error "Invalid Google Service Account name '${PLATFORM_AGENT_GSA_NAME}'. Use 6-30 lowercase letters, digits, or hyphens."
    return 1
  fi
}

collect_provision_configuration() {
  print_step "Configuring installation"
  local requested_project="${PROJECT_ID:-}"
  local requested_cluster="${CLUSTER_NAME:-}"
  local requested_region="${REGION:-}"
  local requested_permissions="${PLATFORM_AGENT_PERMISSION_SET:-}"
  local requested_read_only_capabilities_arg_set="${READ_ONLY_CAPABILITIES_ARG_SET:-0}"
  local requested_read_only_capabilities="${PLATFORM_AGENT_READ_ONLY_CAPABILITIES:-}"
  local requested_model_provider="${MODEL_PROVIDER:-}"
  local requested_model="${MODEL_DEFAULT_NAME:-}"
  local requested_interaction="${INTERACTION_MODE:-}"
  local requested_agent_name="${PLATFORM_AGENT_NAME:-}"
  local requested_ksa_name="${PLATFORM_AGENT_KSA_NAME:-}"
  local requested_gsa_name="${PLATFORM_AGENT_GSA_NAME:-}"
  load_state

  [ -z "$requested_project" ] || save_var "PROJECT_ID" "$requested_project"
  [ -z "$requested_cluster" ] || save_var "CLUSTER_NAME" "$requested_cluster"
  [ -z "$requested_region" ] || save_var "REGION" "$requested_region"
  [ -z "$requested_permissions" ] || save_var "PLATFORM_AGENT_PERMISSION_SET" "$requested_permissions"
  [ -z "$requested_model_provider" ] || save_var "MODEL_PROVIDER" "$requested_model_provider"
  if [ -n "$requested_model_provider" ] && [ -z "$requested_model" ]; then
    save_var "MODEL_DEFAULT_NAME" "$(default_model_for_provider "$requested_model_provider")"
  fi
  [ -z "$requested_model" ] || save_var "MODEL_DEFAULT_NAME" "$requested_model"
  if [ -n "$requested_agent_name" ]; then
    save_var "PLATFORM_AGENT_NAME" "$requested_agent_name"
    if [ -z "$requested_ksa_name" ] && [ "$requested_agent_name" != "platform-agent" ]; then
      requested_ksa_name="kubeagents-${requested_agent_name}"
    fi
    if [ -z "$requested_gsa_name" ] && [ "$requested_agent_name" != "platform-agent" ]; then
      requested_gsa_name="kubeagents-${requested_agent_name}-gsa"
      requested_gsa_name="${requested_gsa_name:0:30}"
      requested_gsa_name="${requested_gsa_name%-}"
    fi
  fi
  [ -z "$requested_ksa_name" ] || save_var "PLATFORM_AGENT_KSA_NAME" "$requested_ksa_name"
  [ -z "$requested_gsa_name" ] || save_var "PLATFORM_AGENT_GSA_NAME" "$requested_gsa_name"
  validate_agent_identity_names
  if [ -n "$requested_interaction" ]; then
    INTERACTION_MODE="$requested_interaction"
  fi

  local active_project
  active_project="$(gcloud config get-value project 2>/dev/null || true)"

  if [ "${ADVANCED_SETUP:-0}" -eq 1 ]; then
    save_var "PROVISION_SETUP" "advanced"
    init_var "PROJECT_ID" "${active_project:-$(whoami 2>/dev/null || echo user)}" "Enter Target GCP Project ID"
    init_var "CLUSTER_NAME" "platform-agent-host" "Enter GKE Cluster Name"
    init_var "REGION" "us-east4" "Enter GKE GCP Region"
    init_var_platform_agent_permission_set
    init_var_model_provider
    init_var "ENABLE_GVISOR" "false" "Enable GKE Sandbox (gVisor) runtime isolation? (true/false)"
    INTERACTION_MODE="${INTERACTION_MODE:-configured-later}"
  else
    save_default_var "PROJECT_ID" "${active_project:-$(whoami 2>/dev/null || echo user)}"
    save_default_var "CLUSTER_NAME" "platform-agent-host"
    save_default_var "REGION" "us-east4"
    apply_standard_provision_defaults
    init_var_platform_agent_permission_set
    init_var_model_provider
  fi

  configure_platform_agent_read_only_capabilities \
    "$requested_read_only_capabilities_arg_set" \
    "$requested_read_only_capabilities"
  print_provision_summary
  confirm_provision_plan
}


load_state() {
  if [ -f "$VARS_FILE" ]; then
    source "$VARS_FILE"
  elif [ "${DRY_RUN:-0}" -ne 1 ]; then
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
    source "$VARS_FILE"
  fi
  export NAMESPACE="${NAMESPACE:-kubeagents-system}"
  export PLATFORM_AGENT_NAME="${PLATFORM_AGENT_NAME:-platform-agent}"
  export PLATFORM_AGENT_KSA_NAME="${PLATFORM_AGENT_KSA_NAME:-kubeagents-platform-agent}"
  export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
  export PLATFORM_AGENT_GSA_NAME="${PLATFORM_AGENT_GSA_NAME:-kubeagents-platform-gsa}"
  export CONTROLLER_KSA_NAME="kubeagents-controller"
  export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
  export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
  export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
}

ensure_teardown_state() {
  if [ -f "$VARS_FILE" ]; then
    source "$VARS_FILE"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    export NAMESPACE="${NAMESPACE:-kubeagents-system}"
    export PLATFORM_AGENT_NAME="${PLATFORM_AGENT_NAME:-platform-agent}"
    export PLATFORM_AGENT_KSA_NAME="${PLATFORM_AGENT_KSA_NAME:-kubeagents-platform-agent}"
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="${PLATFORM_AGENT_GSA_NAME:-kubeagents-platform-gsa}"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  else
    echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
    local ACTIVE_PROJECT
    ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
    if [ "${DRY_RUN:-0}" -eq 1 ]; then
      export PROJECT_ID="${ACTIVE_PROJECT:-dummy-project}"
      export REGION="us-east4"
      export CLUSTER_NAME="platform-agent-host"
    else
      echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_PROJECT_ID
      export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
        exit 1
      fi
      export REGION="${REGION:-us-east4}"
      echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${REGION}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_REGION
      export REGION="${INPUT_REGION:-$REGION}"

      export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
      echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${CLUSTER_NAME}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_CLUSTER_NAME
      export CLUSTER_NAME="${INPUT_CLUSTER_NAME:-$CLUSTER_NAME}"
    fi
    export NAMESPACE="${NAMESPACE:-kubeagents-system}"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-platform-agent-chat-events}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
    else
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-}"
    fi
    export PLATFORM_AGENT_NAME="${PLATFORM_AGENT_NAME:-platform-agent}"
    export PLATFORM_AGENT_KSA_NAME="${PLATFORM_AGENT_KSA_NAME:-kubeagents-platform-agent}"
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="${PLATFORM_AGENT_GSA_NAME:-kubeagents-platform-gsa}"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  fi
}

# ─── Step Runner Framework ────────────────────────────────────────────────────
run_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}
  
  print_step "$name"
  echo -e "  ${C_CYAN}Verifying current state...${C_RESET}"
  
  if $verify_func; then
    print_success "Already completed: $name"
    return 0
  fi
  
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: $name"
    return 0
  fi

  print_info "Executing action..."
  if $execute_func; then
    print_success "Successfully executed."
    if [ "$wait_time" -gt 0 ]; then
      wait_for_a_bit "$wait_time" "Waiting for changes to propagate"
    fi
  else
    print_error "Failed to execute step: $name"
    exit 1
  fi
}

# ─── Smart Deployment Step Runner (Routes based on CI/CD mode) ────────────────
run_deploy_step() {
  local name=$1
  local verify_func=$2
  local execute_func=$3
  local wait_time=${4:-0}

  if is_ci_pipeline; then
    local force_redeploy_verify="false"
    run_step "$name" "$force_redeploy_verify" "$execute_func" "$wait_time"
  else
    run_step "$name" "$verify_func" "$execute_func" "$wait_time"
  fi
}

# ─── Cloud Helpers ────────────────────────────────────────────────────────────
check_prereqs() {
  for cmd in "$@"; do
    echo -ne "  ${C_CYAN}Checking for $cmd... ${C_RESET}"
    if command -v "$cmd" &> /dev/null; then
      echo -e "✅"
    else
      echo -e "❌"
      print_error "$cmd is required but not installed. Please install it and rerun."
      exit 1
    fi
  done
}

cluster_exists() {
  gcloud container clusters list --filter="name=${CLUSTER_NAME} AND location:${REGION}*" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo ""
}

connect_cluster() {
  print_info "Fetching cluster credentials..."
  gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet
}

ensure_k8s_resource_exists() {
  local resource=$1         # e.g., "deployment/cert-manager-cainjector"
  local namespace=$2        # e.g., "cert-manager"
  local retries=${3:-10}    # Default 10 retries (20s timeout)

  print_info "Checking existence of ${resource} in namespace '${namespace}'..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  _check_resource_exists() {
    kubectl get "${resource}" -n "${namespace}" &>/dev/null
  }

  if ! retry "$retries" 2 _check_resource_exists; then
    print_error "Timeout waiting for ${resource} to be created in '${namespace}'." >&2
    return 1
  fi
  print_success "${resource} exists in '${namespace}'."
}

wait_for_k8s_resource() {
  local resource=$1                 # e.g., "deployment/cert-manager"
  local namespace=$2                # e.g., "cert-manager"
  local condition=${3:-"Available"} # e.g., "Available"
  local timeout=${4:-"120s"}

  # Step 1: Ensure resource exists in API server etcd before calling 'kubectl wait'
  ensure_k8s_resource_exists "${resource}" "${namespace}" 10 || return 1

  print_info "Waiting for ${resource} in namespace '${namespace}' (condition=${condition})..."
  if [ "${DRY_RUN:-0}" -eq 1 ]; then return 0; fi

  # Step 2: Wait for condition availability
  kubectl wait --for="condition=${condition}" "${resource}" -n "${namespace}" --timeout="${timeout}" || return 1
  print_success "${resource} reached state: ${condition}."
}

confirm_action() {
  local warning_msg=$1
  shift
  
  if [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
    return 0
  fi
  
  echo ""
  echo -e "${C_RED}${C_BOLD}🚨 WARNING: ${warning_msg}${C_RESET}"
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  for item in "$@"; do
    local key="${item%%:*}"
    local val="${item#*:}"
    printf "  ${C_BOLD}%-15s${C_RESET} %s\n" "$key:" "$val"
  done
  echo -e "${C_YELLOW}==============================================================================${C_RESET}"
  echo ""
  echo -ne "  ${C_CYAN}Are you sure you want to proceed? (y/N): ${C_RESET}"
  read -r -n 1 REPLY
  echo
  if ! is_truthy "$REPLY"; then
      echo -e "  ${C_YELLOW}ℹ Aborted.${C_RESET}"
      exit 0
  fi
}

get_chatgpt_auth_info() {
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi

  # Wait for the deployment to be rolled out first
  kubectl rollout status deployment/litellm -n "${NAMESPACE:-kubeagents-system}" --timeout=60s >/dev/null 2>&1 || true

  # Retry a few times to allow LiteLLM to initialize and print the device code
  _check_litellm_logs() {
    local auth_info
    auth_info=$(kubectl logs deployment/litellm -n "${NAMESPACE:-kubeagents-system}" 2>/dev/null | awk '/Visit https:/ {u=$NF} /Enter code:/ {c=$NF} END {print u, c}') || true
    read -r CHATGPT_URL CHATGPT_CODE <<< "$auth_info"
    if [ -n "$CHATGPT_URL" ] && [ -n "$CHATGPT_CODE" ]; then
      export CHATGPT_URL CHATGPT_CODE
      return 0
    fi
    return 1
  }

  retry 15 1 _check_litellm_logs >/dev/null 2>&1 || true
}
