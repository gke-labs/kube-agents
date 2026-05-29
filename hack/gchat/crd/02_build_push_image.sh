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

# Resolve repository root path dynamically
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

PLATFORM_DOCKERFILE="$REPO_ROOT/agents/platform/Dockerfile"
TEMP_DOCKERFILE="$REPO_ROOT/Dockerfile"

if [ ! -f "$PLATFORM_DOCKERFILE" ]; then
    echo " -> [ERROR] Platform Dockerfile not found at $PLATFORM_DOCKERFILE."
    exit 1
fi

if [ -f "$TEMP_DOCKERFILE" ]; then
    echo " -> [ERROR] A temporary Dockerfile already exists at $TEMP_DOCKERFILE. Please clear it first."
    exit 1
fi

echo " -> [WAIT] Copying platform agent Dockerfile to repository root..."
cp "$PLATFORM_DOCKERFILE" "$TEMP_DOCKERFILE"

# Guarantee cleanup of root Dockerfile even if build fails or is aborted
trap 'rm -f "$TEMP_DOCKERFILE"' EXIT

IMAGE_URI="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME:$IMAGE_TAG"

echo "=== 2. Building Image via Cloud Build ==="
echo " -> [WAIT] Submitting build with root context to Google Cloud Build..."
echo " -> [INFO] Target Image: $IMAGE_URI"

gcloud builds submit \
    --tag "$IMAGE_URI" \
    --project "$PROJECT_ID" \
    "$REPO_ROOT"

echo "=== 3. Verifying Deployment ==="
echo " -> [WAIT] Verifying image exists in Artifact Registry..."
gcloud artifacts docker images list "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$IMAGE_NAME" \
    --project "$PROJECT_ID"

echo ""
echo "===================================================="
echo "✅ Cloud Build Complete!"
echo "===================================================="
