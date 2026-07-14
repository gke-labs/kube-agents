#!/usr/bin/env bash
set -euo pipefail

# Target GKE Cluster Configuration for CI Evaluations
export PROJECT_ID="kube-agents-evals"
export REGION="us-central1"
export CLUSTER_NAME="evals-target-cluster"
TIMEOUT="30s"

echo "=== Sourcing GKE Cluster Credentials ==="
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

echo "=== Verifying GKE Cluster Connectivity ==="
kubectl cluster-info --request-timeout="${TIMEOUT}"

echo "=== Verifying Namespace Access ==="
kubectl get namespaces --request-timeout="${TIMEOUT}"

echo "=== Connectivity Smoke Test Passed ==="