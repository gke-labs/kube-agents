#!/usr/bin/env bash
# ==============================================================================
# 🧹 Kubernetes Agentic Harness (kube-agents) Complete Uninstall Engine
# ==============================================================================
# Refactored interactive tty confirmation & subshell handling thanks to review by @eLeontev
# Discovers and safely deletes all provisioned GCP resources, GKE clusters,
# IAM service accounts, secrets, and Kubernetes control plane components.
#
# Usage:
#   ./uninstall.sh [options]
#   curl -fsSL https://gke-labs.github.io/kube-agents/uninstall.sh | bash
# ==============================================================================

set -Eeuo pipefail

# ANSI Color Tokens
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_BOLD="\033[1m"
C_RESET="\033[0m"
# Process Lock File & Error Trap Handling
LOCK_FILE="/tmp/kube-agents-uninstall.lock"
if command -v flock >/dev/null 2>&1 && exec 200>"$LOCK_FILE" 2>/dev/null; then
  if ! flock -n 200 2>/dev/null; then
    echo -e "  \033[93m⚠ Another instance of kube-agents uninstaller is currently running. Exiting.\033[0m" >&2
    exit 1
  fi
fi

on_error() {
  local exit_code="$1"
  local line_no="$2"
  local bash_cmd="$3"
  echo -e "\n\033[91m\033[1m✗ Teardown error encountered at line ${line_no} (exit code ${exit_code}): ${bash_cmd}\033[0m" >&2
  write_report "FAILED" "true" "${line_no}" "${bash_cmd}" 2>/dev/null || true
  exit "$exit_code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

PARAM_NON_INTERACTIVE="false"
PARAM_DRY_RUN="false"
PARAM_PROJECT_ID=""
PARAM_CLUSTER_NAME=""
PARAM_REGION=""
PARAM_SOURCE_REF=""
TEMP_REPO_DIR=""

cleanup() {
  if [ -n "$TEMP_REPO_DIR" ] && [ -d "$TEMP_REPO_DIR" ]; then
    rm -rf -- "$TEMP_REPO_DIR"
  fi
}
trap cleanup EXIT

print_banner() {
  echo -e "${C_RED}${C_BOLD}"
  echo '==========================================================================='
  echo '🧹  Kubernetes Agentic Harness (kube-agents) Complete Uninstall Engine'
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

json_escape() {
  local value="${1:-}"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

show_help() {
  print_banner
  cat << EOF
Usage: ./uninstall.sh [OPTIONS]

Options:
  -y, --yes, --non-interactive  Automated execution mode (no interactive confirmation prompt)
  --dry-run                     Preview uninstall plan without deleting resources
  --project-id ID               GCP Target Project ID
  --cluster-name NAME           GKE Target Cluster Name (default: platform-agent-host)
  --region REGION               GKE GCP Region
  --source-ref REF              Tag or commit SHA of the release that made the install; that
                                release's own uninstall.sh is fetched and run in place of this one
  --help, -h, -?                Show this help message

Examples:
  # Interactively discover and remove kube-agents cluster & GCP resources
  ./uninstall.sh

  # Automated teardown for a known project and cluster
  ./uninstall.sh --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent-host"
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --uninstall|--delete) shift ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --region) PARAM_REGION="$2"; shift 2 ;;
      --source-ref=*) PARAM_SOURCE_REF="${1#*=}"; shift ;;
      --source-ref) PARAM_SOURCE_REF="$2"; shift 2 ;;
      --help|-h|-\?|help) show_help ;;
      *) print_error "Unknown parameter: $1"; return 2 ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-uninstall-report.json"
  cat << EOF > "$report_file"
{
  "status": "$(json_escape "$status")",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Uninstall report written to: $report_file"
}

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

