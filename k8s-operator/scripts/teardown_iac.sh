#!/usr/bin/env bash
# ==============================================================================
# 🤖 Hybrid E2E Terraform Teardown Script
# ==============================================================================
set -euo pipefail

# Google Environment Terraform Wrapper Function with state isolation
# Clear HTTP/HTTPS proxy variables
export HTTP_PROXY=""
export HTTPS_PROXY=""
export http_proxy=""
export https_proxy=""
export NO_PROXY="*"

# ANSI Colors
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'

print_step() { echo -e "\n${C_BOLD}${C_CYAN}>>>${C_RESET} ${C_BOLD}$1${C_RESET} ${C_BOLD}${C_CYAN}<<<${C_RESET}"; }
print_success() { echo -e "  ${C_GREEN}✓${C_RESET} $1"; }
print_error() { echo -e "  ${C_RED}✗${C_RESET} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TF_DIR="${ROOT_DIR}/deploy/terraform"

# Usage / Help Message
usage() {
  echo -e "${C_BOLD}Usage:${C_RESET} $0 [required-options] [optional-options]"
  echo ""
  echo -e "${C_BOLD}Required Options:${C_RESET}"
  echo "  -p, --project-id VALUE         Target GCP Project ID"
  echo "  -r, --region VALUE             GCP Region for the GKE cluster"
  echo "  -c, --cluster-name VALUE       GKE Cluster Name"
  echo "  -n, --namespace VALUE          Kubernetes namespace"
  echo ""
  echo -e "${C_BOLD}Optional Options:${C_RESET}"
  echo "  -go, --github-org VALUE        GitHub Organization/Owner name (for Token Minter)"
  echo "  -gr, --github-repo VALUE       GitHub Repository name (for Token Minter)"
  echo "  -ga, --github-app-id VALUE     GitHub App ID (for Token Minter)"
  echo "  -gp, --github-pem-path VALUE   GitHub App Private Key PEM file path"
  echo ""
  exit 1
}

# Initialize variables
PROJECT_ID=""
REGION=""
CLUSTER_NAME=""
NAMESPACE=""
GITHUB_ORG=""
GITHUB_REPO=""
GITHUB_APP_ID=""
GITHUB_PEM_PATH=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--project-id)
      PROJECT_ID="$2"
      shift 2
      ;;
    -r|--region)
      REGION="$2"
      shift 2
      ;;
    -c|--cluster-name)
      CLUSTER_NAME="$2"
      shift 2
      ;;
    -n|--namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    -go|--github-org)
      GITHUB_ORG="$2"
      shift 2
      ;;
    -gr|--github-repo)
      GITHUB_REPO="$2"
      shift 2
      ;;
    -ga|--github-app-id)
      GITHUB_APP_ID="$2"
      shift 2
      ;;
    -gp|--github-pem-path)
      GITHUB_PEM_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      print_error "Unknown option: $1"
      usage
      ;;
  esac
done

# Validate parameters
if [ -z "${PROJECT_ID}" ] || [ -z "${REGION}" ] || [ -z "${CLUSTER_NAME}" ] || [ -z "${NAMESPACE}" ]; then
  print_error "Missing required arguments: project-id, region, cluster-name, and namespace must all be specified."
  echo ""
  usage
fi

# Generate the same GSA and Pub/Sub names as in provision_iac.sh to match the state
if [ "${CLUSTER_NAME}" != "kube-agents-dedicated-cluster" ]; then
  SUFFIX="${CLUSTER_NAME#kube-agents-}"
  SUFFIX=$(echo "${SUFFIX}" | tr -cd 'a-zA-Z0-9-')
  CONTROLLER_GSA=$(echo "ka-ctrl-${SUFFIX}" | cut -c1-30)
  PLATFORM_GSA=$(echo "ka-plat-${SUFFIX}" | cut -c1-30)
  OPERATOR_GSA=$(echo "ka-oper-${SUFFIX}" | cut -c1-30)
  DEVTEAM_GSA=$(echo "ka-dev-${SUFFIX}" | cut -c1-30)
  GITHUB_MINTER_GSA=$(echo "ka-git-${SUFFIX}" | cut -c1-30)
  TOPIC_NAME="platform-agent-chat-events-${SUFFIX}"
  SUB_NAME="platform-agent-chat-events-sub-${SUFFIX}"
  DELETION_PROTECTION="false"
