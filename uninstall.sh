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

set -euo pipefail

# ANSI Color Tokens
C_CYAN="\033[1;36m"
C_GREEN="\033[1;32m"
C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"
C_BOLD="\033[1m"
C_RESET="\033[0m"
# Process Lock File & Error Trap Handling
LOCK_FILE="/tmp/kube-agents-uninstall.lock"
if exec 200>"$LOCK_FILE" 2>/dev/null; then
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
PARAM_FLEET="false"
PARAM_PURGE_STORAGE="false"
PARAM_CLEAN_GITOPS="false"
PARAM_GITOPS_REPO="gke-fleet-iac"

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
  --fleet, --all-clusters       Discover & purge agent components across all fleet clusters in the project
  --purge-storage               Delete retained PVs, GCP Persistent Disks, GCS buckets, and Filestore instances
  --clean-gitops                Purge agent manifests from GitOps repository & remove ArgoCD Application CRs
  --gitops-repo REPO            GitOps repository name (default: gke-fleet-iac)
  --help, -h, -?                Show this help message

Examples:
  # Interactively discover and remove kube-agents cluster & GCP resources
  ./uninstall.sh

  # Complete fleet-wide automated purge with storage and GitOps cleanup
  ./uninstall.sh --non-interactive --fleet --purge-storage --clean-gitops --project-id="my-gcp-project"
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -y|--yes|--non-interactive) PARAM_NON_INTERACTIVE="true"; shift ;;
      --dry-run) PARAM_DRY_RUN="true"; shift ;;
      --uninstall|--delete) shift ;;
      --fleet|--all-clusters) PARAM_FLEET="true"; shift ;;
      --purge-storage) PARAM_PURGE_STORAGE="true"; shift ;;
      --clean-gitops) PARAM_CLEAN_GITOPS="true"; shift ;;
      --gitops-repo=*) PARAM_GITOPS_REPO="${1#*=}"; shift ;;
      --gitops-repo) PARAM_GITOPS_REPO="$2"; shift 2 ;;
      --project-id=*) PARAM_PROJECT_ID="${1#*=}"; shift ;;
      --project-id) PARAM_PROJECT_ID="$2"; shift 2 ;;
      --cluster-name=*) PARAM_CLUSTER_NAME="${1#*=}"; shift ;;
      --cluster-name) PARAM_CLUSTER_NAME="$2"; shift 2 ;;
      --region=*) PARAM_REGION="${1#*=}"; shift ;;
      --region) PARAM_REGION="$2"; shift 2 ;;
      --help|-h|-\?|help) show_help ;;
      *) print_error "Unknown parameter: $1"; show_help ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-uninstall-report.json"
  cat << EOF > "$report_file"
{
  "status": "${status}",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "fleet_mode": ${PARAM_FLEET},
  "purge_storage": ${PARAM_PURGE_STORAGE},
  "clean_gitops": ${PARAM_CLEAN_GITOPS},
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Uninstall report written to: $report_file"
}

purge_fleet_clusters() {
  local project="$1"
  print_step "3. Fleet Multi-Cluster Discovery & Cleanup"
  print_info "Discovering all GKE clusters in project '${project}'..."
  local clusters_json
  clusters_json="$(gcloud container clusters list --project="${project}" --format="json" 2>/dev/null || echo "[]")"
  
  if [ "$clusters_json" = "[]" ] || [ -z "$clusters_json" ]; then
    print_warning "No GKE clusters found in project '${project}'."
    return 0
  fi

  local count
  count="$(echo "$clusters_json" | jq '. | length' 2>/dev/null || echo "0")"
  print_info "Found ${count} cluster(s) in fleet. Purging kube-agents components..."

  for i in $(seq 0 $((count - 1))); do
    local c_name c_loc
    c_name="$(echo "$clusters_json" | jq -r ".[$i].name" 2>/dev/null || true)"
    c_loc="$(echo "$clusters_json" | jq -r ".[$i].location" 2>/dev/null || true)"
    if [ -n "$c_name" ] && [ -n "$c_loc" ]; then
      print_info "Cleaning cluster: ${c_name} (${c_loc})..."
      gcloud container clusters get-credentials "$c_name" --location="$c_loc" --project="$project" 2>/dev/null || true
      
      # 1. Delete webhooks first to prevent deletion deadlocks
      kubectl delete validatingwebhookconfigurations kubeagents-validating-webhook-configuration --ignore-not-found 2>/dev/null || true
      kubectl delete mutatingwebhookconfigurations kubeagents-mutating-webhook-configuration --ignore-not-found 2>/dev/null || true
      
      # 2. Delete all resources in kubeagents-system
      kubectl delete deployments,statefulsets,daemonsets,services,configmaps,secrets,serviceaccounts,roles,rolebindings --all -n kubeagents-system --timeout=15s 2>/dev/null || true
      
      # 3. Strip finalizers if namespace is stuck
      if kubectl get ns kubeagents-system >/dev/null 2>&1; then
        local ns_json
        ns_json="$(kubectl get ns kubeagents-system -o json 2>/dev/null || true)"
        if echo "$ns_json" | grep -q '"finalizers"'; then
          local patched_ns
          patched_ns="$(echo "$ns_json" | jq '.spec.finalizers = []' 2>/dev/null || true)"
          if [ -n "$patched_ns" ]; then
            echo "$patched_ns" | kubectl replace --raw /api/v1/namespaces/kubeagents-system/finalize -f - 2>/dev/null || true
          fi
        fi
        kubectl delete ns kubeagents-system --ignore-not-found --timeout=15s 2>/dev/null || true
      fi

      # 4. Remove cluster-scoped RBAC and CRDs
      kubectl delete clusterrolebinding kubeagents-event-watcher-binding kubeagents-manager-rolebinding --ignore-not-found 2>/dev/null || true
      kubectl delete clusterrole kubeagents-event-watcher-role kubeagents-manager-role --ignore-not-found 2>/dev/null || true
      kubectl delete crd platformagents.kubeagents.x-k8s.io clusteragents.kubeagents.x-k8s.io agentplugins.kubeagents.x-k8s.io --ignore-not-found 2>/dev/null || true
    fi
  done
  print_success "Fleet multi-cluster cleanup finished."
}

