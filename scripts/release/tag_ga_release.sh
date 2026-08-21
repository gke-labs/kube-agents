#!/usr/bin/env bash
# Creates and pushes an official GA SemVer Git tag for a target commit SHA safely and idempotently.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"
RELEASE_COMMIT="${2:-${RELEASE_COMMIT:-${TARGET_COMMIT:-}}}"

# Sibling symmetry: support swapped arguments
if [ -n "${1:-}" ] && [ -n "${2:-}" ]; then
  if [[ ! "${1}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && [[ "${2}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    local_tmp="${RELEASE_VERSION}"
    RELEASE_VERSION="${RELEASE_COMMIT}"
    RELEASE_COMMIT="${local_tmp}"
  fi
fi

if [ -z "${RELEASE_VERSION}" ] || [ -z "${RELEASE_COMMIT}" ]; then
  echo "❌ ERROR: RELEASE_VERSION and RELEASE_COMMIT are required as arguments or environment variables." >&2
  echo "Usage: $0 (with RELEASE_VERSION and RELEASE_COMMIT in env) or $0 <RELEASE_VERSION> <RELEASE_COMMIT>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

echo "======================================================================"
echo "🏷️ CREATING AND PUSHING GA RELEASE GIT TAG"
echo "Release Version: ${RELEASE_VERSION}"
echo "Release Commit:  ${RELEASE_COMMIT}"
echo "======================================================================"

ensure_git_tag "${RELEASE_VERSION}" "${RELEASE_COMMIT}" "Release ${RELEASE_VERSION}"
