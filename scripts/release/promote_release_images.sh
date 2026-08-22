#!/usr/bin/env bash
# Promotes verified container images from candidate commit SHA to GA release tag in GHCR without rebuilding.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_COMMIT="${1:-${RELEASE_COMMIT:-${COMMIT_SHA:-${TARGET_COMMIT:-}}}}"
RELEASE_VERSION="${2:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"

# Sibling symmetry: support both <COMMIT> <VERSION> and <VERSION> <COMMIT> signatures
if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
  if [[ "${1}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ ! "${2}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    RELEASE_VERSION="$1"
    RELEASE_COMMIT="$2"
  fi
fi

if [ -z "${RELEASE_COMMIT}" ] || [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_COMMIT and RELEASE_VERSION are required as arguments or environment variables." >&2
  echo "Usage: $0 (with RELEASE_COMMIT and RELEASE_VERSION in env) or $0 <RELEASE_COMMIT> <RELEASE_VERSION>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

echo "======================================================================"
echo "🚀 PROMOTING RELEASE CONTAINER IMAGES (NO-REBUILD)"
echo "Release Commit:  ${RELEASE_COMMIT}"
echo "Release Version: ${RELEASE_VERSION}"
echo "======================================================================"

promote_release_images "${RELEASE_COMMIT}" "${RELEASE_VERSION}"
