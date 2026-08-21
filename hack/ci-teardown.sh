#!/usr/bin/env bash
# ==============================================================================
# Prow CI Teardown Pipeline Script
# ==============================================================================
# Cleans up PR-scoped Kubernetes resources from target GKE cluster.
# Preserves static cluster & GCP IAM setup for fast re-use across PR runs.
#
# One `helm uninstall` (the release owns every Kubernetes object ci-deploy.sh
# created) plus an explicit CRD delete, since the chart leaves CRDs behind by
# Helm's own design.
# ==============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"
ensure_helm

echo "=== Target Cluster Context ==="
echo "Project:   $PROJECT_ID"
echo "Cluster:   $CLUSTER_NAME"
echo "Location:  $REGION"
echo "Namespace: $NAMESPACE"

# Authenticates kubectl to target GKE cluster
gke_dns_endpoint_flag "$CLUSTER_NAME" "$REGION" "$PROJECT_ID"
# Unquoted on purpose: empty must contribute no argument. See gke_dns_endpoint.sh.
# shellcheck disable=SC2086
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet \
  $GKE_DNS_ENDPOINT_FLAG || {
  echo "ERROR: Failed to authenticate to GKE cluster ${CLUSTER_NAME} in project ${PROJECT_ID}! Aborting teardown for safety."
  exit 1
}

# Safety check: Verify active kubectl context matches target cluster and project before running teardown steps
CURRENT_CTX="$(kubectl config current-context 2>/dev/null || echo "")"
EXPECTED_CTX="gke_${PROJECT_ID}_${REGION}_${CLUSTER_NAME}"
if [[ "$CURRENT_CTX" != "$EXPECTED_CTX" ]]; then
  echo "ERROR: Active kubectl context ('${CURRENT_CTX}') does not match expected context ('${EXPECTED_CTX}')! Aborting teardown for safety."
  exit 1
fi

START_TIME=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleaning Up GKE Resources ==="

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 1: Uninstalling the kube-agents release ==="
# The chart's pre-delete hook removes the PlatformAgent CR and waits for the
# operator to clear its finalizer, so one uninstall replaces the old
# per-step teardown scripts (09 LiteLLM, 08 CR, 07 secrets, 03 operator).
if ! helm uninstall kube-agents -n "${NAMESPACE}" --wait --timeout 10m; then
  echo "WARNING: helm uninstall failed or timed out. Stripping dangling finalizers in namespace ${NAMESPACE} so CRD deletion does not deadlock..."
  for name in $(kubectl get platformagents -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    [ -n "$name" ] && kubectl patch platformagent "$name" -n "${NAMESPACE}" --type=merge -p '{"metadata":{"finalizers":[]}}' 2>/dev/null || true
  done
  for name in $(kubectl get agentplugins -n "${NAMESPACE}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
    [ -n "$name" ] && kubectl patch agentplugin "$name" -n "${NAMESPACE}" --type=merge -p '{"metadata":{"finalizers":[]}}' 2>/dev/null || true
  done
fi
echo "✓ Release uninstall finished in $((SECONDS - STEP_START))s"

STEP_START=$SECONDS
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Step 2: Deleting CRDs ==="
# Helm leaves crds/ objects behind by design; a PR evaluation cluster should
# not accumulate them. Bounded by --timeout so it never hangs if CR cleanup stalls.
kubectl delete -f charts/kube-agents/crds/ --ignore-not-found --timeout=2m || true
echo "✓ CRD deletion finished in $((SECONDS - STEP_START))s"

TOTAL_DURATION=$((SECONDS - START_TIME))
echo "=== [$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Cleanup Complete (Total Duration: ${TOTAL_DURATION}s) ==="
