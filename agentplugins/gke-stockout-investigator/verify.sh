#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# GKE Stockout Investigator Extension Verification Script
# Publishes a test scale-up error log event to the PubSub topic and verifies
# that the platform agent processes the alert.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo "")}}"
if [ -z "$PROJECT_ID" ]; then
    echo "Error: Could not determine GCP Project ID. Set GCP_PROJECT_ID or PROJECT_ID."
    exit 1
fi

CONTEXT="${KUBECTL_CONTEXT:-$(kubectl config current-context 2>/dev/null || echo "")}"
if [ -z "$CONTEXT" ]; then
    echo "Error: No kubectl context found. Set KUBECTL_CONTEXT."
    exit 1
fi

# AGENT_NAMESPACE is what the scenario harness (scenarios/lib/common.sh) and the E2E
# suite's fixture both call this, and the suite runs verify.sh in the same breath as its
# own kubectl probes -- so reading a different variable makes the two disagree on any
# non-default namespace. Same default either way.
NAMESPACE="${AGENT_NAMESPACE:-kubeagents-system}"
CLUSTER_NAME="${TARGET_CLUSTER_NAME:-}"
if [ -z "$CLUSTER_NAME" ]; then
    echo "Error: set TARGET_CLUSTER_NAME — it must match the cluster the plugin was installed for,"
    echo "       or the adapter filters the test alert out and this reports a failure that is not one."
    exit 1
fi
# Must match what install.sh created for this deployment; see STOCKOUT_TOPIC there.
TOPIC="${STOCKOUT_TOPIC:-gke-stockout-alerts-topic}"
# The alert claims the workload sits in this location. It only has to agree with the
# cluster the plugin was installed for — the adapter filters on the cluster name, not on
# the location, but a payload naming a region the cluster is not in reads as a bug.
CLUSTER_LOCATION="${TARGET_CLUSTER_LOCATION:-us-east1}"
TEST_ID="test-stockout-$(date +%s)"
# The route deduplicates on cluster + namespace + controller for 24 hours, so a fixed
# controller name makes every run after the first a duplicate — dropped before it reaches
# the agent. A suppressed alert is not a passing smoke test, so vary the name per run.
# The suffix is shaped like the ReplicaSet hash the autoscaler reports, which is exactly
# the field that makes a redeployed workload a new incident.
CONTROLLER_NAME="ml-training-job-gpu-2-${TEST_ID##*-}"
# The adapter route these alerts arrive on, and the string every log line about them
# carries. Must match the subscription key in templates/agentplugin.yaml.
ROUTE_NAME="${STOCKOUT_ROUTE:-gke_stockout_alerts}"
GATEWAY_WAIT_TIMEOUT="${GATEWAY_WAIT_TIMEOUT:-120}"

echo "============================================================"
echo "Verifying GKE Stockout Investigator Extension"
echo "Project ID:      ${PROJECT_ID}"
echo "Kubectl Context: ${CONTEXT}"
echo "Target Cluster:  ${CLUSTER_NAME}"
echo "PubSub Topic:    ${TOPIC}"
echo "Test Event ID:   ${TEST_ID}"
echo "Test Workload:   ${CONTROLLER_NAME}"
echo "============================================================"

echo "Step 0: Verifying platform-agent-gateway availability and readiness..."
kubectl --context="$CONTEXT" rollout status deployment/platform-agent-gateway \
    -n "$NAMESPACE" --timeout="${GATEWAY_WAIT_TIMEOUT}s"

# Construct test payload matching log sink filter & stockout investigator template
PAYLOAD=$(cat <<EOF
{
  "insertId": "${TEST_ID}",
  "logName": "projects/${PROJECT_ID}/logs/test-stockout",
  "resource": {
    "type": "k8s_cluster",
    "labels": {
      "cluster_name": "${CLUSTER_NAME}",
      "location": "${CLUSTER_LOCATION}"
    }
  },
  "jsonPayload": {
    "messageId": "scale.up.error.out.of.resources",
    "noDecisionStatus": {
      "noScaleUp": {
        "unhandledPodGroups": [
          {
            "podGroup": {
              "samplePod": {
                "namespace": "default",
                "controller": {
                  "kind": "ReplicaSet",
                  "name": "${CONTROLLER_NAME}"
                }
              }
            }
          }
        ]
      }
    }
  }
}
EOF
)

echo "Step 1: Publishing test GKE stockout alert event to PubSub topic '${TOPIC}'..."
# Opens this run's log window. Taken before the publish, so the window cannot miss an
# adapter that has already reacted by the time gcloud finishes printing.
PUBLISH_EPOCH="$(date +%s)"
PUB_RESULT=$(gcloud pubsub topics publish "$TOPIC" \
    --project="$PROJECT_ID" \
    --message="$PAYLOAD")
