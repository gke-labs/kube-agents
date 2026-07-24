#!/usr/bin/env bash
# Tear down the local Kind test cluster.
set -euo pipefail
CLUSTER="${CLUSTER:-kube-agents-dev}"
kind delete cluster --name "$CLUSTER"
