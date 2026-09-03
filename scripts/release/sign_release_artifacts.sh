#!/usr/bin/env bash
# Signs release distribution bundle checksums in build/dist using Keyless Cosign OIDC.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
DIST_DIR="${DIST_DIR:-${REPO_ROOT}/build/dist}"

# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 (with RELEASE_VERSION in env) or $0 <RELEASE_VERSION>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

CHECKSUMS_FILE="${DIST_DIR}/checksums.txt"

if [ ! -f "${CHECKSUMS_FILE}" ]; then
  if is_ci_pipeline; then
    echo "❌ ERROR: Checksums file '${CHECKSUMS_FILE}' not found. Run package_release_bundle.sh before signing." >&2
    exit 1
  else
    echo "⚠️ WARNING: Checksums file '${CHECKSUMS_FILE}' not found. Skipping local artifact signing."
    exit 0
  fi
fi

if ! command -v cosign >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'cosign' CLI is mandatory in CI for signing release artifacts but was not found in PATH." >&2
    exit 1
  else
    echo "⚠️ WARNING: 'cosign' CLI not found in PATH. Skipping local artifact signing." >&2
    exit 0
  fi
fi

# Safety Guard: Remote artifact signing executes exclusively inside CI
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: Cosign artifact signing for release '${RELEASE_VERSION}' skipped (runs only in CI)."
  exit 0
fi

BUNDLE_FILE="${DIST_DIR}/checksums.txt.bundle"

echo "======================================================================"
echo "🛡️ SIGNING RELEASE ARTIFACTS (COSIGN OIDC)"
echo "Release Version: ${RELEASE_VERSION}"
echo "Input Target:    ${CHECKSUMS_FILE}"
echo "Output Bundle:   ${BUNDLE_FILE}"
echo "======================================================================"

if ! cosign sign-blob --yes --bundle "${BUNDLE_FILE}" "${CHECKSUMS_FILE}"; then
  echo "❌ ERROR: Failed to sign ${CHECKSUMS_FILE} with cosign sign-blob!" >&2
  exit 1
fi

echo "✅ Successfully signed release checksums to ${BUNDLE_FILE}."