else
  CONTROLLER_GSA="kubeagents-controller-gsa"
  PLATFORM_GSA="kubeagents-platform-gsa"
  OPERATOR_GSA="kubeagents-operator-gsa"
  DEVTEAM_GSA="kubeagents-devteam-gsa"
  GITHUB_MINTER_GSA="kubeagents-github-minter-gsa"
  TOPIC_NAME="platform-agent-chat-events"
  SUB_NAME="platform-agent-chat-events-sub"
  DELETION_PROTECTION="false"
fi

# Check if the state file exists
STATE_FILE="${TF_DIR}/terraform.tfstate.${CLUSTER_NAME}"
if [ ! -f "${STATE_FILE}" ]; then
  print_error "Terraform state file not found at ${STATE_FILE}."
  print_error "Nothing to tear down or cannot determine resources. Exiting."
  exit 1
fi

print_step "Starting Teardown of Kube-Agents IaC Environment: ${CLUSTER_NAME}"

cd "${TF_DIR}"

# Step 1: Disable deletion protection in Terraform state
print_step "Disabling GKE Cluster deletion protection in Terraform state"
terraform apply -auto-approve \
  -target=google_container_cluster.primary \
  -state="terraform.tfstate.${CLUSTER_NAME}" \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="cluster_name=${CLUSTER_NAME}" \
  -var="namespace=${NAMESPACE}" \
  -var="controller_gsa_name=${CONTROLLER_GSA}" \
  -var="platform_gsa_name=${PLATFORM_GSA}" \
  -var="operator_gsa_name=${OPERATOR_GSA}" \
  -var="devteam_gsa_name=${DEVTEAM_GSA}" \
  -var="github_minter_gsa_name=${GITHUB_MINTER_GSA}" \
  -var="github_org=${GITHUB_ORG:-}" \
  -var="github_repo=${GITHUB_REPO:-}" \
  -var="github_app_id=${GITHUB_APP_ID:-}" \
  -var="gchat_topic_name=${TOPIC_NAME}" \
  -var="gchat_subscription_name=${SUB_NAME}" \
  -var="deletion_protection=false"

# Step 2: Run Terraform Destroy
print_step "Running Terraform Destroy"
terraform destroy -auto-approve \
  -state="terraform.tfstate.${CLUSTER_NAME}" \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="cluster_name=${CLUSTER_NAME}" \
  -var="namespace=${NAMESPACE}" \
  -var="controller_gsa_name=${CONTROLLER_GSA}" \
  -var="platform_gsa_name=${PLATFORM_GSA}" \
  -var="operator_gsa_name=${OPERATOR_GSA}" \
  -var="devteam_gsa_name=${DEVTEAM_GSA}" \
  -var="github_minter_gsa_name=${GITHUB_MINTER_GSA}" \
  -var="github_org=${GITHUB_ORG:-}" \
  -var="github_repo=${GITHUB_REPO:-}" \
  -var="github_app_id=${GITHUB_APP_ID:-}" \
  -var="gchat_topic_name=${TOPIC_NAME}" \
  -var="gchat_subscription_name=${SUB_NAME}" \
  -var="deletion_protection=${DELETION_PROTECTION}"

# Clean up local state files
print_step "Cleaning up local state files"
rm -f "${STATE_FILE}"
rm -f "${STATE_FILE}.backup"

print_success "Teardown of ${CLUSTER_NAME} completed successfully!"
