#!/usr/bin/env bash
# ==============================================================================
# Shared Bash Utilities for Provision & Teardown Pipeline
# ==============================================================================

# Determine paths relative to where this helper is loaded
if [ -z "${SCRIPT_DIR:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# Honour a caller-provided path. Scripts under scripts/dev/ set SCRIPT_DIR to
# their own directory but keep the single state file in scripts/, so deriving
# the path from SCRIPT_DIR here would point them at a scripts/dev/vars.sh that
# load_state then creates empty — silently blanking IMAGE_TAG and AGENT_IMAGE.
VARS_FILE="${VARS_FILE:-${SCRIPT_DIR}/vars.sh}"

# Minimum tool versions. Sourced from the helper's own directory rather than
# SCRIPT_DIR, which callers under scripts/dev/ override to point at themselves.
# shellcheck source=k8s-operator/scripts/min_versions.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/min_versions.sh"

# ─── ANSI Colors ──────────────────────────────────────────────────────────────
# Empty unless stdout is a terminal and NO_COLOR is unset. This pipeline's output
# is routinely redirected — install.sh tees it to a log, CI captures it — and
# unconditional escapes turn those files into "^[[95m^[[1m>>> ..." noise. Every
# use is decorative interpolation, so empty values simply render plain text.
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
  C_CYAN='' C_GREEN='' C_YELLOW='' C_MAGENTA='' C_BLUE='' C_RED='' C_RESET='' C_BOLD='' C_WHITE=''
else
  C_CYAN='\033[96m'
  C_GREEN='\033[92m'
  C_YELLOW='\033[93m'
  C_MAGENTA='\033[95m'
  C_BLUE='\033[94m'
  C_RED='\033[91m'
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_WHITE='\033[97m'
fi

# Stable project-level discovery marker for the GKE cluster hosting kube-agents.
# Keep this value aligned with the Terraform full-install composition and admin portal.
KUBE_AGENTS_HOST_LABEL="kube-agents-host"

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

cleanup() { tput cnorm 2>/dev/null || true; }
trap cleanup EXIT

# ─── Universal Argument Parsing ──────────────────────────────────────────────
DRY_RUN="${DRY_RUN:-0}"
NO_CONFIRM="${NO_CONFIRM:-0}"
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
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi

  local old_umask
  old_umask=$(umask)
  umask 077

  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
    chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
    mv "$VARS_FILE.tmp" "$VARS_FILE"
  fi
  printf "export %s=%q\n" "$var_name" "$var_val" >> "$VARS_FILE"
  chmod 600 "$VARS_FILE" 2>/dev/null || true

  umask "$old_umask"
}

save_secret_var() {
  local var_name=$1
  local var_val=$2
  export "${var_name}=${var_val}"
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    return 0
  fi
  if is_truthy "${PERSIST_SECRETS_ON_DISK:-true}"; then
    save_var "$var_name" "$var_val"
  else
    if [ -f "$VARS_FILE" ]; then
      local old_umask
      old_umask=$(umask)
      umask 077
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$VARS_FILE" > "$VARS_FILE.tmp" 2>/dev/null || true
      chmod 600 "$VARS_FILE.tmp" 2>/dev/null || true
      mv "$VARS_FILE.tmp" "$VARS_FILE"
      chmod 600 "$VARS_FILE" 2>/dev/null || true
      umask "$old_umask"
    fi
  fi
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

# Checks if GKE databaseEncryption.state is a valid CMEK-encrypted state.
# Accepts an array of valid active encryption states:
#   - ENCRYPTED: Standard CMEK database encryption state in GKE
#   - ALL_OBJECTS_ENCRYPTION_ENABLED: Present in GKE 1.35+ when Application-layer Secrets Encryption is active
is_valid_cmek_encryption_state() {
  local state="${1:-}"
  local valid_states=(
    "ENCRYPTED"
    "ALL_OBJECTS_ENCRYPTION_ENABLED"
  )

  for valid in "${valid_states[@]}"; do
    if [ "$state" = "$valid" ]; then
      return 0
    fi
  done
  return 1
}

init_var() {
  local var_name=$1
  local default_val=$2
  local prompt_msg=$3
  local current_val="${!var_name:-}"
  if [ -z "$current_val" ]; then
    local final_val
    if is_non_interactive; then
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

# ─── Shared Provisioning Defaults ─────────────────────────────────────────────
# The values the per-step provision scripts and the zero-friction installer must
# agree on. install.sh sources this file rather than restating them, so each
# default has exactly one home and the two entry points cannot drift apart.
DEFAULT_CLUSTER_NAME="platform-agent-host"
DEFAULT_REGION="us-central1"
DEFAULT_MODEL_PROVIDER="gemini"

# Model provider → the model the pipeline defaults to for that provider.
default_model_for_provider() {
  case "${1:-}" in
    chatgpt | openai) echo "gpt-5.4" ;;
    anthropic) echo "claude-opus-5" ;;
    *) echo "gemini-3.5-flash" ;;
  esac
}

