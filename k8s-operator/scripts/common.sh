#!/usr/bin/env bash
# ==============================================================================
# Shared Bash Utilities for Provision & Teardown Pipeline
# ==============================================================================

# Determine paths relative to where this helper is loaded
if [ -z "${SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
VARS_FILE="${SCRIPT_DIR}/vars.sh"

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
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=1 ;;
    --no-confirm|-y) NO_CONFIRM=1 ;;
  esac
done

save_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ -f "$VARS_FILE" ]; then
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
}

is_ci_pipeline() {
  if [ "${CI:-}" = "true" ] || [ "${CI:-}" = "1" ]; then
    return 0
  fi
  return 1
}

init_var() {
  local var_name=$1
  local default_val=$2
  local prompt_msg=$3
  local current_val="${!var_name:-}"
  if [ -z "$current_val" ]; then
    local final_val
    if [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline; then
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

  case "$MODEL_PROVIDER" in
    chatgpt|openai)
      DEFAULT_MODEL="gpt-5.4"
      ;;
    anthropic)
      DEFAULT_MODEL="claude-sonnet-4-5-20250929"
      ;;
    *)
      DEFAULT_MODEL="gemini-3.5-flash"
      ;;
  esac

  init_var "MODEL_DEFAULT_NAME" "$DEFAULT_MODEL" "Enter Model Default Name"
}

load_state() {
  if [ ! -f "$VARS_FILE" ]; then
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
  fi
  source "$VARS_FILE"
  export NAMESPACE="kubeagents-system"
  export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
  export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
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
    export NAMESPACE="kubeagents-system"
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
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
    export NAMESPACE="kubeagents-system"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    if [ "${GOOGLE_CHAT_ENABLED:-false}" = "true" ]; then
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-platform-agent-chat-events}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-platform-agent-chat-events-sub}"
    else
      export CHAT_TOPIC_NAME="${CHAT_TOPIC_NAME:-}"
      export CHAT_SUB_NAME="${CHAT_SUB_NAME:-}"
    fi
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
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
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
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
