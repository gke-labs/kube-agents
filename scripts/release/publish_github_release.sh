#!/usr/bin/env bash
# Publishes an official GitHub Release for a validated GA tag and commit SHA.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"
TARGET_REPO="$(get_target_repo)"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 <RELEASE_VERSION>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

# Single Source of Truth: Resolve commit directly from the Git tag created by tag_ga_release.sh
RELEASE_COMMIT="$(resolve_release_commit "${RELEASE_VERSION}")"

echo "======================================================================"
echo "🚀 PUBLISHING GITHUB RELEASE"
echo "Release Version:   ${RELEASE_VERSION}"
echo "Release Commit:     ${RELEASE_COMMIT}"
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
  --target "${RELEASE_COMMIT}" \
  --title "Release ${RELEASE_VERSION}" \
  --generate-notes

echo "✅ Successfully published GitHub Release '${RELEASE_VERSION}' for commit ${RELEASE_COMMIT:0:7}."
