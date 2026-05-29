#!/bin/bash
set -euo pipefail

# =====================================================================
# GKE Operator and Hermes Agent CRD Deployment Script
# =====================================================================

echo "=== 1. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=(
    "PROJECT_ID" "REGION" "REPO_NAME" "IMAGE_NAME" "IMAGE_TAG" 
    "CLUSTER_NAME" "NAMESPACE" "CHAT_TOPIC_NAME" "CHAT_SUB_NAME" 
    "GSA_NAME" "KSA_NAME" "GOOGLE_CHAT_ALLOWED_USERS"
    "OPERATOR_IMAGE_NAME" "OPERATOR_IMAGE_TAG"
)
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is not set in .env. Please define it."
        exit 1
    fi
done
echo " -> [OK] Environment variables validated."

# Resolve repository root path dynamically
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

# Verify Custom Resource config file exists
BOT_CR="$SCRIPT_DIR/hermes-agent-bot.yaml"
if [ ! -f "$BOT_CR" ]; then
    echo " -> [ERROR] Custom Resource config not found at $BOT_CR."
    exit 1
fi

echo "=== 2. Connecting to GKE Cluster ==="
echo " -> [WAIT] Fetching cluster credentials for '$CLUSTER_NAME'..."
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" --quiet

# =====================================================================
# 3. Synchronize Secret Manager Keys
# =====================================================================
echo "=== 3. Synchronizing Secrets to GCP Secret Manager ==="
if [ -n "${GCP_API_KEY:-}" ] && [ "$GCP_API_KEY" != "placeholder" ]; then
    echo " -> [WAIT] Syncing local GCP_API_KEY to GCP Secret Manager..."
    echo -n "$GCP_API_KEY" | gcloud secrets versions add GCP_API_KEY --data-file=- --project="$PROJECT_ID" --quiet
    echo " -> [OK] GCP_API_KEY pushed to Secret Manager."
fi

if [ -n "${GEMINI_API_KEY:-}" ] && [ "$GEMINI_API_KEY" != "placeholder" ]; then
    echo " -> [WAIT] Syncing local GEMINI_API_KEY to GCP Secret Manager..."
    echo -n "$GEMINI_API_KEY" | gcloud secrets versions add GEMINI_API_KEY --data-file=- --project="$PROJECT_ID" --quiet
    echo " -> [OK] GEMINI_API_KEY pushed to Secret Manager."
fi

# =====================================================================
# 4. Build & Push Operator Image
# =====================================================================
OPERATOR_IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$OPERATOR_IMAGE_NAME:$OPERATOR_IMAGE_TAG"
OPERATOR_DIR="$SCRIPT_DIR/hermes-operator"

echo "=== 4. Building Operator Image via Cloud Build ==="
echo " -> [WAIT] Submitting operator build to Google Cloud Build..."
echo " -> [INFO] Operator Target: $OPERATOR_IMAGE_URI"

gcloud builds submit \
    --tag "$OPERATOR_IMAGE_URI" \
    --project "$PROJECT_ID" \
    "$OPERATOR_DIR"

echo " -> [OK] Operator Image successfully built and pushed."

# =====================================================================
# 5. Configure GKE Operator Workload Identity
# =====================================================================
echo "=== 5. Provisioning GCP IAM Permissions for Operator ==="
OPERATOR_GSA_NAME="${GSA_NAME}-operator"
OPERATOR_GSA_EMAIL="$OPERATOR_GSA_NAME@$PROJECT_ID.iam.gserviceaccount.com"

# Ensure the Operator GSA exists
if ! gcloud iam service-accounts describe "$OPERATOR_GSA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo " -> [WAIT] Creating Operator Google Service Account '$OPERATOR_GSA_NAME'..."
    gcloud iam service-accounts create "$OPERATOR_GSA_NAME" \
        --display-name="Hermes Operator Manager" \
        --project="$PROJECT_ID"
    # Wait short interval for IAM service account replication
    sleep 3
else
    echo " -> [OK] Service Account '$OPERATOR_GSA_NAME' already exists."
fi

