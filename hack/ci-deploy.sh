#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# Provisioning Script Mapping (k8s-operator/scripts/provision.sh):
#  - Pre-Configured: Step 1 (provision_01): Cluster & GKE Context
#  - Pre-Configured: Step 4 (provision_04): GCP IAM & Workload Identity
#  - Step 3 (provision_03): Operator Deploy
#  - Step 7 (provision_07): Secrets Setup
#  - Step 8 (provision_08): Agent Deploy
#  - Step 9 (provision_09): LiteLLM Deploy
# ==============================================================================

set -euo pipefail

# ─── 1. Validation & Pre-checks ───────────────────────────────────────────────
if [ -z "${GEMINI_API_KEY:-}" ]; then
  echo "ERROR: GEMINI_API_KEY environment variable is required"
  exit 1
fi

# ─── 2. Configuration Environment Variables ───────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
source "${SCRIPT_DIR}/../tags.env"
trap dump_prow_artifacts_on_failure EXIT

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export AR_REPO="us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents"

export IMG="${AR_REPO}/kube-agents-operator:${TAG}"
export AGENT_IMAGE="${AR_REPO}/platform-agent"
export AGENT_TAG="${TAG}"
export IMAGE_TAG="${TAG}"

export MODEL_PROVIDER="gemini"
export MODEL_DEFAULT_NAME="gemini-3.1-pro-preview"
# Default to enforcing CMEK database encryption on CI evaluation clusters.
# Set ALLOW_UNENCRYPTED_SECRETS=true to bypass CMEK checks on unencrypted test clusters.
export ALLOW_UNENCRYPTED_SECRETS="${ALLOW_UNENCRYPTED_SECRETS:-false}"

export KSA_NAME="kubeagents-platform-agent"
export GSA_NAME="kubeagents-platform-gsa"
export MEMORY_ENABLED="false"
export USER_PROFILE_ENABLED="false"
export GOOGLE_CHAT_ENABLED="false"
export SLACK_ENABLED="false"

# Optional Cloud Build private worker pool. Unset by default, so builds keep
# going to the project's default pool. Opt in by exporting a full resource
# name: projects/PROJECT/locations/REGION/workerPools/POOL
# The region is read back out of that name because `gcloud builds submit`
# otherwise falls back to the `global` region, which cannot reach a regional
# pool.
BUILD_POOL_ARGS=()
if [ -n "${CLOUD_BUILD_WORKER_POOL:-}" ]; then
  case "$CLOUD_BUILD_WORKER_POOL" in
    projects/*/locations/*/workerPools/*) ;;
    *)
      echo "ERROR: CLOUD_BUILD_WORKER_POOL must be a full resource name: projects/PROJECT/locations/REGION/workerPools/POOL"
      exit 1
      ;;
  esac
  BUILD_POOL_ARGS=(
    --worker-pool="$CLOUD_BUILD_WORKER_POOL"
    --region="$(echo "$CLOUD_BUILD_WORKER_POOL" | cut -d'/' -f4)"
  )
fi

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying PR #${PULL_NUMBER:-local} (${TAG}) to Namespace: ${NAMESPACE} ==="

# ─── 3. Cluster Auth ──────────────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# ─── 4. Build Container Images ────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Building Container Images (platform, credential-proxy, operator) ==="
gcloud builds submit --config="deploy/docker/cloudbuild.yaml" \
  --substitutions="_IMAGE_URI=${AR_REPO}/platform-agent:${TAG},_IMAGE_URI_LATEST=${AR_REPO}/platform-agent:latest,_TARGET=platform,_HERMES_AGENT_TAG=${HERMES_AGENT_TAG}" \
  --project="${PROJECT_ID}" ${BUILD_POOL_ARGS[@]+"${BUILD_POOL_ARGS[@]}"} --quiet .

gcloud builds submit --config="deploy/docker/cloudbuild.yaml" \
  --substitutions="_IMAGE_URI=${AR_REPO}/credential-proxy:${TAG},_IMAGE_URI_LATEST=${AR_REPO}/credential-proxy:latest,_TARGET=credential-proxy,_HERMES_AGENT_TAG=${HERMES_AGENT_TAG}" \
  --project="${PROJECT_ID}" ${BUILD_POOL_ARGS[@]+"${BUILD_POOL_ARGS[@]}"} --quiet .

gcloud builds submit --tag="${AR_REPO}/kube-agents-operator:${TAG}" --project="${PROJECT_ID}" ${BUILD_POOL_ARGS[@]+"${BUILD_POOL_ARGS[@]}"} --quiet k8s-operator
echo "✓ Container image builds finished in $((SECONDS - STEP_START))s"

# ─── 5. Provisioning Pipeline Execution ───────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Executing Provisioning Pipeline Scripts ==="
./k8s-operator/scripts/provision_03_gcp_gke_operator.sh --non-interactive
./k8s-operator/scripts/provision_07_gcp_k8s_secrets.sh --non-interactive
./k8s-operator/scripts/provision_08_deploy_platform_agent.sh --non-interactive
./k8s-operator/scripts/provision_09_deploy_litellm.sh --non-interactive
echo "✓ Provisioning scripts finished in $((SECONDS - STEP_START))s"

# ─── 6. Readiness Verification ────────────────────────────────────────────────
# Stage 13 owns the rollout gate (creation wait, rollout status, and failure
# diagnostics), so this stays a single copy rather than a hand-rolled twin.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying platform-agent rollout ==="
./k8s-operator/scripts/provision_14_verify_agent_rollout.sh --non-interactive
echo "✓ Rollout verification finished in $((SECONDS - STEP_START))s"

# ─── 7. Agent API Connectivity Verification ──────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying Platform Agent API Connectivity ==="
API_KEY="$(kubectl get secret platform-agent-secrets -n "${NAMESPACE}" -o jsonpath='{.data.API_SERVER_KEY}' | base64 --decode)"

kubectl port-forward svc/platform-agent -n "${NAMESPACE}" 8642:8642 >/tmp/pf-8642.log 2>&1 &
PF_PID=$!
cleanup_pf_and_dump() {
  kill "${PF_PID:-}" 2>/dev/null || true
  dump_prow_artifacts_on_failure
}
trap cleanup_pf_and_dump EXIT

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
  -d '{"model": "model-default", "input": "ping"}' || true)"
  
kill $PF_PID 2>/dev/null || true
trap dump_prow_artifacts_on_failure EXIT

if [[ "$HEALTH_RESP" == *"output"* || "$HEALTH_RESP" == *"assistant"* || "$HEALTH_RESP" == *"pong"* ]]; then
  echo "✓ Agent API Server responded successfully in $((SECONDS - STEP_START))s!"
else
  echo "ERROR: Platform Agent API server connectivity check failed!"
  echo "Response received: ${HEALTH_RESP}"
  echo "=== Debug: Port Forward Log ==="
  cat /tmp/pf-8642.log 2>/dev/null || true
  echo "=== Debug: Kubernetes Workloads in Namespace ${NAMESPACE} ==="
  kubectl get pods,svc -n "${NAMESPACE}" || true
  exit 1
fi

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deployment Ready in Namespace: ${NAMESPACE} (Total Duration: ${TOTAL_DURATION}s) ==="
