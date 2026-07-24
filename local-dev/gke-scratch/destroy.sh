#!/usr/bin/env bash
# Destroy the ephemeral scratch GKE cluster.
set -euo pipefail
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
CLUSTER="${CLUSTER:-kube-agents-scratch}"
REGION="${REGION:-us-central1}"
gcloud container clusters delete "$CLUSTER" --project "$PROJECT" --region "$REGION" --quiet
