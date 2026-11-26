#!/usr/bin/env bash
# Copies a commit's SHA-tagged images from the registry they were published to
# (get_registry_prefix) into a second registry, without rebuilding, for
# copy-candidate-images.yml: every image the commit's own
# scripts/release/common.sh lists (required_release_images_at), under the same
# name and `:<sha>` tag.
#
# The destination mirrors the source: after a run that exits 0, every image
# there is at the digest the source holds for that tag. Each copy is a carbon
# copy (`docker buildx imagetools create --prefer-index=false` with one
# source), and the script reads the destination back to check the digest. An
# image already there at the source's digest is skipped, so a re-run copies
# nothing. One there at another digest is replaced, and said so: the source
# tag is what moved (a publish re-run on `main` rebuilds the same `:<sha>`),
# and a destination left behind would hold manifests the source no longer
# serves. A destination that cannot be read is copied to as well, since the
# copy and the read-back settle it either way.
#
# Every image is attempted; the ones that failed are named at the end and the
# exit status is 1. A commit the source answers "not found" for on every image
# is not a failure: it was never published, there is nothing to copy, and the
# run says so and exits 0. That takes the registry's own 404 (ghcr_image_status)
# for each image; a source that could not be asked is a failure, never "not
# published".
#
# Usage: copy_candidate_images.sh <commit-sha> <destination-registry-prefix>
set -euo pipefail

readonly USAGE="Usage: copy_candidate_images.sh <commit-sha> <destination-registry-prefix>"
readonly COMMIT_SHA_PATTERN='^[0-9a-f]{40}$'
# How much of the SHA the messages show.
readonly SHORT_SHA_LENGTH=7

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

COMMIT_SHA="${1:-}"
DESTINATION_PREFIX="${2:-}"

if [ -z "${COMMIT_SHA}" ] || [ -z "${DESTINATION_PREFIX}" ]; then
  echo "❌ ERROR: a commit SHA and a destination registry prefix are required." >&2
  echo "${USAGE}" >&2
  exit 1
fi
if ! [[ "${COMMIT_SHA}" =~ ${COMMIT_SHA_PATTERN} ]]; then
  echo "❌ ERROR: '${COMMIT_SHA}' is not a full commit SHA; the images are tagged with one." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "❌ ERROR: 'docker buildx' is required to copy images between registries." >&2
  exit 1
fi

SHORT_SHA="${COMMIT_SHA:0:${SHORT_SHA_LENGTH}}"
SOURCE_PREFIX="$(get_registry_prefix)"
DESTINATION_PREFIX="${DESTINATION_PREFIX%/}"
if [ "${SOURCE_PREFIX}" = "${DESTINATION_PREFIX}" ]; then
  echo "❌ ERROR: the destination is the registry the images are already in (${SOURCE_PREFIX})." >&2
  exit 1
fi

images=()
while IFS= read -r img; do images+=("${img}"); done < <(required_release_images_at "${COMMIT_SHA}")
if [ "${#images[@]}" -eq 0 ]; then
  echo "❌ ERROR: no required release images resolved for commit ${SHORT_SHA}; nothing copied." >&2
  exit 1
fi

echo "📦 Copying ${#images[@]} images at ${SHORT_SHA} from ${SOURCE_PREFIX} to ${DESTINATION_PREFIX}..."

failed=()
unpublished=0
for img in "${images[@]}"; do
  source_image="${SOURCE_PREFIX}/${img}:${COMMIT_SHA}"
  destination_image="${DESTINATION_PREFIX}/${img}:${COMMIT_SHA}"

  if ! source_digest="$(get_image_manifest_digest "${source_image}")"; then
    failed+=("${img}")
    if [ "$(ghcr_image_status "${source_image}")" = "absent" ]; then
      echo "  ✗ ${img}: ${source_image} is not published" >&2
      unpublished=$((unpublished + 1))
    else
      echo "  ✗ ${img}: ${source_image} could not be read" >&2
    fi
    continue
  fi

  previous_digest="$(get_image_manifest_digest "${destination_image}" || true)"
  if [ "${previous_digest}" = "${source_digest}" ]; then
    echo "  = ${img}: already there at ${source_digest}"
    continue
  fi

  if ! docker buildx imagetools create --prefer-index=false --tag "${destination_image}" "${source_image}"; then
    echo "  ✗ ${img}: the copy to ${destination_image} failed" >&2
    failed+=("${img}")
    continue
  fi
  if ! destination_digest="$(get_image_manifest_digest "${destination_image}")" || [ "${destination_digest}" != "${source_digest}" ]; then
    echo "  ✗ ${img}: ${destination_image} reads ${destination_digest:-nothing} after the copy, not the source's ${source_digest}" >&2
    failed+=("${img}")
    continue
  fi
  if [ -n "${previous_digest}" ]; then
    echo "  ↻ ${img}: ${source_digest}, replacing ${previous_digest}"
    echo "::warning title=Candidate image replaced::${destination_image} was ${previous_digest}; the source now serves ${source_digest}"
  else
    echo "  ✓ ${img}: ${source_digest}"
  fi
done

if [ "${unpublished}" -eq "${#images[@]}" ]; then
  echo "::notice title=Nothing to copy::${SOURCE_PREFIX} holds no image at ${SHORT_SHA}; this commit was not published, so nothing was copied to ${DESTINATION_PREFIX}."
  exit 0
fi
if [ "${#failed[@]}" -gt 0 ]; then
  echo "::error title=Candidate images not copied::${#failed[@]} of ${#images[@]} images at ${SHORT_SHA} are not in ${DESTINATION_PREFIX}: ${failed[*]}"
  exit 1
fi
echo "✅ All ${#images[@]} images at ${SHORT_SHA} are in ${DESTINATION_PREFIX} at the source's digests."
