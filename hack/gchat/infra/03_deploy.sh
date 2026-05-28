#!/bin/bash
set -euo pipefail

echo "=== 1. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=("PROJECT_ID" "REGION" "CLUSTER_NAME" "NAMESPACE" "REPO_NAME" "IMAGE_NAME" "IMAGE_TAG" "CHAT_SUB_NAME" "GSA_NAME" "KSA_NAME" "GOOGLE_CHAT_ALLOWED_USERS")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is not set in .env."
        exit 1
    fi
done

DEPLOY_FILE="./deployment.yaml"
if [ ! -f "$DEPLOY_FILE" ]; then
    echo " -> [ERROR] Deployment file '$DEPLOY_FILE' does not exist."
    exit 1
fi
echo " -> [OK] Environment variables validated."

echo "=== 2. Connecting to Cluster ==="
echo " -> [WAIT] Fetching cluster credentials for '$CLUSTER_NAME'..."
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --quiet

echo "=== 3. Provisioning Kubernetes Resources ==="
echo " -> [WAIT] Ensuring Namespace '$NAMESPACE' exists..."
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo " -> [WAIT] Resolving deployment secrets from GCP Secret Manager..."
RESOLVED_GCP_API_KEY="${GCP_API_KEY:-$(gcloud secrets versions access latest --secret="GCP_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "")}"
RESOLVED_GEMINI_API_KEY="${GEMINI_API_KEY:-$(gcloud secrets versions access latest --secret="GEMINI_API_KEY" --project="$PROJECT_ID" 2>/dev/null || echo "")}"

echo " -> [WAIT] Creating/Updating Kubernetes Secret 'hermes-secrets'..."
kubectl create secret generic hermes-secrets \
  --namespace="$NAMESPACE" \
  --from-literal=GCP_API_KEY="$RESOLVED_GCP_API_KEY" \
  --from-literal=GEMINI_API_KEY="$RESOLVED_GEMINI_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

# Export exclusively what we need so envsubst safely catches them
export IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME:$IMAGE_TAG"
export PROJECT_ID NAMESPACE KSA_NAME GSA_NAME CHAT_SUB_NAME GOOGLE_CHAT_ALLOWED_USERS
export GOOGLE_CHAT_HOME_CHANNEL="${GOOGLE_CHAT_HOME_CHANNEL:-}"

echo "=== 4. Applying Deployment Manifest ==="
echo " -> [WAIT] Deploying Hermes to GKE using image: $IMAGE_URI"
envsubst '${IMAGE_URI} ${PROJECT_ID} ${NAMESPACE} ${KSA_NAME} ${GSA_NAME} ${CHAT_SUB_NAME} ${GOOGLE_CHAT_ALLOWED_USERS} ${GOOGLE_CHAT_HOME_CHANNEL}' < "$DEPLOY_FILE" | kubectl apply -f -

echo ""
echo "===================================================="
echo "✅ Deployment Triggered successfully!"
echo ""
echo "💡 Useful Commands:"
echo "-------------------"
echo "To view the visual dashboard locally:"
echo "  kubectl port-forward -n $NAMESPACE deployment/hermes-gateway 9119:9119"
echo ""
echo "To approve a Google Chat connection:"
echo "  kubectl exec -it deploy/hermes-gateway -n $NAMESPACE -- hermes pairing approve google_chat <CODE>"
echo "===================================================="
