#!/usr/bin/env bash
# ==============================================================================
# Prow CI Teardown Pipeline Script
# ==============================================================================
# Cleans up PR-scoped Kubernetes resources from target GKE cluster.
# Preserves static cluster & GCP IAM setup for fast re-use across PR runs.
#
#  [Step 1] Undeploy LiteLLM Gateway
#  [Step 2] Delete PlatformAgent CR & wait for mutating webhook cleanup
#  [Step 3] Undeploy Operator & CRDs
#  [Step 4] Delete PR namespace 'kubeagents-system'
# ==============================================================================

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# 1. Target Cluster Context
export PROJECT_ID="${PROJECT_ID:-kube-agents-evals}"
export REGION="${REGION:-us-central1}"
export CLUSTER_NAME="${CLUSTER_NAME:-platform-agent-host}"
export PR_ID="${PULL_NUMBER:-local}"
export NAMESPACE="kubeagents-system"

echo "=== Target Cluster Context ==="
echo "Project:   $PROJECT_ID"
echo "Cluster:   $CLUSTER_NAME"
echo "Location:  $REGION"
echo "Namespace: $NAMESPACE"

# Authenticates kubectl to target GKE cluster
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

echo "=== Cleaning Up GKE Resources ==="

# [Step 1] Undeploy LiteLLM Gateway
./k8s-operator/scripts/teardown_08_deploy_litellm.sh --no-confirm || true

# [Step 2] Delete PlatformAgent Custom Resource
./k8s-operator/scripts/teardown_07_deploy_platform_agent.sh --no-confirm || true

# [Step 3] Delete Secrets
./k8s-operator/scripts/teardown_06_gcp_k8s_secrets.sh --no-confirm || true

# [Step 4] Undeploy Operator Controller Manager & CRDs
./k8s-operator/scripts/teardown_02_gcp_gke_operator.sh --no-confirm || true

echo "=== Cleanup Complete ==="
