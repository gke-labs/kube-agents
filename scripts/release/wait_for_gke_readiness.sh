#!/usr/bin/env bash
# Connects to GKE cluster and verifies that required deployments reach Ready state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

readonly READINESS_TIMEOUT="300s" # 5 minutes timeout for GKE pod readiness

CLUSTER_NAME="${GKE_CLUSTER_NAME:-${CLUSTER_NAME:-platform-agent-host}}"
REGION="${GCP_REGION:-${REGION:-us-central1}}"
PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-kube-agents-rc}}"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"

echo "======================================================================"
echo "⏳ CONNECTING TO GKE & WAITING FOR POD READINESS"
echo "Project ID:        ${PROJECT_ID}"
echo "Region:            ${REGION}"
echo "Cluster Name:      ${CLUSTER_NAME}"
echo "Target Commit SHA: ${COMMIT_SHA:-(not specified)}"
echo "Readiness Timeout: ${READINESS_TIMEOUT} (5 minutes)"
echo "======================================================================"

unset CLOUDSDK_PYTHON || true
unset CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE || true
export CLOUDSDK_PYTHON_SITEPACKAGES="0"
export PYTHONNOUSERSITE="1"
export USE_GKE_GCLOUD_AUTH_PLUGIN="True"
export CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS="false"
gcloud config set container/use_application_default_credentials false --quiet || true

if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ] && [ -f "${GOOGLE_APPLICATION_CREDENTIALS}" ]; then
  gcloud auth activate-service-account --key-file="${GOOGLE_APPLICATION_CREDENTIALS}" --quiet || true
fi

CURRENT_CTX="$(kubectl config current-context 2>/dev/null || echo "")"
if ! kubectl cluster-info >/dev/null 2>&1 || [[ "${CURRENT_CTX}" != *"${CLUSTER_NAME}"* || "${CURRENT_CTX}" != *"${PROJECT_ID}"* ]]; then
  echo "Connecting kubectl to target cluster '${CLUSTER_NAME}' in project '${PROJECT_ID}'..."
  gke_dns_endpoint_flag "${CLUSTER_NAME}" "${REGION}" "${PROJECT_ID}"
  # Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --location "${REGION}" --project "${PROJECT_ID}" \
    ${GKE_DNS_ENDPOINT_FLAG}
fi

echo "🔑 Configuring Docker authentication for Artifact Registry (${REGION}-docker.pkg.dev)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet || true

if [ -n "${COMMIT_SHA}" ]; then
  echo "🔍 Verifying platform-agent-gateway deployment container image matches commit ${COMMIT_SHA}..."
  start_time=$(date +%s)
  until kubectl get deploy/platform-agent-gateway -n kubeagents-system -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null | grep -q ":${COMMIT_SHA}"; do
    if [ $(($(date +%s) - start_time)) -gt 300 ]; then
      echo "❌ ERROR: Deployment platform-agent-gateway did not update to image tag :${COMMIT_SHA} within timeout!" >&2
      exit 1
    fi
    echo "Waiting for deployment platform-agent-gateway image to be updated to :${COMMIT_SHA}..."
    sleep 5
  done
  echo "✅ platform-agent-gateway deployment image matches candidate commit ${COMMIT_SHA}."
fi

echo "Waiting for litellm deployment readiness..."
kubectl rollout status deployment/litellm -n kubeagents-system --timeout="${READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/litellm -n kubeagents-system --timeout="${READINESS_TIMEOUT}"

echo "Waiting for platform-agent-gateway deployment readiness..."
kubectl rollout status deployment/platform-agent-gateway -n kubeagents-system --timeout="${READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/platform-agent-gateway -n kubeagents-system --timeout="${READINESS_TIMEOUT}"
