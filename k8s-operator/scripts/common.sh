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


# ─── GKE Cluster Mode ─────────────────────────────────────────────────────────
# CLUSTER_MODE is either "autopilot" or "standard". It is resolved once, in
# provision_01, and persisted to vars.sh so that every later step (gVisor node
# pool, Filestore addon, teardown) branches on the same answer instead of
# re-sniffing the cluster.

is_autopilot() {
  [ "${CLUSTER_MODE:-standard}" = "autopilot" ]
}

# Report the mode of the live cluster. Echoes "autopilot" or "standard"; echoes
# nothing when the cluster cannot be described.
detect_cluster_mode() {
  local enabled
  enabled=$(gcloud container clusters describe "$CLUSTER_NAME" \
      --region="$REGION" --project="$PROJECT_ID" \
      --format="value(autopilot.enabled)" 2>/dev/null) || return 0
  case "$enabled" in
    [Tt][Rr][Uu][Ee]) echo "autopilot" ;;
    *) echo "standard" ;;
  esac
}

# Resolve CLUSTER_MODE. An existing cluster wins: its live mode is adopted and
# persisted, because the downstream steps have to match the cluster we actually
# have. Only when no cluster exists do we ask which one to create.
init_var_cluster_mode() {
  if [ -n "$(cluster_exists)" ]; then
    local detected
    detected="$(detect_cluster_mode)"
    if [ -n "$detected" ]; then
      if [ -n "${CLUSTER_MODE:-}" ] && [ "$CLUSTER_MODE" != "$detected" ]; then
        print_warning "Recorded CLUSTER_MODE='${CLUSTER_MODE}' disagrees with the live cluster; using '${detected}'."
      fi
      print_info "Reusing existing GKE cluster '${CLUSTER_NAME}' (${REGION}) — mode: ${C_WHITE}${detected}${C_RESET}"
      export CLUSTER_MODE="$detected"
      save_var "CLUSTER_MODE" "$detected"
      return 0
    fi
  fi

  init_var "CLUSTER_MODE" "autopilot" "Create GKE cluster in which mode? (autopilot, standard)"

  CLUSTER_MODE=$(echo "$CLUSTER_MODE" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
  if [[ ! "$CLUSTER_MODE" =~ ^(autopilot|standard)$ ]]; then
    print_error "Invalid GKE Cluster Mode '$CLUSTER_MODE'. Must be one of: autopilot, standard."
    exit 1
  fi
  export CLUSTER_MODE
  save_var "CLUSTER_MODE" "$CLUSTER_MODE"
}

is_non_interactive() {
  [ ! -t 0 ] || [ "${NO_CONFIRM:-0}" -eq 1 ] || [ "${DRY_RUN:-0}" -eq 1 ] || is_ci_pipeline
}

init_var_image_tag() {
  if [ -z "${IMAGE_TAG:-}" ]; then
    if is_non_interactive; then
      echo -e "  ${C_RED}❌ ERROR: IMAGE_TAG is required in non-interactive / CI mode. Please export IMAGE_TAG.${C_RESET}" >&2
      exit 1
    else
      local default_tag="latest"
      echo -ne "  ${C_CYAN}Enter Base Image Tag [${C_WHITE}${default_tag}${C_CYAN}]: ${C_RESET}"
      read -r input_tag
      export IMAGE_TAG="${input_tag:-$default_tag}"
    fi
  fi
}

load_state() {
  if [ -f "$VARS_FILE" ]; then
    source "$VARS_FILE"
  elif [ "${DRY_RUN:-0}" -ne 1 ]; then
    echo "# SRE Sourced Variables for GKE & GCP Setup" > "$VARS_FILE"
    source "$VARS_FILE"
  fi
  init_var_image_tag
  export NAMESPACE="kubeagents-system"
  export PLATFORM_AGENT_KSA_NAME="kubeagents-platform-agent"
  export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
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
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
    export PLATFORM_AGENT_GSA_NAME="kubeagents-platform-gsa"
    export CONTROLLER_KSA_NAME="kubeagents-controller"
    export CONTROLLER_GSA_NAME="kubeagents-controller-gsa"
    export GITHUB_MINTER_KSA_NAME="kubeagents-github-minter"
    export GITHUB_MINTER_GSA_NAME="kubeagents-github-minter-gsa"
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
      export REGION="${REGION:-${GCP_REGION:-us-east4}}"
      export CLUSTER_NAME="${CLUSTER_NAME:-${GKE_CLUSTER_NAME:-platform-agent-host}}"
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
    export PLATFORM_AGENT_SANDBOX_KSA_NAME="platform-agent-sandbox"
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

# ─── Namespace ResourceQuota Preflight ────────────────────────────────────────
# Some projects apply a baseline ResourceQuota to every namespace (this is why
# step 03 patches cert-manager's resource block). The harness is deployed across
# several steps, so a quota that fits the agent at step 08 can be full by step
# 11 — the already-admitted Pod keeps running, and the failure only surfaces on
# the first `kubectl rollout restart`. This preflight moves that failure to the
# front of the pipeline, where it is cheap to fix.
#
# Set SKIP_QUOTA_PREFLIGHT=1 to bypass.

# Normalise a Kubernetes CPU quantity to integer milli-cores. "500m" -> 500,
# "2" -> 2000, "1.5" -> 1500.
quantity_to_millicores() {
  local q="${1:-0}"
  if [[ "$q" == *m ]]; then
    printf '%.0f' "${q%m}"
  else
    awk -v v="$q" 'BEGIN { printf "%.0f", v * 1000 }'
  fi
}

# Normalise a Kubernetes memory quantity to integer Mi. Handles the binary and
# decimal suffixes the API server emits, plus plain byte counts.
quantity_to_mebibytes() {
  local q="${1:-0}" num unit
  num="${q%%[A-Za-z]*}"
  unit="${q#"$num"}"
  local div
  case "$unit" in
    Ki) div=1024 ;;
    Mi) div=1 ;;
    Gi) div=$(awk 'BEGIN { print 1/1024 }') ;;
    Ti) div=$(awk 'BEGIN { print 1/1048576 }') ;;
    K | k) div=$(awk 'BEGIN { print 1048576/1000 }') ;;
    M) div=$(awk 'BEGIN { print 1048576/1000000 }') ;;
    G) div=$(awk 'BEGIN { print 1048576/1000000000 }') ;;
    "") div=1048576 ;;
    *) div=1 ;;
  esac
  awk -v n="$num" -v d="$div" 'BEGIN { printf "%.0f", n / d }'
}

