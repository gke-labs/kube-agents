#!/usr/bin/env bash
set -uo pipefail

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

# Ensure kubectl points to correct cluster context
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

echo "=== Cleaning Up GKE Resources ==="

# 1. Undeploy LiteLLM Gateway
echo "Undeploying LiteLLM Gateway..."
kubectl delete deployment/litellm -n "$NAMESPACE" --ignore-not-found=true

# 2. Delete PlatformAgent Custom Resource and wait
if kubectl get platformagent platform-agent -n "$NAMESPACE" >/dev/null 2>&1; then
  echo "Deleting PlatformAgent platform-agent..."
  kubectl delete platformagent platform-agent -n "$NAMESPACE" --timeout=60s || {
    echo "Warning: PlatformAgent delete timed out. Force removing finalizers..."
    kubectl patch platformagent platform-agent -n "$NAMESPACE" -p '{"metadata":{"finalizers":null}}' --type=merge || true
    kubectl delete platformagent platform-agent -n "$NAMESPACE" --ignore-not-found=true
  }
fi

# 3. Undeploy Operator (Deployments, Roles, CRDs)
echo "Undeploying Operator..."
kubectl delete -k k8s-operator/config/default --ignore-not-found=true

# 4. Delete Namespace
echo "Deleting namespace $NAMESPACE..."
kubectl delete namespace "$NAMESPACE" --ignore-not-found=true --timeout=120s || {
  echo "Warning: Namespace deletion timed out. This may be due to stuck resources."
}

echo "=== Cleanup Complete ==="
