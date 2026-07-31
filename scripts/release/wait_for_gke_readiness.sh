#!/usr/bin/env bash
# Connects to GKE cluster and verifies that required deployments reach Ready state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

readonly READINESS_TIMEOUT="300s" # 5 minutes timeout for GKE pod readiness

CLUSTER_NAME="${GKE_CLUSTER_NAME:-${CLUSTER_NAME:-platform-agent-host}}"
REGION="${GCP_REGION:-${REGION:-us-east4}}"
PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-kube-agents-rc}}"

echo "======================================================================"
echo "⏳ CONNECTING TO GKE & WAITING FOR POD READINESS"
echo "Project ID:        ${PROJECT_ID}"
echo "Region:            ${REGION}"
echo "Cluster Name:      ${CLUSTER_NAME}"
echo "Readiness Timeout: ${READINESS_TIMEOUT} (5 minutes)"
echo "======================================================================"

gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" --project "${PROJECT_ID}"

echo "Waiting for litellm deployment readiness..."
kubectl rollout status deployment/litellm -n kubeagents-system --timeout="${READINESS_TIMEOUT}"

echo "Waiting for platform-agent-gateway deployment readiness..."
kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system --timeout="${READINESS_TIMEOUT}"