# Decide WHERE the state lives before pinning the backend. Exporting
# KUBE_AGENTS_STATE_BUCKET first would make lifecycle.sh's ensure_backend
# `terraform init -reconfigure` onto the (possibly empty) remote prefix —
# abandoning a hand-driven install's local state, so the destroy plans
# nothing and reports success with the CR and backups already gone and
# every GCP resource still live.
#
# Needs installer_common.sh sourced (tf_state_bucket/tf_state_prefix) and the
# target coordinates exported. Unit-tested in tests/test_uninstall_script.py;
# keep the branch order — remote state wins, an explicitly named bucket with
# no state is an error, local state is honoured only when nothing is remote,
# and no state anywhere refuses rather than guesses.
resolve_state_location() {
  local compose_dir="$1"
  if gcloud storage cat "gs://$(tf_state_bucket)/$(tf_state_prefix)/default.tfstate" >/dev/null 2>&1; then
    # Remote state where the installer keeps it (or where the caller pointed
    # us): pin the backend for lifecycle.sh.
    export KUBE_AGENTS_STATE_BUCKET="${KUBE_AGENTS_STATE_BUCKET:-auto}"
  elif [ -n "${KUBE_AGENTS_STATE_BUCKET:-}" ]; then
    # The caller named a bucket and it holds no state for this cluster:
    # error out rather than fall back to guessing.
    print_error "No Terraform state at gs://$(tf_state_bucket)/$(tf_state_prefix) (KUBE_AGENTS_STATE_BUCKET was set explicitly)."
    return 1
  elif [ -f "${compose_dir}/terraform.tfstate" ] || [ -f "${compose_dir}/backend_override.tf" ]; then
    # A hand-driven install: local state, or an existing backend override
    # pointing wherever its author keeps state. Leave the backend variable
    # unset so lifecycle.sh touches neither.
    print_info "Using the composition's own state (local terraform.tfstate or existing backend_override.tf)."
  else
    print_error "No Terraform state found for '${CLUSTER_NAME}' (gs://$(tf_state_bucket)/$(tf_state_prefix)) and none locally."
    print_info "If this install was made by a pre-Terraform release, re-run with --source-ref=<that release> so its own teardown runs."
    return 1
  fi
}