is_valid_model_provider() {
  [[ "${1:-}" =~ ^(gemini|vertex_ai|anthropic|chatgpt|openai)$ ]]
}

# The GCP IAM role bundles provision_04_gcp_iam.sh knows how to grant. Kubernetes
# RBAC is read-only in every one of them; see the site's reference/security-and-iam.
is_valid_permission_set() {
  [[ "${1:-}" =~ ^(read-only|gke-admin|custom)$ ]]
}

# ─── Container Registry ───────────────────────────────────────────────────────
# All kube-agents images (k8s-operator, platform-agent, credential-proxy,
# replay-proxy) default to this public registry prefix. Behind-the-firewall
# installs export REGISTRY_PREFIX to pull the mirrored images from a private
# registry instead; individual *_IMAGE variables still win over the prefix.
DEFAULT_REGISTRY_PREFIX="ghcr.io/gke-labs/kube-agents"

registry_prefix() {
  local prefix="${REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}"
  echo "${prefix%/}"
}

init_var_registry_prefix() {
  init_var "REGISTRY_PREFIX" "$DEFAULT_REGISTRY_PREFIX" "Enter Container Registry Prefix"
  case "$REGISTRY_PREFIX" in
    *"://"*)
      print_error "REGISTRY_PREFIX must be a bare registry path without a scheme (got '$REGISTRY_PREFIX'). Use e.g. 'registry.example.com/kube-agents'."
      exit 1
      ;;
  esac
  # init_var only saves values it prompted for; persist an env-exported
  # prefix too, so the remaining steps and later re-runs reuse it.
  save_var "REGISTRY_PREFIX" "$REGISTRY_PREFIX"
}