echo "Published test message. Result: ${PUB_RESULT}"

echo ""
echo "Step 2: Waiting 8 seconds for PubSub adapter to pull and process the alert event..."
sleep 8

echo ""
echo "Step 3: Checking platform-agent-gateway logs for what the adapter did with it..."

# Scoped to this run, not merely to the last N lines. A 'filed alert on route' line from
# an earlier run is indistinguishable from this one's, so --tail alone reported the
# earlier run's success while this run's alert was being dropped — the same false green
# the checks below exist to remove, one level up. --since takes a duration rather than a
# timestamp, and the API server applies it against its own clock, so the two machines do
# not have to agree on the time.
WINDOW_SECONDS=$(( $(date +%s) - PUBLISH_EPOCH + 2 ))
LOGS="$(kubectl --context="$CONTEXT" logs -n "$NAMESPACE" deployment/platform-agent-gateway \
    -c platform-agent --since="${WINDOW_SECONDS}s" --tail=200 2>/dev/null || true)"
# Every line below names the route, including the ones that report the alert being
# thrown away. Grepping for the route alone therefore reported SUCCESS for a filtered or
# deduplicated alert — the two outcomes this script exists to catch. Match on what the
# adapter DID instead.
ROUTE_LOGS="$(printf '%s\n' "$LOGS" | grep -i -- "$ROUTE_NAME" || true)"
saw() { printf '%s\n' "$ROUTE_LOGS" | grep -qi -- "$1"; }

STATUS=1
echo ""
if saw "filed alert on route"; then
    echo "✓ SUCCESS: the alert was filed on the board as a kanban task for the platform profile."
    STATUS=0
elif saw "dispatching alert to profile"; then
    echo "✓ SUCCESS: the alert was dispatched to a specialist profile for a turn."
    STATUS=0
elif saw "Response for pubsub:${ROUTE_NAME}"; then
    echo "✓ SUCCESS: the alert was answered in the gateway and the answer written to the log."
    STATUS=0
elif saw "filtered out by expression"; then
    echo "✗ FAILED: the adapter filtered the alert out."
    echo "  The route's filter is compiled with the cluster name the plugin was installed for."
    echo "  TARGET_CLUSTER_NAME here is '${CLUSTER_NAME}'; check it matches --set clusterName."
elif saw "Duplicate message detected"; then
    echo "✗ FAILED: the alert was suppressed as a duplicate of an earlier one."
    echo "  Unexpected — each run uses a fresh controller name. Clear the registry with:"
    echo "    kubectl --context=${CONTEXT} exec -n ${NAMESPACE} deployment/platform-agent-gateway \\"
    echo "      -c platform-agent -- rm -f /opt/data/pubsub_registry.json"
elif saw "threshold not met"; then
    echo "✗ FAILED: the alert did not meet the route's threshold_count within its window."
    echo "  Publish that many alerts, or lower pubsub.thresholdCount."
elif saw "invalidated by programmatic validation_code"; then
    echo "✗ FAILED: the route's validation_code rejected the payload."
# Matched on the refusal's own clause, not on 'sets require_skills': the startup warning
# about pairing require_skills with dispatch: kanban carries that phrase too, and blaming
# a missing skill for an alert that failed for some other reason sends you to the wrong
# place.
elif saw "so this alert is NOT dispatched"; then
    echo "✗ FAILED: the skill could not be loaded and require_skills refused the dispatch."
    echo "  Check the plugin is installed into the profile that runs the investigation."
elif saw "Received message on route"; then
    echo "✗ FAILED: the adapter received the alert but nothing was dispatched."
    echo "  The full tail below should say why."
else
    echo "✗ FAILED: no sign the adapter saw the message at all."
    echo "  The subscription may not be attached, or the gateway may not have the route."
fi

if [ "$STATUS" -ne 0 ]; then
    echo ""
    # Deliberately outside the window above. The failure that needs this dump most is
    # "no sign the adapter saw the message at all", and in that case the window is empty
    # — what explains it is whatever the gateway logged before the run started.
    echo "Latest container logs (last 30 lines, not limited to this run):"
    kubectl --context="$CONTEXT" logs -n "$NAMESPACE" deployment/platform-agent-gateway \
        -c platform-agent --tail=30 2>/dev/null || true
fi

echo "============================================================"
if [ "$STATUS" -eq 0 ]; then
    echo "Verification complete: PASSED"
else
    echo "Verification complete: FAILED"
fi
echo "============================================================"
exit "$STATUS"
