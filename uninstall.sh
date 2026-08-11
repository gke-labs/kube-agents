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
  --source-ref REF              Release tag or commit SHA to fetch the teardown scripts from
                                when not run from a local checkout (default: main)
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

main() {
  parse_args "$@"
  print_banner

  print_step "1. Discovering Installed Infrastructure Elements"

  local script_dir repo_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "${script_dir}/k8s-operator/scripts/teardown.sh" ]; then
    repo_dir="$script_dir"
  elif [ -f "$(pwd)/k8s-operator/scripts/teardown.sh" ]; then
    repo_dir="$(pwd)"
  else
    TEMP_REPO_DIR="$(mktemp -d)"
    repo_dir="${TEMP_REPO_DIR}/kube-agents"
    if [ -n "$PARAM_SOURCE_REF" ]; then
      print_info "Fetching the teardown scripts pinned at '${PARAM_SOURCE_REF}'..."
      git clone --filter=blob:none --no-checkout https://github.com/gke-labs/kube-agents.git "$repo_dir"
      git -C "$repo_dir" fetch --depth=1 origin "$PARAM_SOURCE_REF"
      git -C "$repo_dir" checkout --detach FETCH_HEAD
    else
      print_warning "No --source-ref given; fetching the teardown scripts from main, which may be newer than your installed release."
      git clone --depth=1 https://github.com/gke-labs/kube-agents.git "$repo_dir"
    fi
  fi
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

  # The delegated teardown steps re-source vars.sh via ensure_teardown_state,
  # so exporting alone is not enough: an explicit CLI target override must be
  # written into the state file, or teardown would silently act on the saved
  # project/cluster/region instead of the target confirmed above.
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

  cd "${repo_dir}/k8s-operator"
  bash scripts/teardown.sh -y --no-confirm
  rm -f scripts/vars.sh
  cd "$repo_dir"

  write_report "SUCCESS"

  print_step "🎉 Uninstall Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆 All kube-agents infrastructure elements have been safely removed.${C_RESET}"
}

main "$@"
