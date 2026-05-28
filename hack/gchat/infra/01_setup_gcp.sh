#!/bin/bash
set -euo pipefail

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

echo " -> [WAIT] Starting GCP Setup for project: $PROJECT_ID..."
gcloud config set project "$PROJECT_ID" --quiet
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

# === 1. Check and Enable APIs ===
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

# === 2. Check and Create Pub/Sub Infrastructure ===
echo "=== 2. Configuring Pub/Sub Infrastructure ==="
if gcloud pubsub topics describe "$CHAT_TOPIC_NAME" >/dev/null 2>&1; then
    echo " -> [OK] Topic '$CHAT_TOPIC_NAME' already exists."
else
    echo " -> [WAIT] Creating Topic '$CHAT_TOPIC_NAME'..."
    gcloud pubsub topics create "$CHAT_TOPIC_NAME"
fi

if gcloud pubsub subscriptions describe "$CHAT_SUB_NAME" >/dev/null 2>&1; then
    echo " -> [OK] Subscription '$CHAT_SUB_NAME' already exists."
else
    echo " -> [WAIT] Creating Subscription '$CHAT_SUB_NAME'..."
    gcloud pubsub subscriptions create "$CHAT_SUB_NAME" \
        --topic="$CHAT_TOPIC_NAME" \
        --ack-deadline=60 \
        --message-retention-duration="7d"
fi

# === 3. Check and Create Bot Service Account ===
echo "=== 3. Configuring Bot Service Account ==="
GSA_EMAIL="${GSA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
if gcloud iam service-accounts describe "$GSA_EMAIL" >/dev/null 2>&1; then
    echo " -> [OK] Service Account '$GSA_NAME' already exists."
else
    echo " -> [WAIT] Creating Service Account '$GSA_NAME'..."
    gcloud iam service-accounts create "$GSA_NAME" \
        --description="Service Account for Hermes Chat Bot in GKE" \
        --display-name="Hermes Chat Bot"
fi

# === 4. Configure IAM Permissions ===
echo "=== 4. Configuring IAM Permissions ==="

ensure_project_iam() {
    local ROLE=$1
    local MEMBER=$2
    local HAS_ROLE=$(gcloud projects get-iam-policy "$PROJECT_ID" \
        --flatten="bindings[].members" \
        --filter="bindings.role:$ROLE AND bindings.members:$MEMBER" \
        --format="value(bindings.role)" 2>/dev/null)
    if [ -n "$HAS_ROLE" ]; then
        echo " -> [OK] $MEMBER already has $ROLE on project."
    else
        echo " -> [WAIT] Granting $ROLE to $MEMBER on project..."
        gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="$MEMBER" \
            --role="$ROLE" \
            --condition=None >/dev/null 2>&1
    fi
}

ensure_pubsub_sub_iam() {
    local ROLE=$1
    local MEMBER=$2
    local HAS_ROLE=$(gcloud pubsub subscriptions get-iam-policy "$CHAT_SUB_NAME" \
        --flatten="bindings[].members" \
        --filter="bindings.role:$ROLE AND bindings.members:$MEMBER" \
        --format="value(bindings.role)" 2>/dev/null)
    if [ -n "$HAS_ROLE" ]; then
        echo " -> [OK] $MEMBER already has $ROLE on subscription $CHAT_SUB_NAME."
    else
        echo " -> [WAIT] Granting $ROLE to $MEMBER on subscription..."
        gcloud pubsub subscriptions add-iam-policy-binding "$CHAT_SUB_NAME" \
            --member="$MEMBER" \
            --role="$ROLE" >/dev/null 2>&1
    fi
}

ensure_pubsub_topic_iam() {
    local ROLE=$1
    local MEMBER=$2
    local HAS_ROLE=$(gcloud pubsub topics get-iam-policy "$CHAT_TOPIC_NAME" \
        --flatten="bindings[].members" \
        --filter="bindings.role:$ROLE AND bindings.members:$MEMBER" \
        --format="value(bindings.role)" 2>/dev/null)
    if [ -n "$HAS_ROLE" ]; then
        echo " -> [OK] $MEMBER already has $ROLE on topic $CHAT_TOPIC_NAME."
    else
        echo " -> [WAIT] Granting $ROLE to $MEMBER on topic..."
        gcloud pubsub topics add-iam-policy-binding "$CHAT_TOPIC_NAME" \
            --member="$MEMBER" \
            --role="$ROLE" >/dev/null 2>&1
    fi
}

