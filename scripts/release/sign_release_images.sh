#!/usr/bin/env bash
# Signs promoted GA release container images in GHCR using Keyless Cosign OIDC.
# Releases strictly use pure numeric SemVer without 'v' prefix (e.g. 0.1.0, 0.2.0).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/release/common.sh
source "${SCRIPT_DIR}/common.sh"

RELEASE_VERSION="${1:-${RELEASE_VERSION:-${TARGET_VERSION:-${TARGET_TAG:-}}}}"

if [ -z "${RELEASE_VERSION}" ]; then
  echo "❌ ERROR: RELEASE_VERSION is required as first argument or environment variable." >&2
  echo "Usage: $0 (with RELEASE_VERSION in env) or $0 <RELEASE_VERSION>" >&2
  exit 1
fi

validate_pure_numeric_semver "${RELEASE_VERSION}" "Release version" || exit 1

if ! command -v cosign >/dev/null 2>&1; then
  if is_ci_pipeline; then
    echo "❌ ERROR: 'cosign' CLI is mandatory in CI for signing images but was not found in PATH." >&2
    exit 1
  else
    echo "⚠️ WARNING: 'cosign' CLI not found in PATH. Skipping local image signing." >&2
    exit 0
  fi
fi

# Safety Guard: Remote image signing executes exclusively inside CI
if ! is_ci_pipeline; then
  echo "⚠️ [Local Execution] Dry-run: Cosign image signing for release '${RELEASE_VERSION}' skipped (runs only in CI)."
  exit 0
fi

REGISTRY_PREFIX="$(get_registry_prefix)"

echo "======================================================================"
echo "🛡️ SIGNING RELEASE CONTAINER IMAGES (COSIGN OIDC)"
echo "Release Version: ${RELEASE_VERSION}"
echo "Registry Prefix: ${REGISTRY_PREFIX}"
echo "======================================================================"

for img in "${REQUIRED_RELEASE_IMAGES[@]}"; do
  local_target="${REGISTRY_PREFIX}/${img}:${RELEASE_VERSION}"
  echo "  • Signing ${local_target}..."
  if ! cosign sign --yes "${local_target}"; then
    echo "❌ ERROR: Failed to sign ${local_target} with cosign!" >&2
    exit 1
  fi
  echo "    ✅ Signed ${local_target}"
done

echo "✅ Successfully signed all ${#REQUIRED_RELEASE_IMAGES[@]} container images for release ${RELEASE_VERSION}."
