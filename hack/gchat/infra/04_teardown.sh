#!/bin/bash
set -euo pipefail

echo "=== 1. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=("PROJECT_ID" "REGION" "CLUSTER_NAME" "REPO_NAME" "CHAT_TOPIC_NAME" "CHAT_SUB_NAME" "GSA_NAME")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is missing from .env."
        exit 1
    fi
done

echo ""
echo "🚨 WARNING: This will permanently delete your Hermes cluster, persistent data,"
echo "            Docker images, Pub/Sub chat events, and secrets."
read -p "Proceed? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo " -> [INFO] Teardown aborted."
    exit 1
fi

echo "=== 2. Tearing Down Infrastructure ==="
echo " -> [WAIT] Deleting Pub/Sub Subscription: $CHAT_SUB_NAME..."
gcloud pubsub subscriptions delete "$CHAT_SUB_NAME" --quiet >/dev/null 2>&1 || true

echo " -> [WAIT] Deleting Pub/Sub Topic: $CHAT_TOPIC_NAME..."
gcloud pubsub topics delete "$CHAT_TOPIC_NAME" --quiet >/dev/null 2>&1 || true

echo " -> [WAIT] Deleting GCP Service Account: $GSA_NAME..."
gcloud iam service-accounts delete "${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" --quiet >/dev/null 2>&1 || true

echo " -> [WAIT] Deleting Artifact Registry repository '$REPO_NAME'..."
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" > /dev/null 2>&1; then
    gcloud artifacts repositories delete "$REPO_NAME" --location="$REGION" --quiet
fi

echo " -> [WAIT] Deleting GKE Autopilot Cluster: $CLUSTER_NAME (This takes several minutes)..."
if gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" > /dev/null 2>&1; then
    gcloud container clusters delete "$CLUSTER_NAME" --region="$REGION" --quiet
fi

echo ""
echo "===================================================="
echo "✅ Teardown Complete! GCP environment is clean."
echo "===================================================="
