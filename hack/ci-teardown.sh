#!/usr/bin/env bash
# ==============================================================================
# Prow CI Teardown Pipeline Script
# ==============================================================================
# Cleans up PR-scoped Kubernetes resources from target GKE cluster.
# Preserves static cluster & GCP IAM setup for fast re-use across PR runs.
#
#  - Step 9 (teardown_09): LiteLLM Gateway Teardown
#  - Step 8 (teardown_08): PlatformAgent CR Teardown
#  - Step 7 (teardown_07): Secrets Teardown
#  - Step 3 (teardown_03): Operator & CRD Teardown
# ==============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# 1. Target Cluster Context
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/ci-env.sh"

echo "=== Target Cluster Context ==="
echo "Project:   $PROJECT_ID"
echo "Cluster:   $CLUSTER_NAME"
echo "Location:  $REGION"
echo "Namespace: $NAMESPACE"

# Authenticates kubectl to target GKE cluster
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet || {
  echo "ERROR: Failed to authenticate to GKE cluster ${CLUSTER_NAME} in project ${PROJECT_ID}! Aborting teardown for safety."
  exit 1
}

# Safety check: Verify active kubectl context matches target cluster before running teardown steps
CURRENT_CTX="$(kubectl config current-context 2>/dev/null || echo "")"
if [[ "$CURRENT_CTX" != *"${CLUSTER_NAME}"* && "$CURRENT_CTX" != *"${PROJECT_ID}"* ]]; then
  echo "ERROR: Active kubectl context ('${CURRENT_CTX}') does not match target cluster '${CLUSTER_NAME}'! Aborting teardown for safety."
  exit 1
fi

echo "=== Cleaning Up GKE Resources ==="

# [Step 1] Undeploy LiteLLM Gateway
./k8s-operator/scripts/teardown_09_deploy_litellm.sh --no-confirm || true

# [Step 2] Delete PlatformAgent Custom Resource
./k8s-operator/scripts/teardown_08_deploy_platform_agent.sh --no-confirm || true

# [Step 3] Delete Secrets
./k8s-operator/scripts/teardown_07_gcp_k8s_secrets.sh --no-confirm || true

# [Step 4] Undeploy Operator Controller Manager & CRDs
./k8s-operator/scripts/teardown_03_gcp_gke_operator.sh --no-confirm || true

echo "=== Cleanup Complete ==="