# Render milli-cores / Mi back into a quantity a human can paste into a patch.
millicores_to_quantity() { echo "${1}m"; }
mebibytes_to_quantity() { echo "${1}Mi"; }

# Sum the resource footprint of everything the pipeline will place in
# $NAMESPACE, and export it as QUOTA_NEED_<RESOURCE>. Mirrors the container
# specs in k8s-operator/internal/controller/ and config/integrations/; keep the
# two in step.
#
#   component                      req cpu  req mem  lim cpu  lim mem
#   platform-agent-gateway Pod      1006m   3008Mi    3700m   6016Mi
#     ├─ platform-agent              500m   2048Mi    2000m   4096Mi  manifest_helpers.go resolveResources
#     ├─ platform-agent-dashboard    256m    512Mi     500m   1024Mi  platformagent_manifests.go
#     ├─ fluent-bit                  100m    128Mi     500m    256Mi  platformagent_manifests.go
#     ├─ event-watcher                50m     64Mi     200m    128Mi  platformagent_manifests.go
#     └─ envoy-credential-proxy      100m    256Mi     500m    512Mi  platformagent_manifests.go
#   litellm                          100m    512Mi     500m   2048Mi  config/integrations/litellm
#   github-token-minter              100m    128Mi     500m    256Mi  config/integrations/github
#   inference-replay                 100m    256Mi     500m   1024Mi  config/integrations/inference-replay
#
# The gateway's `sandbox-credential-cleanup` init container (100m/128Mi request)
# is not added: a Pod's quota charge is max(init containers, sum of containers),
# and the containers win. The controller-manager is deployed at step 03, so it
# is already counted in the quota's `used` and must not be added here.
#
# Only cpu, memory, and pods are compared. Any other resource the quota tracks
# is reported as unchecked rather than silently ignored.
compute_harness_footprint() {
  QUOTA_NEED_REQUESTS_CPU=1006
  QUOTA_NEED_REQUESTS_MEMORY=3008
  QUOTA_NEED_LIMITS_CPU=3700
  QUOTA_NEED_LIMITS_MEMORY=6016
  QUOTA_NEED_PODS=1
  QUOTA_COMPONENTS="platform-agent-gateway"

  # LiteLLM is unconditional (step 09).
  QUOTA_NEED_REQUESTS_CPU=$((QUOTA_NEED_REQUESTS_CPU + 100))
  QUOTA_NEED_REQUESTS_MEMORY=$((QUOTA_NEED_REQUESTS_MEMORY + 512))
  QUOTA_NEED_LIMITS_CPU=$((QUOTA_NEED_LIMITS_CPU + 500))
  QUOTA_NEED_LIMITS_MEMORY=$((QUOTA_NEED_LIMITS_MEMORY + 2048))
  QUOTA_NEED_PODS=$((QUOTA_NEED_PODS + 1))
  QUOTA_COMPONENTS="${QUOTA_COMPONENTS}, litellm"

  # Step 10 deploys the minter only when the GitHub App is fully configured.
  if [ -n "${GITHUB_ORG:-}" ] && [ -n "${GITHUB_REPO:-}" ] && [ -n "${GITHUB_APP_ID:-}" ]; then
    QUOTA_NEED_REQUESTS_CPU=$((QUOTA_NEED_REQUESTS_CPU + 100))
    QUOTA_NEED_REQUESTS_MEMORY=$((QUOTA_NEED_REQUESTS_MEMORY + 128))
    QUOTA_NEED_LIMITS_CPU=$((QUOTA_NEED_LIMITS_CPU + 500))
    QUOTA_NEED_LIMITS_MEMORY=$((QUOTA_NEED_LIMITS_MEMORY + 256))
    QUOTA_NEED_PODS=$((QUOTA_NEED_PODS + 1))
    QUOTA_COMPONENTS="${QUOTA_COMPONENTS}, github-token-minter"
  fi

  # Step 11 is opt-in.
  if is_truthy "${INFERENCE_REPLAY_ENABLED:-false}"; then
    QUOTA_NEED_REQUESTS_CPU=$((QUOTA_NEED_REQUESTS_CPU + 100))
    QUOTA_NEED_REQUESTS_MEMORY=$((QUOTA_NEED_REQUESTS_MEMORY + 256))
    QUOTA_NEED_LIMITS_CPU=$((QUOTA_NEED_LIMITS_CPU + 500))
    QUOTA_NEED_LIMITS_MEMORY=$((QUOTA_NEED_LIMITS_MEMORY + 1024))
    QUOTA_NEED_PODS=$((QUOTA_NEED_PODS + 1))
    QUOTA_COMPONENTS="${QUOTA_COMPONENTS}, inference-replay"
  fi
}

