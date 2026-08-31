#!/usr/bin/env bash
# Connects to GKE cluster and verifies that required deployments reach Ready state.
#
# Waits; it does not install. Where a caller needs alert ingress it installs it
# in an earlier step — e2e-run.yml, the reusable job the RC and nightly
# pipelines both call, runs install_pubsub_platform.sh before this, so the
# gateway re-template the adapter causes is already in flight by the time the
# rollout waits below start. That ordering is the caller's to get right, not an
# invariant this script can assume: e2e-manual-runner.yml calls this script with
# no such step today.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# Per-Deployment, because the two have different ceilings and one number cannot
# respect both. The rule test_gateway_rollout_budgets.py enforces is
#
#     startupProbe budget  <  rollout gate  <  progressDeadlineSeconds
#
# and past the deadline `kubectl rollout status` returns "exceeded its progress
# deadline" however long it was given, so a gate at or above it buys nothing.
#
# litellm sets no progressDeadlineSeconds and so runs on Kubernetes' 600s
# default; the gateway's is pinned at 1200s by the operator
# (gatewayProgressDeadlineSeconds) against a 605s startupProbe budget from
# agentAPIProbe(10, 60). Both values below are the ones the deploy workflows
# already use for these same Deployments -- reusable-deploy-integrations.yml
# gates litellm at 420s and reusable-deploy-agent.yml gates the gateway at
# 900s, the latter after 180s reported red on deploys that had succeeded.
#
# The single 300s that used to cover both was under the gateway's cold-start
# cost. It went unnoticed while the RC provisioned Standard clusters; a fresh
# Autopilot cluster pays node scale-up plus a first image pull before the
# container starts at all (measured 215s in autopush, 259s in staging, and
# those were warm).
readonly LITELLM_READINESS_TIMEOUT="420s"
readonly GATEWAY_READINESS_TIMEOUT="900s"

release_resolve_target

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"

echo "======================================================================"
echo "⏳ CONNECTING TO GKE & WAITING FOR POD READINESS"
echo "Project ID:        ${PROJECT_ID}"
echo "Region:            ${REGION}"
echo "Cluster Name:      ${CLUSTER_NAME}"
echo "Agent Namespace:   ${AGENT_NAMESPACE}"
echo "Target Commit SHA: ${COMMIT_SHA:-(not specified)}"
echo "Readiness Timeouts: litellm ${LITELLM_READINESS_TIMEOUT}, gateway ${GATEWAY_READINESS_TIMEOUT}"
echo "======================================================================"

release_connect_kubectl

echo "🔑 Configuring Docker authentication for Artifact Registry (${REGION}-docker.pkg.dev)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet || true

if [ -n "${COMMIT_SHA}" ]; then
  echo "🔍 Verifying platform-agent-gateway deployment container image matches commit ${COMMIT_SHA}..."
  start_time=$(date +%s)
  until kubectl get deploy/platform-agent-gateway -n "${AGENT_NAMESPACE}" -o jsonpath='{.spec.template.spec.containers[*].image}' 2>/dev/null | grep -q ":${COMMIT_SHA}"; do
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
kubectl rollout status deployment/litellm -n "${AGENT_NAMESPACE}" --timeout="${LITELLM_READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/litellm -n "${AGENT_NAMESPACE}" --timeout="${LITELLM_READINESS_TIMEOUT}"

echo "Waiting for platform-agent-gateway deployment readiness..."
kubectl rollout status deployment/platform-agent-gateway -n "${AGENT_NAMESPACE}" --timeout="${GATEWAY_READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/platform-agent-gateway -n "${AGENT_NAMESPACE}" --timeout="${GATEWAY_READINESS_TIMEOUT}"