main() {
  parse_args "$@"
  print_banner

  print_step "1. Discovering Installed Infrastructure Elements"

  local script_dir repo_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -n "$PARAM_SOURCE_REF" ]; then
    # The pinned release owns its own teardown: clone it and hand over
    # wholesale. This script's engine (installer_common.sh, lifecycle.sh
    # destroy) exists only at post-Terraform refs, and --source-ref exists
    # precisely for the installs those refs did not make — so continuing in
    # this main() would source files the clone does not carry. The exec also
    # releases the flock for the dispatched script and skips the temp-dir
    # cleanup trap, which must not delete a tree that is still executing.
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    print_info "Fetching the teardown engine pinned at '${PARAM_SOURCE_REF}'..."
    git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$repo_dir"
    git -C "$repo_dir" fetch --depth=1 origin "$PARAM_SOURCE_REF"
    git -C "$repo_dir" checkout --detach FETCH_HEAD
    if [ ! -f "${repo_dir}/uninstall.sh" ]; then
      print_error "'${PARAM_SOURCE_REF}' carries no uninstall.sh; tear the install down with that release's documented procedure."
      exit 1
    fi
    local dispatch_args=()
    if [ "$PARAM_NON_INTERACTIVE" = "true" ]; then
      dispatch_args+=(--non-interactive)
    fi
    if [ "$PARAM_DRY_RUN" = "true" ]; then
      dispatch_args+=(--dry-run)
    fi
    if [ -n "$PARAM_PROJECT_ID" ]; then
      dispatch_args+=(--project-id="$PARAM_PROJECT_ID")
    fi
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      dispatch_args+=(--cluster-name="$PARAM_CLUSTER_NAME")
    fi
    if [ -n "$PARAM_REGION" ]; then
      dispatch_args+=(--region="$PARAM_REGION")
    fi
    print_info "Handing over to the '${PARAM_SOURCE_REF}' release's own uninstall.sh..."
    TEMP_REPO_DIR=""
    exec bash "${repo_dir}/uninstall.sh" "${dispatch_args[@]}"
  elif [ -f "${script_dir}/terraform/examples/full-install/lifecycle.sh" ]; then
    repo_dir="$script_dir"
  elif [ -f "$(pwd)/terraform/examples/full-install/lifecycle.sh" ]; then
    repo_dir="$(pwd)"
  else
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    print_warning "No --source-ref given; fetching the teardown engine from main, which may be newer than your installed release."
    git clone --depth=1 https://github.com/gke-labs/kube-agents.git "$repo_dir"
  fi
  # Defaults, validators, and the terraform.tfvars generator shared with
  # install.sh. Print helpers are already defined above, as the file expects.
  # shellcheck disable=SC1091
  source "${repo_dir}/k8s-operator/scripts/installer_common.sh"
  if [ -f "${repo_dir}/k8s-operator/scripts/vars.sh" ]; then
    # shellcheck disable=SC1091
    if ! source "${repo_dir}/k8s-operator/scripts/vars.sh"; then
      print_error "Configuration state is invalid and could not be loaded."
      exit 1
    fi
    print_success "Loaded configuration state from k8s-operator/scripts/vars.sh"
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
    print_step "2. Dry-Run Uninstall Preview"
    echo -e "  • ${C_CYAN}Target Cluster:${C_RESET} ${target_cluster} in ${target_project} (${target_region})"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    echo -e "\n${C_RED}${C_BOLD}⚠️  WARNING: This will PERMANENTLY DELETE all kube-agents infrastructure in GCP project '${target_project}'!${C_RESET}"
    local confirm_choice=""
    if [ -c /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
      read -rp "Are you sure you want to proceed with complete uninstallation? (y/N): " confirm_choice </dev/tty >/dev/tty || confirm_choice=""
    else
      print_error "No interactive terminal is available. Re-run with --non-interactive only after reviewing the target."
      exit 1
    fi
    if [[ ! "$confirm_choice" =~ ^[Yy]$ ]]; then
      print_warning "Uninstall cancelled by user."
      exit 0
    fi
  fi

  print_step "2. Executing Automated Teardown Engine"

  # Keep the state file agreeing with the confirmed target: the tfvars
  # generator below reads the environment, but a saved vars.sh that names a
  # different cluster would mislead the next tool that sources it.
  local state_file="${repo_dir}/k8s-operator/scripts/vars.sh"
  if [ -f "$state_file" ]; then
    if [ -n "$PARAM_PROJECT_ID" ]; then
      persist_state_var "$state_file" PROJECT_ID "$target_project"
    fi
    if [ -n "$PARAM_CLUSTER_NAME" ]; then
      persist_state_var "$state_file" CLUSTER_NAME "$target_cluster"
    fi
    if [ -n "$PARAM_REGION" ]; then
      persist_state_var "$state_file" REGION "$target_region"
    fi
  fi

  export PROJECT_ID="$target_project"
  export CLUSTER_NAME="$target_cluster"
  export REGION="$target_region"
  export NO_CONFIRM="1"

  # The engine is `lifecycle.sh destroy` against the install's Terraform state
  # in GCS (derived from the coordinates, so a fresh clone finds it). An
  # install with no state anywhere predates the Terraform engine — this
  # uninstaller cannot take it apart, but the release that installed it
  # can, which is exactly what --source-ref pins.
  local compose_dir="${repo_dir}/terraform/examples/full-install"
  export KUBE_AGENTS_STATE_PREFIX
  KUBE_AGENTS_STATE_PREFIX="$(tf_state_prefix)"
  resolve_state_location "$compose_dir" || exit 1

  # terraform destroy still evaluates the configuration, so required variables
  # must be present even from a fresh clone; the placeholder key feeds nothing
  # that survives the destroy.
  export API_SERVER_KEY="${API_SERVER_KEY:-uninstall-placeholder}"
  write_tfvars_from_state "${compose_dir}/terraform.tfvars"
  (
    cd "$compose_dir"
    ./lifecycle.sh destroy -auto-approve -input=false
  )
  rm -f "$state_file"

  write_report "SUCCESS"

  print_step "🎉 Uninstall Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆 All kube-agents infrastructure elements have been safely removed.${C_RESET}"
  print_info "Kept by design: the Cloud KMS key rings (GCP cannot delete them; the next install adopts them) and the Terraform state bucket gs://$(tf_state_bucket)."
  print_info "If this project will not host kube-agents again, delete the bucket yourself: gcloud storage rm -r gs://$(tf_state_bucket)"
}

if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ]; then
  main "$@"
else
  echo "ℹ️ Sourced uninstall.sh functions without executing main (KUBE_AGENTS_SOURCE_ONLY=true)." >&2
fi
