#!/usr/bin/env bash
# ==============================================================================
# 🔄 Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine
# ==============================================================================
# Modular CLI tool for Day-2 upgrades of the Platform Agent harness and operator.
#
# Usage:
#   ./upgrade.sh [options]
#   curl -fsSL https://gke-labs.github.io/kube-agents/upgrade.sh | bash -s -- \
#     --upgrade-mode=full --image-tag=<SEMVER_TAG_OR_FULL_COMMIT_SHA>
#
# Run this from the directory holding your original install checkout: the
# upgrade refuses to re-render cluster configuration without the saved
# k8s-operator/scripts/vars.sh state from the installation.
# ==============================================================================

set -Eeuo pipefail

# ANSI Color Tokens
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_BOLD="\033[1m"
C_RESET="\033[0m"

# Default CLI Configuration
PARAM_UPGRADE_MODE="full"
PARAM_NON_INTERACTIVE="false"
PARAM_DRY_RUN="false"
PARAM_PROJECT_ID=""
PARAM_CLUSTER_NAME=""
PARAM_REGION=""
PARAM_IMAGE_TAG="${IMAGE_TAG:-}"
TEMP_REPO_DIR=""

cleanup() {
  if [ -n "$TEMP_REPO_DIR" ] && [ -d "$TEMP_REPO_DIR" ]; then
    rm -rf -- "$TEMP_REPO_DIR"
  fi
}
trap cleanup EXIT

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  echo -e "\n${C_RED}${C_BOLD}✗ Upgrade error encountered at line ${line_no} (exit code ${exit_code}): ${bash_cmd}${C_RESET}" >&2
  write_report "FAILED" 2>/dev/null || true
  exit "$exit_code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

print_banner() {
  echo -e "${C_CYAN}${C_BOLD}"
  echo '==========================================================================='
  echo '🔄  Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine'
  echo '==========================================================================='
  echo -e "${C_RESET}"
}

print_step() {
  echo -e "\n${C_CYAN}${C_BOLD}>>> $1 <<<${C_RESET}"
}

print_info() {
  echo -e "  ${C_CYAN}ℹ $1${C_RESET}"
}

print_success() {
  echo -e "  ${C_GREEN}✓ $1${C_RESET}"
}

print_warning() {
  echo -e "  ${C_YELLOW}⚠ $1${C_RESET}"
}

print_error() {
  echo -e "  ${C_RED}✗ $1${C_RESET}"
}

show_help() {
  print_banner
  cat << EOF
Usage: ./upgrade.sh [OPTIONS]

Options:
  --upgrade-mode, -m MODE  Upgrade mode: full, harness, operator (Default: full)
  --non-interactive, -y    Automated execution mode (no interactive prompts)
  --dry-run                Preview upgrade plan and configuration state without touching cloud resources
  --project-id ID          GCP Target Project ID
  --cluster-name NAME      GKE Target Cluster Name
  --region REGION          GKE GCP Region
  --image-tag TAG          Validated immutable release tag or full commit SHA (required)
  --help, -h               Show this help message

Examples:
  # Perform full atomic upgrade of harness, operator, and skills
  ./upgrade.sh --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent-host"

  # Dry-run upgrade preview
  ./upgrade.sh --dry-run --upgrade-mode=full
EOF
}

