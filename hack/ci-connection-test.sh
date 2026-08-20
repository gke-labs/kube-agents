#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Running GKE Cluster Connectivity Verification ==="
TIMEOUT="30s"

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Sourcing GKE Cluster Credentials ==="
gke_dns_endpoint_flag "$HOST_CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying GKE Cluster Connectivity ==="
kubectl cluster-info --request-timeout="${TIMEOUT}"

echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Verifying Namespace Access ==="
kubectl get namespaces --request-timeout="${TIMEOUT}"

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Connectivity Smoke Test Passed (Duration: ${TOTAL_DURATION}s) ==="