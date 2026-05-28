#!/bin/bash
set -euo pipefail

echo "=== 1. Environment Setup ==="
if [ -f .env ]; then
  echo " -> [OK] Loading variables from local .env file..."
  source .env
fi

REQUIRED_VARS=("PROJECT_ID" "REGION" "REPO_NAME" "IMAGE_NAME" "IMAGE_TAG")
for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var:-}" ]; then
        echo " -> [ERROR] $var is not set in .env."
        exit 1
    fi
done

# Ensure we are in the directory containing the Dockerfile
APP_DIR="../app"
if [ ! -f "$APP_DIR/Dockerfile" ]; then
    echo " -> [ERROR] Dockerfile not found in $APP_DIR."
    exit 1
fi
echo " -> [OK] Found Dockerfile in $APP_DIR."

IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME:$IMAGE_TAG"

echo "=== 2. Building Image via Cloud Build ==="
echo " -> [WAIT] Submitting build to Google Cloud Build..."
echo " -> [INFO] Target Image: $IMAGE_URI"

gcloud builds submit \
    --tag "$IMAGE_URI" \
    --project "$PROJECT_ID" \
    "$APP_DIR"

echo "=== 3. Verifying Deployment ==="
echo " -> [WAIT] Verifying image exists in Artifact Registry..."
gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME" \
    --project "$PROJECT_ID"

echo ""
echo "===================================================="
echo "✅ Cloud Build Complete!"
echo "===================================================="
