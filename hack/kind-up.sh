#!/usr/bin/env bash
# hack/kind-up.sh -- run kube-agents in a local kind cluster.
#
# Builds the images from this checkout, creates a kind cluster, installs the
# chart with LiteLLM routed to the Gemini API, and prints the command that runs
# a bench case against it. Most of the evals depend on GKE or GCP, so we can't
# run them; bench/tasks/chat-routing-own-cluster-namespaces is one that works.
#
# This script can be re-run; it deploys the latest code.
#
# Usage:
#   hack/kind-up.sh
#   hack/kind-up.sh --delete
#
# Environment:
#   GEMINI_API_KEY      required (not with --delete)
#   KIND_CLUSTER_NAME   default: kube-agents
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLUSTER_NAME="${KIND_CLUSTER_NAME:-kube-agents}"
CONTEXT="kind-${CLUSTER_NAME}"
NAMESPACE="kubeagents-system"
TAG="kind-local"
AGENT_IMAGE="kind.local/platform-agent:${TAG}"
PROXY_IMAGE="kind.local/credential-proxy:${TAG}"
SANDBOX_IMAGE="kind.local/agent-sandbox:${TAG}"
OPERATOR_IMAGE="kind.local/k8s-operator:${TAG}"

DELETE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --delete) DELETE=1 ;;
    -h|--help) sed -n '2,/^[^#]/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

KEY_DIR=""
PF_PID=""
cleanup() {
  [ -n "${PF_PID}" ] && { kill "${PF_PID}" && wait "${PF_PID}"; } 2>/dev/null || true
  [ -n "${KEY_DIR}" ] && rm -rf "${KEY_DIR}"
  return 0
}
trap cleanup EXIT

if [ "${DELETE}" = 1 ]; then
  kind delete cluster --name "${CLUSTER_NAME}"
  exit 0
fi

for tool in docker kind helm kubectl ssh-keygen openssl curl; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not on PATH"
done
docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
[ -n "${GEMINI_API_KEY:-}" ] || die "GEMINI_API_KEY is not set"

# ─── Images (the published ones are linux/amd64 only; build for the daemon) ──
PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"
HERMES_AGENT_TAG="$(sed -n 's/^HERMES_AGENT_TAG=//p' "${REPO_ROOT}/tags.env")"
log "building images for ${PLATFORM}"
docker build --platform "${PLATFORM}" --build-arg KUBE_AGENTS_VERSION="${TAG}" --build-arg HERMES_AGENT_TAG="${HERMES_AGENT_TAG}" \
  --target platform -t "${AGENT_IMAGE}" -f "${REPO_ROOT}/deploy/docker/Dockerfile" "${REPO_ROOT}"
docker build --platform "${PLATFORM}" --build-arg KUBE_AGENTS_VERSION="${TAG}" --build-arg HERMES_AGENT_TAG="${HERMES_AGENT_TAG}" \
  --target credential-proxy -t "${PROXY_IMAGE}" -f "${REPO_ROOT}/deploy/docker/Dockerfile" "${REPO_ROOT}"
docker build --platform "${PLATFORM}" -t "${SANDBOX_IMAGE}" -f "${REPO_ROOT}/deploy/sandbox/Dockerfile" "${REPO_ROOT}"
docker build --platform "${PLATFORM}" --build-arg VERSION="${TAG}" -t "${OPERATOR_IMAGE}" "${REPO_ROOT}/k8s-operator"

# Deploy under tags derived from the image layers, so a changed image gets a
# new tag and its workload rolls, and an unchanged one does not. (The image ID
# changes on every build, the layers do not.) The operator derives the
# credential-proxy image from the agent image's tag, so those two share one
# tag built from both.
image_id() { docker image inspect --format '{{join .RootFS.Layers ","}}' "$1" | openssl dgst -sha256 | cut -d' ' -f2 | cut -c1-12; }
AGENT_TAG="kind-$(image_id "${AGENT_IMAGE}")$(image_id "${PROXY_IMAGE}")"
SANDBOX_TAG="kind-$(image_id "${SANDBOX_IMAGE}")"
OPERATOR_TAG="kind-$(image_id "${OPERATOR_IMAGE}")"
docker tag "${AGENT_IMAGE}" "kind.local/platform-agent:${AGENT_TAG}"
docker tag "${PROXY_IMAGE}" "kind.local/credential-proxy:${AGENT_TAG}"
docker tag "${SANDBOX_IMAGE}" "kind.local/agent-sandbox:${SANDBOX_TAG}"
docker tag "${OPERATOR_IMAGE}" "kind.local/k8s-operator:${OPERATOR_TAG}"

# ─── Cluster ─────────────────────────────────────────────────────────────────
if ! kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  log "creating kind cluster ${CLUSTER_NAME}"
  kind create cluster --name "${CLUSTER_NAME}" --wait 120s
fi
log "loading images"
kind load docker-image --name "${CLUSTER_NAME}" "kind.local/platform-agent:${AGENT_TAG}" "kind.local/credential-proxy:${AGENT_TAG}" \
  "kind.local/agent-sandbox:${SANDBOX_TAG}" "kind.local/k8s-operator:${OPERATOR_TAG}"

