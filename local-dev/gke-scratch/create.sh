#!/usr/bin/env bash
# Create an EPHEMERAL scratch GKE cluster for identity/cloud-specific verification (Workload Identity,
# real cloud IAM) that Kind can't cover. Costs money — destroy.sh when done.
# Requires: gcloud auth + a project. K8s >= 1.30 for ValidatingAdmissionPolicy GA.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
CLUSTER="${CLUSTER:-kube-agents-scratch}"
REGION="${REGION:-us-central1}"
CHANNEL="${CHANNEL:-regular}"

[ -n "$PROJECT" ] || { echo "Set PROJECT or 'gcloud config set project <id>'." >&2; exit 1; }

echo "Creating scratch GKE '$CLUSTER' in $PROJECT/$REGION (Workload Identity on)..."
gcloud container clusters create-auto "$CLUSTER" \
  --project "$PROJECT" --region "$REGION" --release-channel "$CHANNEL"

gcloud container clusters get-credentials "$CLUSTER" --project "$PROJECT" --region "$REGION"
kubectl config rename-context "$(kubectl config current-context)" "gke-scratch-$CLUSTER" 2>/dev/null || true
echo "Ready. Context: gke-scratch-$CLUSTER  —  remember to run destroy.sh."
