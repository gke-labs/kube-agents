#!/usr/bin/env bash
# Publishes an official GitHub Release for a validated GA tag and commit SHA.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"
RELEASE_COMMIT="${2:-${RELEASE_COMMIT:-${TARGET_COMMIT:-}}}"
TARGET_REPO="$(get_target_repo)"

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

# Canonicalize commit SHA to full 40-character hash
if ! RESOLVED_COMMIT="$(git rev-parse --verify "${RELEASE_COMMIT}^{commit}" 2>/dev/null)"; then
  echo "❌ ERROR: Cannot resolve valid Git commit from '${RELEASE_COMMIT}'!" >&2
  exit 1
fi

echo "======================================================================"
echo "🚀 PUBLISHING GITHUB RELEASE"
echo "Release Version:   ${RELEASE_VERSION}"
echo "Resolved Commit:   ${RESOLVED_COMMIT:0:7}"
echo "Target Repository: ${TARGET_REPO}"
echo "======================================================================"

if ! command -v gh >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'gh' CLI is mandatory in CI for creating releases but was not found in PATH." >&2
    exit 1
  else
    echo "⚠️ WARNING: 'gh' CLI not found in PATH. Dry-run: skipped GitHub release creation." >&2
    exit 0
  fi
fi

# Check if release already exists on GitHub
if gh release view "${RELEASE_VERSION}" --repo "${TARGET_REPO}" >/dev/null 2>&1; then
  echo "ℹ️ GitHub Release '${RELEASE_VERSION}' already exists for repository ${TARGET_REPO}. Idempotent skip."
  exit 0
fi

# Safety Guard: Remote release creation executes exclusively inside CI
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: GitHub release '${RELEASE_VERSION}' creation skipped (runs only in CI)."
  exit 0
fi

gh release create "${RELEASE_VERSION}" \
  --repo "${TARGET_REPO}" \
  --target "${RESOLVED_COMMIT}" \
  --title "Release ${RELEASE_VERSION}" \
  --generate-notes

echo "✅ Successfully published GitHub Release '${RELEASE_VERSION}' for commit ${RESOLVED_COMMIT:0:7}."
