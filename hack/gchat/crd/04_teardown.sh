#!/bin/bash
set -euo pipefail

# =====================================================================
# Hermes GCP & GKE Infrastructure Teardown Script
# =====================================================================

echo "=== 0. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=("PROJECT_ID" "REGION" "CLUSTER_NAME" "REPO_NAME" "GSA_NAME")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is missing from .env."
        exit 1
    fi
done

echo ""
echo "🚨 WARNING: This will permanently delete your GKE cluster, Docker images, and secrets."
echo "   IMPORTANT: Make sure you have deleted the HermesAgent Custom Resource via kubectl"
echo "   first! If you delete the cluster now, the operator cannot run its finalizer to clean"
echo "   up Pub/Sub and Service Accounts."
echo ""
read -p "Proceed with infrastructure teardown? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo " -> [INFO] Teardown aborted."
    exit 1
fi

gcloud config set project "$PROJECT_ID" --quiet

# =====================================================================
# 1. Delete Artifact Registry
# =====================================================================
echo "=== 1. Tearing Down Artifact Registry ==="
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" > /dev/null 2>&1; then
    echo " -> [WAIT] Deleting Artifact Registry repository '$REPO_NAME'..."
    gcloud artifacts repositories delete "$REPO_NAME" --location="$REGION" --quiet
else
    echo " -> [OK] Repository '$REPO_NAME' already deleted or does not exist."
fi

# =====================================================================
# 2. Delete Secret Manager Placeholders
# =====================================================================
echo "=== 2. Tearing Down Secret Manager Placeholders ==="
SECRETS_TO_DELETE=("GCP_API_KEY" "GEMINI_API_KEY")
for SECRET in "${SECRETS_TO_DELETE[@]}"; do
    if gcloud secrets describe "$SECRET" > /dev/null 2>&1; then
        echo " -> [WAIT] Deleting Secret '$SECRET'..."
        gcloud secrets delete "$SECRET" --quiet
    else
        echo " -> [OK] Secret '$SECRET' already deleted or does not exist."
    fi
done

# =====================================================================
# 3. Delete Operator GSA
# =====================================================================
echo "=== 3. Tearing Down Operator Google Service Account ==="
OPERATOR_GSA_NAME="${GSA_NAME}-operator"
OPERATOR_GSA_EMAIL="$OPERATOR_GSA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$OPERATOR_GSA_EMAIL" --project="$PROJECT_ID" > /dev/null 2>&1; then
    echo " -> [WAIT] Deleting Operator Service Account '$OPERATOR_GSA_NAME'..."
    gcloud iam service-accounts delete "$OPERATOR_GSA_EMAIL" --project="$PROJECT_ID" --quiet
else
    echo " -> [OK] Operator Service Account '$OPERATOR_GSA_NAME' already deleted or does not exist."
fi

# =====================================================================
# 4. Delete GKE Cluster
# =====================================================================
echo "=== 4. Tearing Down GKE Cluster ==="
if gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" > /dev/null 2>&1; then
    echo " -> [WAIT] Deleting GKE Autopilot Cluster '$CLUSTER_NAME' (This takes several minutes)..."
    gcloud container clusters delete "$CLUSTER_NAME" --region="$REGION" --quiet
else
    echo " -> [OK] Cluster '$CLUSTER_NAME' already deleted or does not exist."
fi

# =====================================================================
# Execution Complete
# =====================================================================
echo ""
echo "===================================================="
echo "✅ Teardown Complete! Core GCP infrastructure is clean."
echo "===================================================="
