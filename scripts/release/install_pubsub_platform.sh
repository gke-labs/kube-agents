#!/usr/bin/env bash
# Installs the Pub/Sub platform adapter (alert ingress) onto the target cluster.
#
# WHY THIS SCRIPT EXISTS AT ALL
#
# The Pub/Sub adapter is a separate AgentPlugin and is not in the agent image.
# deploy/docker/Dockerfile bakes the google_chat, slack and chat platforms and
# installs only the google-cloud-pubsub library; the adapter itself ships solely
# as agentplugins/pubsub-platform, and nothing in the install engine — Terraform,
# the chart, or install.sh — puts it on a cluster.
#
# A consumer such as gke-stockout-investigator contributes only route config
# under platforms.pubsub.extra.subscriptions, which the operator files on the
# default profile (gatewayScopedPluginConfigSubtrees, platformagent_manifests.go).
# With no adapter to read it the gateway opens no listener at all, and the
# failure is silence: verify.sh reports "no sign the adapter saw the message at
# all" and each stockout scenario then burns its full 360s watch reporting that
# the agent never started an investigation. Runs 32986207520, 33018980784,
# 33031877720 and 33061389550 each failed exactly that way, in both tests,
# deterministically.
#
# That the install engine cannot deploy an AgentPlugin is a gap in the install,
# not in the harness, and it is tracked in gke-labs/kube-agents#1013. This script
# is the stopgap that makes the RC gate honest until that lands, and it is meant
# to be deleted wholesale when it does — which is why it is its own file rather
# than a block inside wait_for_gke_readiness.sh. Removing it is `git rm` plus the
# workflow step that calls it and `tests/test_install_pubsub_platform.py`.
#
# FAILURE POLICY LIVES IN THE WORKFLOW, NOT HERE
#
# This script exits non-zero when it cannot deliver working alert ingress. It
# does NOT decide whether that should fail the run: the answer differs per
# caller, so e2e-run.yml takes it as the `alert_ingress_required` input and
# renders it into the step's `continue-on-error`.
#
# What `continue-on-error` does and does not buy, precisely: it covers failures
# THIS script detects and reports. It does not make the adapter harmless to the
# mandatory Google Chat gate, because the adapter re-templates the shared
# platform-agent-gateway Deployment. An adapter that installs cleanly and then
# leaves the gateway unable to roll out — an unpullable plugin image keeps the
# whole agent pod from starting, since the image volume is part of its pod spec
# — is not something this script can see: it waits on plugin readiness and
# generation stability, not on a rollout. wait_for_gke_readiness.sh's
# `kubectl rollout status` then fails the step that has no continue-on-error,
# and the Chat gate never runs. That was equally true before this script
# existed; the split narrows the blast radius, it does not remove it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

# Seconds, not a kubectl duration: the wait below is a shell loop rather than
# `kubectl wait --for=condition=Ready`. The CRD does carry status.conditions and
# the operator does write a Ready one, so that command would work — what it
# cannot express is the observedGeneration == generation comparison, which is the
# half that tells a fresh reconcile from a Ready left over from the previous one.
readonly PLUGIN_READY_TIMEOUT="${PLUGIN_READY_TIMEOUT:-300}"
# How long the gateway's generation must hold still before its spec counts as
# settled. Taken, with its reasoning, from _GENERATION_STABLE_SECONDS in
# tests/e2e/test_stockout_investigation.py: the first bump is not always the
# last, so observing one and calling `rollout status` can succeed against an
# intermediate revision moments before the next arrives.
readonly GENERATION_STABLE_SECONDS="${GENERATION_STABLE_SECONDS:-20}"
# An absolute ceiling on the settle wait. The stability window resets on every
# observed change, so a gateway whose generation keeps moving faster than the
# window would never satisfy it; without this the only bound is the job's
# timeout-minutes, which spends the whole budget and then fails the step for a
# reason that looks nothing like the cause.
readonly GENERATION_SETTLE_TIMEOUT="${GENERATION_SETTLE_TIMEOUT:-180}"

release_resolve_target

echo "======================================================================"
echo "📡 INSTALLING PUB/SUB ALERT INGRESS"
echo "Project ID:      ${PROJECT_ID}"
echo "Region:          ${REGION}"
echo "Cluster Name:    ${CLUSTER_NAME}"
echo "Agent Namespace: ${AGENT_NAMESPACE}"
echo "======================================================================"

if is_truthy "${SKIP_PUBSUB_PLATFORM:-false}"; then
  echo "⏭️  SKIP_PUBSUB_PLATFORM is set: leaving Pub/Sub alert ingress uninstalled."
  echo "    Any alert-driven test in this run will report that the agent never saw its alert."
  exit 0
fi

release_connect_kubectl

# Only this region's host. The plugin installer pushes to whichever registry the
# running agent's image is in, which need not be this one — but it authenticates
# that host itself: plugin_image_resolve calls plugin_image_check_credential,
# which calls plugin_image_login, which runs `gcloud auth configure-docker` for
# the exact push host and aborts if it fails (agentplugins/lib/plugin_image.sh).
# Adding the agent's host here as well was covering a gap that does not exist.
echo "🔑 Configuring Docker authentication for Artifact Registry (${REGION}-docker.pkg.dev)..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet || true

