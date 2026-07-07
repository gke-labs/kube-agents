#!/usr/bin/env bash
set -euo pipefail

TIMEOUT="30s"

echo "=== Verifying GKE Cluster Connectivity ==="
kubectl cluster-info --request-timeout="${TIMEOUT}"

echo "=== Verifying Namespace Access ==="
kubectl get namespaces --request-timeout="${TIMEOUT}"

echo "=== Connectivity Smoke Test Passed ==="