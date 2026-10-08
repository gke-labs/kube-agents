#!/usr/bin/env bash
# Connects to GKE cluster and verifies that required deployments reach Ready state.
#
# Waits; it does not install. Infrastructure and plugins are deployed in the deploy step
# prior to waiting for readiness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"
# shellcheck source=scripts/release/platform_agent_mode.sh
source "${SCRIPT_DIR}/platform_agent_mode.sh"

# Per-Deployment, because the two have different ceilings and one number cannot
# respect both. The rule test_gateway_rollout_budgets.py enforces is
#
#     startupProbe budget  <  rollout gate  <  progressDeadlineSeconds
#
# and past the deadline `kubectl rollout status` returns "exceeded its progress
# deadline" however long it was given, so a gate at or above it buys nothing.
#
# litellm sets no progressDeadlineSeconds and so runs on Kubernetes' 600s
# default; the gateway's is pinned at 1800s by the operator
# (gatewayProgressDeadlineSeconds) against a 905s startupProbe budget from
# agentAPIProbe(10, 90). Both values below are sized for these Deployments:
# litellm is gated at 420s and the gateway at 1500s (matching upgrade.sh),
# the latter after 180s and 900s reported red on deploys that had succeeded.
#
# The single 300s that used to cover both was under the gateway's cold-start
# cost. It went unnoticed while the RC provisioned Standard clusters; a fresh
# Autopilot cluster pays node scale-up plus a first image pull before the
# container starts at all (measured 215s in autopush, 259s in staging, and
# those were warm).
readonly LITELLM_READINESS_TIMEOUT="420s"
readonly GATEWAY_READINESS_TIMEOUT="1500s"

# PLATFORM_AGENT_MODE, refused before anything connects when it is not a mode.
# Once connected, it is checked against the installed CR's spec.mode (one
# read, whatever the mode); beyond that, unset and `today` add nothing below
# and `next` adds the gate at the end.
platform_agent_mode_resolve

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

# Before any gate, so a run given the wrong mode stops at once and says so.
platform_agent_mode_check_installed "${AGENT_NAMESPACE}"

echo "🔑 Configuring Docker authentication for Artifact Registry (${REGION}-docker.pkg.dev)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet || true

if [ -n "${COMMIT_SHA}" ]; then
  echo "🔍 Verifying platform-agent-gateway deployment container image matches commit ${COMMIT_SHA}..."
  # Delegated rather than open-coded. This read-back used to grep
  # `.containers[*].image` for the SHA, which passed on the first container that
  # matched and never looked at `.initContainers` at all -- so the agent could
  # sit at an old tag behind an envoy-credential-proxy that had rolled forward,
  # and the loop would exit satisfied. confirm_agent_image.sh checks every
  # first-party release image in the template and reports which ones came apart.
  "${SCRIPT_DIR}/../confirm_agent_image.sh" "${AGENT_NAMESPACE}" platform-agent-gateway "${COMMIT_SHA}"
  echo "✅ platform-agent-gateway deployment image matches candidate commit ${COMMIT_SHA}."
fi

echo "Waiting for litellm deployment readiness..."
kubectl rollout status deployment/litellm -n "${AGENT_NAMESPACE}" --timeout="${LITELLM_READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/litellm -n "${AGENT_NAMESPACE}" --timeout="${LITELLM_READINESS_TIMEOUT}"

echo "Waiting for platform-agent-gateway deployment readiness..."
kubectl rollout status deployment/platform-agent-gateway -n "${AGENT_NAMESPACE}" --timeout="${GATEWAY_READINESS_TIMEOUT}"
kubectl wait --for=condition=Available deployment/platform-agent-gateway -n "${AGENT_NAMESPACE}" --timeout="${GATEWAY_READINESS_TIMEOUT}"

# Under spec.mode: next, what the mode renders as well, so the suites do not
# start on a bus still coming up or mid-way through a roll. The deploy job ran
# the same gate after it switched the mode; this one holds the line for
# anything the operator rolls after that job ended.
if [ "${RELEASE_PLATFORM_AGENT_MODE}" = "${PLATFORM_AGENT_MODE_NEXT}" ]; then
  platform_agent_mode_wait_next "${AGENT_NAMESPACE}"
fi