# ─── Credentials: keep what an earlier run generated ─────────────────────────
existing_key() { kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get secret platform-agent-secrets -o "go-template={{index .data \"$1\" | base64decode}}" 2>/dev/null || true; }
KEY_DIR="$(umask 077 && mktemp -d)"
API_SERVER_KEY="$(existing_key API_SERVER_KEY)"
[ -n "${API_SERVER_KEY}" ] || API_SERVER_KEY="$(openssl rand -hex 32)"
existing_key SANDBOX_SSH_PRIVATE_KEY > "${KEY_DIR}/id_sandbox"
existing_key SANDBOX_SSH_PUBLIC_KEY > "${KEY_DIR}/id_sandbox.pub"
if [ ! -s "${KEY_DIR}/id_sandbox" ] || [ ! -s "${KEY_DIR}/id_sandbox.pub" ]; then
  rm -f "${KEY_DIR}/id_sandbox" "${KEY_DIR}/id_sandbox.pub"
  ssh-keygen -q -t ed25519 -N '' -C "kube-agents kind sandbox" -f "${KEY_DIR}/id_sandbox"
fi

# ─── Chart ───────────────────────────────────────────────────────────────────
# harness.location=kind tells the operator there is no GKE cluster: the
# credential proxy uses the cluster it runs in. No gVisor, no PodMonitoring,
# one small LiteLLM replica.
log "installing the chart"
helm --kube-context "${CONTEXT}" upgrade --install kube-agents "${REPO_ROOT}/charts/kube-agents" \
  --namespace "${NAMESPACE}" --create-namespace \
  --set-string "operator.image.repository=kind.local/k8s-operator" --set-string "operator.image.tag=${OPERATOR_TAG}" \
  --set-string "platformAgent.deployment.image.repository=kind.local/platform-agent" --set-string "platformAgent.deployment.image.tag=${AGENT_TAG}" \
  --set-string "platformAgent.deployment.image.pullPolicy=IfNotPresent" \
  --set-string "agentSandbox.image.repository=kind.local/agent-sandbox" --set-string "agentSandbox.image.tag=${SANDBOX_TAG}" \
  --set-string "platformAgent.harness.projectId=kind" --set-string "platformAgent.harness.location=kind" --set-string "platformAgent.harness.clusterName=kind" \
  --set "platformAgent.deployment.availability.runtimeClassName=" \
  --set "platformAgent.harness.hermes.dashboardEnabled=false" \
  --set "platformAgent.credentials.create=true" \
  --set-string "platformAgent.credentials.data.API_SERVER_KEY=${API_SERVER_KEY}" \
  --set-string "platformAgent.credentials.data.GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --set-file "platformAgent.credentials.data.SANDBOX_SSH_PRIVATE_KEY=${KEY_DIR}/id_sandbox" \
  --set-file "platformAgent.credentials.data.SANDBOX_SSH_PUBLIC_KEY=${KEY_DIR}/id_sandbox.pub" \
  --set-string "litellm.modelProvider=gemini" --set-string "litellm.modelDefaultName=gemini-3.7-flash" --set "litellm.replicaCount=1" \
  --set-string "litellm.resources.requests.memory=256Mi" --set-string "litellm.resources.limits.memory=1Gi" \
  --set "litellm.podMonitoring=false" --set "litellm.podDisruptionBudget.enabled=false" --set "litellm.topologySpread.enabled=false" \
  --set "operator.podDisruptionBudget.enabled=false" --set "operator.topologySpread.enabled=false" \
  --wait --timeout 15m

# The agent container requests 2Gi by default and the chart has no value for
# spec.deployment.resources yet.
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" patch platformagent platform-agent --type merge \
  -p '{"spec":{"deployment":{"resources":{"requests":{"cpu":"250m","memory":"768Mi"},"limits":{"memory":"2Gi"}}}}}' >/dev/null

# ─── Wait for what the operator renders ──────────────────────────────────────
# The operator applies the CR a little after helm returns, so wait for the
# workload to carry the image this run built before waiting for its rollout.
wait_for() { # <kind> <name> <image>
  for _ in $(seq 1 120); do
    [ "$(kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get "$1" "$2" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null)" = "$3" ] && break
    sleep 5
  done
  kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout status "$1/$2" --timeout=600s || {
    kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get pods
    die "$1/$2 did not roll out"
  }
}
wait_for deployment platform-agent-credential-proxy "kind.local/credential-proxy:${AGENT_TAG}"
wait_for deployment platform-agent-gateway "kind.local/platform-agent:${AGENT_TAG}"
wait_for statefulset platform-agent-shell "kind.local/agent-sandbox:${SANDBOX_TAG}"

log "probing the agent API"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" port-forward svc/platform-agent 28641:8642 >/dev/null 2>&1 &
PF_PID=$!
for _ in $(seq 1 12); do
  curl -fsS -H "Authorization: Bearer ${API_SERVER_KEY}" http://127.0.0.1:28641/v1/models >/dev/null 2>&1 && break
  sleep 5
done
curl -fsS -H "Authorization: Bearer ${API_SERVER_KEY}" http://127.0.0.1:28641/v1/models >/dev/null 2>&1 || die "the agent API did not answer"

cat <<EOF

kube-agents is up in kind cluster '${CLUSTER_NAME}' (context ${CONTEXT}). To run a bench case:

  export AGENT_CLUSTER_CONTEXT=${CONTEXT} PLATFORM_AGENT_TOKEN=${API_SERVER_KEY}
  export JUDGE_PROVIDER=gemini JUDGE_MODEL=gemini-3.7-flash AGENT_API_KEY="\${GEMINI_API_KEY}"
  export BENCH_TF_ROOT=./tf PROJECT_ID=kind CLUSTER_NAME=kind
  cd bench && uv sync && uv run devops-bench ./tasks/chat-routing-own-cluster-namespaces --agent-type kubeagents
EOF