ensure_service_account_iam() {
    local TARGET_SA=$1
    local ROLE=$2
    local MEMBER=$3
    local HAS_ROLE=$(gcloud iam service-accounts get-iam-policy "$TARGET_SA" \
        --flatten="bindings[].members" \
        --filter="bindings.role:$ROLE AND bindings.members:$MEMBER" \
        --format="value(bindings.role)" 2>/dev/null)
    if [ -n "$HAS_ROLE" ]; then
        echo " -> [OK] $MEMBER already has $ROLE on service account $(basename "$TARGET_SA")."
    else
        echo " -> [WAIT] Granting $ROLE to $MEMBER on service account..."
        gcloud iam service-accounts add-iam-policy-binding "$TARGET_SA" \
            --member="$MEMBER" \
            --role="$ROLE" >/dev/null 2>&1
    fi
}

# 4a. Pub/Sub Bindings for our Bot
ensure_pubsub_sub_iam "roles/pubsub.subscriber" "serviceAccount:$GSA_EMAIL"
ensure_pubsub_sub_iam "roles/pubsub.viewer" "serviceAccount:$GSA_EMAIL"
ensure_project_iam "roles/aiplatform.user" "serviceAccount:$GSA_EMAIL"

# 4b. Pub/Sub Bindings for Google Chat systems to push to us
ensure_pubsub_topic_iam "roles/pubsub.publisher" "serviceAccount:chat-api-push@system.gserviceaccount.com"

gcloud beta services identity create --service=gsuiteaddons.googleapis.com > /dev/null 2>&1 || true
CHAT_SYSTEM_SA="service-${PROJECT_NUMBER}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
ensure_pubsub_topic_iam "roles/pubsub.publisher" "serviceAccount:$CHAT_SYSTEM_SA"

# 4c. Compute Engine Bindings (Required for GKE Node pools)
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
ensure_project_iam "roles/artifactregistry.reader" "serviceAccount:$COMPUTE_SA"
ensure_project_iam "roles/artifactregistry.writer" "serviceAccount:$COMPUTE_SA"
ensure_project_iam "roles/storage.objectViewer" "serviceAccount:$COMPUTE_SA"
ensure_project_iam "roles/logging.logWriter" "serviceAccount:$COMPUTE_SA"

# 4d. Workload Identity Binding (Mapping K8s KSA to GCP GSA)
WORKLOAD_IDENTITY_MEMBER="serviceAccount:${PROJECT_ID}.svc.id.goog[${NAMESPACE}/${KSA_NAME}]"
ensure_service_account_iam "$GSA_EMAIL" "roles/iam.workloadIdentityUser" "$WORKLOAD_IDENTITY_MEMBER"


# === 5. Check and Create Artifact Registry ===
echo "=== 5. Configuring Artifact Registry ==="
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" > /dev/null 2>&1; then
    echo " -> [OK] Repository '$REPO_NAME' already exists."
else
    echo " -> [WAIT] Creating Artifact Registry repository '$REPO_NAME'..."
    gcloud artifacts repositories create "$REPO_NAME" --repository-format=docker --location="$REGION"
fi

# === 6. Check and Create GKE Cluster ===
echo "=== 6. Configuring GKE Cluster ==="
if gcloud container clusters describe "$CLUSTER_NAME" --region="$REGION" > /dev/null 2>&1; then
    echo " -> [OK] Cluster '$CLUSTER_NAME' already exists."
else
    echo " -> [WAIT] Creating GKE Autopilot cluster (this takes a few minutes)..."
    gcloud container clusters create-auto "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID"
fi

# === 7. Check and Create Secret Manager Placeholders ===
echo "=== 7. Configuring Secret Manager ==="
SECRETS_TO_CREATE=("GCP_API_KEY" "GEMINI_API_KEY")
for SECRET in "${SECRETS_TO_CREATE[@]}"; do
    if gcloud secrets describe "$SECRET" > /dev/null 2>&1; then
        echo " -> [OK] Secret '$SECRET' already exists."
    else
        echo " -> [WAIT] Creating '$SECRET' placeholder..."
        echo "placeholder" | gcloud secrets create "$SECRET" --data-file=- --replication-policy="automatic"
    fi
done

# === 8. Initialize kubectl Connection ===
echo "=== 8. Connecting to Kubernetes ==="
echo " -> [WAIT] Fetching Kubernetes credentials..."
gcloud container clusters get-credentials "$CLUSTER_NAME" --region "$REGION" --project "$PROJECT_ID" > /dev/null 2>&1
echo " -> [OK] Credentials configured."

echo ""
echo "===================================================="
echo "✅ GCP Setup Complete!"
echo "Next Step: Configure the Google Chat App in Google Cloud Console."
echo "Connection settings: projects/$PROJECT_ID/topics/$CHAT_TOPIC_NAME"
echo "===================================================="
