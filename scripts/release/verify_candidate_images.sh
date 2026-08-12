#!/usr/bin/env bash
# Verifies that prebuilt container images exist in GHCR for a candidate commit SHA.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

COMMIT_SHA="${1:-${COMMIT_SHA:-}}"

if [ -z "${COMMIT_SHA}" ]; then
  echo "❌ ERROR: COMMIT_SHA is required." >&2
  exit 1
fi

registry_prefix="$(get_registry_prefix)"
echo "🔍 Checking candidate container images in GHCR for commit ${COMMIT_SHA} (${registry_prefix})..."

for img_name in "${REQUIRED_RELEASE_IMAGES[@]}"; do
  target_img="${registry_prefix}/${img_name}:${COMMIT_SHA}"

  echo "Checking image '${target_img}'..."

  if ! docker manifest inspect "${target_img}" >/dev/null 2>&1; then
    echo "❌ ERROR: Container image '${img_name}' for commit '${COMMIT_SHA}' not found in GHCR (${target_img})!" >&2
    exit 1
  fi
done

echo "✅ All candidate container images verified successfully in GHCR!"
