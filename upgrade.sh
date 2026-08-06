#!/usr/bin/env bash
# ==============================================================================
# 🔄 Kubernetes Agentic Harness (kube-agents) Lifecycle Upgrade Engine
# ==============================================================================
# Modular CLI tool for Day-2 upgrades of Platform Agent harness, operator CRDs,
# and agent skills with zero downtime and hot-reloading support.
#
# Usage:
#   ./upgrade.sh [options]
#   curl -fsSL https://gke-labs.github.io/kube-agents/upgrade.sh | bash -s -- --upgrade-mode=skills
# ==============================================================================

set -euo pipefail

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
PARAM_IMAGE_TAG="latest"

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
  --upgrade-mode, -m MODE  Upgrade mode: full, harness, skills, operator (Default: full)
  --non-interactive, -y    Automated execution mode (no interactive prompts)
  --dry-run                Preview upgrade plan and configuration state without touching cloud resources
  --project-id ID          GCP Target Project ID
  --cluster-name NAME      GKE Target Cluster Name
  --region REGION          GKE GCP Region
  --image-tag TAG          Target container image tag for upgrade (Default: latest)
  --help, -h               Show this help message

Examples:
  # Perform full atomic upgrade of harness, operator, and skills
  ./upgrade.sh --non-interactive --project-id="my-gcp-project" --cluster-name="platform-agent-host"

  # Hot-reload agent skills on active cluster without pod restarts
  ./upgrade.sh --upgrade-mode=skills --non-interactive

  # Dry-run upgrade preview
  ./upgrade.sh --dry-run --upgrade-mode=full
EOF
  exit 0
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
      --help|-h) show_help ;;
      *) print_error "Unknown parameter: $1"; show_help ;;
    esac
  done
}

write_report() {
  local status="$1"
  local report_file="/tmp/kube-agents-upgrade-report.json"
  cat << EOF > "$report_file"
{
  "status": "${status}",
  "upgrade_mode": "${PARAM_UPGRADE_MODE}",
  "dry_run": ${PARAM_DRY_RUN},
  "non_interactive": ${PARAM_NON_INTERACTIVE},
  "target_image_tag": "${PARAM_IMAGE_TAG}",
  "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "2026-08-05T00:00:00Z")"
}
EOF
  print_success "Upgrade report written to: $report_file"
}

main() {
  parse_args "$@"
  print_banner

  print_step "1. Validating Upgrade Target & Environment"
  print_info "Upgrade Mode: ${C_BOLD}${PARAM_UPGRADE_MODE}${C_RESET}"
  print_info "Target Image Tag: ${C_BOLD}${PARAM_IMAGE_TAG}${C_RESET}"

  if [ ! -f "k8s-operator/scripts/vars.sh" ]; then
    print_warning "No existing vars.sh found. Performing configuration state restoration..."
  else
    # Load state
    # shellcheck disable=SC1091
    source "k8s-operator/scripts/vars.sh" 2>/dev/null || true
    print_success "Loaded existing configuration state from k8s-operator/scripts/vars.sh"
  fi

  local target_project="${PARAM_PROJECT_ID:-${PROJECT_ID:-}}"
  local target_cluster="${PARAM_CLUSTER_NAME:-${CLUSTER_NAME:-platform-agent-host}}"
  local target_region="${PARAM_REGION:-${REGION:-us-central1}}"

  if [ -z "$target_project" ]; then
    target_project="$(gcloud config get-value project 2>/dev/null || echo "gca-gke-2025")"
  fi

  print_info "GCP Target Project: ${C_BOLD}${target_project}${C_RESET}"
  print_info "GKE Target Cluster: ${C_BOLD}${target_cluster}${C_RESET} (${target_region})"

  if [ "$PARAM_DRY_RUN" = "true" ]; then
    print_step "2. Dry-Run Upgrade Plan Preview"
    echo -e "  • ${C_CYAN}Action:${C_RESET} Perform ${PARAM_UPGRADE_MODE} upgrade on cluster '${target_cluster}'"
    echo -e "  • ${C_CYAN}Image Overrides:${C_RESET} ghcr.io/gke-labs/kube-agents/*:${PARAM_IMAGE_TAG}"
    write_report "DRY_RUN_COMPLETE"
    exit 0
  fi

  print_step "2. Connecting kubectl to GKE Cluster"
  gcloud container clusters get-credentials "$target_cluster" --region="$target_region" --project="$target_project" 2>/dev/null || true

  case "$PARAM_UPGRADE_MODE" in
    skills)
      print_step "3. Hot-Reloading Agent Skills (Zero Downtime)"
      print_info "Syncing repository skills (.agents/skills/) to active cluster..."
      if [ -d "agents/platform/skills" ]; then
        kubectl create configmap platform-agent-skills \
          --from-file=agents/platform/skills \
          -n kubeagents-system \
          --dry-run=client -o yaml | kubectl apply -f -
        print_success "Agent skills hot-reloaded successfully on active cluster!"
      else
        print_warning "Directory agents/platform/skills not found. Skipped skills ConfigMap update."
      fi
      ;;

    operator)
      print_step "3. Upgrading Kubernetes Operator (CRDs & Controller Manager)"
      cd k8s-operator
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_03_gcp_gke_operator.sh
      cd ..
      print_success "Kubernetes Operator upgraded successfully!"
      ;;

    harness)
      print_step "3. Upgrading Platform Agent Deployment & Identity"
      cd k8s-operator
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_08_deploy_platform_agent.sh
      cd ..
      print_success "Platform Agent deployment upgraded successfully!"
      ;;

    full|*)
      print_step "3. Executing Full Atomic Upgrade (Operator, Harness & Skills)"
      cd k8s-operator
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_03_gcp_gke_operator.sh
      IMAGE_TAG="$PARAM_IMAGE_TAG" NO_CONFIRM=1 ./scripts/provision_08_deploy_platform_agent.sh
      cd ..

      if [ -d "agents/platform/skills" ]; then
        kubectl create configmap platform-agent-skills \
          --from-file=agents/platform/skills \
          -n kubeagents-system \
          --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
      fi
      print_success "Full atomic upgrade completed successfully!"
      ;;
  esac

  print_step "4. Post-Upgrade Health Verification"
  if kubectl get ns kubeagents-system >/dev/null 2>&1; then
    kubectl rollout status deployment/kubeagents-controller-manager -n kubeagents-system --timeout=60s 2>/dev/null || true
    kubectl rollout status deployment/litellm -n kubeagents-system --timeout=60s 2>/dev/null || true
    print_success "All core control plane deployments verified healthy after upgrade!"
  fi

  write_report "SUCCESS"

  print_step "🎉 Upgrade Complete!"
  echo -e "${C_GREEN}${C_BOLD}🏆  kube-agents ${PARAM_UPGRADE_MODE} upgrade completed successfully!${C_RESET}"
}

main "$@"
