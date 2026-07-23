#!/usr/bin/env bash
# ==============================================================================
# Shared Prow CI Environment Configuration
# ==============================================================================
# Centralizes common variables sourced by ci-deploy.sh, ci-eval-pr.sh, and ci-teardown.sh.
# ==============================================================================

export PROJECT_ID="kube-agents-evals"
export GCP_PROJECT_ID="${PROJECT_ID}"
export REGION="${REGION:-us-central1}"

export HOST_CLUSTER_NAME="platform-agent-host"
export CLUSTER_NAME="${HOST_CLUSTER_NAME}"
export GKE_CLUSTER_NAME="test-cluster"

export TARGET_NAMESPACE="kubeagents-system"
export NAMESPACE="${TARGET_NAMESPACE}"
export PR_ID="${PULL_NUMBER:-local}"
