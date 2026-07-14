#!/usr/bin/env bash
# ==============================================================================
# 🚀 Cloud Shell IaC Deployer Wrapper (State Persistence on Host)
# ==============================================================================
set -euo pipefail

# ANSI Colors
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'

print_step() { echo -e "\n${C_BOLD}${C_CYAN}>>>${C_RESET} ${C_BOLD}$1${C_RESET} ${C_BOLD}${C_CYAN}<<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓${C_RESET} $1"; }
print_info() { echo -e "  ${C_CYAN}ℹ${C_RESET} $1"; }
print_error() { echo -e "  ${C_RED}✗${C_RESET} $1"; }

ACTION="${1:-}"
if [[ "${ACTION}" != "provision" && "${ACTION}" != "teardown" ]]; then
  print_error "Usage: $0 [provision|teardown] [arguments...]"
  exit 1
fi
shift

# Parse arguments to find cluster name and project ID for state secret
CLUSTER_NAME="kube-agents-dedicated-cluster"
PROJECT_ID=""
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [[ "${args[i]}" == "-c" || "${args[i]}" == "--cluster-name" ]]; then
    CLUSTER_NAME="${args[i+1]}"
  fi
  if [[ "${args[i]}" == "-p" || "${args[i]}" == "--project-id" ]]; then
    PROJECT_ID="${args[i+1]}"
  fi
done

# Resolve project ID if not explicitly provided
if [ -z "${PROJECT_ID}" ]; then
  if command -v gcloud &>/dev/null; then
    PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
  fi
fi

if [ -z "${PROJECT_ID}" ]; then
  print_error "GCP Project ID is required. Please specify it using -p/--project-id or set your active project in gcloud."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TF_DIR="${ROOT_DIR}/k8s-operator/deploy/terraform"
SECRET_NAME="tf-state-${CLUSTER_NAME}"
TF_STATE_PATH="${TF_DIR}/terraform.tfstate.${CLUSTER_NAME}"

print_step "Initializing Cloud Shell Deployment: ${ACTION}"
print_info "Using current kubectl context to manage state..."
CURRENT_CONTEXT=$(kubectl config current-context)
print_info "Active Context: ${CURRENT_CONTEXT}"

# 1. Restore state if it exists
if kubectl get secret "${SECRET_NAME}" >/dev/null 2>&1; then
  print_info "Found existing Terraform state in Kubernetes Secret ${SECRET_NAME}. Restoring..."
  kubectl get secret "${SECRET_NAME}" -o jsonpath='{.data.state}' | base64 -d > "${TF_STATE_PATH}"
else
  print_info "No existing Terraform state found in Kubernetes Secret ${SECRET_NAME}."
fi

# 2. Run the action
EXIT_CODE=0
if [ "${ACTION}" = "provision" ]; then
  print_step "Running Provisioning Script..."
  "${SCRIPT_DIR}/provision_iac.sh" "$@" || EXIT_CODE=$?
elif [ "${ACTION}" = "teardown" ]; then
  print_step "Running Teardown Script..."
  "${SCRIPT_DIR}/teardown_iac.sh" "$@" || EXIT_CODE=$?
fi

# 3. Persist or clean up state
if [ ${EXIT_CODE} -eq 0 ]; then
  if [ "${ACTION}" = "provision" ]; then
    if [ -f "${TF_STATE_PATH}" ]; then
      print_step "Saving Terraform state to Kubernetes Secret ${SECRET_NAME}..."
      kubectl create secret generic "${SECRET_NAME}" \
        --from-file=state="${TF_STATE_PATH}" \
        --dry-run=client -o yaml | kubectl apply -f -
      print_success "State successfully persisted."
    else
      print_error "Warning: Provisioning succeeded but no state file was found at ${TF_STATE_PATH}."
    fi
  elif [ "${ACTION}" = "teardown" ]; then
    print_step "Cleaning up state store..."
    kubectl delete secret "${SECRET_NAME}" --ignore-not-found=true
    print_success "State secret deleted."
  fi
else
  print_error "Action '${ACTION}' failed with exit code ${EXIT_CODE}. State will not be updated."
fi

exit ${EXIT_CODE}
