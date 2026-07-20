#!/usr/bin/env bash
set -euo pipefail

# 1. Environment & PR Variables
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"

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

# 5. Deploy Operator CRDs, Controller Manager, and LiteLLM Gateway
kubectl apply -k k8s-operator/config/crd
kubectl apply -k k8s-operator/config/default
kubectl set image deployment/kubeagents-controller-manager -n kubeagents-system manager="${AR_REPO}/kube-agents-operator:${TAG}" || true
kubectl rollout status deployment/kubeagents-controller-manager -n kubeagents-system --timeout=600s

export MODEL_PROVIDER="gemini"
export MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"
kubectl apply -k k8s-operator/config/integrations/litellm/base -n "${NAMESPACE}"
kubectl rollout status deployment/litellm -n "${NAMESPACE}" --timeout=300s

# 6. Apply PlatformAgent Custom Resource in PR Namespace (triggers mutating webhook)
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
export MEMORY_ENABLED="false"
export MEMORY_PROVIDER="none"
export USER_PROFILE_ENABLED="false"
export GITHUB_FULL_REPO="gke-labs/kube-agents"
export GOOGLE_CHAT_ENABLED="false"
export GOOGLE_CHAT_MODE="default"
export CHAT_TOPIC_NAME="platform-agent-chat-topic"
export CHAT_SUB_NAME="platform-agent-chat-sub"
export ALLOWED_USERS=""
export SLACK_ENABLED="false"
export SLACK_HOME_CHANNEL=""
export SLACK_HOME_CHANNEL_NAME=""
export SLACK_ALLOWED_USERS=""

# Use Python for template substitution since envsubst and make are not installed in the harness container
python3 -c "import os, sys; t=sys.stdin.read(); [t:=t.replace(f'\${{{k}}}', v) for k, v in os.environ.items()]; print(t)" < k8s-operator/scripts/platform-agent.yaml.template | kubectl apply -n "${NAMESPACE}" -f -

# 7. Readiness Check for PlatformAgent Deployment
echo "Waiting for deployment/platform-agent-gateway to be created by operator..."
until kubectl get deployment/platform-agent-gateway -n "${NAMESPACE}" &>/dev/null; do
  sleep 2
done

kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s

echo "=== Deployment Ready in Namespace: ${NAMESPACE} ==="