validate_immutable_ref() {
  local ref="${1:-}"
  if [ -z "$ref" ]; then
    print_error "--image-tag is required; use a validated release tag or full commit SHA."
    return 1
  fi
  case "$ref" in
    latest|main|master|HEAD)
      print_error "Mutable image/source ref '$ref' is not supported. Use a validated release tag or full commit SHA."
      return 1
      ;;
  esac
  if [[ ! "$ref" =~ ^[0-9a-fA-F]{40}$ ]] \
    && [[ ! "$ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
    print_error "Image/source ref must be a full 40-character commit SHA or a SemVer release tag (vX.Y.Z)."
    return 1
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

# Persist one variable into the saved installer state. The provisioning
# scripts re-source vars.sh via load_state, so exporting alone is not enough:
# a value must be written here for the delegated scripts to honor it.
persist_state_var() {
  local state_file="$1"
  local var_name="$2"
  local var_value="$3"
  if [ -f "$state_file" ]; then
    grep -E -v "^[[:space:]]*export[[:space:]]+${var_name}=" "$state_file" > "${state_file}.tmp" || true
    mv "${state_file}.tmp" "$state_file"
  fi
  printf 'export %s=%q\n' "$var_name" "$var_value" >> "$state_file"
  chmod 600 "$state_file" 2>/dev/null || true
}

random_hex_32() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    # head reads a fixed count from a file, so no SIGPIPE reaches the producer
    # and `set -o pipefail` stays satisfied.
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

# Add the pod-scoped Session KV keys to an existing Secret that predates them.
#
# provision_07_gcp_k8s_secrets.sh generates these, and the upgrade path does not
# run it: every mode runs provision_03 and provision_08, and neither touches
# platform-agent-secrets. The operator marks both Secret references optional, so
# a Secret without the keys yields containers without the variables rather than
# a failed mount — and the k8s-event-watcher treats an empty --token-env
# variable as fatal, so it exits on every start and NO cluster events are
# watched from that moment on, in a container that stays Ready throughout. The
# Session KV server answering 503 and unstable pseudonyms are the visible half;
# the dead watcher is the half that needs this backfill.
#
# Additive only. An existing value is never rewritten: rotating SESSION_KV_SALT
# re-anonymises every user, severing their past sessions from their future ones.
SESSION_KV_KEYS_PATCHED="false"
backfill_session_kv_keys() {
  local namespace="$1"
  local secret_name="platform-agent-secrets"

  if ! kubectl get secret "$secret_name" -n "$namespace" >/dev/null 2>&1; then
    print_warning "Secret '$secret_name' not found in '$namespace'; skipping the Session KV key backfill."
    print_info "Whatever manages that Secret (Helm with credentials.create, Terraform, or your own secret store) must supply SESSION_KV_API_KEY and SESSION_KV_SALT."
    return 0
  fi

  local key existing
  for key in SESSION_KV_API_KEY SESSION_KV_SALT; do
    existing="$(kubectl get secret "$secret_name" -n "$namespace" -o jsonpath="{.data.$key}" 2>/dev/null || echo "")"
    if [ -n "$existing" ]; then
      print_info "$key is already present; leaving it untouched."
      continue
    fi
    print_info "Generating the missing $key into Secret '$secret_name'..."
    kubectl patch secret "$secret_name" -n "$namespace" --type=merge \
      -p "{\"stringData\":{\"$key\":\"$(random_hex_32)\"}}" >/dev/null
    SESSION_KV_KEYS_PATCHED="true"
  done

  if [ "$SESSION_KV_KEYS_PATCHED" = "true" ]; then
    print_success "Session KV keys backfilled; the event watcher and Session KV server can authenticate after the rollout."
  fi
}

verify_local_source_ref() {
  local repo_dir="$1"
  local expected_ref="$2"
  if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "Dry-run cannot verify source/image alignment because '$repo_dir' is not a Git worktree."
      return 0
    fi
    print_error "Refusing to upgrade from an unversioned source directory: $repo_dir"
    return 1
  fi

  local expected_commit current_commit
  if ! expected_commit="$(git -C "$repo_dir" rev-parse --verify "${expected_ref}^{commit}" 2>/dev/null)"; then
    print_error "The requested image/source ref '$expected_ref' is not present in the current checkout. Check out that exact revision first."
    return 1
  fi
  current_commit="$(git -C "$repo_dir" rev-parse HEAD)"
  if [ "$current_commit" != "$expected_commit" ]; then
    print_error "Source/image version mismatch: checkout is ${current_commit}, requested ref resolves to ${expected_commit}."
    return 1
  fi
  if [ -n "$(git -C "$repo_dir" status --porcelain --untracked-files=no)" ]; then
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      print_warning "Dry-run is using uncommitted source changes; a real upgrade would require a clean checkout."
    else
      print_error "Refusing to upgrade from a dirty checkout because its scripts do not exactly match '$expected_ref'."
      return 1
    fi
  fi
  print_success "Verified upgrade scripts and image ref resolve to commit ${expected_commit}."
}

# Parameter Parsing
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --upgrade-mode=*|-m=*) PARAM_UPGRADE_MODE="${1#*=}"; shift ;;
      --upgrade-mode|-m) PARAM_UPGRADE_MODE="$2"; shift 2 ;;
      --non-interactive|-y) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --region) PARAM_REGION="$2"; shift 2 ;;
      --image-tag=*) PARAM_IMAGE_TAG="${1#*=}"; shift ;;
      --image-tag) PARAM_IMAGE_TAG="$2"; shift 2 ;;
      --help|-h) show_help; exit 0 ;;
      *) print_error "Unknown parameter: $1"; show_help >&2; return 2 ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-upgrade-report.json"
  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "upgrade_mode": "$(json_escape "$PARAM_UPGRADE_MODE")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "target_image_tag": "$(json_escape "$PARAM_IMAGE_TAG")",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Upgrade report written to: $report_file"
}

