#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running GKE Cluster Connectivity Verification ==="
TIMEOUT="30s"

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sourcing GKE Cluster Credentials ==="
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying GKE Cluster Connectivity ==="
kubectl cluster-info --request-timeout="${TIMEOUT}"

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying Namespace Access ==="
kubectl get namespaces --request-timeout="${TIMEOUT}"

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Connectivity Smoke Test Passed (Duration: ${TOTAL_DURATION}s) ==="