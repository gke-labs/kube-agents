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

# The candidate's own list: the names its scripts/release/common.sh carries,
# which are the images its publish run built (required_release_images_at).
candidate_images=()
while IFS= read -r img_name; do candidate_images+=("${img_name}"); done < <(required_release_images_at "${COMMIT_SHA}")
if [ "${#candidate_images[@]}" -eq 0 ]; then
  echo "❌ ERROR: no required release images resolved for commit ${COMMIT_SHA}." >&2
  exit 1
fi

for img_name in "${candidate_images[@]}"; do
  target_img="${registry_prefix}/${img_name}:${COMMIT_SHA}"

  echo "Checking image '${target_img}'..."

  if ! docker manifest inspect "${target_img}" >/dev/null 2>&1; then
    echo "❌ ERROR: Container image '${img_name}' for commit '${COMMIT_SHA}' not found in GHCR (${target_img})!" >&2
    exit 1
  fi
done

echo "✅ All candidate container images verified successfully in GHCR!"
