#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
trap dump_prow_artifacts_on_failure EXIT

TIMEOUT="30s"

echo "=== Sourcing GKE Cluster Credentials ==="
gcloud container clusters get-credentials "$HOST_CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

echo "=== Verifying GKE Cluster Connectivity ==="
kubectl cluster-info --request-timeout="${TIMEOUT}"

echo "=== Verifying Namespace Access ==="
kubectl get namespaces --request-timeout="${TIMEOUT}"

echo "=== Connectivity Smoke Test Passed ==="