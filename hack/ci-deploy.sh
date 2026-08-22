#!/usr/bin/env bash
# ==============================================================================
# Prow CI Deployment Pipeline Script
# ==============================================================================
# The evaluation cluster and its IAM are pre-configured; this script builds
# the PR's images and deploys the kube-agents chart onto that cluster.
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
ensure_helm

RAW_PULL_SHA="${PULL_PULL_SHA:-latest}"
PULL_SHA_SHORT="${RAW_PULL_SHA:0:7}"
export TAG="pr-${PULL_NUMBER:-local}-${PULL_SHA_SHORT:-latest}"
export AR_REPO="${AR_REPO:-us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents}"

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

# Where the image builds run. Either a private worker pool or a sized machine
# on the default pool -- never both, because a pool declares its own machine
# and rejects being told a different one.
#
# Opt into a pool by exporting CLOUD_BUILD_WORKER_POOL as a full resource name:
# projects/PROJECT/locations/REGION/workerPools/POOL. Unset by default, which
# is the CI path. The region is read back out of that name because
# `gcloud builds submit` otherwise falls back to the `global` region, which
# cannot reach a regional pool.
if [ -n "${CLOUD_BUILD_WORKER_POOL:-}" ]; then
  case "$CLOUD_BUILD_WORKER_POOL" in
    projects/*/locations/*/workerPools/*) ;;
    *)
      echo "ERROR: CLOUD_BUILD_WORKER_POOL must be a full resource name: projects/PROJECT/locations/REGION/workerPools/POOL"
      exit 1
      ;;
  esac
  BUILD_WORKER_ARGS=(
    --worker-pool="$CLOUD_BUILD_WORKER_POOL"
    --region="$(echo "$CLOUD_BUILD_WORKER_POOL" | cut -d'/' -f4)"
  )
else
  # The default pool's unspecified machine is two vCPUs, which is most of why
  # the image builds are the single largest phase of this job. The build also
  # runs the operator step alongside the agent build (see
  # deploy/docker/cloudbuild-ci.yaml), and that is only real overlap on a
  # worker with cores to spare rather than two contending for the same pair.
  BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)
fi

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying PR #${PULL_NUMBER:-local} (${TAG}) to Namespace: ${NAMESPACE} ==="

# ─── 3. Cluster Auth ──────────────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Authenticating to GKE Cluster ==="
gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG
echo "✓ Cluster authentication finished in $((SECONDS - STEP_START))s"

# ─── 4. Build Container Images ────────────────────────────────────────────────
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Building Container Images (platform, credential-proxy, operator) ==="
# One submit, not three. The two agent images share the agent-base chain, so
# building them as consecutive steps on one worker lets the second reuse the
# first's layers instead of rebuilding that chain on a cold daemon; the operator
# build runs alongside them. See the header of cloudbuild-ci.yaml, and #635.
# Set REQUIRE_CACHE=true in the job environment to fail the build on a cache
# miss instead of cold-building. Default false so a broken cache source cannot
# block the PR that fixes it.
export CACHE_IMAGE="${CACHE_IMAGE:-us-docker.pkg.dev/kube-agents-prow/kube-agents/platform-agent:latest}"
gcloud builds submit --config="deploy/docker/cloudbuild-ci.yaml" \
  --substitutions="_PLATFORM_URI=${AR_REPO}/platform-agent:${TAG},_PROXY_URI=${AR_REPO}/credential-proxy:${TAG},_OPERATOR_URI=${AR_REPO}/kube-agents-operator:${TAG},_CACHE_IMAGE=${CACHE_IMAGE},_HERMES_AGENT_TAG=${HERMES_AGENT_TAG},_REQUIRE_CACHE=${REQUIRE_CACHE:-false}" \
  --project="${PROJECT_ID}" "${BUILD_WORKER_ARGS[@]}" --quiet .
echo "✓ Container image builds finished in $((SECONDS - STEP_START))s"

# ─── 5. Chart Deployment ──────────────────────────────────────────────────────
# One helm release carries the whole install — operator, credentials Secret,
# agent CR, and LiteLLM — so there is nothing to apply piecemeal or keep in order.
# Webhooks stay at the chart's default (off): a PR evaluation cluster carries
# no cert-manager, and admission-webhook coverage belongs to the operator's
# own test suite rather than this smoke pipeline.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Deploying the kube-agents chart ==="
API_SERVER_KEY="${API_SERVER_KEY:-$(openssl rand -hex 16)}"
helm upgrade --install kube-agents ./charts/kube-agents \
  --namespace "${NAMESPACE}" --create-namespace \
  --set-string "operator.image.repository=${AR_REPO}/kube-agents-operator" \
  --set-string "operator.image.tag=${TAG}" \
  --set-string "platformAgent.deployment.image.repository=${AR_REPO}/platform-agent" \
  --set-string "platformAgent.deployment.image.tag=${TAG}" \
  --set-string "platformAgent.harness.clusterName=${CLUSTER_NAME}" \
  --set-string "platformAgent.harness.location=${REGION}" \
  --set-string "platformAgent.harness.projectId=${PROJECT_ID}" \
  --set-string "platformAgent.security.serviceAccountAnnotations.iam\.gke\.io/gcp-service-account=${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set "platformAgent.credentials.create=true" \
  --set-string "platformAgent.credentials.data.API_SERVER_KEY=${API_SERVER_KEY}" \
  --set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --set-string "litellm.modelProvider=${MODEL_PROVIDER}" \
  --set-string "litellm.modelDefaultName=${MODEL_DEFAULT_NAME}" \
  --wait --timeout 15m
echo "✓ Chart deployment finished in $((SECONDS - STEP_START))s"

# ─── 6. Readiness Verification ────────────────────────────────────────────────
# helm --wait covers the chart-created Deployments (operator, LiteLLM); the
# agent Deployment is created by the operator reconciling the CR, so it gets
# its own gate with diagnostics.
STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying platform-agent rollout ==="
for i in {1..60}; do
  kubectl get deployment platform-agent-gateway -n "${NAMESPACE}" >/dev/null 2>&1 && break
  sleep 5
done
if ! kubectl rollout status deployment/platform-agent-gateway -n "${NAMESPACE}" --timeout=600s; then
  echo "ERROR: platform-agent-gateway rollout failed"
  kubectl describe deployment/platform-agent-gateway -n "${NAMESPACE}" || true
  kubectl get pods -n "${NAMESPACE}" || true
  kubectl logs -n "${NAMESPACE}" -l app=platform-agent-gateway --all-containers --tail=50 || true
  exit 1
fi
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