# Grant necessary project level roles to the Operator GSA
OPERATOR_ROLES=(
    "roles/pubsub.admin"
    "roles/iam.serviceAccountAdmin"
    "roles/resourcemanager.projectIamAdmin"
    "roles/secretmanager.secretAccessor"
)
for role in "${OPERATOR_ROLES[@]}"; do
    echo " -> [WAIT] Binding role '$role' to '$OPERATOR_GSA_EMAIL'..."
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$OPERATOR_GSA_EMAIL" \
        --role="$role" --quiet
done

# Bind GKE Kubernetes Service Account to Operator GSA in GCP IAM
OPERATOR_KSA_NAME="hermes-operator-controller-manager"
OPERATOR_KSA_NAMESPACE="hermes-operator-system"

echo " -> [WAIT] Mapping GKE Workload Identity bindings for Operator..."
gcloud iam service-accounts add-iam-policy-binding "$OPERATOR_GSA_EMAIL" \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:$PROJECT_ID.svc.id.goog[$OPERATOR_KSA_NAMESPACE/$OPERATOR_KSA_NAME]" \
    --project="$PROJECT_ID" --quiet

# =====================================================================
# 6. Deploy Operator to Cluster
# =====================================================================
echo "=== 6. Deploying Operator and CRDs to GKE Cluster ==="
echo " -> [WAIT] Installing CustomResourceDefinitions & Manager workload..."

# Use Operator Makefile target to dynamically deploy the custom image URI
make -C "$OPERATOR_DIR" deploy IMG="$OPERATOR_IMAGE_URI"

# Ensure GKE is annotated so GKE workload identity associates KSA to GCP GSA
echo " -> [WAIT] Overriding annotation for Operator ServiceAccount..."
kubectl annotate serviceaccount -n "$OPERATOR_KSA_NAMESPACE" "$OPERATOR_KSA_NAME" \
    iam.gke.io/gcp-service-account="$OPERATOR_GSA_EMAIL" --overwrite

# Force rollout restart of operator pods so Workload Identity tokens bind immediately
echo " -> [WAIT] Rolling out rollout restart for controller manager pods..."
kubectl rollout restart deployment/hermes-operator-controller-manager -n "$OPERATOR_KSA_NAMESPACE"
kubectl rollout status deployment/hermes-operator-controller-manager -n "$OPERATOR_KSA_NAMESPACE"

echo " -> [OK] Operator manager is deployed, configured and running."

# =====================================================================
# 7. Apply the Platform Agent Custom Resource
# =====================================================================
echo "=== 7. Applying Platform Agent Custom Resource ==="

# Re-ensure Namespace exists
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

export IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME:$IMAGE_TAG"
export PROJECT_ID NAMESPACE CHAT_TOPIC_NAME CHAT_SUB_NAME GSA_NAME KSA_NAME GOOGLE_CHAT_ALLOWED_USERS
export GOOGLE_CHAT_HOME_CHANNEL="${GOOGLE_CHAT_HOME_CHANNEL:-}"

echo " -> [WAIT] Rendering and applying Custom Resource config using Image: $IMAGE_URI"
envsubst < "$BOT_CR" | kubectl apply -f -

echo ""
echo "===================================================="
echo "✅ CRD Deployment Initiated successfully!"
echo "===================================================="
echo "💡 Operator Observability:"
echo "  kubectl get pods -n $OPERATOR_KSA_NAMESPACE"
echo "  kubectl logs -f deploy/hermes-operator-controller-manager -n $OPERATOR_KSA_NAMESPACE -c manager"
echo ""
echo "💡 Agent Observability:"
echo "  kubectl get hermesagent -n $NAMESPACE"
echo "  kubectl get pods -n $NAMESPACE -l app=hermes-gateway"
echo "  kubectl logs -f deploy/hermes-gateway -n $NAMESPACE"
echo ""
echo "💡 Interactive Pairings:"
echo "To view visual dashboard locally:"
echo "  kubectl port-forward -n $NAMESPACE deployment/hermes-gateway 9119:9119"
echo ""
echo "To approve Google Chat pairings (only when bot logs ask for it):"
echo "  kubectl exec -it deploy/hermes-gateway -n $NAMESPACE -- hermes pairing approve google_chat <CODE>"
echo "===================================================="
