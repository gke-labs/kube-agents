#!/usr/bin/env bash
# ==============================================================================
# 🔄 Utility: Update GKE Cluster Name in PlatformAgent CRD
# ==============================================================================
# Renders and patches the PlatformAgent resource with a new target GKE cluster name.
# ==============================================================================

set -e

NAMESPACE="kubeagents-system"
AGENT_NAME="platform-agent"

# Colors for output
C_GREEN="\033[0;32m"
C_RED="\033[0;31m"
C_YELLOW="\033[0;33m"
C_RESET="\033[0m"
C_BOLD="\033[1m"

print_info() {
  echo -e "${C_YELLOW}[INFO]${C_RESET} $1"
}

print_success() {
  echo -e "${C_GREEN}[SUCCESS]${C_RESET} $1"
}

print_error() {
  echo -e "${C_RED}[ERROR]${C_RESET} $1"
}

# 1. Parse arguments or prompt user
CLUSTER_NAME="$1"
if [ -z "$CLUSTER_NAME" ]; then
  read -p "Enter new GKE Cluster Name to watch: " CLUSTER_NAME
fi

if [ -z "$CLUSTER_NAME" ]; then
  print_error "Cluster name cannot be empty."
  exit 1
fi

# 2. Check if platform-agent resource exists in the cluster
print_info "Verifying PlatformAgent resource '${AGENT_NAME}' in namespace '${NAMESPACE}'..."
if ! kubectl get platformagent "${AGENT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
  print_error "PlatformAgent resource '${AGENT_NAME}' not found in namespace '${NAMESPACE}'."
  print_info "Make sure you are connected to the correct GKE cluster context."
  exit 1
fi

# 3. Patch the resource
print_info "Updating cluster name to '${CLUSTER_NAME}'..."
if kubectl patch platformagent "${AGENT_NAME}" -n "${NAMESPACE}" --type='merge' -p "{\"spec\":{\"harness\":{\"clusterName\":\"${CLUSTER_NAME}\"}}}" >/dev/null 2>&1; then
  print_success "Successfully updated GKE cluster name to '${CLUSTER_NAME}' in PlatformAgent spec!"
  print_info "The operator will automatically reconcile the deployment and restart the sidecar container shortly."
else
  print_error "Failed to patch PlatformAgent resource."
  exit 1
fi
