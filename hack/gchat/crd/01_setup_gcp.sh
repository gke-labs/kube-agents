#!/bin/bash
set -euo pipefail

# =====================================================================
# Hermes GCP & GKE Infrastructure Bootstrap Script
# =====================================================================

echo "=== 0. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=("PROJECT_ID" "REGION" "REPO_NAME" "CLUSTER_NAME" "NAMESPACE" "CHAT_TOPIC_NAME" "CHAT_SUB_NAME" "GSA_NAME" "KSA_NAME")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is not set. Please define it in your .env file."
        exit 1
    fi
done

echo " -> [WAIT] Configuring GCP project to: $PROJECT_ID..."
gcloud config set project "$PROJECT_ID" --quiet

# Note: projectNumber is fetched but unused in this script, 
# keeping it in case it's needed for later external steps.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

# =====================================================================
# 1. Check and Enable Required GCP APIs
# =====================================================================
echo "=== 1. Checking Required APIs ==="
REQUIRED_APIS=(
    "container.googleapis.com"
    "artifactregistry.googleapis.com"
    "cloudbuild.googleapis.com"
    "secretmanager.googleapis.com"
    "pubsub.googleapis.com"
    "chat.googleapis.com"
    "gsuiteaddons.googleapis.com"
    "aiplatform.googleapis.com"
)
ENABLED_APIS=$(gcloud services list --enabled --format="value(config.name)")
for API in "${REQUIRED_APIS[@]}"; do
    if echo "$ENABLED_APIS" | grep -q "$API"; then
        echo " -> [OK] $API is already enabled."
    else
        echo " -> [WAIT] Enabling $API..."
        gcloud services enable "$API"
    fi
done

# =====================================================================
# 2. Configure Artifact Registry
# =====================================================================
echo "=== 2. Configuring Artifact Registry ==="
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" > /dev/null 2>&1; then
    echo " -> [OK] Repository '$REPO_NAME' already exists."
else
    echo " -> [WAIT] Creating Artifact Registry repository '$REPO_NAME'..."
    gcloud artifacts repositories create "$REPO_NAME" --repository-format=docker --location="$REGION"
fi

# =====================================================================
# 3. Configure GKE Cluster
# =====================================================================
echo "=== 3. Configuring GKE Cluster ==="
if gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" > /dev/null 2>&1; then
    echo " -> [OK] Cluster '$CLUSTER_NAME' already exists."
else
    echo " -> [WAIT] Creating GKE Autopilot cluster (this takes a few minutes)..."
    gcloud container clusters create-auto "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID"
fi

# =====================================================================
# 4. Configure Secret Manager Placeholders
# =====================================================================
echo "=== 4. Configuring Secret Manager ==="
SECRETS_TO_CREATE=("GCP_API_KEY" "GEMINI_API_KEY")
for SECRET in "${SECRETS_TO_CREATE[@]}"; do
    if gcloud secrets describe "$SECRET" > /dev/null 2>&1; then
        echo " -> [OK] Secret '$SECRET' already exists."
    else
        echo " -> [WAIT] Creating '$SECRET' placeholder..."
        echo "placeholder" | gcloud secrets create "$SECRET" --data-file=- --replication-policy="automatic"
    fi
done

# =====================================================================
# 5. Connect to Kubernetes & Ensure Namespace
# =====================================================================
echo "=== 5. Connecting to Kubernetes ==="
echo " -> [WAIT] Fetching Kubernetes credentials..."
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" > /dev/null 2>&1
echo " -> [OK] Credentials configured."

echo " -> [WAIT] Ensuring Namespace '$NAMESPACE' exists..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# =====================================================================
# 6. Create Kubernetes Secret from GCP Secret Manager secrets
# =====================================================================
echo "=== 6. Create Kubernetes Secret ==="
echo " -> [WAIT] Resolving deployment secrets from GCP Secret Manager..."
RESOLVED_GCP_API_KEY="${GCP_API_KEY:-$(gcloud secrets versions access latest --secret="GCP_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "")}"
RESOLVED_GEMINI_API_KEY="${GEMINI_API_KEY:-$(gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "")}"

echo " -> [WAIT] Creating/Updating Kubernetes Secret 'hermes-secrets'..."
kubectl create secret generic hermes-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=GCP_API_KEY="$RESOLVED_GCP_API_KEY" \
  --from-literal=GEMINI_API_KEY="$RESOLVED_GEMINI_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# =====================================================================
# Execution Complete
# =====================================================================
echo ""
echo "===================================================="
echo "✅ GCP Setup Complete!"
echo "Next Step: Configure the Google Chat App in Google Cloud Console."
echo "Connection settings: projects/$PROJECT_ID/topics/$CHAT_TOPIC_NAME"
echo "===================================================="
