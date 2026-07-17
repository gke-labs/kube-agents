#!/usr/bin/env bash
set -euo pipefail

# 1. Environment & PR Variables
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="${CLUSTER_NAME:-evals-target-cluster}"

# Determine Git SHA / PR tag and PR-specific namespace
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

# 4. Provision PR Namespace & Dynamic Ephemeral Secrets
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

# 5. Reuse Standard Makefile Targets (PlatformAgent CRDs, Operator, LiteLLM)
make -C k8s-operator install
make -C k8s-operator deploy IMG="${AR_REPO}/kube-agents-operator:${TAG}"
kubectl rollout status deployment/kubeagents-controller-manager -n kubeagents-system --timeout=600s

make -C k8s-operator deploy-litellm MODEL_PROVIDER="gemini" MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"
kubectl rollout status deployment/litellm -n "${NAMESPACE}" --timeout=300s

# 6. Apply PlatformAgent Custom Resource in PR Namespace (triggers mutating webhook)
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
envsubst < k8s-operator/scripts/platform-agent.yaml.template | kubectl apply -n "${NAMESPACE}" -f -

# 7. Readiness Check for PlatformAgent Deployment
echo "Waiting for deployment/platform-agent-gateway to be created by operator..."
until kubectl get deployment/platform-agent-gateway -n "${NAMESPACE}" &>/dev/null; do
  sleep 2
done

kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s

echo "=== Deployment Ready in Namespace: ${NAMESPACE} ==="