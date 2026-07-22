#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# Provisioning Script Mapping (k8s-operator/scripts/provision.sh):
#  - [Pre-Configured] Step 1 (provision_01): Cluster & GKE Context
#  - [Pre-Configured] Step 3 (provision_03): GCP IAM & Workload Identity
#  - [Runs]           Step 2 (provision_02): Operator Deploy
#  - [Runs]           Step 6 (provision_06): Secrets Setup
#  - [Runs]           Step 7 (provision_07): Agent Deploy
#  - [Runs]           Step 8 (provision_08): LiteLLM Deploy
# ==============================================================================

set -euo pipefail

# ─── 1. Validation & Pre-checks ───────────────────────────────────────────────
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
  exit 1
fi

# ─── 2. Configuration Environment Variables ───────────────────────────────────
export PROJECT_ID="kube-agents-evals"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="platform-agent-host"

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export NAMESPACE="kubeagents-system"
export AR_REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents"

export IMG="${AR_REPO}/kube-agents-operator:${TAG}"
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"

export MODEL_PROVIDER="gemini"
export MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"

export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
export MEMORY_ENABLED="false"
export USER_PROFILE_ENABLED="false"
export GOOGLE_CHAT_ENABLED="false"
export SLACK_ENABLED="false"

echo "=== Deploying PR #${PULL_NUMBER:-local} (${TAG}) to Namespace: ${NAMESPACE} ==="

# ─── 3. Cluster Auth ──────────────────────────────────────────────────────────
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

# ─── 4. Build Container Images ────────────────────────────────────────────────
# Temporarily disable github-issue-resolver cron job for Prow CI runs
python3 -c "import json; p='agents/platform/cron/jobs.json'; data=json.load(open(p)); [j.update({'enabled': False}) for j in data.get('jobs',[]) if j.get('id')=='github-issue-resolver']; json.dump(data, open(p,'w'), indent=2)" 2>/dev/null || true

gcloud builds submit --config="deploy/docker/cloudbuild.yaml" \
  --substitutions="_IMAGE_URI=${AR_REPO}/platform-agent:${TAG},_IMAGE_URI_LATEST=${AR_REPO}/platform-agent:latest,_TARGET=platform,_HERMES_AGENT_TAG=latest" \
  --project="${PROJECT_ID}" --quiet .

gcloud builds submit --tag="${AR_REPO}/kube-agents-operator:${TAG}" --project="${PROJECT_ID}" --quiet k8s-operator

# ─── 5. Provisioning Pipeline Execution ───────────────────────────────────────
./k8s-operator/scripts/provision_03_gcp_gke_operator.sh --non-interactive
./k8s-operator/scripts/provision_07_gcp_k8s_secrets.sh --non-interactive
./k8s-operator/scripts/provision_08_deploy_platform_agent.sh --non-interactive
./k8s-operator/scripts/provision_09_deploy_litellm.sh --non-interactive

# ─── 6. Readiness Verification ────────────────────────────────────────────────
echo "Waiting for deployment/platform-agent-gateway to be created by operator..."
until kubectl get deployment/platform-agent-gateway -n "${NAMESPACE}" &>/dev/null; do
  sleep 2
done

kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s

# ─── 7. Agent API Connectivity Verification ──────────────────────────────────
echo "=== Verifying Platform Agent API Connectivity ==="
API_KEY="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"

kubectl port-forward svc/platform-agent -n "${NAMESPACE}" 8642:8642 >/dev/null 2>&1 &
PF_PID=$!
trap 'kill $PF_PID 2>/dev/null || true' EXIT

echo "Waiting for platform-agent port-forward on port 8642..."
for i in {1..30}; do
  if nc -z localhost 8642 2>/dev/null; then
    break
  fi
  sleep 1
done

HEALTH_RESP="$(curl -s -X POST http://localhost:8642/v1/responses \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model": "hermes-agent", "input": "ping"}' || true)"

kill $PF_PID 2>/dev/null || true
trap - EXIT

if [[ "$HEALTH_RESP" == *"output"* || "$HEALTH_RESP" == *"assistant"* || "$HEALTH_RESP" == *"pong"* ]]; then
  echo "✓ Agent API Server responded successfully!"
else
  echo "ERROR: Platform Agent API server connectivity check failed!"
  echo "Response received: ${HEALTH_RESP}"
  echo "--- Agent Gateway Logs ---"
  kubectl logs deployment/platform-agent-gateway -n "${NAMESPACE}" -c platform-agent --tail=200 || true
  exit 1
fi

echo "=== Deployment Ready in Namespace: ${NAMESPACE} ==="