main() {
  parse_args "$@"
  print_banner

  if [ -z "$PARAM_IMAGE_TAG" ]; then
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      print_error "--image-tag is required; use a validated release tag or full commit SHA."
      exit 1
    fi
    if [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
      printf '%b' "  ${C_CYAN}Target image tag (validated release tag or full commit SHA): ${C_RESET}" >/dev/tty
      read -r PARAM_IMAGE_TAG </dev/tty
    else
      print_error "--image-tag is required when no interactive terminal is available (e.g. curl | bash)."
      exit 1
    fi
  fi
  validate_immutable_ref "$PARAM_IMAGE_TAG"

  case "$PARAM_UPGRADE_MODE" in
    full|harness|operator) ;;
    *) print_error "Unsupported upgrade mode '$PARAM_UPGRADE_MODE'. Use full, harness, or operator."; exit 1 ;;
  esac

  local script_dir repo_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${script_dir}/k8s-operator/scripts/provision_03_gcp_gke_operator.sh" ]; then
    repo_dir="$script_dir"
    verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
  elif [ -f "$(pwd)/k8s-operator/scripts/provision_03_gcp_gke_operator.sh" ]; then
    repo_dir="$(pwd)"
    verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
  else
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    print_info "Fetching provisioning scripts for '${PARAM_IMAGE_TAG}'..."
    git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$repo_dir"
    git -C "$repo_dir" fetch --depth=1 origin "$PARAM_IMAGE_TAG"
    git -C "$repo_dir" checkout --detach FETCH_HEAD
    verify_local_source_ref "$repo_dir" "$PARAM_IMAGE_TAG"
  fi

  print_step "1. Validating Upgrade Target & Environment"
  print_info "Upgrade Mode: ${C_BOLD}${PARAM_UPGRADE_MODE}${C_RESET}"
  print_info "Target Image Tag: ${C_BOLD}${PARAM_IMAGE_TAG}${C_RESET}"

  local state_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  local state_loaded="false"
  if [ -f "$state_file" ]; then
    # Load state
    # shellcheck disable=SC1090,SC1091
    if ! source "$state_file"; then
      print_error "Configuration state is invalid and could not be loaded."
      exit 1
    fi
    state_loaded="true"
    print_success "Loaded existing configuration state from k8s-operator/scripts/vars.sh"
  else
    print_warning "No saved configuration state (k8s-operator/scripts/vars.sh) was found in ${repo_dir}."
  fi

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-platform-agent-host}}"
  local target_region="${PARAM_REGION:-${REGION:-us-central1}}"

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [ -z "$target_project" ]; then
    print_error "A GCP project is required. Pass --project-id or configure one with gcloud."
    exit 1
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Upgrade Plan Preview"
    echo -e "  • ${C_CYAN}Action:${C_RESET} Perform ${PARAM_UPGRADE_MODE} upgrade on cluster '${target_cluster}'"
    echo -e "  • ${C_CYAN}Image Overrides:${C_RESET} ${REGISTRY_PREFIX:-ghcr.io/gke-labs/kube-agents}/*:${PARAM_IMAGE_TAG}"
    echo -e "  • ${C_CYAN}Secrets:${C_RESET} generate SESSION_KV_API_KEY / SESSION_KV_SALT into 'platform-agent-secrets' only if absent (existing values are never rewritten)"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  # Fail closed without saved installer state: the delegated provisioning
  # scripts re-render the PlatformAgent Custom Resource (and operator images)
  # from vars.sh, so upgrading without it would silently reset chat, allowed
  # users, dashboard, and model-provider configuration to blank defaults.
  if [ "$state_loaded" != "true" ]; then
    print_error "Refusing to upgrade without the installation's saved configuration state."
    print_info "Run upgrade.sh from the directory where kube-agents was installed (it contains k8s-operator/scripts/vars.sh), or restore that file first."
    exit 1
  fi

  # Persist explicit target overrides so the delegated provisioning scripts,
  # which re-source vars.sh, act on the same cluster we fetch credentials for.
  if [ -n "$PARAM_PROJECT_ID" ]; then
    persist_state_var "$state_file" PROJECT_ID "$target_project"
  fi
  if [ -n "$PARAM_CLUSTER_NAME" ]; then
    persist_state_var "$state_file" CLUSTER_NAME "$target_cluster"
  fi
  if [ -n "$PARAM_REGION" ]; then
    persist_state_var "$state_file" REGION "$target_region"
  fi
  export PROJECT_ID="$target_project"
  export CLUSTER_NAME="$target_cluster"
  export REGION="$target_region"

  print_step "2. Connecting kubectl to GKE Cluster"
  gcloud container clusters get-credentials "$target_cluster" --location="$target_region" --project="$target_project"

  local target_namespace="${NAMESPACE:-kubeagents-system}"
  print_step "3. Reconciling Pod-Scoped Session Keys"
  backfill_session_kv_keys "$target_namespace"

  case "$PARAM_UPGRADE_MODE" in
    operator)
      print_step "4. Upgrading Kubernetes Operator (CRDs & Controller Manager)"
      cd "${repo_dir}/k8s-operator"
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_03_gcp_gke_operator.sh
      cd "$repo_dir"
      print_success "Kubernetes Operator upgraded successfully!"
      ;;

    harness)
      print_step "4. Upgrading Platform Agent Deployment & Identity"
      cd "${repo_dir}/k8s-operator"
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_08_deploy_platform_agent.sh
      cd "$repo_dir"
      print_success "Platform Agent deployment upgraded successfully!"
      ;;

    full)
      print_step "4. Executing Full Atomic Upgrade (Operator, Harness & Skills)"
      cd "${repo_dir}/k8s-operator"
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_03_gcp_gke_operator.sh
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_08_deploy_platform_agent.sh
      cd "$repo_dir"
      print_success "Full atomic upgrade completed successfully!"
      ;;
  esac

  # An operator-mode upgrade rolls the controller manager and nothing else, so a
  # Secret patched above would sit unread until some later harness upgrade —
  # with the watcher dead in the meantime. The other two modes re-render the
  # agent Deployment and pick the keys up on their own rollout.
  local restarted_agent="false"
  if [ "$SESSION_KV_KEYS_PATCHED" = "true" ] && [ "$PARAM_UPGRADE_MODE" = "operator" ]; then
    if kubectl get deployment platform-agent-gateway -n "$target_namespace" >/dev/null 2>&1; then
      print_info "Restarting the Platform Agent so it reads the newly added Session KV keys..."
      kubectl rollout restart deployment/platform-agent-gateway -n "$target_namespace"
      restarted_agent="true"
    else
      print_warning "Session KV keys were added but Deployment 'platform-agent-gateway' was not found in '$target_namespace'; restart the agent yourself so it reads them."
    fi
  fi

  print_step "5. Post-Upgrade Health Verification"
  kubectl get ns kubeagents-system >/dev/null
  if [ "$PARAM_UPGRADE_MODE" = "operator" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]; then
    kubectl rollout status deployment/kubeagents-controller-manager -n kubeagents-system --timeout=120s
  fi
  if [ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ] || [ "$restarted_agent" = "true" ]; then
    kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system --timeout=120s
  fi
  print_success "Upgraded deployments verified healthy."

  write_report "SUCCESS"

  print_step "🎉 Upgrade Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆  kube-agents ${PARAM_UPGRADE_MODE} upgrade completed successfully!${C_RESET}"
}

main "$@"
