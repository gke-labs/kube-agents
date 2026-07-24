#!/usr/bin/env bash
# Bring up the local Kind test cluster (K8s >= 1.30 for ValidatingAdmissionPolicy GA).
# Usage: local-dev/kind/up.sh   (override image with KIND_IMAGE=kindest/node:v1.30.x)
set -euo pipefail

CLUSTER="${CLUSTER:-kube-agents-dev}"
KIND_IMAGE="${KIND_IMAGE:-kindest/node:v1.31.2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v kind >/dev/null 2>&1; then
  echo "kind not found. Install: go install sigs.k8s.io/kind@latest  (or: brew install kind)" >&2
  exit 1
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "Kind cluster '$CLUSTER' already exists."
else
  kind create cluster --name "$CLUSTER" --image "$KIND_IMAGE" --config "$HERE/kind-config.yaml"
fi

kubectl --context "kind-$CLUSTER" version --output=json | grep -i gitVersion || true
echo "Cluster '$CLUSTER' ready. Context: kind-$CLUSTER"
