#!/usr/bin/env bash
# Verifies that prebuilt container images exist in GHCR for a candidate commit SHA.
set -euo pipefail

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"

if [ -z "${COMMIT_SHA}" ]; then
  echo "❌ ERROR: COMMIT_SHA is required." >&2
  exit 1
fi

DEFAULT_REPO="gke-labs/kube-agents"
REPO_FULL="${GITHUB_REPOSITORY:-$DEFAULT_REPO}"
REPO_DOWNCASED=$(echo "$REPO_FULL" | tr '[:upper:]' '[:lower:]')
REGISTRY_PREFIX="${REGISTRY_PREFIX:-ghcr.io/${REPO_DOWNCASED}}"
REQUIRED_IMAGES=("k8s-operator" "platform-agent")

echo "🔍 Checking candidate container images in GHCR for commit ${COMMIT_SHA}..."

for img_name in "${REQUIRED_IMAGES[@]}"; do
  target_img="${REGISTRY_PREFIX}/${img_name}:${COMMIT_SHA}"

  echo "Checking image '${target_img}'..."

  if ! docker manifest inspect "${target_img}" >/dev/null 2>&1; then
    echo "❌ ERROR: Container image '${img_name}' for commit '${COMMIT_SHA}' not found in GHCR (${target_img})!" >&2
    exit 1
  fi
done

echo "✅ All candidate container images verified successfully in GHCR!"
