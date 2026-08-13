#!/usr/bin/env bash
# Executes environment teardown and provisioning for release candidate deployment.
set -euo pipefail

export PROJECT_ID="${GCP_PROJECT_ID}"
export REGION="${GCP_REGION}"
export CLUSTER_NAME="${GKE_CLUSTER_NAME}"
export PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-}"

./k8s-operator/scripts/teardown.sh --no-confirm
./k8s-operator/scripts/provision.sh --no-confirm