# The plugin's own install.sh is the canonical installer and is idempotent
# (`helm upgrade --install`, and plugin_image_publish skips the build when the
# content tag is already published — agentplugins/lib/plugin_image.sh:919-973),
# so a re-run costs a no-op.
echo "📡 Installing the Pub/Sub platform adapter..."
if ! KUBECTL_CONTEXT="$(kubectl config current-context)" \
  GCP_PROJECT_ID="${PROJECT_ID}" \
  HERMES_NAMESPACE="${AGENT_NAMESPACE}" \
  "${REPO_ROOT}/agentplugins/pubsub-platform/install.sh"; then
  echo "::error title=Pub/Sub alert ingress not installed::agentplugins/pubsub-platform/install.sh failed. Alert ingress is dead, so any alert-driven test will report that the agent never saw its alert." >&2
  exit 1
fi

# A caught-up observedGeneration is necessary but NOT sufficient, so this also
# waits on the gateway's own generation settling. updatePluginStatuses runs at
# the top of reconcileWorkload, before anything is rendered or applied
# (platformagent_controller.go:527-529), and it returns no error — so
# `phase=Ready` with observedGeneration caught up is consistent with the
# Deployment patch not having landed, or having failed.
# Returning there and letting the caller fall straight into `rollout status`
# would let it succeed against the pre-plugin ReplicaSet, and the gateway would
# then restart mid-suite — during the mandatory Chat gate, in the RC pipeline.
echo "Waiting for the pubsubplatform AgentPlugin to reconcile..."
plugin_deadline=$(($(date +%s) + PLUGIN_READY_TIMEOUT))
while :; do
  plugin_phase=""
  plugin_observed=""
  plugin_generation=""
  # Pipe-separated, not space-separated. kubectl renders a missing key as the
  # empty string, so a plugin the operator has not reconciled yet emits "  3"
  # for the space form, and word splitting then lands the generation in
  # plugin_phase — the timeout below would report phase='3' and the generation
  # absent, sending the reader after a phase that does not exist instead of the
  # real answer, which is that nothing reconciled it. An explicit IFS keeps the
  # empty fields.
  IFS='|' read -r plugin_phase plugin_observed plugin_generation <<<"$(
    kubectl get agentplugin pubsubplatform -n "${AGENT_NAMESPACE}" \
      -o jsonpath='{.status.phase}|{.status.observedGeneration}|{.metadata.generation}' 2>/dev/null || true
  )" || true
  if [ "${plugin_phase}" = "Ready" ] && [ -n "${plugin_observed}" ] &&
    [ "${plugin_observed}" = "${plugin_generation}" ]; then
    echo "✅ pubsubplatform AgentPlugin is Ready at generation ${plugin_generation}."
    break
  fi
  if [ "$(date +%s)" -ge "${plugin_deadline}" ]; then
    echo "::error title=Pub/Sub adapter did not reconcile::The pubsubplatform AgentPlugin did not reach Ready within ${PLUGIN_READY_TIMEOUT}s (phase='${plugin_phase:-<none>}', observedGeneration='${plugin_observed:-<none>}', generation='${plugin_generation:-<none>}'). Alert ingress is not serving." >&2
    exit 1
  fi
  sleep 5
done

# Seed from a first read so the loop is not guaranteed to observe a "change" on
# its first iteration and reset the window before it has begun.
echo "Waiting for the gateway's generation to settle (${GENERATION_STABLE_SECONDS}s stable, ${GENERATION_SETTLE_TIMEOUT}s max)..."
gateway_generation="$(kubectl get deployment platform-agent-gateway -n "${AGENT_NAMESPACE}" \
  -o jsonpath='{.metadata.generation}' 2>/dev/null || true)"
settle_deadline=$(($(date +%s) + GENERATION_STABLE_SECONDS))
settle_hard_deadline=$(($(date +%s) + GENERATION_SETTLE_TIMEOUT))
while [ "$(date +%s)" -lt "${settle_deadline}" ]; do
  if [ "$(date +%s)" -ge "${settle_hard_deadline}" ]; then
    echo "::warning title=Gateway generation still changing::platform-agent-gateway did not hold a stable generation for ${GENERATION_STABLE_SECONDS}s within ${GENERATION_SETTLE_TIMEOUT}s (last seen ${gateway_generation:-<unreadable>}). Continuing; a rollout wait may observe an intermediate revision." >&2
    break
  fi
  sleep 3
  current_generation="$(kubectl get deployment platform-agent-gateway -n "${AGENT_NAMESPACE}" \
    -o jsonpath='{.metadata.generation}' 2>/dev/null || true)"
  if [ -n "${current_generation}" ] && [ "${current_generation}" != "${gateway_generation}" ]; then
    gateway_generation="${current_generation}"
    settle_deadline=$(($(date +%s) + GENERATION_STABLE_SECONDS))
  fi
done

echo "✅ Pub/Sub alert ingress installed; gateway generation ${gateway_generation:-<unreadable>}."
