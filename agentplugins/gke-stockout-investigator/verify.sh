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

NAMESPACE="kubeagents-system"
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

echo "============================================================"
echo "Verifying GKE Stockout Investigator Extension"
echo "Project ID:      ${PROJECT_ID}"
echo "Kubectl Context: ${CONTEXT}"
echo "Target Cluster:  ${CLUSTER_NAME}"
echo "PubSub Topic:    ${TOPIC}"
echo "Test Event ID:   ${TEST_ID}"
echo "============================================================"

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
                  "name": "ml-training-job-gpu-2"
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
PUB_RESULT=$(gcloud pubsub topics publish "$TOPIC" \
    --project="$PROJECT_ID" \
    --message="$PAYLOAD")
echo "Published test message. Result: ${PUB_RESULT}"

echo ""
echo "Step 2: Waiting 8 seconds for PubSub adapter to pull and process the alert event..."
sleep 8

echo ""
echo "Step 3: Checking platform-agent-gateway logs for event ingestion..."
if kubectl --context="$CONTEXT" logs -n "$NAMESPACE" deployment/platform-agent-gateway -c platform-agent --tail=100 | grep -i "gke_stockout_alerts"; then
    echo ""
    echo "✓ SUCCESS: PubSub adapter received and processed event for route 'gke_stockout_alerts'!"
else
    echo ""
    echo "Tailing container logs to display latest output:"
    kubectl --context="$CONTEXT" logs -n "$NAMESPACE" deployment/platform-agent-gateway -c platform-agent --tail=30
fi

echo "============================================================"
echo "Verification complete!"
echo "============================================================"
