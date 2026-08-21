#!/usr/bin/env bash
# ==============================================================================
# 🚀 Staging Environment PoC Deployer (Multi-Cluster, Workload Bundle)
# ==============================================================================
# This script provisions GKE Autopilot & GKE Standard clusters dynamically using
# Terraform and deploys a modular bundle of workloads + simulators using Helm.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_DIR="${SCRIPT_DIR}/charts/workload-bundle"

# gke_dns_endpoint_flag: the clusters main.tf creates enable external DNS access,
# so both the credential fetch and the copy-paste hints below must name it.
# shellcheck source=k8s-operator/scripts/gke_dns_endpoint.sh
source "${SCRIPT_DIR}/../../scripts/gke_dns_endpoint.sh"

# ANSI Colors
C_CYAN='\033[96m'
C_GREEN='\033[92m'
C_YELLOW='\033[93m'
C_RED='\033[91m'
C_RESET='\033[0m'
C_BOLD='\033[1m'

log_info() {
  echo -e "${C_CYAN}INFO: $1${C_RESET}"
}

log_success() {
  echo -e "${C_GREEN}SUCCESS: $1${C_RESET}"
}

log_warn() {
  echo -e "${C_YELLOW}WARN: $1${C_RESET}"
}

log_error() {
  echo -e "${C_RED}ERROR: $1${C_RESET}"
}

# Prerequisites Check
PREREQS=("terraform" "helm" "gcloud" "kubectl")
for cmd in "${PREREQS[@]}"; do
  if ! command -v "$cmd" &> /dev/null; then
    log_error "Prerequisite '$cmd' is not installed. Please install it and retry."
    exit 1
  fi
done

# 1. Terraform Provisioning
log_info "Initializing Terraform..."
cd "$SCRIPT_DIR"
terraform init -input=false

log_info "Applying Terraform configuration (creating GKE cluster)..."
log_info "This may take 5-10 minutes..."
terraform apply -input=false -auto-approve

# Retrieve outputs from Terraform
PROJECT_ID=$(terraform output -raw project_id)
STANDARD_CLUSTERS_LIST=$(terraform output -raw standard_clusters_list)

# Fetch Autopilot clusters list as a pipe-delimited text string directly from Terraform
AUTOPILOT_CLUSTERS_LIST=$(terraform output -raw autopilot_clusters_list)

log_success "Terraform apply completed."

NAMESPACE="staging-workloads"

deploy_to_cluster() {
  local c_name="$1"
  local c_loc="$2"
  local p_id="$3"
  local c_type="$4"
  local t_shape="$5"
  local e_ray="${6:-false}"

  log_info "------------------------------------------------------------"
  log_info "Deploying to GKE ${c_type} Cluster: ${c_name} in ${c_loc}..."
  log_info "------------------------------------------------------------"

  log_info "Configuring kubectl credentials..."
  gke_dns_endpoint_flag "${c_name}" "${c_loc}" "${p_id}"
  # Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
  # shellcheck disable=SC2086
  gcloud container clusters get-credentials "${c_name}" --region "${c_loc}" --project "${p_id}" \
    ${GKE_DNS_ENDPOINT_FLAG}

  log_info "Deploying workload bundle via Helm to namespace '${NAMESPACE}'..."
  helm upgrade --install workload-bundle "$CHART_DIR" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --set trafficSimulator.trafficShape="${t_shape}" \
    --set rayCluster.enabled="${e_ray}" \
    --timeout 10m \
    --wait

  log_success "Workload bundle deployed successfully to ${c_name}!"
  
  log_info "Verifying deployments..."
  kubectl get all -n "$NAMESPACE"
}

# Deploy to GKE Autopilot clusters dynamically
while IFS='|' read -r c_name c_loc t_shape e_ray; do
  if [ -n "$c_name" ]; then
    deploy_to_cluster "$c_name" "$c_loc" "$PROJECT_ID" "Autopilot" "$t_shape" "$e_ray"
  fi
done <<< "$AUTOPILOT_CLUSTERS_LIST"

# Deploy to GKE Standard clusters dynamically
while IFS='|' read -r c_name c_loc t_shape; do
  if [ -n "$c_name" ]; then
    deploy_to_cluster "$c_name" "$c_loc" "$PROJECT_ID" "Standard" "$t_shape" "false"
  fi
done <<< "$STANDARD_CLUSTERS_LIST"

log_success "All deployments complete across all configured clusters!"

# The hints below are meant to be pasted verbatim, so they carry the same flag
# the deploy used. Resolved per cluster rather than hard-coded: main.tf asks for
# external DNS access on every cluster in the map, but the flag also depends on
# what the local gcloud supports and on the describe succeeding, and either can
# leave a cluster on its IP endpoint. See gke_dns_endpoint.sh.
while IFS='|' read -r c_name c_loc t_shape _; do
  if [ -n "$c_name" ]; then
    gke_dns_endpoint_flag "${c_name}" "${c_loc}" "${PROJECT_ID}"
    log_info "To monitor traffic simulator logs on Autopilot cluster ${c_name}, switch context and run:"
    log_info "  gcloud container clusters get-credentials ${c_name} --region ${c_loc} --project ${PROJECT_ID} ${GKE_DNS_ENDPOINT_FLAG}"
    log_info "  kubectl logs -n ${NAMESPACE} -l app=traffic-simulator --tail=50 -f"
  fi
done <<< "$AUTOPILOT_CLUSTERS_LIST"

while IFS='|' read -r c_name c_loc _; do
  if [ -n "$c_name" ]; then
    gke_dns_endpoint_flag "${c_name}" "${c_loc}" "${PROJECT_ID}"
    log_info "To monitor traffic simulator logs on Standard cluster ${c_name}, switch context and run:"
    log_info "  gcloud container clusters get-credentials ${c_name} --region ${c_loc} --project ${PROJECT_ID} ${GKE_DNS_ENDPOINT_FLAG}"
    log_info "  kubectl logs -n ${NAMESPACE} -l app=traffic-simulator --tail=50 -f"
  fi
done <<< "$STANDARD_CLUSTERS_LIST"