# Emit "<quota-name> <resource> <hard> <used>" per tracked resource. Uses a Go
# template rather than jq so the prerequisite list stays gcloud/kubectl only.
_dump_namespace_quotas() {
  kubectl get resourcequota -n "$1" -o go-template='{{range .items}}{{$n := .metadata.name}}{{$used := .status.used}}{{range $k, $v := .status.hard}}{{$n}} {{$k}} {{$v}} {{if $used}}{{with index $used $k}}{{.}}{{else}}0{{end}}{{else}}0{{end}}{{"\n"}}{{end}}{{end}}' 2>/dev/null
}

check_namespace_quota_headroom() {
  local ns="${1:-${NAMESPACE:-kubeagents-system}}"

  if [ "${SKIP_QUOTA_PREFLIGHT:-0}" -eq 1 ]; then
    print_warning "Skipping namespace quota preflight (SKIP_QUOTA_PREFLIGHT=1)."
    return 0
  fi
  if [ "${DRY_RUN:-0}" -eq 1 ]; then
    print_info "[DRY-RUN] Would check ResourceQuota headroom in namespace '${ns}'."
    return 0
  fi

  compute_harness_footprint

  local quotas
  quotas="$(_dump_namespace_quotas "$ns")"
  if [ -z "$quotas" ]; then
    print_success "No ResourceQuota enforced in '${ns}' — nothing to check."
    return 0
  fi

  local shortfall=0 patch_name="" patch_fields="" unchecked=""

  while read -r qname resource hard used; do
    [ -n "$qname" ] || continue

    local need=0 have_used=0 have_hard=0 fmt="raw"
    case "$resource" in
      requests.cpu | cpu) need=$QUOTA_NEED_REQUESTS_CPU; fmt="cpu" ;;
      limits.cpu) need=$QUOTA_NEED_LIMITS_CPU; fmt="cpu" ;;
      requests.memory | memory) need=$QUOTA_NEED_REQUESTS_MEMORY; fmt="mem" ;;
      limits.memory) need=$QUOTA_NEED_LIMITS_MEMORY; fmt="mem" ;;
      pods) need=$QUOTA_NEED_PODS; fmt="count" ;;
      *)
        unchecked="${unchecked}${unchecked:+, }${resource}"
        continue
        ;;
    esac

    case "$fmt" in
      cpu)
        have_hard=$(quantity_to_millicores "$hard")
        have_used=$(quantity_to_millicores "$used")
        ;;
      mem)
        have_hard=$(quantity_to_mebibytes "$hard")
        have_used=$(quantity_to_mebibytes "$used")
        ;;
      *)
        have_hard="${hard:-0}"
        have_used="${used:-0}"
        ;;
    esac

    local required=$((have_used + need))
    if [ "$required" -le "$have_hard" ]; then
      continue
    fi

    if [ "$shortfall" -eq 0 ]; then
      print_error "ResourceQuota in '${ns}' cannot hold the harness (${QUOTA_COMPONENTS})."
      printf "  %-18s %-10s %-10s %-10s %s\n" "RESOURCE" "HARD" "USED" "NEEDED" "QUOTA"
    fi
    shortfall=1
    patch_name="$qname"

    # Report every column in one unit — the quota's own spelling of "4" versus a
    # computed "4500m" is needlessly hard to compare. Round the suggestion up so
    # the namespace is not left with exactly zero headroom.
    local suggested
    case "$fmt" in
      cpu)
        suggested=$(millicores_to_quantity $(( (required * 12 + 9) / 10 )))
        printf "  %-18s %-10s %-10s %-10s %s\n" "$resource" \
          "$(millicores_to_quantity "$have_hard")" "$(millicores_to_quantity "$have_used")" \
          "$(millicores_to_quantity "$required")" "$qname"
        ;;
      mem)
        suggested=$(mebibytes_to_quantity $(( (required * 12 + 9) / 10 )))
        printf "  %-18s %-10s %-10s %-10s %s\n" "$resource" \
          "$(mebibytes_to_quantity "$have_hard")" "$(mebibytes_to_quantity "$have_used")" \
          "$(mebibytes_to_quantity "$required")" "$qname"
        ;;
      *)
        suggested=$(( required + 2 ))
        printf "  %-18s %-10s %-10s %-10s %s\n" "$resource" "$have_hard" "$have_used" "$required" "$qname"
        ;;
    esac
    patch_fields="${patch_fields}${patch_fields:+,}\"${resource}\":\"${suggested}\""
  done <<< "$quotas"

  if [ -n "$unchecked" ]; then
    print_warning "Not compared (this check only covers cpu, memory, and pods): ${unchecked}."
  fi

  if [ "$shortfall" -eq 0 ]; then
    print_success "ResourceQuota headroom in '${ns}' is sufficient for ${QUOTA_COMPONENTS}."
  else
    echo -e "\n  ${C_YELLOW}Raise the quota (values include ~20% headroom), then re-run:${C_RESET}"
    echo -e "    ${C_WHITE}kubectl patch resourcequota ${patch_name} -n ${ns} \\"
    echo -e "      --type=merge -p '{\"spec\":{\"hard\":{${patch_fields}}}}'${C_RESET}"
    echo -e "  ${C_CYAN}Or bypass this check with SKIP_QUOTA_PREFLIGHT=1.${C_RESET}"
    return 1
  fi

  # Restart headroom is advisory, not a failure condition. At replicas=1 the
  # gateway Deployment uses the Recreate strategy, so a rollout releases the old
  # Pod's quota before admitting the new one and needs no spare room. Raising
  # spec.deployment.availability.replicas switches it to RollingUpdate with a
  # 25% surge, which does.
  print_info "Restart headroom: replicas=1 uses Recreate (no surge). If you set availability.replicas>1, keep one extra gateway Pod (3700m CPU / 6016Mi limits) of quota free or rollouts will be refused."
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
