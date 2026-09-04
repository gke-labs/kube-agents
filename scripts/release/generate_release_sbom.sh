#!/usr/bin/env bash
# Generates SPDX 2.3 and CycloneDX 1.5 JSON Software Bill of Materials (SBOM) using Syft for the release bundle and OCI images.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

TAG_NAME="${1:-${TAG_NAME:-${GITHUB_REF_NAME:-}}}"
TARGET_DIR="${2:-${TARGET_DIR:-${REPO_ROOT}}}"
DIST_DIR="${DIST_DIR:-${REPO_ROOT}/build/dist}"

if [ -z "${TAG_NAME}" ]; then
  echo "❌ ERROR: TAG_NAME must be specified as first argument or environment variable." >&2
  exit 1
fi

validate_pure_numeric_semver "${TAG_NAME}" "Release tag" || exit 1

if [ ! -d "${TARGET_DIR}" ]; then
  echo "❌ ERROR: Target directory '${TARGET_DIR}' does not exist!" >&2
  exit 1
fi

if ! command -v syft >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'syft' CLI is mandatory in CI for SBOM generation but not found in PATH." >&2
    exit 1
  else
    echo "⚠️ WARNING: 'syft' CLI is not found in PATH. Skipping local SBOM generation." >&2
    exit 0
  fi
fi

BUNDLE_PREFIX="kube-agents-${TAG_NAME}"
REGISTRY_PREFIX="${TARGET_REGISTRY_PREFIX:-$(get_registry_prefix)}"
SOURCE_REGISTRY="${SOURCE_REGISTRY_PREFIX:-$DEFAULT_REGISTRY_PREFIX}"
SOURCE_TAG="${SOURCE_IMAGE_TAG:-$TAG_NAME}"

echo "======================================================================"
echo "🛡️ GENERATING RELEASE SBOMs (SPDX 2.3 & CycloneDX 1.5 via Syft)"
echo "Tag Name:     ${TAG_NAME}"
echo "Target Dir:   ${TARGET_DIR}"
echo "Destination:  ${DIST_DIR}"
echo "======================================================================"

# Isolated staging directory for all-or-nothing generation (guarantees idempotency and clean crash recovery)
TMP_SBOM_DIR="$(mktemp -d -t kube-agents-sbom-XXXXXX)"
SWAP_FILE_PATH="${SWAP_FILE_PATH:-/swapfile}"
SWAP_ALLOCATED="false"
cleanup_sbom_staging() {
  local exit_code=$?
  rm -rf "${TMP_SBOM_DIR}"
  if [ "${SWAP_ALLOCATED}" = "true" ] && [ -f "${SWAP_FILE_PATH}" ] && command -v sudo >/dev/null 2>&1; then
    echo "  ℹ️ Disabling and removing temporary CI swap space..."
    sudo -n swapoff "${SWAP_FILE_PATH}" || echo "⚠️ Warning: Failed to disable swapfile on exit" >&2
    sudo -n rm -f "${SWAP_FILE_PATH}" || echo "⚠️ Warning: Failed to remove swapfile on exit" >&2
  fi
  exit "${exit_code}"
}
trap cleanup_sbom_staging EXIT

# Ensure sufficient virtual memory in Linux CI to handle peak serialization of large images
if is_ci_pipeline && [ "$(uname -s)" = "Linux" ] && command -v sudo >/dev/null 2>&1; then
  if sudo -n true 2>/dev/null; then
    total_swap="$(free -m | awk '/Swap:/ {print $2}')"
    if [ "${total_swap:-0}" -lt 4096 ] && [ ! -f "${SWAP_FILE_PATH}" ]; then
      echo "  ℹ️ Configuring temporary 10G swap space in CI for container image SBOM analysis..."
      if sudo -n fallocate -l 10G "${SWAP_FILE_PATH}" && \
         sudo -n chmod 600 "${SWAP_FILE_PATH}" && \
         sudo -n mkswap "${SWAP_FILE_PATH}" && \
         sudo -n swapon "${SWAP_FILE_PATH}"; then
        SWAP_ALLOCATED="true"
        echo "  ✓ Temporary swap space configured ($(free -h | awk '/Swap:/ {print $2}') total swap)"
      else
        echo "  ⚠️ Warning: Failed to configure swap space; proceeding with available memory" >&2
      fi
    fi
  fi
fi

# 1. Staging filesystem SBOMs
echo "  • Generating SPDX 2.3 JSON SBOM for ${BUNDLE_PREFIX} filesystem..."
syft "dir:${TARGET_DIR}" -o spdx-json > "${TMP_SBOM_DIR}/${BUNDLE_PREFIX}.spdx.json"

echo "  • Generating CycloneDX 1.5 JSON SBOM for ${BUNDLE_PREFIX} filesystem..."
syft "dir:${TARGET_DIR}" -o cyclonedx-json > "${TMP_SBOM_DIR}/${BUNDLE_PREFIX}.cdx.json"

# 2. Staging container image SBOMs with explicit error reporting
export SYFT_PARALLELISM="${SYFT_PARALLELISM:-2}"
export GOMAXPROCS="${GOMAXPROCS:-2}"
export GOMEMLIMIT="${GOMEMLIMIT:-4GiB}"
for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
  img_ref="${REGISTRY_PREFIX}/${img}:${TAG_NAME}"
  if [ "${REGISTRY_PREFIX}" != "${SOURCE_REGISTRY}" ] || [ "${TAG_NAME}" != "${SOURCE_TAG}" ]; then
    if ! docker manifest inspect "${img_ref}" >/dev/null 2>&1; then
      echo "  ℹ️ Target image '${img_ref}' not in registry; scanning source image '${SOURCE_REGISTRY}/${img}:${SOURCE_TAG}'..."
      img_ref="${SOURCE_REGISTRY}/${img}:${SOURCE_TAG}"
    fi
  fi
  echo "  • Generating SPDX SBOM for container image ${img_ref}..."
  err_file="${TMP_SBOM_DIR}/${img}.err"
  set +e
  syft "${img_ref}" --scope squashed -o spdx-json > "${TMP_SBOM_DIR}/${img}-${TAG_NAME}.spdx.json" 2>"${err_file}"
  exit_code=$?
  set -e

  if [ "${exit_code}" -ne 0 ]; then
    err_msg="$(cat "${err_file}" 2>/dev/null || echo "Unknown error")"
    if is_ci_pipeline; then
      echo "❌ ERROR: Failed to generate SBOM for container image ${img_ref} in CI (exit code ${exit_code}): ${err_msg}" >&2
      exit "${exit_code}"
    else
      echo "  ⚠️ Warning: Could not generate remote image SBOM for ${img_ref} locally (exit code ${exit_code}): ${err_msg}" >&2
      rm -f "${TMP_SBOM_DIR}/${img}-${TAG_NAME}.spdx.json"
    fi
  fi
done

# 3. All-or-nothing publication to DIST_DIR (atomic and idempotent promotion)
mkdir -p "${DIST_DIR}"
find "${TMP_SBOM_DIR}" -maxdepth 1 -name "*.json" -size +0c | while read -r staged_file; do
  mv -f "${staged_file}" "${DIST_DIR}/"
done

echo "✅ Generated SPDX & CycloneDX SBOM artifacts in ${DIST_DIR}:"
find "${DIST_DIR}" -maxdepth 1 -name "*.json" | sort | while read -r fname; do
  echo "  • $(basename "$fname")"
done