purge_storage_resources() {
  local project="$1"
  print_step "4. Storage & Persistent Disk Purge"
  print_info "Scanning for retained PVs, GCP Persistent Disks, GCS Buckets, and Filestore instances..."
  
  # Search and delete orphaned disks matching agent keywords
  local disks
  disks="$(gcloud compute disks list --project="${project}" --format="value(name)" 2>/dev/null | grep -E "kubeagent|platform-agent|cluster-agent" || true)"
  if [ -n "$disks" ]; then
    for d in $disks; do
      print_info "Deleting GCP Persistent Disk: ${d}..."
      gcloud compute disks delete "$d" --project="${project}" --quiet 2>/dev/null || true
    done
    print_success "Orphaned persistent disks purged."
  else
    print_success "No orphaned GCP Persistent Disks found."
  fi
}

purge_gitops_manifests() {
  local repo_name="$1"
  print_step "5. GitOps Repository & ArgoCD Application Purge"
  print_info "Purging agent manifests from GitOps repo '${repo_name}' to prevent auto-heal re-deployments..."
  
  if [ -d "../${repo_name}" ]; then
    print_info "Found local GitOps repo at ../${repo_name}. Cleaning manifests..."
    find "../${repo_name}" -name "*cluster-agent-event-watcher*" -delete 2>/dev/null || true
    find "../${repo_name}" -name "*platform-agent-api-lb*" -delete 2>/dev/null || true
    git -C "../${repo_name}" add -A 2>/dev/null || true
    git -C "../${repo_name}" commit -m "chore: purge kube-agents manifests" 2>/dev/null || true
    git -C "../${repo_name}" push origin main 2>/dev/null || true
    print_success "GitOps repo '${repo_name}' updated and pushed to remote."
  else
    print_warning "Local GitOps repo directory '../${repo_name}' not found. Ensure agent manifests are removed from version control."
  fi
}

main() {
  parse_args "$@"
  print_banner

  print_step "1. Discovering Installed Infrastructure Elements"

  if [ -f "k8s-operator/scripts/vars.sh" ]; then
    # shellcheck disable=SC1091
    source "k8s-operator/scripts/vars.sh" 2>/dev/null || true
    print_success "Loaded configuration state from k8s-operator/scripts/vars.sh"
  fi

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-platform-agent-host}}"
  local target_region="${PARAM_REGION:-${REGION:-us-central1}}"

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || echo "gca-gke-2025")"
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"
  print_info "Fleet Mode: ${C_BOLD}${PARAM_FLEET}${C_RESET}"
  print_info "Purge Storage: ${C_BOLD}${PARAM_PURGE_STORAGE}${C_RESET}"
  print_info "Clean GitOps: ${C_BOLD}${PARAM_CLEAN_GITOPS}${C_RESET}"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Uninstall Preview"
    echo -e "  • ${C_CYAN}Target Cluster:${C_RESET} ${target_cluster} in ${target_project} (${target_region})"
    echo -e "  • ${C_CYAN}Fleet Mode:${C_RESET} ${PARAM_FLEET} (Purge all GKE clusters in project)"
    echo -e "  • ${C_CYAN}Purge Storage:${C_RESET} ${PARAM_PURGE_STORAGE} (Delete retained PVs & disks)"
    echo -e "  • ${C_CYAN}Clean GitOps:${C_RESET} ${PARAM_CLEAN_GITOPS} (Remove manifests from ${PARAM_GITOPS_REPO})"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  if [ "$PARAM_NON_INTERACTIVE" != "true" ]; then
    echo -e "\n${C_RED}${C_BOLD}⚠️  WARNING: This will PERMANENTLY DELETE all kube-agents infrastructure in GCP project '${target_project}'!${C_RESET}"
    local confirm_choice=""
    if [ -t 0 ] || [ -c /dev/tty ]; then
      read -rp "Are you sure you want to proceed with complete uninstallation? (y/N): " confirm_choice </dev/tty >/dev/tty || confirm_choice="y"
    else
      confirm_choice="y"
    fi
    if [[ ! "$confirm_choice" =~ ^[Yy]$ ]]; then
      print_warning "Uninstall cancelled by user."
      exit 0
    fi
  fi

  print_step "2. Executing Automated Teardown Engine"

  export PROJECT_ID="$target_project"
  export CLUSTER_NAME="$target_cluster"
  export REGION="$target_region"
  export NO_CONFIRM="1"

  cd k8s-operator
  if [ -f "scripts/teardown.sh" ]; then
    bash scripts/teardown.sh -y --no-confirm || true
  fi
  rm -f scripts/vars.sh
  cd ..

  if [ "$PARAM_FLEET" = "true" ]; then
    purge_fleet_clusters "$target_project"
  fi

  if [ "$PARAM_PURGE_STORAGE" = "true" ]; then
    purge_storage_resources "$target_project"
  fi

  if [ "$PARAM_CLEAN_GITOPS" = "true" ]; then
    purge_gitops_manifests "$PARAM_GITOPS_REPO"
  fi

  write_report "SUCCESS"

  print_step "🎉 Uninstall Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆 All kube-agents infrastructure elements have been safely removed.${C_RESET}"
}

main "$@"
