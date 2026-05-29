#!/bin/bash
set -euo pipefail

# =====================================================================
# Hermes GKE Custom Resource Local Debugging Runner Script
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

# Verify manifests config exists
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
# 4. Install CustomResourceDefinitions
# =====================================================================
OPERATOR_DIR="$SCRIPT_DIR/hermes-operator"
echo "=== 4. Registering CustomResourceDefinitions (CRDs) ==="
make -C "$OPERATOR_DIR" install

# =====================================================================
# 5. Boot Up Operator Manager Locally
# =====================================================================
echo "=== 5. Launching Operator Manager Locally ==="
LOG_FILE="$SCRIPT_DIR/operator_local.log"
echo " -> [INFO] Operator logs will be written to: $LOG_FILE"

# Start the operator manager process in the background, redirecting stdout/stderr
make -C "$OPERATOR_DIR" run > "$LOG_FILE" 2>&1 &
OPERATOR_PID=$!

# Register exit handler traps to safely terminate background operator manager
cleanup() {
    echo ""
    echo " -> [WAIT] Tearing down background local operator manager process (PID $OPERATOR_PID)..."
    kill "$OPERATOR_PID" >/dev/null 2>&1 || true
    echo " -> [OK] Process successfully terminated."
    exit 0
}
trap cleanup EXIT SIGINT SIGTERM

echo " -> [WAIT] Bootstrapping local operator loop (waiting 6s for logs to stabilize)..."
sleep 6

if ! kill -0 "$OPERATOR_PID" >/dev/null 2>&1; then
    echo " -> [ERROR] Local operator manager failed to start. Review logs at: $LOG_FILE"
    exit 1
fi
echo " -> [OK] Operator manager is actively running in the background."

# =====================================================================
# 6. Apply Custom Resource dynamically
# =====================================================================
echo "=== 6. Deploying Custom Resource ==="

# Re-ensure Namespace exists
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

export IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME:$IMAGE_TAG"
export PROJECT_ID NAMESPACE CHAT_TOPIC_NAME CHAT_SUB_NAME GSA_NAME KSA_NAME GOOGLE_CHAT_ALLOWED_USERS
export GOOGLE_CHAT_HOME_CHANNEL="${GOOGLE_CHAT_HOME_CHANNEL:-}"

echo " -> [WAIT] Rendering and applying Custom Resource inline using Image: $IMAGE_URI"
envsubst < "$BOT_CR" | kubectl apply -f -

echo ""
echo "===================================================="
echo "✅ Local Operator Deployment Active!"
echo "===================================================="
echo "💡 Agent Observability:"
echo "  kubectl get hermesagent -n $NAMESPACE"
echo "  kubectl get pods -n $NAMESPACE -l app=hermes-gateway"
echo "  kubectl logs -f deploy/hermes-gateway -n $NAMESPACE"
echo ""
echo "===================================================="
echo "📡 STREAMING LOCAL OPERATOR LOGS:"
echo " (Press Ctrl+C to terminate the operator and exit)"
echo "----------------------------------------------------"
tail -f "$LOG_FILE"
