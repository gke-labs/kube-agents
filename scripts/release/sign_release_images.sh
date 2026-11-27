#!/usr/bin/env bash
# Signs promoted GA release container images using Keyless Cosign OIDC, in each
# registry the release is published to (release_registry_prefixes in common.sh).
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

release_registries=()
release_registry_lines="$(release_registry_prefixes)"
while IFS= read -r release_registry; do release_registries+=("${release_registry}"); done <<<"${release_registry_lines}"

echo "======================================================================"
echo "🛡️ SIGNING RELEASE CONTAINER IMAGES (COSIGN OIDC)"
echo "Release Version: ${RELEASE_VERSION}"
echo "Registry Prefix: ${release_registries[*]}"
echo "======================================================================"

# The list to sign is the release's own: scripts/release/common.sh as the
# version's tag commit carries it (required_release_images_for_release says why
# that is the candidate's list, and what happens without the tag).
release_images=()
while IFS= read -r img; do release_images+=("${img}"); done < <(required_release_images_for_release "${RELEASE_VERSION}")
if [ "${#release_images[@]}" -eq 0 ]; then
  echo "❌ ERROR: no required release images resolved for release ${RELEASE_VERSION}; nothing signed." >&2
  exit 1
fi

for release_registry in "${release_registries[@]}"; do
  for img in "${release_images[@]}"; do
    local_target="${release_registry}/${img}:${RELEASE_VERSION}"
    echo "  • Signing ${local_target}..."
    if ! cosign sign --yes "${local_target}"; then
      echo "❌ ERROR: Failed to sign ${local_target} with cosign!" >&2
      exit 1
    fi
    echo "    ✅ Signed ${local_target}"
  done
done

echo "✅ Successfully signed all ${#release_images[@]} container images for release ${RELEASE_VERSION}."