# Warn when a persisted *_IMAGE value no longer lives under the effective
# registry prefix — e.g. REGISTRY_PREFIX was exported after a first run
# already saved image defaults derived from another registry. The saved
# value still wins (state reuse), so surface the mixed state instead of
# silently applying it halfway.
warn_on_registry_prefix_mismatch() {
  local var_name=$1
  local image_val="${!var_name:-}"
  [ -z "$image_val" ] && return 0
  case "$image_val" in
    "$(registry_prefix)"/*) ;;
    *)
      print_warning "${var_name}='${image_val}' does not match REGISTRY_PREFIX '$(registry_prefix)'. The saved value wins; edit ${VARS_FILE} (or unset ${var_name}) to migrate this image to the new registry."
      ;;
  esac
}

# Cloud KMS has no zonal locations, so a zonal cluster's REGION (eg.
# "us-central1-c") is not a valid key location. REGION doubles as the cluster
# location, which for a zonal cluster must stay the zone, so KMS needs its own
# variable. Default to the enclosing region and allow an explicit override.
derive_kms_location() {
  local loc="${1:-}"
  if [[ "$loc" =~ ^(.+)-[a-z]$ ]]; then
    loc="${BASH_REMATCH[1]}"
  fi
  echo "$loc"
}

init_var_kms_location() {
  init_var "KMS_LOCATION" "$(derive_kms_location "${REGION:-}")" "Enter Cloud KMS Location (a region; zones are not valid)"
}

init_var_model_provider() {
  init_var "MODEL_PROVIDER" "$DEFAULT_MODEL_PROVIDER" "Enter Model Provider (gemini, vertex_ai, anthropic, chatgpt, openai)"

  MODEL_PROVIDER=$(echo "$MODEL_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if ! is_valid_model_provider "$MODEL_PROVIDER"; then
    print_error "Invalid Model Provider '$MODEL_PROVIDER'. Must be one of: gemini, vertex_ai, anthropic, chatgpt, openai."
    exit 1
  fi

  local DEFAULT_MODEL
  DEFAULT_MODEL="$(default_model_for_provider "$MODEL_PROVIDER")"

  init_var "MODEL_DEFAULT_NAME" "$DEFAULT_MODEL" "Enter Model Default Name"

  # Vertex has no API key; it needs a billing project and a serving location,
  # which is not always the cluster's region — Model Garden serves each partner
  # model from its own subset.
  if [ "$MODEL_PROVIDER" = "vertex_ai" ]; then
    init_var "VERTEX_PROJECT_ID" "${PROJECT_ID:-}" "Enter Vertex AI Project ID"
    init_var "VERTEX_LOCATION" "${REGION:-$DEFAULT_REGION}" "Enter Vertex AI Location"
  fi
}

init_var_platform_agent_permission_set() {
  init_var "PLATFORM_AGENT_PERMISSION_SET" "read-only" "Enter Platform Agent Permission Set (read-only, gke-admin, custom)"

  PLATFORM_AGENT_PERMISSION_SET=$(echo "$PLATFORM_AGENT_PERMISSION_SET" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if ! is_valid_permission_set "$PLATFORM_AGENT_PERMISSION_SET"; then
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

# ─── Memory Provider ──────────────────────────────────────────────────────────
# The accepted values for MEMORY_PROVIDER.
#
# Two of these ship in this repo, and the difference between them is the whole
# choice: `kube_agents_memory` wraps the upstream `hindsight` plugin and needs an
# API server and a Postgres database in the cluster, while `multiuser_memory`
# keeps a per-user Markdown file inside the pod and needs nothing at all. The
# rest are the external plugins Hermes ships — see `memory.provider` in its
# hermes_cli/config.py.
#
# `multiuser_memory` is the default because it is what this repo shipped before
# `kube_agents_memory` existed: re-running provisioning against an install that
# never chose a provider must not silently grow it a Postgres database.
#
# `none` is this installer's spelling of "no external provider — keep Hermes'
# built-in store". Hermes itself spells that as the empty string, but an empty
# string cannot survive the trip through the CR: an absent field takes the CRD
# default, and the operator only overrides a non-empty one. So the choice is
# carried as `none` and the operator translates it back to "" when it renders
# config.yaml.
MEMORY_PROVIDER_CHOICES="none kube_agents_memory multiuser_memory hindsight mem0 openviking holographic retaindb byterover"

init_var_memory_provider() {
  init_var "MEMORY_PROVIDER" "multiuser_memory" \
    "Enter agent memory provider (${MEMORY_PROVIDER_CHOICES// /, })"

  MEMORY_PROVIDER=$(echo "$MEMORY_PROVIDER" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')

  # Someone answering the prompt with a bare Enter after clearing the default
  # means "no memory", which is `none` here.
  if [ -z "$MEMORY_PROVIDER" ]; then
    MEMORY_PROVIDER="none"
  fi

  local choice valid=1
  for choice in $MEMORY_PROVIDER_CHOICES; do
    if [ "$MEMORY_PROVIDER" = "$choice" ]; then
      valid=0
      break
    fi
  done
  if [ "$valid" -ne 0 ]; then
    print_error "Invalid agent memory provider '$MEMORY_PROVIDER'. Must be one of: ${MEMORY_PROVIDER_CHOICES// /, }."
    exit 1
  fi

  # Persist the normalised value so the migration and the lower-casing stick,
  # and so the later steps that read vars.sh see what this step decided.
  save_var "MEMORY_PROVIDER" "$MEMORY_PROVIDER"
}

# True when the selected provider is backed by the in-cluster Hindsight service.
# `kube_agents_memory` wraps the upstream `hindsight` plugin, so both talk to the
# same API server and both need step 13 to have run; nothing else does.
memory_provider_uses_hindsight() {
  local provider
  provider=$(echo "${1:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  case "$provider" in
    kube_agents_memory | hindsight) return 0 ;;
    *) return 1 ;;
  esac
}

is_non_interactive() {
  [ ! -t 0 ] || [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline
}

# IMAGE_TAG is deliberately NOT persisted to vars.sh: the tag usually changes
# between deploys, so it is scoped to a single pipeline execution. provision.sh
# prompts once up front and exports it; the per-step scripts inherit it from
# the environment and only prompt when run standalone.
#
# Only the steps that deploy an image built from this repo need one, and they
# say so by setting REQUIRES_IMAGE_TAG=1 before calling load_state. Demanding it
# from every step made the secrets and integration steps — none of which mention
# IMAGE_TAG — fail outright in non-interactive mode.
init_var_image_tag() {
  if [ -z "${IMAGE_TAG:-}" ]; then
    if is_non_interactive; then
      print_error "IMAGE_TAG is required in non-interactive mode. Set it to an immutable release tag or validated commit SHA."
      exit 1
    else
      local default_tag="latest"
      echo -e "  ${C_CYAN}The base image tag is used for all images built from the kube-agents repo.${C_RESET}"
      echo -ne "  ${C_CYAN}Enter Base Image Tag (a commit SHA; 'latest' = latest commit on main) [${C_WHITE}${default_tag}${C_CYAN}]: ${C_RESET}"
      read -r input_tag
      export IMAGE_TAG="${input_tag:-$default_tag}"
    fi
  fi
}

load_state() {
  local env_registry_prefix="${REGISTRY_PREFIX:-}"
  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    source "$VARS_FILE"
  elif [ "${DRY_RUN:-0}" -ne 1 ]; then
    local old_umask
    old_umask=$(umask)
    umask 077
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    umask "$old_umask"
    source "$VARS_FILE"
  fi
  # Sourcing vars.sh restores the saved REGISTRY_PREFIX over a freshly
  # exported one (saved state wins, as for every knob). Say so instead of
  # silently ignoring the export.
  if [ -n "$env_registry_prefix" ] && [ -n "${REGISTRY_PREFIX:-}" ] \
    && [ "$env_registry_prefix" != "$REGISTRY_PREFIX" ]; then
    print_warning "Ignoring exported REGISTRY_PREFIX='${env_registry_prefix}': the saved value '${REGISTRY_PREFIX}' from ${VARS_FILE} wins. Edit ${VARS_FILE} (REGISTRY_PREFIX and the saved *_IMAGE values) to change registries."
  fi
  if [ "${REQUIRES_IMAGE_TAG:-0}" -eq 1 ]; then
    init_var_image_tag
  fi
  init_var_registry_prefix
  export NAMESPACE="kubeagents-system"
  export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
  export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
  export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
  export CONTROLLER_KSA_NAME="kubeagents-controller"
  export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
  export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
  export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
  export LITELLM_KSA_NAME="kubeagents-litellm"
  export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
}

ensure_teardown_state() {
  if [ -f "$VARS_FILE" ]; then
    chmod 600 "$VARS_FILE" 2>/dev/null || true
    source "$VARS_FILE"
    export GKE_DB_KMS_KEYRING="${GKE_DB_KMS_KEYRING:-}"
    export GKE_DB_KMS_KEY="${GKE_DB_KMS_KEY:-}"
    export GCP_ARTIFACT_REGISTRY_REPO_NAME="${GCP_ARTIFACT_REGISTRY_REPO_NAME:-${REPO_NAME:-kube-agents}}"
    export DEV_ARTIFACT_REGISTRY_CREATED="${DEV_ARTIFACT_REGISTRY_CREATED:-false}"
    export NAMESPACE="kubeagents-system"
    export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
    export LITELLM_KSA_NAME="kubeagents-litellm"
    export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
  else
    echo -e "  ${C_YELLOW}⚠ State file ${VARS_FILE} not found. Prompting for target values...${C_RESET}"
    local ACTIVE_PROJECT
    ACTIVE_PROJECT="$(gcloud config get-value project 2>/dev/null || echo "")"
    if is_non_interactive; then
      export PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT_ID:-${ACTIVE_PROJECT:-}}}"
      if [ -z "$PROJECT_ID" ] && [ "${DRY_RUN:-0}" -eq 1 ]; then
        export PROJECT_ID="dummy-project"
      fi
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required. Please export PROJECT_ID.${C_RESET}" >&2
        exit 1
      fi
      export REGION="${REGION:-${GCP_REGION:-$DEFAULT_REGION}}"
      export CLUSTER_NAME="${CLUSTER_NAME:-${GKE_CLUSTER_NAME:-$DEFAULT_CLUSTER_NAME}}"
    else
      echo -ne "  ${C_CYAN}Enter Target GCP Project ID [${C_WHITE}${ACTIVE_PROJECT}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_PROJECT_ID
      export PROJECT_ID="${INPUT_PROJECT_ID:-$ACTIVE_PROJECT}"
      if [ -z "$PROJECT_ID" ]; then
        echo -e "  ${C_RED}✗ Project ID is required.${C_RESET}"
        exit 1
      fi
      export REGION="${REGION:-$DEFAULT_REGION}"
      echo -ne "  ${C_CYAN}Enter GKE GCP Region [${C_WHITE}${REGION}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_REGION
      export REGION="${INPUT_REGION:-$REGION}"

      export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
      echo -ne "  ${C_CYAN}Enter GKE Cluster Name [${C_WHITE}${CLUSTER_NAME}${C_CYAN}]: ${C_RESET}"
      read -r INPUT_CLUSTER_NAME
      export CLUSTER_NAME="${INPUT_CLUSTER_NAME:-$CLUSTER_NAME}"
    fi
    export NAMESPACE="kubeagents-system"
    export GKE_DB_KMS_KEYRING="${GKE_DB_KMS_KEYRING:-}"
    export GKE_DB_KMS_KEY="${GKE_DB_KMS_KEY:-}"
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
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
    export LITELLM_KSA_NAME="kubeagents-litellm"
    export LITELLM_GSA_NAME="kubeagents-litellm-gsa"
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

# Classifies a GitHub account name against the public API, echoing exactly one
# of: organization | user | missing | unknown.
#
# "unknown" is the catch-all for every inconclusive answer — curl absent, the
# network down, rate limiting, an unexpected payload — so a caller can tell
# "GitHub says no" apart from "we could not ask". Never exits and never prints,
# so it is safe to call from an interactive prompt loop; callers decide whether
# an answer is fatal. install.sh uses it to validate before provisioning starts.
github_account_type() {
  local name="${1:-}"
  if [ -z "$name" ] || ! command -v curl &>/dev/null; then
    echo "unknown"
    return 0
  fi

  # Status is appended on its own line so a transport failure (curl non-zero)
  # stays distinguishable from an HTTP error (curl zero, status in the body).
  local response status body
  if ! response=$(curl -sS --max-time 10 -H "Accept: application/vnd.github+json" \
      -w '\n%{http_code}' "https://api.github.com/users/${name}" 2>/dev/null); then
    echo "unknown"
    return 0
  fi
  status="${response##*$'\n'}"
  body="${response%$'\n'*}"

  if [ "$status" = "404" ]; then
    echo "missing"
    return 0
  fi
  if [ "$status" != "200" ]; then
    echo "unknown"
    return 0
  fi

  # Organization is matched first so it wins even if the payload somehow carries
  # both spellings. Both spacings are covered because the API is not guaranteed
  # to keep pretty-printing, and no script here depends on jq.
  case "$body" in
    *'"type": "Organization"'*|*'"type":"Organization"'*) echo "organization" ;;
    *'"type": "User"'*|*'"type":"User"'*) echo "user" ;;
    *) echo "unknown" ;;
  esac
}

# Minty resolves App installations with GET /orgs/{org}/installation and has no
# fallback to the /users/{user}/installation endpoint that serves personal
# accounts, so a user-owned GitOps repo can never mint a token. Left unchecked
# that surfaces far downstream, as an HTTP 500 from a Minty that deployed and
# passed its readiness probes, so catch it while GITHUB_ORG is still being set.
#
# This exits, so it is the wrong entry point for anything that can still
# re-prompt: install.sh calls github_account_type directly and settles the value
# before provisioning starts. An inconclusive lookup is never fatal — an
# unreachable api.github.com must not block a provision that is otherwise fine.
check_github_org_is_organization() {
  local org="${1:-}"
  [ -z "$org" ] && return 0

  if is_truthy "${SKIP_GITHUB_ORG_CHECK:-false}"; then
    print_warning "SKIP_GITHUB_ORG_CHECK=true is set; not verifying that '${org}' is an organization."
    return 0
  fi

  case "$(github_account_type "$org")" in
    organization) return 0 ;;
    user)
      print_error "GITHUB_ORG='${org}' is a GitHub user account, not an organization."
      print_error "The GitHub Token Minter looks installations up at /orgs/${org}/installation,"
      print_error "which does not exist for personal accounts, so every token request would"
      print_error "fail with a 404 after deployment."
      print_error "Move the GitOps repository to an organization (a free one is enough) and set"
      print_error "GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh} to it, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "See k8s-operator/config/integrations/github/README.md."
      exit 1
      ;;
    missing)
      print_error "GITHUB_ORG='${org}' does not exist on GitHub."
      print_error "Check the spelling. The Token Minter resolves installations at"
      print_error "/orgs/${org}/installation, so a name that does not exist fails every"
      print_error "token request after deployment."
      print_error "Edit GITHUB_ORG in ${VARS_FILE:-scripts/vars.sh}, or re-run with"
      print_error "SKIP_GITHUB_ORG_CHECK=true to bypass this check."
      print_error "(GitHub Enterprise Server is not supported: this check, and the Minter,"
      print_error "both talk to api.github.com.)"
      exit 1
      ;;
    *)
      print_warning "Could not determine whether '${org}' is an organization; continuing."
      return 0
      ;;
  esac
}

cluster_exists() {
  gcloud container clusters list --filter="name=${CLUSTER_NAME} AND location=${REGION}" --format="value(name)" --project="${PROJECT_ID}" 2>/dev/null || echo ""
}

host_label_value() {
  gcloud container clusters describe "$CLUSTER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --format="value(resourceLabels.${KUBE_AGENTS_HOST_LABEL})" 2>/dev/null
}

verify_host_label() {
  [ "$(host_label_value)" = "true" ]
}

update_host_label() {
  gcloud container clusters update "$CLUSTER_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --update-labels="${KUBE_AGENTS_HOST_LABEL}=true" \
    --quiet
}

execute_host_label() {
  retry 3 5 update_host_label
}

connect_cluster() {
  print_info "Fetching cluster credentials..."
  gcloud container clusters get-credentials "$CLUSTER_NAME" --location "$REGION" --project "$PROJECT_ID" --quiet
}

# Shared readiness budget for stages 08 and 13. Accepts a bare number of
# seconds or an s/m/h suffix. kubectl rejects a bare integer for --timeout
# ("time: missing unit in duration"), and without this normalization that
# parse error would be reported as the rollout having failed.
init_agent_ready_timeout() {
  AGENT_READY_TIMEOUT="${AGENT_READY_TIMEOUT:-600s}"
  if [[ "$AGENT_READY_TIMEOUT" =~ ^[0-9]+$ ]]; then
    AGENT_READY_TIMEOUT="${AGENT_READY_TIMEOUT}s"
  fi
  if [[ ! "$AGENT_READY_TIMEOUT" =~ ^[0-9]+[smh]$ ]]; then
    print_error "AGENT_READY_TIMEOUT must be a duration like 600s, 10m or 1h (got '${AGENT_READY_TIMEOUT}')."
    exit 1
  fi
  case "$AGENT_READY_TIMEOUT" in
    *s) AGENT_READY_TIMEOUT_SECONDS="${AGENT_READY_TIMEOUT%s}" ;;
    *m) AGENT_READY_TIMEOUT_SECONDS="$(( ${AGENT_READY_TIMEOUT%m} * 60 ))" ;;
    *h) AGENT_READY_TIMEOUT_SECONDS="$(( ${AGENT_READY_TIMEOUT%h} * 3600 ))" ;;
  esac
  export AGENT_READY_TIMEOUT AGENT_READY_TIMEOUT_SECONDS
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

register_host_label() {
  print_step "3. Register kube-agents host"
  print_info "Verifying current state..."

  if verify_host_label; then
    print_success "Already completed: 3. Register kube-agents host"
    return 0
  fi

  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would execute: 3. Register kube-agents host"
    return 0
  fi

  print_info "Applying ${KUBE_AGENTS_HOST_LABEL}=true to '${CLUSTER_NAME}'..."
  if execute_host_label; then
    print_success "Registered the kube-agents host."
  else
    print_warning "Could not apply ${KUBE_AGENTS_HOST_LABEL}=true. Provisioning will continue; rerun step 08 to retry host discovery registration."
  fi
  return 0
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
