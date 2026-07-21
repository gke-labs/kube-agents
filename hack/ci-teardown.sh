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
echo "Undeploying LiteLLM Gateway..."
make -C k8s-operator undeploy-litellm ignore-not-found=true || true

# [Step 2] Delete PlatformAgent Custom Resource & wait for controller teardown
if kubectl get platformagent platform-agent -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "Deleting PlatformAgent platform-agent..."
  kubectl delete platformagent platform-agent -n "$NAMESPACE" --timeout=60s || {
    echo "Warning: PlatformAgent delete timed out. Force removing finalizers..."
    kubectl patch platformagent platform-agent -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
    kubectl delete platformagent platform-agent -n "$NAMESPACE" --ignore-not-found=true
  }
fi

# [Step 3] Undeploy Operator Controller Manager & CRDs
echo "Undeploying Operator..."
make -C k8s-operator undeploy ignore-not-found=true || true

# [Step 4] Delete PR Ephemeral Namespace
echo "Deleting namespace $NAMESPACE..."
kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --timeout=120s || {
  echo "Warning: Namespace deletion timed out. This may be due to stuck resources."
}

echo "=== Cleanup Complete ==="
