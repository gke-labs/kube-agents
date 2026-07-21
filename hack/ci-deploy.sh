#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
#
# Provisioning Script Mapping (k8s-operator/scripts/provision.sh):
#  - [Pre-Configured] Step 1 (provision_01): Cluster & GKE Context
#  - [Pre-Configured] Step 3 (provision_03): GCP IAM & Workload Identity
#  - [Runs]           Step 2 (provision_02): Operator Deploy
#  - [Runs]           Step 6 (provision_06): Secrets Setup
#  - [Runs]           Step 7 (provision_07): Agent Deploy
#  - [Runs]           Step 8 (provision_08): LiteLLM Deploy
# ==============================================================================

set -euo pipefail

# 1. Environment & PR Variables
# Connects to static GKE cluster 'platform-agent-host' (provision_01)
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"

PULL_SHA_SHORT="${PULL_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export NAMESPACE="kubeagents-system"
export AR_REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents"

echo "=== Deploying PR #${PULL_NUMBER:-local} (${TAG}) to Namespace: ${NAMESPACE} ==="

# 2. Cluster Auth
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

# 3. Build & Push PR Container Images via Cloud Build
gcloud builds submit --config="deploy/docker/cloudbuild.yaml" \
  --substitutions="_IMAGE_URI=${AR_REPO}/platform-agent:${TAG},_IMAGE_URI_LATEST=${AR_REPO}/platform-agent:latest,_TARGET=platform,_HERMES_AGENT_TAG=latest" \
  --project="${PROJECT_ID}" --quiet .

gcloud builds submit --tag="${AR_REPO}/kube-agents-operator:${TAG}" --project="${PROJECT_ID}" --quiet k8s-operator

# 4. Deploy Operator Controller Manager (provision_02_gcp_gke_operator)
# Verifies cert-manager CRDs exist on target GKE cluster (provision_02 verify_cert_manager)
kubectl get crd certificates.cert-manager.io >/dev/null

make -C k8s-operator deploy IMG="${AR_REPO}/kube-agents-operator:${TAG}"
# Wait for operator controller manager deployment to roll out and become ready
kubectl rollout status deployment/kubeagents-controller-manager -n kubeagents-system --timeout=600s

# 5. Secrets Setup (provision_06_gcp_secrets)
export API_SERVER_KEY="${API_SERVER_KEY:-$(openssl rand -hex 16)}"
export PLATFORM_AGENT_TOKEN="${API_SERVER_KEY}"
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
  exit 1
fi

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic platform-agent-secrets -n "${NAMESPACE}" \
  --from-literal=API_SERVER_KEY="${API_SERVER_KEY}" \
  --from-literal=GEMINI_API_KEY="${GEMINI_API_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

# 6. Deploy PlatformAgent Custom Resource (provision_07_gcp_platform_agent)
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
export MEMORY_ENABLED="false"
export USER_PROFILE_ENABLED="false"
export GOOGLE_CHAT_ENABLED="false"
export SLACK_ENABLED="false"

envsubst < k8s-operator/scripts/platform-agent.yaml.template | kubectl apply -n "${NAMESPACE}" -f -

# 7. Deploy LiteLLM Gateway (provision_08_gcp_litellm)
export MODEL_PROVIDER="gemini"
export MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"
make -C k8s-operator deploy-litellm
# Wait for LiteLLM gateway deployment to roll out and become ready
kubectl rollout status deployment/litellm -n "${NAMESPACE}" --timeout=300s

# 8. Readiness Check for PlatformAgent Deployment
echo "Waiting for deployment/platform-agent-gateway to be created by operator..."
until kubectl get deployment/platform-agent-gateway -n "${NAMESPACE}" &>/dev/null; do
  sleep 2
done

# Wait for platform agent gateway deployment to roll out and become ready
kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s

echo "=== Deployment Ready in Namespace: ${NAMESPACE} ==="